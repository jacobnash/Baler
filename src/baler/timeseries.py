"""
Point history in Postgres/Timescale - the thing Grafana (or Tableau, or
anything else that speaks SQL) reads natively, no plugin, no custom
connector. Three nullable value columns because sources emit int/float/str/
bool values today (e.g. Windrow's fan_status.presentValue = "active") - one
float column would lose that.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg import sql
from psycopg_pool import ConnectionPool

from baler import ddl

DEFAULT_DSN = os.environ.get(
    "BALER_PG_DSN", "postgresql://baler:baler@localhost:5433/baler"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS point_history (
    point_uri  TEXT NOT NULL,
    ts         TIMESTAMPTZ NOT NULL,
    value      DOUBLE PRECISION,
    value_text TEXT,
    value_bool BOOLEAN,
    unit       TEXT,
    label      TEXT,
    PRIMARY KEY (point_uri, ts)
);
"""

# read_point_labels' lookup: only derivation_engine sets unit/label, so on a
# raw point "newest row with a unit or label" would otherwise walk its whole
# history to find nothing. Small: it indexes the labelled rows only.
LABELS_INDEX = """
CREATE INDEX IF NOT EXISTS point_history_labelled
ON point_history (point_uri, ts DESC) WHERE unit IS NOT NULL OR label IS NOT NULL
"""

# Pre-folded rollups (#51/#54): TimescaleDB continuous aggregates of
# point_history in 5-minute buckets, and 1-hour buckets built on those. Each
# keeps sum/count/min/max (avg = sum/count), which recombine exactly into any
# coarser bucket, so read_rollup can fold a day of 5M raw samples from ~150k
# pre-summed rows instead. Refreshed every 5/30 minutes over the last
# ROLLUP_WINDOW_DAYS + 5 days. read_rollup never trusts a bucket TimescaleDB
# itself records as out of date (unrefreshed tail, backfill since the last
# refresh, never materialized): it folds those from raw rows (_rollup_plan),
# so results are the raw fold's, never stale. (materialized_only = false
# alone wasn't enough: it covers only data newer than the watermark, and
# backfill below it stayed invisible until the next refresh.)
ROLLUP_VIEWS = (("point_history_1h", 3600), ("point_history_5m", 300))
ROLLUP_WINDOW_DAYS = 30
_V = "COALESCE(value, value_bool::int::float8)"
ROLLUP_DDL = (
    f"""CREATE MATERIALIZED VIEW IF NOT EXISTS point_history_5m
    WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
    SELECT point_uri, time_bucket(INTERVAL '5 minutes', ts) AS bucket,
           sum({_V}) AS vsum, count({_V}) AS vcount, min({_V}) AS vmin, max({_V}) AS vmax
    FROM point_history GROUP BY point_uri, bucket WITH NO DATA""",
    """CREATE MATERIALIZED VIEW IF NOT EXISTS point_history_1h
    WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
    SELECT point_uri, time_bucket(INTERVAL '1 hour', bucket) AS bucket,
           sum(vsum) AS vsum, sum(vcount) AS vcount, min(vmin) AS vmin, max(vmax) AS vmax
    FROM point_history_5m GROUP BY point_uri, time_bucket(INTERVAL '1 hour', bucket) WITH NO DATA""",
    f"""SELECT add_continuous_aggregate_policy('point_history_5m', if_not_exists => true,
        start_offset => INTERVAL '{ROLLUP_WINDOW_DAYS + 5} days', end_offset => INTERVAL '5 minutes',
        schedule_interval => INTERVAL '5 minutes')""",
    f"""SELECT add_continuous_aggregate_policy('point_history_1h', if_not_exists => true,
        start_offset => INTERVAL '{ROLLUP_WINDOW_DAYS + 5} days', end_offset => INTERVAL '1 hour',
        schedule_interval => INTERVAL '30 minutes')""",
)


# Native columnar compression (#192). Uncompressed, every sample costs ~150-220
# bytes, most of it point_uri repeated in the row and again in the primary
# key. Segmenting by point_uri stores the URI once per batch of up to 1000
# samples, and ordering each batch by ts delta-encodes the timestamps and
# values. Raw history is compressed, never downsampled: hunting and Cx need
# the actual samples (todo/building-lifecycle-ubis.md §10.1, §15).
#
# unit and label are segment columns too, not just point_uri: they are
# constant per point (only derivation_engine and hisWrite set them), so they
# cost nothing in ratio, and read_point_labels' "newest row with a unit or
# label" filter then runs on the stored segment values. Compressed in the
# value columns instead, a raw point (never labelled) had every batch of its
# whole history decompressed on each lookup: 4 ms -> 310 ms for 766 points
# over half a day, growing with every year of history.
#
# A chunk is compressed once all of it is older than COMPRESS_AFTER. Writes
# that land in an already-compressed chunk still work (TimescaleDB 2.11+
# decompresses just the affected batch for an upsert, and the policy
# recompresses the chunk on its next run), so this setting is about how much
# late traffic takes that slower path, not about correctness. What arrives
# late: retained MQTT replays (seconds), Windrow #13's trend backfill (hours),
# an edge node's spool after an outage (a weekend, say). 7 days covers all
# three with room. hisWrite and haystack_import can write any age; they
# take the slower path.
COMPRESS_AFTER_DEFAULT = "7 days"
COMPRESS_AFTER = os.environ.get("BALER_HISTORY_COMPRESS_AFTER", "").strip() or COMPRESS_AFTER_DEFAULT

# Present already? One catalog read, as for _READY_SQL. Compression that was
# enabled some other way (by hand, with other settings) is left as it is;
# only the policy's compress_after is kept in step with COMPRESS_AFTER.
_COMPRESSION_STATE_SQL = """
SELECT h.compression_enabled,
       j.job_id IS NOT NULL,
       COALESCE((j.config->>'compress_after')::interval = %s::interval, false)
FROM timescaledb_information.hypertables h
LEFT JOIN timescaledb_information.jobs j
       ON j.proc_name = 'policy_compression' AND j.hypertable_name = h.hypertable_name
WHERE h.hypertable_name = 'point_history'
"""
_ENABLE_COMPRESSION = """
ALTER TABLE point_history SET (timescaledb.compress,
    timescaledb.compress_segmentby = 'point_uri, unit, label', timescaledb.compress_orderby = 'ts DESC')
"""
# ALTER TABLE takes point_history's ACCESS EXCLUSIVE lock. Queued behind a
# long read it would block every ingest write queued behind it in turn, so
# it gives up instead and the next new connection tries again.
_COMPRESSION_LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"

log = logging.getLogger(__name__)


def _ensure_compression(conn: psycopg.Connection) -> None:
    """Enable compression and its policy on point_history, idempotently and
    in place: a no-op catalog read once they're there, so it is safe on
    every new connection of every service (as _READY_SQL is). Never fails
    the connection: ingest matters more than when the next chunk shrinks."""
    try:
        row = conn.execute(_COMPRESSION_STATE_SQL, (COMPRESS_AFTER,)).fetchone()
    except psycopg.errors.InvalidDatetimeFormat:
        log.error("BALER_HISTORY_COMPRESS_AFTER=%r is not a Postgres interval; point_history "
                  "compression left as it is", COMPRESS_AFTER)
        return
    if row is None:  # no point_history hypertable (yet)
        return
    enabled, has_policy, policy_matches = row
    if enabled and policy_matches:
        return
    stmts: list[str | sql.Composable] = [_COMPRESSION_LOCK_TIMEOUT]
    if not enabled:
        stmts.append(_ENABLE_COMPRESSION)
    if has_policy:  # compress_after changed: replace the policy
        stmts.append("SELECT remove_compression_policy('point_history', if_exists => true)")
    stmts.append(sql.SQL(
        "SELECT add_compression_policy('point_history', compress_after => {}::interval, if_not_exists => true)"
    ).format(sql.Literal(COMPRESS_AFTER)))
    try:
        ddl.run(conn, *stmts)
    except psycopg.Error as exc:
        log.warning("point_history compression setup skipped this time (retried on the next "
                    "new connection): %s", exc)


# Everything SCHEMA, create_hypertable and LABELS_INDEX make, present.
# (Without TimescaleDB this errors, as create_hypertable always has.)
_READY_SQL = """
SELECT to_regclass('point_history') IS NOT NULL
   AND to_regclass('point_history_labelled') IS NOT NULL
   AND EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')
   AND EXISTS (SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_name = 'point_history')
"""


def _ensure_rollup_views(conn: psycopg.Connection) -> None:
    """Only on TimescaleDB, and only until they exist (a cheap catalog check
    on every other connection)."""
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'), "
        "to_regclass('point_history_1h') IS NOT NULL"
    ).fetchone()
    if row and row[0] and not row[1]:
        ddl.run(conn, *ROLLUP_DDL)


# For pools serving requests through the gateway: below nginx's 60s proxy
# timeout, so a query the gateway has already given up on stops instead of
# running on with nobody waiting for the answer.
GATEWAY_STATEMENT_TIMEOUT = os.environ.get("BALER_PG_STATEMENT_TIMEOUT", "55s")


def _setup(conn: psycopg.Connection, statement_timeout: str | None = None) -> None:
    """Runs on every fresh connection, pooled or not - idempotent, same as
    CREATE TABLE IF NOT EXISTS."""
    # The hypertable is why we run the TimescaleDB image - a plain
    # CREATE TABLE gets none of its time-based chunking (faster inserts
    # and time-range reads at volume as the table grows past what one
    # unpartitioned btree handles well). migrate_data converts any rows
    # that already exist under the plain-table schema; idempotent like
    # the CREATE TABLE. Under ddl's lock: every service runs this on
    # every new connection.
    # Only when something is missing, though: the IF NOT EXISTS forms still
    # lock before they check - CREATE INDEX takes a SHARE lock on
    # point_history and every chunk, which waits for in-flight ingest and
    # blocks new ingest meanwhile, and the advisory lock queues every
    # service's new connections one behind another after a deploy
    # (Baler#107). One catalog read answers "all there" instead.
    row = conn.execute(_READY_SQL).fetchone()
    if not (row and row[0]):
        ddl.run(conn, SCHEMA, "SELECT create_hypertable('point_history', 'ts', if_not_exists => TRUE, migrate_data => TRUE)",
                LABELS_INDEX)
    _ensure_rollup_views(conn)
    _ensure_compression(conn)
    # Real tradeoff, taken deliberately: every INSERT otherwise waits on a
    # full WAL fsync before returning. Scoped to this connection only (a
    # session-level SET, not a postgresql.conf edit) - other clients
    # (Grafana reads, anything else) are unaffected. The risk this accepts:
    # a write could be lost on an OS/DB crash within the tiny WAL-flush
    # window between "client got 204" and "WAL synced to disk" - not a
    # corruption risk, a durability-window risk, and one this point-history
    # table (telemetry, not financial records) can afford.
    conn.execute("SET synchronous_commit = off")
    if statement_timeout:
        conn.execute("SELECT set_config('statement_timeout', %s, false)", (statement_timeout,))


def connect(dsn: str = DEFAULT_DSN) -> psycopg.Connection:
    conn = psycopg.connect(dsn, autocommit=True)
    _setup(conn)
    return conn


def connect_pool(dsn: str = DEFAULT_DSN, min_size: int = 2, max_size: int = 10,
                 statement_timeout: str | None = None) -> ConnectionPool:
    """One connection per request instead of one shared connection for
    every concurrent request - psycopg.Connection isn't safe for
    concurrent use from multiple threads, and a single shared connection
    is exactly what made ingest_api.py fragile (it died once already this
    session, silently, from a stale connection with no way to recover
    without a restart)."""
    pool = ConnectionPool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        kwargs={"autocommit": True},
        configure=_setup if statement_timeout is None else lambda c: _setup(c, statement_timeout),
        open=True,
    )
    pool.wait()
    return pool


# Rows decoded per fetchmany call. Decoding a result into Python objects
# holds the GIL for the whole call: fetchall() on a 6M-row /his read held it
# ~3 s, long enough to time out ingest_api's 2 s health probe and stall every
# other request in the process (#51). 10k rows is ~5 ms of decoding, and
# the interpreter can switch threads between batches.
FETCH_BATCH = 10_000


def iter_rows(cur):
    """cur's rows, decoded FETCH_BATCH at a time (see above)."""
    while batch := cur.fetchmany(FETCH_BATCH):
        yield from batch


def write_point_value(conn: psycopg.Connection, point_uri: str, value, ts: datetime, unit: str | None = None, label: str | None = None) -> None:
    """Dispatches on type(value) into the right nullable column. bool must
    be checked before int/float - bool is a subclass of int in Python, the
    same trap normalize_value() guards against in Windrow's bridges."""
    num, text, flag = None, None, None
    if isinstance(value, bool):
        flag = value
    elif isinstance(value, (int, float)):
        num = float(value)
    elif value is None:
        pass
    else:
        text = str(value)

    conn.execute(
        """
        INSERT INTO point_history (point_uri, ts, value, value_text, value_bool, unit, label)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (point_uri, ts) DO UPDATE SET
            value = EXCLUDED.value,
            value_text = EXCLUDED.value_text,
            value_bool = EXCLUDED.value_bool,
            unit = EXCLUDED.unit,
            label = EXCLUDED.label
        """,
        (point_uri, ts, num, text, flag, unit, label),
    )


def _row_to_value(row):
    value, value_text, value_bool = row
    if value_bool is not None:
        return value_bool
    if value_text is not None:
        return value_text
    return value


def read_latest(conn: psycopg.Connection, point_uri: str):
    cur = conn.execute(
        "SELECT value, value_text, value_bool FROM point_history WHERE point_uri = %s ORDER BY ts DESC LIMIT 1",
        (point_uri,),
    )
    row = cur.fetchone()
    return None if row is None else _row_to_value(row)


# "This point" for the per-point LATERAL probes, written as a range, and
# their ORDER BY led by point_uri. With `point_uri = u.uri` the planner
# drops point_uri from the sort, orders by ts alone and, costing LIMIT 1 on
# a per-point average, often walks a chunk's ts index filtering for the
# point: for a point with no rows in that chunk (new, retired, or simply
# another site's) that is every row of the chunk, chunk after chunk - 10-17
# s per point at 35M rows, timing a statement out. As a range the order is
# (point_uri, ts), which only the primary key gives: one index probe per
# chunk, sub-millisecond, whatever the history depth (#150/#107). Same
# rows: for text, a >= b and a <= b is a = b (ties break bytewise).
_ONE_POINT = "point_uri >= u.uri AND point_uri <= u.uri"


def read_latest_many(
    conn: psycopg.Connection,
    point_uris: list[str],
    before: datetime | None = None,
    since: datetime | None = None,
    with_value: bool = False,
) -> dict[str, tuple[datetime, object]]:
    """Latest (ts, value) per point, in one query: haystack_api's curVal
    and the commissioning existence check both need it for hundreds of
    points at once. Points with no samples are absent from the result.

    `before` makes it "latest as of" (ts <= before) - a past Cx test window
    is judged on what had arrived by its end. `with_value` skips rows with
    no value at all (a bridge that publishes null when it can't reach the
    device, Baler#85); pair it with `since` so a point with nothing but
    empty rows doesn't walk its whole history."""
    if not point_uris:
        return {}
    conds: list[str] = []
    params: list = []
    if before is not None:
        conds.append("ts <= %s")
        params.append(before)
    if since is not None:
        conds.append("ts >= %s")
        params.append(since)
    if with_value:
        conds.append("(value IS NOT NULL OR value_text IS NOT NULL OR value_bool IS NOT NULL)")
    rows = probe_points(conn, point_uris, "ts, value, value_text, value_bool", conds, params, newest=True)
    return {uri: (ts, _row_to_value((v, vt, vb))) for uri, (ts, v, vt, vb) in rows.items()}


# Where the compressed chunks end (#192): chunks wholly below this ts are
# compressed. timescaledb_information.chunks is a catalog view, a few ms.
_COMPRESSED_BELOW_SQL = """
SELECT bool_or(is_compressed), min(range_start) FILTER (WHERE NOT is_compressed)
FROM timescaledb_information.chunks
WHERE hypertable_schema = 'public' AND hypertable_name = 'point_history'
"""
_ALL_COMPRESSED = datetime.max.replace(tzinfo=timezone.utc)


def _compressed_below(conn: psycopg.Connection) -> datetime | None:
    """None when no chunk is compressed; otherwise the oldest uncompressed
    chunk's start (every chunk older than it is compressed), or
    _ALL_COMPRESSED."""
    try:
        with conn.transaction():  # a savepoint: a failure mustn't abort the caller's transaction
            row = conn.execute(_COMPRESSED_BELOW_SQL).fetchone()
    except psycopg.Error:  # not TimescaleDB, or the view changed: one plain probe
        return None
    if not row or not row[0]:
        return None
    return row[1] or _ALL_COMPRESSED


def probe_points(conn: psycopg.Connection, point_uris: list[str], cols: str, conds: list[str], params: list,
                 newest: bool) -> dict[str, tuple]:
    """Per point, `cols` of its newest (or oldest) row matching `conds`
    (SQL with %s placeholders, filled from `params` in order): curVal,
    hisStart and the Cx "latest as of" checks.

    On uncompressed chunks this is #237's probe, point_uri as a range
    ordered (point_uri, ts): one primary-key probe per chunk, never a walk
    of a chunk's ts index (see _ONE_POINT). That order can't stop at the
    first chunk with a row, though: it asks every chunk, and on a
    compressed chunk the answer costs a sort of the point's compressed
    batches (~20 ms a chunk a point, measured), so a curVal would grow
    with every week of compressed history. So the probe is split at
    _compressed_below. Below it, chunks are compressed and have no
    full-size ts index to walk, so the plain `point_uri = u.uri ORDER BY
    ts` probe is safe there and stops at the first chunk with a row
    (~0.2 ms a point). The side nearest the answer goes first (the newest
    rows for curVal, the oldest for hisStart), and only points it didn't
    find go to the other side: the same rows a single probe returns."""
    if not point_uris:
        return {}
    direction = "DESC" if newest else "ASC"

    def run(uris: list[str], split: str | None, plain: bool) -> dict[str, tuple]:
        where = ["point_uri = u.uri" if plain else _ONE_POINT, *conds]
        extra: list = []
        if split:
            where.append(split)
            extra.append(boundary)
        order = f"ts {direction}" if plain else f"point_uri {direction}, ts {direction}"
        cur = conn.execute(
            f"""
            SELECT u.uri, h.* FROM unnest(%s::text[]) AS u(uri)
            CROSS JOIN LATERAL (
                SELECT {cols} FROM point_history WHERE {" AND ".join(where)} ORDER BY {order} LIMIT 1
            ) h
            """,
            [uris, *params, *extra],
        )
        return {r[0]: tuple(r[1:]) for r in cur.fetchall()}

    boundary = _compressed_below(conn)
    if boundary is None:
        return run(list(point_uris), None, plain=False)
    near: tuple[str, bool]
    far: tuple[str, bool] | None
    near, far = (("ts >= %s", False), ("ts < %s", True)) if newest else (("ts < %s", True), ("ts >= %s", False))
    if boundary is _ALL_COMPRESSED:
        near, far = ("ts < %s", True), None
    found = run(list(point_uris), *near)
    rest = [u for u in point_uris if u not in found]
    if rest and far is not None:
        found.update(run(rest, *far))
    return found


def read_range(conn: psycopg.Connection, point_uri: str, start_ts: datetime, end_ts: datetime):
    cur = conn.execute(
        """
        SELECT ts, value, value_text, value_bool FROM point_history
        WHERE point_uri = %s AND ts >= %s AND ts <= %s
        ORDER BY ts ASC
        """,
        (point_uri, start_ts, end_ts),
    )
    return [(ts, _row_to_value((value, value_text, value_bool))) for ts, value, value_text, value_bool in iter_rows(cur)]


def read_range_many(
    conn: psycopg.Connection, point_uris: list[str], start_ts: datetime, end_ts: datetime, limit: int
) -> dict[str, tuple[list, bool]]:
    """Every point's raw history over [start_ts, end_ts) in one query, for
    hisquery's multi-point reads - one round trip for an AHU's 20 points
    instead of 20. Half-open on the end, unlike read_range's inclusive
    bounds: a day span ending at next-midnight must not pick up the first
    sample of the following day. `limit` is per point; the returned flag
    says whether that point had more rows than were returned."""
    if not point_uris:
        return {}
    # One primary-key range scan per point that stops at its limit.
    # (ROW_NUMBER() over every row in the span numbered and sorted all of
    # them first: >55s for a day of 614 points at 5s data on nash-srv.)
    cur = conn.execute(
        """
        SELECT u.uri, h.ts, h.value, h.value_text, h.value_bool
        FROM unnest(%s::text[]) AS u(uri)
        CROSS JOIN LATERAL (
            SELECT ts, value, value_text, value_bool FROM point_history
            WHERE point_uri >= u.uri AND point_uri <= u.uri AND ts >= %s AND ts < %s
            ORDER BY point_uri, ts LIMIT %s
        ) h
        """,
        (point_uris, start_ts, end_ts, limit + 1),
    )
    out: dict[str, tuple[list, bool]] = {uri: ([], False) for uri in point_uris}
    for point_uri, ts, value, value_text, value_bool in iter_rows(cur):
        rows, _ = out[point_uri]
        rows.append((ts, _row_to_value((value, value_text, value_bool))))
    for uri, (rows, _) in out.items():
        if len(rows) > limit:
            out[uri] = (rows[:limit], True)
    return out


def read_point_labels(conn: psycopg.Connection, point_uris: list[str]) -> dict[str, tuple[str | None, str | None]]:
    """(One index probe per point - DISTINCT ON sorted every row of every
    point's history, minutes at 20M rows.) Latest (unit, label) recorded alongside each point's samples. Only
    derivation_engine writes these columns (ingest_reading leaves them
    null) - so this is how a computed point, which has no haystack:dis/
    haystack:unit tags in the graph, still gets a display name and unit
    in hisquery's output."""
    if not point_uris:
        return {}
    cur = conn.execute(
        """
        SELECT u.uri, h.unit, h.label
        FROM unnest(%s::text[]) AS u(uri)
        CROSS JOIN LATERAL (
            SELECT unit, label FROM point_history
            WHERE point_uri = u.uri AND (unit IS NOT NULL OR label IS NOT NULL)
            ORDER BY ts DESC LIMIT 1
        ) h
        """,
        (point_uris,),
    )
    return {point_uri: (unit, label) for point_uri, unit, label in cur.fetchall()}


# Calendar-length intervals date_bin can't express - handled by date_trunc
# instead, which only knows these three month multiples.
_CALENDAR_TRUNC = {1: "month", 3: "quarter", 12: "year"}

_FOLD_SQL = {
    "avg": "AVG(v)",
    "min": "MIN(v)",
    "max": "MAX(v)",
    "sum": "SUM(v)",
    "count": "COUNT(v)",
}


# Folds over pre-summed parts (vsum, vcount, vmin, vmax) - the same answers
# _FOLD_SQL gives over raw samples.
_PART_FOLD_SQL = {
    "avg": "SUM(vsum) / NULLIF(SUM(vcount), 0)",
    "min": "MIN(vmin)",
    "max": "MAX(vmax)",
    "sum": "SUM(vsum)",
    "count": "SUM(vcount)",
}
# _PART_FOLD_SQL over exactly one part row.
_ROW_FOLD_SQL = {
    "avg": "vsum / NULLIF(vcount, 0)",
    "min": "vmin",
    "max": "vmax",
    "sum": "vsum",
    "count": "vcount",
}

# Each view's source, for hierarchical aggregates: a view is only as fresh
# as the one it is built from.
_VIEW_SOURCE = {"point_history_1h": "point_history_5m"}
# Past this many separate pieces the plan isn't worth it (lots of scattered
# backfill): fold everything from raw rows instead, which is always exact.
_MAX_PLAN_PARTS = 24
_US = 1_000_000

# What each view has NOT got right yet, straight from TimescaleDB's own
# bookkeeping, in its internal time (microseconds since the Unix epoch):
# - the view's materialization log: ranges never materialized (before the
#   policy window, after the last refresh) or invalidated since;
# - its source hypertable's log: writes below the invalidation threshold
#   (backfill, late data) that no refresh has picked up yet;
# - everything from the view's watermark on.
# Internal catalog tables (no public API exposes pending invalidations): if
# a TimescaleDB upgrade renames them this query fails and read_rollup folds
# raw rows, which is slower but still exact.
_DIRTY_SQL = """
WITH ca AS (
    SELECT c.user_view_name::text AS v, c.mat_hypertable_id AS mat, c.raw_hypertable_id AS raw,
           h.schema_name::text AS mat_schema, h.table_name::text AS mat_table
    FROM _timescaledb_catalog.continuous_agg c
    JOIN _timescaledb_catalog.hypertable h ON h.id = c.mat_hypertable_id
    WHERE c.user_view_name = ANY(%s)
)
SELECT v, mat_schema, mat_table, _timescaledb_functions.cagg_watermark(mat), NULL::bigint FROM ca
UNION ALL
SELECT ca.v, NULL, NULL, l.lowest_modified_value, l.greatest_modified_value
FROM ca JOIN _timescaledb_catalog.continuous_aggs_materialization_invalidation_log l ON l.materialization_id = ca.mat
UNION ALL
SELECT ca.v, NULL, NULL, l.lowest_modified_value, l.greatest_modified_value
FROM ca JOIN _timescaledb_catalog.continuous_aggs_hypertable_invalidation_log l ON l.hypertable_id = ca.raw
"""


def _rollup_levels(start_ts: datetime, interval_seconds: int | None) -> list[tuple[str, int]]:
    """The pre-folded views usable for this rollup, coarsest first: the
    view's bucket divides the interval and the span starts on one of its
    boundaries, so none of its buckets straddles two output buckets."""
    if not interval_seconds or start_ts.tzinfo is None:
        return []
    start_epoch = start_ts.timestamp()
    return [(view, size) for view, size in ROLLUP_VIEWS
            if interval_seconds % size == 0 and start_epoch % size == 0]


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _subtract(region: tuple[int, int], holes: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """region minus the (merged, sorted) holes, as half-open ranges."""
    a, b = region
    out = []
    for ha, hb in holes:
        if hb <= a or ha >= b:
            continue
        if ha > a:
            out.append((a, ha))
        a = max(a, hb)
    if a < b:
        out.append((a, b))
    return out


def _dirty_by_view(conn, views: list[str]) -> tuple[dict[str, list[tuple[int, int]]], dict[str, tuple[str, str]]] | None:
    """Per view: merged half-open µs ranges, widened to whole view buckets,
    where its materialized rows can't be trusted - including whatever its
    source view can't (transitively). Plus each view's materialization
    table. None when the views or the catalog aren't there."""
    wanted = set(views)
    for v in views:
        while v in _VIEW_SOURCE:
            v = _VIEW_SOURCE[v]
            wanted.add(v)
    try:
        with conn.transaction():  # a savepoint: a failure here mustn't abort the caller's transaction
            rows = conn.execute(_DIRTY_SQL, (sorted(wanted),)).fetchall()
    except psycopg.Error:
        return None
    sizes = dict(ROLLUP_VIEWS)
    raw: dict[str, list[tuple[int, int]]] = {v: [] for v in wanted}
    tables: dict[str, tuple[str, str]] = {}
    for v, schema, table, lo, hi in rows:
        size = sizes[v] * _US
        if schema is not None:  # the watermark row
            tables[v] = (schema, table)
            raw[v].append((lo // size * size, 2**63))
        else:  # inclusive [lo, hi] -> every view bucket it touches
            raw[v].append((lo // size * size, (hi // size + 1) * size))
    if set(tables) != wanted:
        return None
    dirty = {}
    for v in views:
        ranges, src = list(raw[v]), v
        while src in _VIEW_SOURCE:
            src = _VIEW_SOURCE[src]
            size = sizes[v] * _US  # a dirty source bucket spoils the whole bucket of v around it
            ranges += [(a // size * size, -(-b // size) * size) for a, b in raw[src]]
        dirty[v] = _merge(ranges)
    return dirty, tables


def _rollup_plan(conn, levels: list[tuple[str, int]], start_ts: datetime, end_ts: datetime) -> list[tuple] | None:
    """Cover [start_ts, end_ts) with the coarsest clean pieces available:
    ("view", schema, table, bucket_seconds, a, b) for whole materialized buckets that are
    up to date, finer views for what coarser ones haven't got right, and
    ("raw", a, b) for the rest - the unrefreshed tail, the partial last
    bucket, backfill no refresh has seen yet. Every raw sample is counted
    exactly once, from wherever its bucket is clean. None means fold
    everything from raw rows."""
    found = _dirty_by_view(conn, [v for v, _ in levels])
    if found is None:
        return None
    dirty, tables = found
    todo = [(int(start_ts.timestamp() * _US), int(end_ts.timestamp() * _US))]
    parts: list[tuple] = []
    for view, size_s in levels:
        size = size_s * _US
        left = []
        for a, b in todo:
            whole = (-(-a // size) * size, b // size * size)
            clean = _subtract(whole, dirty[view]) if whole[0] < whole[1] else []
            parts += [("view", *tables[view], size_s, x, y) for x, y in clean]
            left += _subtract((a, b), clean)
        todo = left
    parts += [("raw", a, b) for a, b in todo]
    if not any(p[0] == "view" for p in parts) or len(parts) > _MAX_PLAN_PARTS:
        return None
    return parts


def _ts(us: int) -> datetime:
    return datetime.fromtimestamp(0, tz=timezone.utc) + timedelta(microseconds=us)


def _read_rollup_planned(conn, parts: list[tuple], point_uris: list[str], start_ts: datetime,
                         fold: str, interval_seconds: int, epoch: bool = False) -> list[tuple]:
    """Fold the plan's pieces together into date_bin buckets aligned to
    start_ts: pre-summed (vsum, vcount, vmin, vmax) rows from the views,
    raw samples as one-sample parts. A view whose bucket IS the output
    bucket needs no regrouping: each of its rows is a finished bucket (no
    other piece shares it - dirty ranges are whole buckets), so those rows
    skip the hash aggregate, with the same arithmetic on one row."""
    direct: list[sql.Composable] = []
    grouped: list[sql.Composable] = []
    direct_params: list = []
    grouped_params: list = []
    for part in parts:
        branch: sql.Composable
        if part[0] == "view":
            _, schema, table, size, a, b = part
            branch = sql.SQL(
                "SELECT point_uri, bucket AS b, vsum, vcount::bigint AS vcount, vmin, vmax FROM {} "
                "WHERE point_uri = ANY(%s) AND bucket >= %s AND bucket < %s").format(sql.Identifier(schema, table))
            target, target_params = (direct, direct_params) if size == interval_seconds else (grouped, grouped_params)
        else:
            _, a, b = part
            branch = sql.SQL(
                f"SELECT point_uri, ts AS b, v AS vsum, 1::bigint AS vcount, v AS vmin, v AS vmax FROM (SELECT point_uri, ts, {_V} AS v FROM point_history "
                "WHERE point_uri = ANY(%s) AND ts >= %s AND ts < %s) raw WHERE v IS NOT NULL")
            target, target_params = grouped, grouped_params
        target.append(branch)
        target_params += [point_uris, _ts(a), _ts(b)]
    selects: list[sql.Composable] = []
    params: list = []
    if direct:
        selects.append(sql.SQL(
            "SELECT point_uri, b AS bucket, {fold} AS agg FROM ({parts}) parts WHERE vcount > 0"
        ).format(fold=sql.SQL(_ROW_FOLD_SQL[fold]), parts=sql.SQL(" UNION ALL ").join(direct)))
        params += direct_params
    if grouped:
        selects.append(sql.SQL(
            "SELECT point_uri, date_bin(make_interval(secs => %s), b, %s) AS bucket, {fold} AS agg "
            "FROM ({parts}) parts GROUP BY point_uri, 2 HAVING SUM(vcount) > 0"
        ).format(fold=sql.SQL(_PART_FOLD_SQL[fold]), parts=sql.SQL(" UNION ALL ").join(grouped)))
        params += [interval_seconds, start_ts, *grouped_params]
    query = sql.SQL("SELECT point_uri, {}, agg FROM ({}) r ORDER BY bucket").format(
        sql.SQL(_EPOCH_SQL if epoch else "bucket"), sql.SQL(" UNION ALL ").join(selects))
    return list(iter_rows(conn.cursor(binary=True).execute(query, params)))


# A bucket's start as Unix seconds, the float datetime.timestamp() gives
# (both are integer microseconds / 1e6). date_part, not EXTRACT: float8.
_EPOCH_SQL = "date_part('epoch', bucket)"


def read_rollup(
    conn: psycopg.Connection,
    point_uris: list[str],
    start_ts: datetime,
    end_ts: datetime,
    fold: str,
    interval_seconds: int | None,
    interval_months: int | None,
    tz: str,
    epoch: bool = False,
) -> dict[str, list]:
    """Axon hisRollup(fold, interval), done in Postgres rather than
    shipping a month of raw samples to Python (or the browser) to fold
    there. Fixed intervals use date_bin aligned to the span start, the
    same origin Axon buckets from; month/quarter/year use date_trunc in
    the caller's zone so a "day" or "month" boundary is the local one.
    Booleans fold as 0/1 (so avg of a run status = fraction of the
    interval it was on); text values are skipped - there's nothing
    numeric to fold. Each bucket is stamped with its START.

    Fixed intervals read the continuous aggregates where they are up to
    date and raw rows elsewhere (_rollup_plan), inside one REPEATABLE READ
    snapshot so the bookkeeping read and the data read agree - the answer
    is the raw fold's as of that snapshot, never a stale one.

    `epoch`: buckets as Unix-seconds floats instead of datetimes - what
    /his sends anyway; skips building ~100k datetimes only to convert them
    back (Baler#107)."""
    if not point_uris:
        return {}
    if fold not in _FOLD_SQL:
        raise ValueError(f"unknown fold {fold!r}")
    out: dict[str, list] = {uri: [] for uri in point_uris}
    levels = _rollup_levels(start_ts, interval_seconds) if interval_months is None and end_ts > start_ts else []
    if levels:
        outermost = conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        with conn.transaction():
            if outermost:  # inside a caller's transaction, its snapshot rules apply
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            parts = _rollup_plan(conn, levels, start_ts, end_ts)
            rows = (_read_rollup_planned(conn, parts, point_uris, start_ts, fold, int(interval_seconds or 0), epoch)
                    if parts else None)
        if rows is not None:
            for point_uri, bucket, agg in rows:
                out[point_uri].append((bucket, float(agg) if agg is not None else None))
            return out
    if interval_months is not None:
        unit = _CALENDAR_TRUNC.get(interval_months)
        if unit is None:
            raise ValueError("calendar rollups support 1mo, 3mo, and 1yr/12mo only")
        bucket_sql = "date_trunc(%s, ts, %s)"
        bucket_params: tuple = (unit, tz)
    else:
        if not interval_seconds or interval_seconds <= 0:
            raise ValueError("rollup interval must be positive")
        bucket_sql = "date_bin(make_interval(secs => %s), ts, %s)"
        bucket_params = (interval_seconds, start_ts)
    # ORDER BY bucket alone: rows are filed per point below, so only each
    # point's own order matters, and sorting on point_uri too (a collated
    # text sort) was most of this query's time. Binary results: about half
    # the client-side decode cost of text for ~100k rows.
    cur = conn.cursor(binary=True).execute(
        f"""
        SELECT point_uri, {_EPOCH_SQL if epoch else "bucket"}, {_FOLD_SQL[fold]} AS agg FROM (
            SELECT point_uri, {bucket_sql} AS bucket,
                   COALESCE(value, value_bool::int::float8) AS v
            FROM point_history
            WHERE point_uri = ANY(%s) AND ts >= %s AND ts < %s
        ) t
        WHERE v IS NOT NULL
        GROUP BY point_uri, bucket
        ORDER BY bucket
        """,
        (*bucket_params, point_uris, start_ts, end_ts),
    )
    for point_uri, bucket, agg in iter_rows(cur):
        out[point_uri].append((bucket, float(agg) if agg is not None else None))
    return out


def delete_point_value(conn: psycopg.Connection, point_uri: str, ts: datetime) -> bool:
    """The Haxall IHisExt side of `val == None.val`: a specific timestamp
    is removed outright, not overwritten with a null value. Returns
    whether a row actually existed to delete."""
    cur = conn.execute(
        "DELETE FROM point_history WHERE point_uri = %s AND ts = %s",
        (point_uri, ts),
    )
    return cur.rowcount > 0

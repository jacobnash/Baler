"""
Point history in Postgres/Timescale - the thing Grafana (or Tableau, or
anything else that speaks SQL) reads natively, no plugin, no custom
connector. Three nullable value columns because sources emit int/float/str/
bool values today (e.g. Windrow's fan_status.presentValue = "active") - one
float column would lose that.
"""

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
    ddl.run(conn, SCHEMA, "SELECT create_hypertable('point_history', 'ts', if_not_exists => TRUE, migrate_data => TRUE)",
            LABELS_INDEX)
    _ensure_rollup_views(conn)
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
    conds = ["point_uri = u.uri"]
    params: list = [point_uris]
    if before is not None:
        conds.append("ts <= %s")
        params.append(before)
    if since is not None:
        conds.append("ts >= %s")
        params.append(since)
    if with_value:
        conds.append("(value IS NOT NULL OR value_text IS NOT NULL OR value_bool IS NOT NULL)")
    cur = conn.execute(
        f"""
        SELECT u.uri, h.ts, h.value, h.value_text, h.value_bool
        FROM unnest(%s::text[]) AS u(uri)
        CROSS JOIN LATERAL (
            -- one backward primary-key probe per point; DISTINCT ON sorted
            -- every row of every point's history (>60s at 20M rows)
            SELECT ts, value, value_text, value_bool FROM point_history
            WHERE {" AND ".join(conds)} ORDER BY ts DESC LIMIT 1
        ) h
        """,
        params,
    )
    return {uri: (ts, _row_to_value((v, vt, vb))) for uri, ts, v, vt, vb in cur.fetchall()}


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
            WHERE point_uri = u.uri AND ts >= %s AND ts < %s
            ORDER BY ts LIMIT %s
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
                         fold: str, interval_seconds: int) -> list[tuple]:
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
    query = sql.SQL("SELECT * FROM ({}) r ORDER BY bucket").format(sql.SQL(" UNION ALL ").join(selects))
    return list(iter_rows(conn.cursor(binary=True).execute(query, params)))


def read_rollup(
    conn: psycopg.Connection,
    point_uris: list[str],
    start_ts: datetime,
    end_ts: datetime,
    fold: str,
    interval_seconds: int | None,
    interval_months: int | None,
    tz: str,
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
    is the raw fold's as of that snapshot, never a stale one."""
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
            rows = _read_rollup_planned(conn, parts, point_uris, start_ts, fold, int(interval_seconds or 0)) if parts else None
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
        SELECT point_uri, bucket, {_FOLD_SQL[fold]} AS agg FROM (
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

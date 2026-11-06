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
# pre-summed rows instead. materialized_only = false: anything newer than the
# last refresh is folded from raw rows at query time, so results are never
# stale. Refreshed every 5/30 minutes over the last ROLLUP_WINDOW_DAYS + 5
# days; older spans keep using raw rows (never materialized there).
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


def read_latest_many(conn: psycopg.Connection, point_uris: list[str]) -> dict[str, tuple[datetime, object]]:
    """Latest (ts, value) per point, in one query: haystack_api's curVal
    and the commissioning existence check both need it for hundreds of
    points at once. Points with no samples are absent from the result."""
    if not point_uris:
        return {}
    cur = conn.execute(
        """
        SELECT u.uri, h.ts, h.value, h.value_text, h.value_bool
        FROM unnest(%s::text[]) AS u(uri)
        CROSS JOIN LATERAL (
            -- one backward primary-key probe per point; DISTINCT ON sorted
            -- every row of every point's history (>60s at 20M rows)
            SELECT ts, value, value_text, value_bool FROM point_history
            WHERE point_uri = u.uri ORDER BY ts DESC LIMIT 1
        ) h
        """,
        (point_uris,),
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
    return [(ts, _row_to_value((value, value_text, value_bool))) for ts, value, value_text, value_bool in cur.fetchall()]


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
    for point_uri, ts, value, value_text, value_bool in cur.fetchall():
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
_views_present: bool | None = None


def _rollup_view_for(conn, start_ts: datetime, end_ts: datetime, interval_seconds: int | None) -> tuple[str, int] | None:
    """The coarsest pre-folded view that gives exactly the raw answer: its
    bucket divides the interval, the span starts on one of its boundaries
    (so no bucket straddles two output buckets), and the span is inside the
    window the refresh policy keeps materialized."""
    global _views_present
    if not interval_seconds or start_ts.tzinfo is None:
        return None
    if start_ts < datetime.now(timezone.utc) - timedelta(days=ROLLUP_WINDOW_DAYS) or end_ts <= start_ts:
        return None
    if _views_present is not True:
        row = conn.execute("SELECT to_regclass('point_history_1h') IS NOT NULL").fetchone()
        _views_present = bool(row and row[0])
        if not _views_present:
            return None
    start_epoch = start_ts.timestamp()
    for view, size in ROLLUP_VIEWS:
        if interval_seconds % size == 0 and start_epoch % size == 0:
            return view, size
    return None


def _read_rollup_from_view(conn, view: str, size: int, point_uris: list[str], start_ts: datetime,
                           end_ts: datetime, fold: str, interval_seconds: int) -> list[tuple]:
    """Whole buckets from the view up to the last full one before end_ts,
    then the raw samples after it (the tail of a "today" span), folded
    together into date_bin buckets aligned to start_ts."""
    aligned_end = datetime.fromtimestamp(end_ts.timestamp() // size * size, tz=timezone.utc)
    aligned_end = max(aligned_end, start_ts)
    return conn.execute(
        f"""
        SELECT point_uri, date_bin(make_interval(secs => %s), b, %s) AS bucket, {_PART_FOLD_SQL[fold]} AS agg
        FROM (
            SELECT point_uri, bucket AS b, vsum, vcount::bigint AS vcount, vmin, vmax FROM {view}
            WHERE point_uri = ANY(%s) AND bucket >= %s AND bucket < %s
            UNION ALL
            SELECT point_uri, ts, v, 1::bigint, v, v FROM (
                SELECT point_uri, ts, {_V} AS v FROM point_history
                WHERE point_uri = ANY(%s) AND ts >= %s AND ts < %s
            ) raw WHERE v IS NOT NULL
        ) parts
        GROUP BY point_uri, 2
        HAVING SUM(vcount) > 0
        ORDER BY point_uri, 2
        """,
        (interval_seconds, start_ts, point_uris, start_ts, aligned_end, point_uris, aligned_end, end_ts),
    ).fetchall()


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
    numeric to fold. Each bucket is stamped with its START."""
    if not point_uris:
        return {}
    if fold not in _FOLD_SQL:
        raise ValueError(f"unknown fold {fold!r}")
    view = _rollup_view_for(conn, start_ts, end_ts, interval_seconds) if interval_months is None else None
    if view is not None:
        out: dict[str, list] = {uri: [] for uri in point_uris}
        for point_uri, bucket, agg in _read_rollup_from_view(conn, view[0], view[1], point_uris, start_ts, end_ts,
                                                             fold, int(interval_seconds or 0)):
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
    cur = conn.execute(
        f"""
        SELECT point_uri, bucket, {_FOLD_SQL[fold]} AS agg FROM (
            SELECT point_uri, {bucket_sql} AS bucket,
                   COALESCE(value, value_bool::int::float8) AS v
            FROM point_history
            WHERE point_uri = ANY(%s) AND ts >= %s AND ts < %s
        ) t
        WHERE v IS NOT NULL
        GROUP BY point_uri, bucket
        ORDER BY point_uri, bucket
        """,
        (*bucket_params, point_uris, start_ts, end_ts),
    )
    out = {uri: [] for uri in point_uris}
    for point_uri, bucket, agg in cur.fetchall():
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

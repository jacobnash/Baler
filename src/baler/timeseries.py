"""
Point history in Postgres/Timescale - the thing Grafana (or Tableau, or
anything else that speaks SQL) reads natively, no plugin, no custom
connector. Three nullable value columns because sources emit int/float/str/
bool values today (e.g. Windrow's fan_status.presentValue = "active") - one
float column would lose that.
"""

import os
from datetime import datetime

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
    out: dict[str, list] = {uri: [] for uri in point_uris}
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

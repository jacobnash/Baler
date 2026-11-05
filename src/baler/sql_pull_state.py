"""
Durable watermark for sql_puller.py - "queried this source through this
timestamp," not "last row's timestamp" (an empty poll window still
advances it, or the same empty range gets re-queried forever). Same
upsert shape as haystack_pull_state.py, keyed by source_id instead of
point_uri since one query pulls every point for a source at once.
"""

import psycopg

SCHEMA = """
CREATE TABLE IF NOT EXISTS sql_pull_checkpoint (
    source_id      TEXT NOT NULL PRIMARY KEY,
    last_pulled_ts DOUBLE PRECISION NOT NULL
);
"""


def ensure_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA)


def get_checkpoint(conn: psycopg.Connection, source_id: str) -> float | None:
    row = conn.execute(
        "SELECT last_pulled_ts FROM sql_pull_checkpoint WHERE source_id = %s",
        (source_id,),
    ).fetchone()
    return row[0] if row else None


def set_checkpoint(conn: psycopg.Connection, source_id: str, ts: float) -> None:
    conn.execute(
        """
        INSERT INTO sql_pull_checkpoint (source_id, last_pulled_ts)
        VALUES (%s, %s)
        ON CONFLICT (source_id) DO UPDATE SET last_pulled_ts = EXCLUDED.last_pulled_ts
        """,
        (source_id, ts),
    )

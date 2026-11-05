"""
Durable per-point watermark for haystack_puller.py - "queried this point's
history through this timestamp," not "last row's timestamp" (an empty poll
window still advances it, or the same empty range gets re-queried forever).
Same upsert shape as derivation_health.py's failure-count table, keyed
differently.
"""

import psycopg

SCHEMA = """
CREATE TABLE IF NOT EXISTS haystack_pull_checkpoint (
    point_uri      TEXT NOT NULL PRIMARY KEY,
    last_pulled_ts DOUBLE PRECISION NOT NULL
);
"""


def ensure_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA)


def get_checkpoint(conn: psycopg.Connection, point_uri: str) -> float | None:
    row = conn.execute(
        "SELECT last_pulled_ts FROM haystack_pull_checkpoint WHERE point_uri = %s",
        (point_uri,),
    ).fetchone()
    return row[0] if row else None


def set_checkpoint(conn: psycopg.Connection, point_uri: str, ts: float) -> None:
    conn.execute(
        """
        INSERT INTO haystack_pull_checkpoint (point_uri, last_pulled_ts)
        VALUES (%s, %s)
        ON CONFLICT (point_uri) DO UPDATE SET last_pulled_ts = EXCLUDED.last_pulled_ts
        """,
        (point_uri, ts),
    )

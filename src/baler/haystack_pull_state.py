"""
Durable per-point watermark for haystack_puller.py - "queried this point's
history through this timestamp," not "last row's timestamp" (an empty poll
window still advances it, or the same empty range gets re-queried forever).
Same upsert shape as derivation_health.py's failure-count table, keyed
differently.
"""

import psycopg

from baler import schema

# Schema (haystack_pull_checkpoint): baler/migrations/, from 0001 (#194).
# Change it with a new migration there, not here.


def ensure_schema(conn: psycopg.Connection) -> None:
    schema.ensure(conn)


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

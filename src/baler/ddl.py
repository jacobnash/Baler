"""
The advisory lock every Baler process takes before it changes the schema.

Schema changes are baler.schema's numbered migrations now (#194), run once
per deploy; before that every service ran its own `CREATE TABLE IF NOT
EXISTS ...` at startup under this lock. `IF NOT EXISTS` isn't safe under
concurrency in Postgres (two sessions creating the same table at once can
both pass the check, and the loser fails with a duplicate key on pg_type),
which killed a random API at first start. The migrations take the same key,
so a process still running the old connect-time DDL mid-deploy and the
migration runner take turns. `run` is left for settings kept in the
database (timeseries' compression policy).
"""

from __future__ import annotations

from psycopg import sql as _sql

# Arbitrary, fixed, and shared by every Baler process ("bale").
LOCK_KEY = 0x62616C65


def run(conn, *statements: str | _sql.Composable) -> None:
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        for stmt in statements:
            conn.execute(stmt)

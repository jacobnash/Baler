"""
Startup DDL, serialized across processes.

Every service runs its `CREATE TABLE IF NOT EXISTS ...` at startup, and
several share tables (auth's by five APIs, faults' by fault_api and
fault_detector, point_history by everything). `IF NOT EXISTS` isn't safe
under concurrency in Postgres: two sessions creating the same table at once
can both pass the check, and the loser fails with a duplicate key on
pg_type. Under `docker compose up` that killed a random API at first start
(restart: unless-stopped hid it; `up --wait` didn't). One transaction-scoped
advisory lock around all of it makes the services take turns.
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

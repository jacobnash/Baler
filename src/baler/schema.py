"""
Versioned schema migrations (#194).

Until this, every service ran its own `CREATE ... IF NOT EXISTS` at connect
time (baler.ddl). That adds tables well but can't change one safely over a
building's 15+ years. Now the schema is a numbered list of migrations
(baler.migrations), and the database records which it has
(baler_schema_migrations). Migration 0001 is the baseline: a frozen copy of
what the old connect-time code built, which on an existing install only
adopts what's there.

How it runs:

- Once per deploy, before any daemon: compose's `migrate` service runs
  `python -m baler.migrate`, which runs `upgrade` here (and the Timberdoodle
  graph rewrite it always ran).
- Once per process, never per connection: `ensure(conn)` (what
  timeseries.connect/connect_pool and every module's ensure_schema call)
  reads the recorded versions on its first call for a database and then
  never again in that process. If the database is behind (a process
  started without the migrate service: tests, bare-metal), it upgrades.
- Each migration runs in its own transaction, under baler.ddl's advisory
  lock (the same key the old connect-time DDL takes, so old and new code
  mid-deploy take turns), and re-reads the recorded versions after taking
  it, so concurrent starters apply each migration exactly once. Its row in
  baler_schema_migrations commits with it: a migration that fails leaves
  nothing applied and nothing recorded.

    python -m baler.schema status     # applied and pending
    python -m baler.schema plan       # dry run: what upgrade would run (--sql for the statements)
    python -m baler.schema upgrade    # apply what's pending
    python -m baler.schema stamp N    # record 1..N as applied without running them

See docs-site/pages/schema-migrations.mdx.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql

from baler import ddl

log = logging.getLogger(__name__)

TABLE = "baler_schema_migrations"
VERSION_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS baler_schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    how         TEXT NOT NULL,          -- applied | adopted (baseline on an existing install) | stamped
    statements  INTEGER NOT NULL,       -- how many statements it ran here
    duration_ms INTEGER NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)"""


@dataclass(frozen=True)
class Step:
    """One statement a migration runs, and what it is for (shown by `plan`)."""

    sql: str | sql.Composable
    note: str = ""
    params: tuple | None = None


def migrations() -> tuple:
    from baler.migrations import ALL

    versions = [m.VERSION for m in ALL]
    if versions != list(range(1, len(ALL) + 1)):
        raise RuntimeError(f"baler.migrations.ALL must be numbered 1..N in order, got {versions}")
    return ALL


def head() -> int:
    return len(migrations())


def applied(conn) -> dict[int, dict[str, Any]]:
    """The recorded migrations, by version; {} on a database that has never
    been migrated (no table yet)."""
    if conn.execute("SELECT to_regclass(%s)", (TABLE,)).fetchone()[0] is None:
        return {}
    rows = conn.execute(
        f"SELECT version, name, how, statements, duration_ms, applied_at FROM {TABLE} ORDER BY version").fetchall()
    return {r[0]: {"name": r[1], "how": r[2], "statements": r[3], "duration_ms": r[4], "applied_at": r[5]}
            for r in rows}


def pending(conn) -> list:
    have = applied(conn)
    return [m for m in migrations() if m.VERSION not in have]


def plan(conn) -> list[tuple[Any, list[Step]]]:
    """Dry run: each pending migration and the steps it would run against
    the database as it is now. Read-only. A later migration whose steps
    depend on the catalog is planned against today's catalog, not the one
    the earlier migrations would leave; upgrade re-plans each one just
    before it runs."""
    return [(m, list(m.plan(conn))) for m in pending(conn)]


def _lock(conn) -> None:
    # No statement timeout for a migration: a pool connection may carry
    # the gateway's 55 s one, and a migration that rewrites chunks must
    # not die halfway because of it (it would roll back, but never finish).
    conn.execute("SET LOCAL statement_timeout = 0")
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (ddl.LOCK_KEY,))
    conn.execute(VERSION_TABLE_DDL)


def upgrade(conn, echo=None) -> list[tuple[int, str, str, int]]:
    """Apply every pending migration, in order, each in its own
    transaction under the lock. Returns (version, name, how, statements)
    for each one this call applied; [] when the database was current."""
    done: list[tuple[int, str, str, int]] = []
    for m in migrations():
        with conn.transaction():
            _lock(conn)
            if conn.execute(f"SELECT 1 FROM {TABLE} WHERE version = %s", (m.VERSION,)).fetchone():
                continue  # applied before, or by another process while we waited for the lock
            how = "adopted" if getattr(m, "adopts", None) and m.adopts(conn) else "applied"
            steps = list(m.plan(conn))
            if echo:
                echo(f"schema: {'adopting' if how == 'adopted' else 'applying'} {m.VERSION:04d} {m.NAME} "
                     f"({len(steps)} statement(s))")
            t0 = time.monotonic()
            for step in steps:
                conn.execute(step.sql, step.params)
            ms = int((time.monotonic() - t0) * 1000)
            conn.execute(f"INSERT INTO {TABLE} (version, name, how, statements, duration_ms) VALUES (%s, %s, %s, %s, %s)",
                         (m.VERSION, m.NAME, how, len(steps), ms))
        log.info("schema: %s %04d %s (%d statement(s), %d ms)", how, m.VERSION, m.NAME, len(steps), ms)
        done.append((m.VERSION, m.NAME, how, len(steps)))
    return done


def stamp(conn, version: int) -> list[int]:
    """Record migrations 1..version as applied without running them: for a
    database whose schema someone brought to that point by other means.
    Returns the versions it recorded (already-recorded ones are left)."""
    ms = {m.VERSION: m for m in migrations()}
    if version not in ms:
        raise ValueError(f"no migration {version}; head is {head()}")
    out = []
    with conn.transaction():
        _lock(conn)
        for v in range(1, version + 1):
            cur = conn.execute(f"INSERT INTO {TABLE} (version, name, how, statements, duration_ms) "
                               "VALUES (%s, %s, 'stamped', 0, 0) ON CONFLICT (version) DO NOTHING",
                               (v, ms[v].NAME))
            if cur.rowcount:
                out.append(v)
    return out


# --- once per process --------------------------------------------------------

_ready: set[tuple] = set()
_ready_lock = threading.Lock()


def _key(conn) -> tuple:
    info = conn.info
    return (info.host, info.port, info.dbname)


def ensure(conn) -> None:
    """The database is at this code's schema version. Reads the recorded
    versions on the first call per database in this process, upgrading if
    it is behind; every later call is free. A database *ahead* of this code
    (a rollback after a newer release migrated it) is left as it is, with
    a warning: migrations only add, so older code keeps working on it."""
    key = _key(conn)
    if key in _ready:
        return
    with _ready_lock:
        if key in _ready:
            return
        have = applied(conn)
        if any(m.VERSION not in have for m in migrations()):
            upgrade(conn)
        elif max(have) > head():
            log.warning("schema: database is at migration %d, this code knows only up to %d", max(have), head())
        _ready.add(key)


# --- what the schema is, for comparing two databases -------------------------

_SNAPSHOT_SQL = {
    "columns": """
        SELECT c.table_name, c.column_name, c.data_type, c.udt_name, c.is_nullable, c.column_default
        FROM information_schema.columns c JOIN information_schema.tables t
          ON t.table_schema = c.table_schema AND t.table_name = c.table_name
        WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'""",
    "constraints": """
        SELECT cl.relname, co.conname, pg_get_constraintdef(co.oid)
        FROM pg_constraint co JOIN pg_class cl ON cl.oid = co.conrelid JOIN pg_namespace n ON n.oid = cl.relnamespace
        WHERE n.nspname = 'public'""",
    "indexes": "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'",
    "hypertables": """
        SELECT h.hypertable_name, d.column_name, d.time_interval::text, h.compression_enabled
        FROM timescaledb_information.hypertables h
        JOIN timescaledb_information.dimensions d ON d.hypertable_name = h.hypertable_name
        WHERE h.hypertable_schema = 'public'""",
    "compression": """
        SELECT hypertable_name, attname, segmentby_column_index, orderby_column_index, orderby_asc
        FROM timescaledb_information.compression_settings WHERE hypertable_schema = 'public'""",
    "aggregates": """
        SELECT view_name, materialized_only, regexp_replace(view_definition, '\\s+', ' ', 'g')
        FROM timescaledb_information.continuous_aggregates WHERE view_schema = 'public'""",
    "policies": """
        SELECT j.proc_name, COALESCE(c.view_name, j.hypertable_name), j.schedule_interval::text,
               (j.config - 'hypertable_id' - 'mat_hypertable_id')::text
        FROM timescaledb_information.jobs j
        LEFT JOIN timescaledb_information.continuous_aggregates c ON c.materialization_hypertable_name = j.hypertable_name
        WHERE j.proc_name LIKE 'policy_%'""",
}


def snapshot(conn) -> dict[str, list[tuple]]:
    """The schema as comparable data: every public table's columns,
    constraints and indexes, and TimescaleDB's hypertables, compression
    settings, continuous aggregates and policies, with the internal ids
    that differ between two databases left out. Column order isn't part
    of it: a column a later ALTER added sits last on an old install."""
    return {k: sorted(tuple("" if v is None else str(v) for v in row) for row in conn.execute(q).fetchall())
            for k, q in _SNAPSHOT_SQL.items()}


# --- command line ------------------------------------------------------------


def _status(conn) -> int:
    have = applied(conn)
    for m in migrations():
        row = have.get(m.VERSION)
        state = (f"{row['how']} {row['applied_at']:%Y-%m-%d %H:%M} ({row['statements']} statement(s), "
                 f"{row['duration_ms']} ms)") if row else "PENDING"
        print(f"{m.VERSION:04d} {m.NAME:<30} {state}")
    for v in sorted(set(have) - {m.VERSION for m in migrations()}):
        print(f"{v:04d} {have[v]['name']:<30} {have[v]['how']} (newer than this code)")
    return 0


def _plan(conn, show_sql: bool) -> int:
    planned = plan(conn)
    if not planned:
        print(f"schema: at migration {head()}, nothing to do")
        return 0
    for m, steps in planned:
        how = "adopt" if getattr(m, "adopts", None) and m.adopts(conn) else "apply"
        print(f"{how} {m.VERSION:04d} {m.NAME}: {len(steps)} statement(s)")
        for step in steps:
            print(f"  - {step.note or str(step.sql).strip().splitlines()[0]}")
            if show_sql:
                text = step.sql.as_string(conn) if isinstance(step.sql, sql.Composable) else step.sql
                print("    " + text.strip().replace("\n", "\n    ") + (f"   -- params {step.params}" if step.params else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    from baler import timeseries  # not at import: timeseries imports this module

    p = argparse.ArgumentParser(prog="python -m baler.schema", description="Baler's versioned schema migrations")
    p.add_argument("--dsn", default=timeseries.DEFAULT_DSN, help="default: BALER_PG_DSN")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="applied and pending migrations")
    pp = sub.add_parser("plan", help="dry run: what upgrade would run")
    pp.add_argument("--sql", action="store_true", help="print each statement too")
    sub.add_parser("upgrade", help="apply pending migrations")
    sp = sub.add_parser("stamp", help="record 1..VERSION as applied without running them")
    sp.add_argument("version", type=int)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with psycopg.connect(args.dsn, autocommit=True) as conn:
        if args.cmd == "status":
            return _status(conn)
        if args.cmd == "plan":
            return _plan(conn, args.sql)
        if args.cmd == "stamp":
            print(f"schema: stamped {stamp(conn, args.version) or 'nothing new'}")
            return 0
        done = upgrade(conn, echo=lambda msg: print(msg, flush=True))
        print(f"schema: at migration {head()}" + ("" if done else ", nothing to do"))
        # A setting, not schema: the compression policy follows
        # BALER_HISTORY_COMPRESS_AFTER.
        timeseries.sync_settings(conn)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

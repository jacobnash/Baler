"""Versioned schema migrations against a real TimescaleDB (#194).

Each test gets its own scratch database on docker-compose's Postgres
(CREATE DATABASE, dropped afterwards), so nothing here touches the shared
`baler` database the other integration tests use.

- A fresh database: migration 0001 builds everything, and a second run
  changes nothing.
- An upgrade from every release: the database is built by that release's
  own code (the pre-migration release builds its schema at connect time,
  as the live install's was built), with history in compressed chunks and
  a late write into one, then upgraded by this code. It must end with
  exactly the schema a fresh database gets, with every row and every
  compressed chunk as it was, and a second run must change nothing.
- Concurrent starters apply a migration exactly once.
- A migration that needs its hypertable's compressed chunks decompressed
  (migrations.hypertable) runs, keeps every row, and leaves the chunks
  compressed again.

The release builds need the git history (CI's integration job checks out
with fetch-depth 0): the source of each release is taken with `git archive`
and run in a subprocess with its own src/ first on PYTHONPATH.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import threading
import types
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from baler import schema, timeseries
from baler.migrations import hypertable

pytestmark = pytest.mark.integration

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The last release before versioned migrations: main just before #194.
# Every database made before then (the live install's included) was built
# by this code's connect-time DDL.
PRE_MIGRATIONS_REF = "6946378981eae2db09a7e312309b26b19ce66db6"


def _releases() -> list[str]:
    tags = subprocess.run(["git", "-C", REPO, "tag", "--list", "v*", "--sort=v:refname"],
                          capture_output=True, text=True, check=True).stdout.split()
    return [PRE_MIGRATIONS_REF, *tags]


# --- scratch databases -------------------------------------------------------


def _dsn(dbname: str) -> str:
    return psycopg.conninfo.make_conninfo(timeseries.DEFAULT_DSN, dbname=dbname)


@pytest.fixture
def scratch():
    """A factory for empty databases (TimescaleDB comes from template1, as
    in the timescale image), each dropped at the end of the test."""
    made: list[str] = []
    admin = psycopg.connect(timeseries.DEFAULT_DSN, autocommit=True)

    def make() -> str:
        name = f"baler_schema_{uuid.uuid4().hex[:10]}"
        admin.execute(f'CREATE DATABASE "{name}"')
        made.append(name)
        return _dsn(name)

    yield make
    for name in made:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.close()


def _connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, autocommit=True)


def _without_version_table(snap: dict) -> dict:
    return {k: [r for r in rows if schema.TABLE not in r] for k, rows in snap.items()}


# Every table's row count, and point_history's every value, in one string.
_FINGERPRINT = """
SELECT count(*), md5(string_agg(concat_ws('|', point_uri, ts, value, value_text, value_bool, unit, label, quality),
                                 ',' ORDER BY point_uri, ts))
FROM point_history
"""


def _data(conn) -> dict:
    tables = [r[0] for r in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> %s ORDER BY 1",
        (schema.TABLE,)).fetchall()]
    out: dict = {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
    out["point_history"] = conn.execute(_FINGERPRINT).fetchone()
    out["compressed chunks"] = conn.execute(
        "SELECT count(*) FILTER (WHERE is_compressed), count(*) FROM timescaledb_information.chunks "
        "WHERE hypertable_name = 'point_history'").fetchone()
    return out


# --- a fresh database ---------------------------------------------------------


def test_fresh_database(scratch):
    with _connect(scratch()) as conn:
        planned = schema.plan(conn)
        assert [m.VERSION for m, _ in planned] == list(range(1, schema.head() + 1))
        done = schema.upgrade(conn)
        assert [(v, how) for v, _, how, _ in done] == [(m.VERSION, "applied") for m, _ in planned]
        assert done[0][3] == len(planned[0][1])  # it ran what plan said
        snap = schema.snapshot(conn)
        assert ("point_history", "ts", "7 days", "True") in snap["hypertables"]
        assert {r[0] for r in snap["aggregates"]} == {"point_history_5m", "point_history_1h"}

        assert schema.plan(conn) == []
        assert schema.upgrade(conn) == []  # twice: nothing
        assert schema.snapshot(conn) == snap
        assert set(schema.applied(conn)) == set(range(1, schema.head() + 1))


def test_stamp_records_without_running(scratch):
    with _connect(scratch()) as conn:
        assert schema.stamp(conn, 1) == [1]
        assert schema.stamp(conn, 1) == []
        assert schema.applied(conn)[1]["how"] == "stamped"
        assert conn.execute("SELECT to_regclass('point_history')").fetchone()[0] is None  # nothing ran


# --- upgrade from every release ------------------------------------------------

# Runs in a subprocess with the release's src/ first on PYTHONPATH: builds
# the schema the way that release's services did, then writes data the way
# they write it. Compressed chunks, and a late write into one, as on the
# live install.
_BUILD = r"""
import os, sys
from datetime import datetime, timedelta, timezone
import baler
from baler import timeseries
dsn = os.environ["BALER_PG_DSN"]
assert baler.__path__[0].startswith(os.environ["RELEASE_SRC"]), baler.__path__
if os.path.exists(os.path.join(os.environ["RELEASE_SRC"], "baler", "schema.py")):
    from baler import schema
    schema.main(["--dsn", dsn, "upgrade"])
conn = timeseries.connect(dsn)  # point_history, its aggregates and compression, as every service did
from baler import (auth, broker_accounts, conventions, derivation_health, equip_inference, faults,
                   haystack_pull_state, heartbeat, mapping_proposals, memory, point_write, quality, record_edit,
                   scope, sources, sql_pull_state)
from baler.commissioning import db as cx_db
for mod in (auth, scope, faults, sources, broker_accounts, conventions, derivation_health, haystack_pull_state,
            heartbeat, memory, quality, sql_pull_state, point_write, record_edit, cx_db):
    mod.ensure_schema(conn)
mapping_proposals.PgProposals(conn).ensure_schema()
equip_inference.PgRelationships(conn).ensure_schema()

now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
rows = []
for p in range(4):
    uri = f"urn:point:fbf/upgrade/p{p}"
    for span, step in (((now - timedelta(days=60), now - timedelta(days=45)), 300), ((now - timedelta(days=1), now), 60)):
        ts, i = span[0], 0
        while ts < span[1]:
            value = (i % 2 == 0) if p == 3 else (p * 100 + i * 0.25)
            rows.append((uri, value, ts, "degF" if p == 0 else None, "Zone Temp" if p == 0 else None,
                         "suspect" if i % 101 == 0 else None))
            ts, i = ts + timedelta(seconds=step), i + 1
timeseries.write_point_values(conn, rows)
n = conn.execute("SELECT count(compress_chunk(c)) FROM show_chunks('point_history', older_than => %s::timestamptz) c",
                 (now - timedelta(days=30),)).fetchone()[0]
assert n > 0, "no chunk compressed"
timeseries.write_point_value(conn, "urn:point:fbf/upgrade/p1", 12.5, now - timedelta(days=59, seconds=30))  # late, into a compressed chunk
conn.execute("CALL refresh_continuous_aggregate('point_history_5m', NULL, NULL)")
conn.execute("CALL refresh_continuous_aggregate('point_history_1h', NULL, NULL)")
org = auth.create_org(conn, "Upgrade Org", "upgrade-admin@example.com", "pw-123456789")
auth.create_site(conn, org["id"], "Upgrade Site", [])
faults.open_fault(conn, "zone-temp-high", "urn:point:fbf/upgrade/p0", now - timedelta(hours=2), {"value": 81.5}, "critical")
print(f"built with {baler.__path__[0]}, {len(rows)} rows, {n} chunk(s) compressed")
"""


def _build_with_release(ref: str, dsn: str, tmp_path) -> str:
    if subprocess.run(["git", "-C", REPO, "cat-file", "-e", f"{ref}^{{commit}}"], capture_output=True, check=False).returncode:
        pytest.fail(f"release {ref} isn't in this checkout's git history: the upgrade test needs it "
                    "(CI: actions/checkout with fetch-depth: 0; locally: git fetch origin --tags)")
    archive = subprocess.run(["git", "-C", REPO, "archive", ref, "src"], capture_output=True, check=True).stdout
    root = tmp_path / ref[:12]
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(root, filter="data")
    src = str(root / "src")
    env = {**os.environ, "PYTHONPATH": src, "RELEASE_SRC": src, "BALER_PG_DSN": dsn,
           "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("BALER_HISTORY_COMPRESS_AFTER", None)
    out = subprocess.run([sys.executable, "-c", _BUILD], env=env, capture_output=True, text=True, cwd=str(root), check=False)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize("ref", _releases())
def test_upgrade_from_release(ref, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(timeseries, "COMPRESS_AFTER", timeseries.COMPRESS_AFTER_DEFAULT)
    with _connect(scratch()) as fresh:
        schema.upgrade(fresh)
        want = schema.snapshot(fresh)

    dsn = scratch()
    print(_build_with_release(ref, dsn, tmp_path))
    with _connect(dsn) as conn:
        before_schema, before_data = schema.snapshot(conn), _data(conn)
        assert before_data["compressed chunks"][0] > 0

        planned = schema.plan(conn)
        done = schema.upgrade(conn)
        assert [v for v, *_ in done] == [m.VERSION for m, _ in planned]
        if ref == PRE_MIGRATIONS_REF:
            # Adopted, not rebuilt: nothing on point_history ran (its pieces
            # were all there), only the other tables' idempotent statements.
            assert done[0][2] == "adopted"
            assert not [s for s in planned[0][1] if "point_history" in s.note]
            assert _without_version_table(before_schema) == _without_version_table(want)

        assert schema.snapshot(conn) == want  # exactly a fresh database's schema
        assert _data(conn) == before_data  # every row, every compressed chunk, as it was

        assert schema.plan(conn) == []
        assert schema.upgrade(conn) == []  # twice: nothing
        assert schema.snapshot(conn) == want and _data(conn) == before_data

    # and the upgraded database works for this code
    schema._ready.discard(schema._key(_connect(dsn)))
    conn = timeseries.connect(dsn)
    try:
        ts = datetime.now(timezone.utc) - timedelta(days=58)  # into a compressed chunk
        timeseries.write_point_value(conn, "urn:point:fbf/upgrade/p2", 7.0, ts, quality="ok")
        assert timeseries.read_range(conn, "urn:point:fbf/upgrade/p2", ts, ts + timedelta(seconds=1))[0][1] == 7.0
    finally:
        conn.close()


# --- concurrency and once per process -------------------------------------------


def test_concurrent_starters_apply_once(scratch):
    dsn = scratch()
    results: list = []
    barrier = threading.Barrier(6)

    def start():
        with _connect(dsn) as conn:
            barrier.wait()
            results.append(schema.upgrade(conn))

    threads = [threading.Thread(target=start) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(len(r) for r in results) == [0] * 5 + [schema.head()]
    with _connect(dsn) as conn:
        assert len(schema.applied(conn)) == schema.head()


def test_ensure_reads_the_catalog_once_per_process(scratch, monkeypatch):
    dsn = scratch()
    with _connect(dsn) as conn:
        schema._ready.discard(schema._key(conn))
        schema.ensure(conn)
        assert set(schema.applied(conn)) == set(range(1, schema.head() + 1))
        monkeypatch.setattr(schema, "applied", lambda c: pytest.fail("read the catalog again"))
        schema.ensure(conn)
    with _connect(dsn) as other:  # a new connection to the same database: free too
        schema.ensure(other)


# --- a migration that needs decompressed chunks ----------------------------------


def _decompress_migration(version: int):
    """A migration of the kind TimescaleDB refuses on compressed chunks:
    a column type change on point_history."""
    stmt = "ALTER TABLE point_history ALTER COLUMN quality TYPE VARCHAR(32)"
    return types.SimpleNamespace(
        VERSION=version, NAME="quality as varchar (test)",
        plan=lambda conn: hypertable.with_decompressed(conn, "point_history", [stmt]))


def test_migration_over_compressed_chunks(scratch, monkeypatch):
    dsn = scratch()
    with _connect(dsn) as conn:
        schema.upgrade(conn)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        timeseries.write_point_values(conn, [(f"urn:point:fbf/dc/p{p}", float(i), now - timedelta(days=40, minutes=i),
                                              None, None, "suspect" if i % 50 == 0 else None)
                                             for p in range(3) for i in range(3000)])
        conn.execute("SELECT compress_chunk(c) FROM show_chunks('point_history', older_than => now() - interval '30 days') c")
        before = _data(conn)
        compressed = hypertable.compressed_chunks(conn, "point_history")
        assert compressed and before["compressed chunks"][0] == len(compressed)

        with pytest.raises(psycopg.errors.FeatureNotSupported), conn.transaction():
            conn.execute("ALTER TABLE point_history ALTER COLUMN quality TYPE VARCHAR(32)")

        monkeypatch.setattr("baler.migrations.ALL", (*schema.migrations(), _decompress_migration(schema.head() + 1)))
        (m, steps), = schema.plan(conn)
        assert "need that much free disk" in steps[0].note
        assert [s.note.split()[0] for s in steps].count("decompress") == len(compressed)
        assert schema.upgrade(conn) == [(m.VERSION, m.NAME, "applied", len(steps))]

        assert conn.execute("SELECT data_type FROM information_schema.columns WHERE table_name = 'point_history' "
                            "AND column_name = 'quality'").fetchone()[0] == "character varying"
        assert _data(conn) == before  # every row back, the same chunks compressed again
        assert schema.upgrade(conn) == []

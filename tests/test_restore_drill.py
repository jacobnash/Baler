"""The restore drill end to end (#196, #241): seed a stack the way Baler's
own code builds it (point_history hypertable with its compression policy
and continuous aggregates, the auth, site and Cx tables, the graph), force
some chunks compressed, run the real scripts/backup.py, then the real
scripts/restore_drill.py (which restores with scripts/restore.py into a
fresh scratch stack and runs its own checks), and compare the restored
stack with the source: every table's rows, every point_history value in
the compressed and the uncompressed range, the aggregates after a refresh,
the policies, the graph and the Cx report.

Needs docker but not the main stack: both stacks are scratch compose
projects, "restore" and "restore-drill" (scripts/restore-drill.compose.yml,
loopback ports Docker picks), removed afterwards with their volumes.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
import requests
from rdflib import Graph, Literal, URIRef

from baler import (
    auth,
    broker_accounts,
    conventions,
    derivation_health,
    faults,
    haystack_pull_state,
    heartbeat,
    memory,
    quality,
    scope,
    sources,
    sql_pull_state,
    timeseries,
)
from baler.commissioning import db as cx_db
from baler.commissioning import phases
from baler.remote_store import RemoteStore
from baler.store import Store

pytestmark = pytest.mark.integration

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
PROJECT = "restore"  # scratch compose projects "restore" (the source) and "restore-drill"

_spec = importlib.util.spec_from_file_location("restore_drill", os.path.join(SCRIPTS, "restore_drill.py"))
assert _spec and _spec.loader
rd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rd)

POINTS = [f"urn:point:fbf/drill/p{i}" for i in range(6)]
NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)
OLD = (NOW - timedelta(days=60), NOW - timedelta(days=46))  # compressed below
RECENT = (NOW - timedelta(hours=36), NOW)  # stays uncompressed


def _sample(i: int, p: int):
    """(value, value_text, value_bool, unit, label, quality) of sample i of
    point p: numbers with fractions, a boolean point, a text point, a unit
    and label on one, a quality flag now and then."""
    if p == 4:
        return None, None, i % 3 == 0, None, None, None
    if p == 5:
        return None, ("on" if i % 2 else "off \t\"x\"\n"), None, None, None, None
    return (p * 1000 + i * 0.37 - (i % 7) * 1e-3, None, None, "degF" if p == 0 else None,
            "Zone Temp" if p == 0 else None, "suspect" if i % 97 == 0 else None)


def _seed_history(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur, cur.copy(
            "COPY point_history (point_uri, ts, value, value_text, value_bool, unit, label, quality) FROM STDIN") as cp:
        for (a, b), step in ((OLD, timedelta(minutes=5)), (RECENT, timedelta(minutes=1))):
            for p, uri in enumerate(POINTS):
                ts, i = a, 0
                while ts < b:
                    cp.write_row((uri, ts, *_sample(i, p)))
                    ts, i = ts + step, i + 1
    # A raw COPY bypasses the writers that keep point_registry (#243); fill
    # it the way they (and migration 0002's seed) would, so the drill's
    # idempotent re-insert finds it already current.
    conn.execute("INSERT INTO point_registry (point_uri, last_ts) SELECT point_uri, max(ts) FROM point_history "
                 "GROUP BY point_uri ON CONFLICT (point_uri) DO UPDATE SET last_ts = EXCLUDED.last_ts")


def _seed(src) -> dict:
    conn = timeseries.connect(src.dsn)  # point_history, its aggregates and compression policy, as every service does
    for mod in (auth, scope, faults, sources, broker_accounts, conventions, derivation_health, haystack_pull_state,
                heartbeat, memory, quality, sql_pull_state, cx_db):
        mod.ensure_schema(conn)
    _seed_history(conn)
    # Compress the old range, then write late into it (what an edge node's
    # spool or a hisWrite does): that chunk is now partially compressed.
    compressed = conn.execute(
        "SELECT count(compress_chunk(c)) FROM show_chunks('point_history', older_than => %s::timestamptz) c",
        (NOW - timedelta(days=30),)).fetchone()[0]
    timeseries.write_point_value(conn, POINTS[0], 12.5, OLD[0] + timedelta(minutes=2, seconds=30), "degF", "Zone Temp")
    timeseries.write_point_value(conn, POINTS[1], 99.0, OLD[0] + timedelta(minutes=5))  # overwrite a compressed sample
    conn.execute("CALL refresh_continuous_aggregate('point_history_5m', NULL, NULL)")
    conn.execute("CALL refresh_continuous_aggregate('point_history_1h', NULL, NULL)")

    org = auth.create_org(conn, "Drill Org", "drill-admin@example.com", "pw-123456789")
    site = auth.create_site(conn, org["id"], "Drill Site", ["fbf/drill/"])
    auth.create_user(conn, org["id"], "drill-viewer@example.com", "pw-987654321", "viewer", "Drill Viewer")
    auth.create_api_key(conn, org["id"], "viewer", [site["id"]])
    faults.open_fault(conn, "zone-temp-high", POINTS[0], NOW - timedelta(hours=2), {"value": 81.5}, "critical")

    repo = cx_db.PgRepo(timeseries.connect_pool(src.dsn, min_size=1, max_size=2), Store())
    pid, rid = cx_db.new_id(), cx_db.new_id()
    repo.create_project({"id": pid, "org_id": org["id"], "site_id": site["id"], "name": "Drill Cx", "settings": {},
                         "created_by": "drill", "created_at": NOW, "updated_at": NOW,
                         "phases": phases.set_current(phases.default_phases(), "functional", NOW)})
    results = [{"equip_uri": "urn:equip:fbf/drill/ahu-1", "check_id": "exists", "subject": POINTS[0], "status": "pass",
                "severity": None, "symptom": None, "candidates": [], "evidence": {"last_ts": NOW.isoformat()}},
               {"equip_uri": "urn:equip:fbf/drill/ahu-1", "check_id": "frozen", "subject": POINTS[2], "status": "fail",
                "severity": "high", "symptom": "flat for 6 h", "candidates": [], "evidence": {}}]
    repo.save_run({"id": rid, "project_id": pid, "phase": "functional", "started_at": NOW - timedelta(minutes=1),
                   "finished_at": NOW, "window_start": NOW - timedelta(hours=6), "window_end": NOW, "triggered_by": "drill",
                   "scope": {"caller": {"prefixes": None, "exclude": []}, "site_prefixes": []},
                   "summary": {"pass": 1, "fail": 1}}, results)
    repo.pool.close()

    store = RemoteStore(src.oxigraph_url)
    brick = "https://brickschema.org/schema/Brick#"
    store.add_entity(URIRef("urn:equip:fbf/drill/ahu-1"), URIRef(brick + "AHU"))
    for uri in POINTS:
        store.add_entity(URIRef(uri), URIRef(brick + "Point"))
        store.add_relationship(URIRef("urn:equip:fbf/drill/ahu-1"), URIRef(brick + "hasPoint"), URIRef(uri))
    store.add_many([(URIRef(POINTS[0]), URIRef("http://www.w3.org/2000/01/rdf-schema#label"),
                     Literal('Zone "Temp" – AHU-1\nline two'))])
    conn.close()
    return {"compressed": compressed}


@pytest.fixture(scope="module")
def drilled(tmp_path_factory):
    src, dst = rd.Scratch(PROJECT), rd.Scratch(PROJECT + "-drill")
    out = tmp_path_factory.mktemp("restore-drill")
    try:
        src.up()
        seeded = _seed(src)
        subprocess.run([sys.executable, os.path.join(SCRIPTS, "backup.py"), "--out-dir", str(out / "backups"),
                        "--oxigraph-url", src.oxigraph_url, "--compose-project-dir", REPO], env=src.env, check=True)
        report_path = out / "report.json"
        # No off-site target anywhere (#196 stands on its own): the drill
        # restores the newest local backup. It reports to the "live" stack
        # its environment names, here the source stack, as backup.py did.
        env = {k: v for k, v in src.env.items() if not k.startswith("BALER_BACKUP_")}
        drill = subprocess.run([sys.executable, os.path.join(SCRIPTS, "restore_drill.py"), "--backup-dir",
                                str(out / "backups"), "--project", dst.project, "--keep", "--report", str(report_path)],
                               env=env, check=False)
        report = json.loads(report_path.read_text())
        dst.attach()
        yield {"src": src, "dst": dst, "report": report, "returncode": drill.returncode, "out": out, **seeded}
    finally:
        dst.down()
        src.down()


def _both(drilled, q, params=None):
    out = []
    for side in ("src", "dst"):
        with psycopg.connect(drilled[side].dsn, autocommit=True) as conn:
            out.append(conn.execute(q, params).fetchall())
    return out


def test_drill_passes(drilled):
    failed = [c for c in drilled["report"]["checks"] if not c["ok"]]
    assert drilled["returncode"] == 0 and drilled["report"]["ok"], failed
    names = {c["check"] for c in drilled["report"]["checks"]}
    # The checks that matter actually ran (not skipped for lack of data).
    for name in ("every table has the dump's rows", "point_history samples", "write into a compressed chunk",
                 "schema migrations", "point_history chunk interval",
                 "point_history_5m = raw fold, compressed window", "point_history_1h = raw fold, recent window",
                 "Cx report renders", "graph triples"):
        assert name in names
    assert not any("skipped" in c["detail"] for c in drilled["report"]["checks"])
    assert drilled["report"]["facts"]["source"] == "local"


def test_backup_and_drill_reported_to_ops_status(drilled):
    """Both scripts wrote their result where /ops/status reads it (#276,
    #196): the source stack's service_heartbeats, judged by ops_status."""
    from baler import ops_status

    with psycopg.connect(drilled["src"].dsn, autocommit=True) as conn:
        rows = dict(conn.execute("SELECT service, detail FROM service_heartbeats "
                                 "WHERE service IN ('backup', 'restore_drill')").fetchall())
    assert rows["backup"]["ok"] and rows["backup"]["local"]["ok"] and not rows["backup"]["offsite"]["enabled"]
    assert ops_status.backup_state(rows["backup"])[0] == "ok"
    assert rows["restore_drill"]["ok"] and rows["restore_drill"]["source"] == "local"
    assert rows["restore_drill"]["backup"].startswith("postgres-")
    assert ops_status.drill_state(rows["restore_drill"])[0] == "ok"
    status_file = json.loads((drilled["out"] / "backups" / "backup-status.json").read_text())
    assert status_file["stamp"] == rows["backup"]["stamp"]


def test_source_had_compressed_and_uncompressed_chunks(drilled):
    assert drilled["compressed"] >= 2
    src, dst = _both(drilled, "SELECT range_start, range_end, is_compressed FROM timescaledb_information.chunks "
                              "WHERE hypertable_name = 'point_history' ORDER BY range_start")
    assert any(c for _, _, c in src) and not all(c for _, _, c in src)
    assert dst == src


@pytest.mark.parametrize("window", [OLD, RECENT], ids=["compressed", "uncompressed"])
def test_point_history_values(drilled, window):
    a, b = window
    q = """SELECT count(*), count(DISTINCT point_uri), sum(value), count(value_text), count(*) FILTER (WHERE value_bool),
                  count(quality), md5(string_agg(h::text, '|' ORDER BY point_uri, ts))
           FROM point_history h WHERE ts >= %s AND ts < %s"""
    src, dst = _both(drilled, q, (a, b + timedelta(minutes=1)))
    assert src[0][0] > 10_000 and src[0][1] == len(POINTS)
    assert dst == src
    sample = "SELECT * FROM point_history WHERE ts >= %s AND ts < %s ORDER BY point_uri, ts LIMIT 25 OFFSET 400"
    src, dst = _both(drilled, sample, (a, b))
    assert len(src) == 25 and dst == src


def test_late_write_into_compressed_chunk_survived(drilled):
    src, dst = _both(drilled, "SELECT point_uri, ts, value FROM point_history WHERE (point_uri, ts) IN ((%s, %s), (%s, %s)) "
                              "ORDER BY 1", (POINTS[0], OLD[0] + timedelta(minutes=2, seconds=30), POINTS[1],
                                             OLD[0] + timedelta(minutes=5)))
    assert [r[2] for r in src] == [12.5, 99.0] and dst == src


@pytest.mark.parametrize("view", ["point_history_5m", "point_history_1h"])
def test_continuous_aggregates_after_refresh(drilled, view):
    for side in ("src", "dst"):
        with psycopg.connect(drilled[side].dsn, autocommit=True) as conn:
            conn.execute("CALL refresh_continuous_aggregate('point_history_5m', NULL, NULL)")
            conn.execute("CALL refresh_continuous_aggregate('point_history_1h', NULL, NULL)")
    q = f"""SELECT count(*), count(DISTINCT point_uri), sum(vcount),
                   md5(string_agg(concat_ws(',', point_uri, bucket, round(vsum::numeric, 6), vcount, vmin, vmax), '|'
                                  ORDER BY point_uri, bucket))
            FROM {view}"""
    src, dst = _both(drilled, q)
    assert src[0][0] > 0 and dst == src
    # Every raw sample with a value is in the aggregate (value or boolean).
    raw = _both(drilled, "SELECT count(COALESCE(value, value_bool::int::float8)) FROM point_history")[0]
    assert src[0][2] == raw[0][0]


def test_every_other_table_identical(drilled):
    tables = [r[0] for r in _both(drilled, "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                                           "AND tablename <> 'point_history' ORDER BY 1")[0]]
    assert {"orgs", "sites", "users", "api_keys", "faults", "cx_projects", "cx_runs", "cx_results",
            "baler_schema_migrations"} <= set(tables)
    for t in tables:
        # The source got the backup's and the drill's own reports after the dump.
        where = " WHERE service NOT IN ('backup', 'restore_drill')" if t == "service_heartbeats" else ""
        src, dst = _both(drilled, f'SELECT count(*), md5(string_agg(x::text, \'|\' ORDER BY x::text)) FROM "{t}" x{where}')
        assert dst == src, t
    counts = _both(drilled, "SELECT (SELECT count(*) FROM orgs), (SELECT count(*) FROM users), (SELECT count(*) FROM sites)")
    assert counts[0] == [(1, 2, 1)]


def test_timescaledb_version_and_policies(drilled):
    src, dst = _both(drilled, "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
    assert dst == src
    q = ("SELECT proc_name, hypertable_name, schedule_interval, config::text, scheduled FROM timescaledb_information.jobs "
         "WHERE job_id >= 1000 ORDER BY 1, 2")
    src, dst = _both(drilled, q)
    assert len(src) == 3 and dst == src


def test_graph_identical(drilled):
    graphs = []
    for side in ("src", "dst"):
        resp = requests.get(f"{drilled[side].oxigraph_url}/store", params={"default": ""}, headers={"Accept": "text/turtle"},
                            timeout=30)
        resp.raise_for_status()
        graphs.append(set(Graph().parse(data=resp.text, format="turtle")))
    assert len(graphs[0]) == 1 + 2 * len(POINTS) + 1 and graphs[1] == graphs[0]


def test_cx_report_identical(drilled):
    src = rd.render_cx_report(drilled["src"].dsn, drilled["src"].oxigraph_url)
    dst = rd.render_cx_report(drilled["dst"].dsn, drilled["dst"].oxigraph_url)
    assert src is not None and src[2] == 2 and "flat for 6 h" in src[1]
    assert dst == src

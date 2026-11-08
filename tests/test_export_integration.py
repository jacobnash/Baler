"""Building export round trip (#193): seed one site's history, graph, Cx
records and faults beside another site's, export that site, verify the
export against the source, import it into a fresh database (and the graph
back into Oxigraph after deleting it), and check the fresh install gives
the same answers: every raw sample, the hourly rollups, the graph, the
issues log and the faults. The other site's data must not leak into it.

Needs docker-compose's Postgres (TimescaleDB) and Oxigraph. The fresh
install is a scratch database on the same server, dropped afterwards."""

from __future__ import annotations

import json
import math
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
import requests
from rdflib import Graph
from rdflib.compare import isomorphic

pytest.importorskip("pyarrow")

from baler import auth, export, faults, scope, timeseries
from baler.commissioning import db as cx_db

pytestmark = pytest.mark.integration

OXIGRAPH = os.environ.get("OXIGRAPH_URL", "http://localhost:7878")
BRICK = "https://brickschema.org/schema/Brick#"
T0 = datetime(2026, 1, 31, 12, tzinfo=timezone.utc)


def _scratch_dsn(dsn: str, db: str) -> str:
    u = urlsplit(dsn)
    return urlunsplit((u.scheme, u.netloc, "/" + db, u.query, u.fragment))


def _construct(prefix: str) -> Graph:
    q = (f'CONSTRUCT {{ ?s ?p ?o }} WHERE {{ ?s ?p ?o . FILTER(STRSTARTS(STR(?s), "urn:equip:{prefix}") '
         f'|| STRSTARTS(STR(?s), "urn:point:{prefix}")) }}')
    r = requests.post(f"{OXIGRAPH}/query", data=q.encode(), timeout=30,
                      headers={"Content-Type": "application/sparql-query", "Accept": "text/turtle"})
    r.raise_for_status()
    return Graph().parse(data=r.text, format="turtle")


def _same(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return a == b


@pytest.fixture
def seeded():
    tag = secrets.token_hex(4)
    mine, other = f"fbf/exp-{tag}-a", f"fbf/exp-{tag}-b"
    points = {
        "num": f"urn:point:{mine}/ahu-1/sat",
        "text": f"urn:point:{mine}/ahu-1/fan",
        "bool": f"urn:point:{mine}/ahu-1/occ",
        "other": f"urn:point:{other}/ahu-9/sat",
    }
    conn = timeseries.connect()
    auth.ensure_schema(conn)
    scope.ensure_schema(conn)
    cx_db.ensure_schema(conn)
    faults.ensure_schema(conn)
    # three days of 1-minute samples across a month boundary, a NaN and quality flags
    conn.execute("""
        INSERT INTO point_history (point_uri, ts, value, quality)
        SELECT %s, g, CASE WHEN g = %s THEN 'NaN'::float8 ELSE extract(epoch FROM g)::float8 / 7 END,
               CASE WHEN extract(minute FROM g) = 13 THEN 'spike' END
        FROM generate_series(%s::timestamptz, %s::timestamptz, interval '1 minute') g
    """, (points["num"], T0 + timedelta(minutes=5), T0, T0 + timedelta(days=3)))
    timeseries.write_point_values(conn, [
        (points["text"], "active", T0),
        (points["text"], 'a,"b"\nc', T0 + timedelta(hours=1)),
        (points["text"], "", T0 + timedelta(hours=2)),
        (points["bool"], True, T0, "", "Occupied", "overridden"),
        (points["other"], 99.0, T0),
    ])

    turtle = f"""
        <urn:equip:{mine}/ahu-1> a <{BRICK}AHU> ; <{BRICK}hasPoint> <{points['num']}>, <{points['text']}> .
        <{points['num']}> a <{BRICK}Supply_Air_Temperature_Sensor> ; <urn:baler:dis> "SAT" .
        <urn:equip:{other}/ahu-9> a <{BRICK}AHU> ; <{BRICK}hasPoint> <{points['other']}> .
    """
    requests.post(f"{OXIGRAPH}/store", params={"default": ""}, data=turtle.encode(),
                  headers={"Content-Type": "text/turtle"}, timeout=30).raise_for_status()

    org = auth.create_org(conn, f"export-{tag}", f"admin-{tag}@export.test", "export-password-123")
    org_id = org["id"]
    site_a = auth.create_site(conn, org_id, "Building A")["id"]
    site_b = auth.create_site(conn, org_id, "Building B")["id"]
    now = datetime.now(timezone.utc)
    pa_, pb = f"cxp-{tag}-a", f"cxp-{tag}-b"
    for pid, site in ((pa_, site_a), (pb, site_b)):
        conn.execute("INSERT INTO cx_projects (id, org_id, site_id, name, settings, created_at, updated_at) "
                     "VALUES (%s, %s, %s, %s, %s, %s, %s)", (pid, org_id, site, f"project {pid}",
                                                             json.dumps({"k": [1, 2]}), now, now))
        conn.execute("INSERT INTO cx_runs (id, project_id, status, started_at, finished_at, window_start, window_end, "
                     "scope, summary) VALUES (%s, %s, 'completed', %s, %s, %s, %s, '{}', '{\"pass\": 1}')",
                     (f"{pid}-run", pid, now, now, T0, T0 + timedelta(days=1)))
        conn.execute("INSERT INTO cx_results (run_id, equip_uri, check_id, subject, status, evidence) "
                     "VALUES (%s, %s, 'sat_range', 'sat', 'fail', '{\"n\": 3}')",
                     (f"{pid}-run", f"urn:equip:{mine}/ahu-1"))
        conn.execute("INSERT INTO cx_issues (id, project_id, equip_uri, status, title, severity, detail, created_at, "
                     "updated_at) VALUES (%s, %s, %s, 'risk_accepted', 'SAT, \"high\"', 'major', '{}', %s, %s)",
                     (f"{pid}-iss", pid, f"urn:equip:{mine}/ahu-1", now, now))
        conn.execute("INSERT INTO cx_issue_events (issue_id, ts, action, detail) VALUES (%s, %s, 'opened', '{}')",
                     (f"{pid}-iss", now))
        conn.execute("INSERT INTO cx_risks (id, issue_id, reason, review_by, created_at) "
                     "VALUES (%s, %s, 'owner accepts', '2027-01-01', %s)", (f"{pid}-risk", f"{pid}-iss", now))
        conn.execute("INSERT INTO cx_spec_documents (id, project_id, filename, media_type, size, sha256, method, "
                     "status, created_at) VALUES (%s, %s, 'M-601.pdf', 'application/pdf', 1234, %s, 'llm', "
                     "'completed', %s)", (f"{pid}-doc", pid, "ab" * 32, now))
    for uri in (points["num"], points["other"]):
        conn.execute("INSERT INTO faults (rule_id, point_uri, status, started_at, detail) "
                     "VALUES ('sat_high', %s, 'open', %s, '{\"x\": 1}')", (uri, T0))
    yield {"conn": conn, "mine": mine, "other": other, "points": points, "org_id": org_id,
           "site_a": site_a, "projects": (pa_, pb)}

    for table, col in (("cx_risks", "issue_id"), ("cx_issue_events", "issue_id")):
        conn.execute(f"DELETE FROM {table} WHERE {col} = ANY(%s)", ([f"{p}-iss" for p in (pa_, pb)],))
    conn.execute("DELETE FROM cx_results WHERE run_id = ANY(%s)", ([f"{p}-run" for p in (pa_, pb)],))
    for table in ("cx_issues", "cx_runs", "cx_spec_documents"):
        conn.execute(f"DELETE FROM {table} WHERE project_id = ANY(%s)", ([pa_, pb],))
    conn.execute("DELETE FROM cx_projects WHERE id = ANY(%s)", ([pa_, pb],))
    conn.execute("DELETE FROM faults WHERE point_uri = ANY(%s)", (list(points.values()),))
    conn.execute("DELETE FROM point_history WHERE point_uri = ANY(%s)", (list(points.values()),))
    conn.execute("DELETE FROM sites WHERE id = ANY(%s)", ([site_a, site_b],))
    for prefix in (mine, other):
        requests.post(f"{OXIGRAPH}/update", timeout=30, headers={"Content-Type": "application/sparql-update"},
                      data=(f'DELETE {{ ?s ?p ?o }} WHERE {{ ?s ?p ?o . FILTER(STRSTARTS(STR(?s), "urn:equip:{prefix}") '
                            f'|| STRSTARTS(STR(?s), "urn:point:{prefix}")) }}').encode())
    conn.close()


def test_site_export_round_trips_into_a_fresh_install(seeded, tmp_path):
    s, conn, pts = seeded, seeded["conn"], seeded["points"]
    out = str(tmp_path / "building-a")
    graph_before = _construct(s["mine"])
    # the export reads compressed history as well as uncompressed
    # Compress every chunk overlapping the window, whatever its width.
    # show_chunks(older_than=...) only takes chunks that END before the bound:
    # with the default 7-day chunks (epoch-aligned, so 29 Jan - 5 Feb here)
    # the chunk holding T0 ends after T0 + 4 days and was silently skipped,
    # which failed CI while passing on databases with narrower chunks.
    conn.execute("SELECT compress_chunk(format('%%I.%%I', chunk_schema, chunk_name)::regclass, "
                 "if_not_compressed => true) FROM timescaledb_information.chunks "
                 "WHERE hypertable_name = 'point_history' AND range_start < %s AND range_end > %s",
                 (T0 + timedelta(days=4), T0 - timedelta(days=7)))
    assert conn.execute("SELECT bool_and(is_compressed) FROM timescaledb_information.chunks WHERE "
                        "hypertable_name = 'point_history' AND range_start <= %s AND range_end > %s",
                        (T0, T0)).fetchone()[0]
    assert len(graph_before) == 5

    # Scoping is global once any site owns a prefix: own it only for the export.
    auth.set_site_prefixes(conn, s["org_id"], s["site_a"], [s["mine"]])
    try:
        manifest = export.export_building(timeseries.DEFAULT_DSN, OXIGRAPH, out, site_id=s["site_a"],
                                          until=T0 + timedelta(days=10))
    finally:
        auth.set_site_prefixes(conn, s["org_id"], s["site_a"], [])

    # what's in it: only site A's points, projects and faults
    assert manifest["scope"]["site"]["topic_prefixes"] == [s["mine"]]
    assert manifest["history"]["points"] == 3
    assert manifest["history"]["rows"] == 3 * 24 * 60 + 1 + 3 + 1
    assert manifest["records"]["cx_projects"] == 1 and manifest["records"]["faults"] == 1
    assert manifest["records"]["cx_results"] == 1 and manifest["records"]["cx_risks"] == 1
    assert manifest["documents"] == 1
    text = Path(out, "graph", "building.ttl").read_text()
    assert s["other"] not in text
    assert not any(s["other"] in line for line in Path(out, "history", "points.csv").read_text().splitlines())
    assert all(s["projects"][1] not in Path(out, "records", f).read_text()
               for f in os.listdir(os.path.join(out, "records")))
    months = {r.split(",")[2] for r in Path(out, "history", "files.csv").read_text().splitlines()[1:]}
    assert months == {"2026-01", "2026-02"}
    issues_csv = Path(out, "records", "cx_issues.csv").read_text()
    assert 'SAT, ""high""' in issues_csv

    res = export.verify(out, timeseries.DEFAULT_DSN)
    assert res.ok, res.errors

    # a fresh install: a new database, and the site's graph removed from Oxigraph
    db = f"export_rt_{secrets.token_hex(4)}"
    with psycopg.connect(timeseries.DEFAULT_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{db}"')
    target = _scratch_dsn(timeseries.DEFAULT_DSN, db)
    try:
        requests.post(f"{OXIGRAPH}/update", timeout=30, headers={"Content-Type": "application/sparql-update"},
                      data=(f'DELETE {{ ?s ?p ?o }} WHERE {{ ?s ?p ?o . FILTER(STRSTARTS(STR(?s), "urn:equip:{s["mine"]}") '
                            f'|| STRSTARTS(STR(?s), "urn:point:{s["mine"]}")) }}').encode()).raise_for_status()
        assert len(_construct(s["mine"])) == 0
        with psycopg.connect(target, autocommit=True) as t:
            t.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
            auth.ensure_schema(t)
            new_org = auth.create_org(t, "new owner", "owner@export.test", "export-password-123")
            new_org_id = new_org["id"]

        result = export.import_building(out, target, OXIGRAPH, org_id=new_org_id)
        assert result["history_rows"] == manifest["history"]["rows"]
        assert result["records"]["cx_issues"] == 1

        # same answers
        assert isomorphic(_construct(s["mine"]), graph_before)
        with psycopg.connect(target, autocommit=True) as t:
            timeseries._setup(t)
            start, end = T0 - timedelta(days=1), T0 + timedelta(days=5)
            for key in ("num", "text", "bool"):
                a = timeseries.read_range(conn, pts[key], start, end, with_quality=True)
                b = timeseries.read_range(t, pts[key], start, end, with_quality=True)
                assert len(a) == len(b) and a
                assert all(x[0] == y[0] and _same(x[1], y[1]) and x[2] == y[2] for x, y in zip(a, b, strict=True))
            assert timeseries.read_point_labels(t, [pts["bool"]]) == timeseries.read_point_labels(conn, [pts["bool"]])
            assert timeseries.read_range(t, pts["other"], start, end) == []
            ra = timeseries.read_rollup(conn, [pts["num"]], start, end, "max", 3600, None, "UTC")
            rb = timeseries.read_rollup(t, [pts["num"]], start, end, "max", 3600, None, "UTC")
            assert len(ra[pts["num"]]) == 73
            assert all(x[0] == y[0] and _same(x[1], y[1]) for x, y in zip(ra[pts["num"]], rb[pts["num"]], strict=True))

            def rows(c, q, *p):
                return c.execute(q, p).fetchall()

            pid = s["projects"][0]
            q_issue = ("SELECT id, status, title, severity, equip_uri, created_at FROM cx_issues "
                       "WHERE project_id = %s")
            assert rows(t, q_issue, pid) == rows(conn, q_issue, pid)
            assert rows(t, "SELECT org_id, site_id, settings FROM cx_projects WHERE id = %s", pid) == [
                (new_org_id, None, {"k": [1, 2]})]
            q_res = "SELECT equip_uri, check_id, status, evidence FROM cx_results WHERE run_id = %s"
            assert rows(t, q_res, f"{pid}-run") == rows(conn, q_res, f"{pid}-run")
            q_f = "SELECT rule_id, point_uri, status, started_at, detail, severity FROM faults WHERE point_uri = %s"
            assert rows(t, q_f, pts["num"]) == rows(conn, q_f, pts["num"])
            assert rows(t, "SELECT count(*) FROM faults") == [(1,)]
            # the serial sequence moved past the imported ids
            t.execute("INSERT INTO cx_issue_events (issue_id, ts, action, detail) "
                      "VALUES (%s, now(), 'closed', '{}')", (f"{pid}-iss",))

        # importing again adds nothing
        again = export.import_building(out, target, OXIGRAPH, org_id=new_org_id, graph=False)
        assert again["history_rows"] == 0 and sum(again["records"].values()) == 0
    finally:
        with psycopg.connect(timeseries.DEFAULT_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')


def test_import_refuses_a_damaged_export(seeded, tmp_path):
    s = seeded
    out = str(tmp_path / "all")
    export.export_building(timeseries.DEFAULT_DSN, OXIGRAPH, out, since=T0, until=T0 + timedelta(hours=1))
    path = os.path.join(out, "records", "faults.jsonl")
    with open(path, "a") as f:
        f.write('{"id": 1}\n')
    with pytest.raises(export.ExportError, match="sha256 mismatch: records/faults.jsonl"):
        export.import_building(out, timeseries.DEFAULT_DSN, OXIGRAPH, org_id=s["org_id"])

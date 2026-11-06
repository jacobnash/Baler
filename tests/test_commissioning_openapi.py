"""commissioning_api (#37): the spec is valid OpenAPI, live responses
match it, and tenancy holds - org and site checked against the caller's
own JWT, graph reads scoped, results filtered by the reader's scope.
Runs the real handler on MemoryRepo: no services."""

from __future__ import annotations

import threading
import time
from http.server import ThreadingHTTPServer
from threading import Thread

import cx_fixtures as fx
import jwt as pyjwt
import pytest
import requests
import yaml
from conftest import assert_matches_schema, load_spec
from openapi_spec_validator import validate

from baler.commissioning import db, engine, runner
from baler.commissioning_api import OPENAPI_SPEC_PATH, make_handler
from baler.scope import Scope

GATEWAY = "test-gateway-secret"
JWT = "j" * 32
SITES = {
    "site-a": {"id": "site-a", "org_id": "org-a", "name": "A", "topic_prefixes": ["fbf/a"]},
    "site-a2": {"id": "site-a2", "org_id": "org-a", "name": "A2", "topic_prefixes": ["fbf/a2"]},
    "site-b": {"id": "site-b", "org_id": "org-b", "name": "B", "topic_prefixes": ["fbf/b"]},
}


def _scope(claims):
    """scope.scope_for's rules over SITES, without Postgres."""
    if not claims:
        return Scope.nothing()
    mine = [s for s in SITES.values() if s["org_id"] == claims["org_id"]]
    if claims.get("site_ids"):
        mine = [s for s in mine if s["id"] in claims["site_ids"]]
    return Scope(tuple(sorted(p for s in mine for p in s["topic_prefixes"])))


@pytest.fixture(scope="module")
def spec() -> dict:
    return load_spec(OPENAPI_SPEC_PATH)


def test_spec_is_well_formed_openapi(spec):
    validate(spec)


@pytest.fixture
def repo():
    return db.MemoryRepo(store=fx.store(), history=fx.history(), sites=SITES, scope_fn=_scope, users={"u-a": "a@example.com"})


def _serve(monkeypatch, repo, worker=None):
    monkeypatch.setenv("BALER_GATEWAY_SECRET", GATEWAY)
    monkeypatch.setenv("BALER_JWT_SECRET", JWT)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(repo, clock=lambda: fx.NOW, worker=worker))
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def base(monkeypatch, repo):
    server = _serve(monkeypatch, repo)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _wait(base, run_id, headers=None, timeout=10.0):
    """Poll GET /runs/{id} the way a client does until the run is done."""
    deadline = time.monotonic() + timeout
    while True:
        got = requests.get(f"{base}/runs/{run_id}", headers=headers or A)
        assert got.status_code == 200, got.text
        if got.json()["run"]["status"] in ("completed", "failed"):
            return got
        assert time.monotonic() < deadline, got.json()["run"]
        time.sleep(0.02)


def _h(org="org-a", sites=None, sub="u-a"):
    tok = pyjwt.encode({"sub": sub, "org_id": org, "site_ids": sites or [], "role": "operator", "kind": "session"}, JWT, algorithm="HS256")
    return {"X-Gateway-Secret": GATEWAY, "Authorization": f"Bearer {tok}"}


A = _h()
B = _h(org="org-b", sub="u-b")
A2_ONLY = _h(sites=["site-a2"], sub="u-a2")


def _schema(spec, path, method, status):
    """A $ref into the spec (not the sub-schema itself) so nested $refs
    resolve against the whole document."""
    assert spec["paths"][path][method]["responses"][str(status)]["content"]["application/json"]["schema"]
    esc = path.replace("~", "~0").replace("/", "~1")
    return {"$ref": f"#/paths/{esc}/{method}/responses/{status}/content/application~1json/schema"}


def _project(base, headers=A, **body):
    body = {"name": "Building A", "site_id": "site-a", "phase": "startup"} | body
    r = requests.post(f"{base}/projects", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def test_openapi_and_docs_are_public(base):
    r = requests.get(f"{base}/openapi.yaml")
    assert r.status_code == 200 and yaml.safe_load(r.text)["info"]["title"] == "Baler Commissioning API"


def test_requires_gateway_and_token(base):
    assert requests.get(f"{base}/projects").status_code == 401
    assert requests.get(f"{base}/projects", headers={"X-Gateway-Secret": GATEWAY}).status_code == 401
    assert requests.get(f"{base}/nope", headers=A).status_code == 404


def test_create_and_read_project(base, spec):
    p = _project(base)
    assert_matches_schema(p, _schema(spec, "/projects", "post", 201), spec)
    # #57: `phase` is what the request sets, so it's what comes back
    assert p["phase"] == p["current_phase"] == "startup" and p["org_id"] == "org-a"
    assert [x["key"] for x in p["phases"]][:3] == ["design", "construction", "startup"]
    got = requests.get(f"{base}/projects/{p['id']}", headers=A).json()
    assert got == p
    listed = requests.get(f"{base}/projects", headers=A).json()
    assert_matches_schema(listed, _schema(spec, "/projects", "get", 200), spec)
    assert [x["id"] for x in listed] == [p["id"]]


@pytest.mark.parametrize("body", [
    {"phase": "startup"},  # no name
    {"name": "x"},  # no phase - always stated
    {"name": "x", "phase": "nope"},
    {"name": "x", "phase": "startup", "site_id": "site-b"},  # another org's site
    {"name": "x", "phase": "startup", "site_id": "missing"},
    {"name": "x", "phase": "startup", "settings": {"window_hours": 0}},
])
def test_create_rejects(base, body):
    assert requests.post(f"{base}/projects", json=body, headers=A).status_code == 400


def test_other_org_cannot_see_or_touch(base):
    p = _project(base)
    assert requests.get(f"{base}/projects", headers=B).json() == []
    for method, path in (("get", ""), ("patch", ""), ("delete", ""), ("get", "/events"), ("get", "/inventory"),
                         ("post", "/runs"), ("get", "/runs"), ("post", "/phase"), ("put", "/phases")):
        r = getattr(requests, method)(f"{base}/projects/{p['id']}{path}", json={}, headers=B)
        assert r.status_code == 404, (method, path)


def test_site_restricted_login(base):
    p = _project(base)  # on site-a
    assert requests.get(f"{base}/projects", headers=A2_ONLY).json() == []
    assert requests.get(f"{base}/projects/{p['id']}", headers=A2_ONLY).status_code == 404
    # must name one of its own sites
    assert requests.post(f"{base}/projects", json={"name": "x", "phase": "startup"}, headers=A2_ONLY).status_code == 400
    assert requests.post(f"{base}/projects", json={"name": "x", "phase": "startup", "site_id": "site-a"}, headers=A2_ONLY).status_code == 400
    mine = _project(base, headers=A2_ONLY, site_id="site-a2")
    assert [x["id"] for x in requests.get(f"{base}/projects", headers=A2_ONLY).json()] == [mine["id"]]


def test_phases_are_manageable_and_audited(base, spec):
    p = _project(base)
    r = requests.put(f"{base}/projects/{p['id']}/phases", headers=A, json={"phases": [
        {"key": "construction"}, {"key": "startup", "name": "Power-on / start-up"}, {"key": "functional"}, {"key": "warranty"}]})
    assert r.status_code == 200, r.text
    assert_matches_schema(r.json(), _schema(spec, "/projects/{id}/phases", "put", 200), spec)
    assert [x["key"] for x in r.json()["phases"]] == ["construction", "startup", "functional", "warranty"]
    assert r.json()["phases"][1]["name"] == "Power-on / start-up"
    # dropping the active phase is refused
    assert requests.put(f"{base}/projects/{p['id']}/phases", headers=A, json={"phases": [{"key": "warranty"}]}).status_code == 400
    r = requests.post(f"{base}/projects/{p['id']}/phase", headers=A, json={"key": "functional", "note": "AHU-1 started"})
    assert r.status_code == 200 and r.json()["current_phase"] == "functional"
    assert {x["key"]: x["status"] for x in r.json()["phases"]}["startup"] == "complete"
    assert requests.post(f"{base}/projects/{p['id']}/phase", headers=A, json={"key": "nope"}).status_code == 400
    events = requests.get(f"{base}/projects/{p['id']}/events", headers=A).json()
    assert_matches_schema(events, _schema(spec, "/projects/{id}/events", "get", 200), spec)
    assert [e["action"] for e in events] == ["phase.set", "phases.update", "project.create"]
    assert events[0]["detail"] == {"from": "startup", "to": "functional", "note": "AHU-1 started"}
    assert events[0]["user_sub"] == "u-a" and events[0]["user_email"] == "a@example.com"


def test_inventory_is_scoped(base, spec):
    p = _project(base)
    r = requests.get(f"{base}/projects/{p['id']}/inventory", headers=A)
    assert r.status_code == 200
    assert_matches_schema(r.json(), _schema(spec, "/projects/{id}/inventory", "get", 200), spec)
    ids = {e["id"]: e for e in r.json()["equipment"]}
    assert set(ids) == {fx.AHU, fx.VAV, fx.MTR}
    assert ids[fx.AHU]["templates"] == ["AHU_ExpectedPoints"] and ids[fx.MTR]["templates"] == []


def test_org_wide_project_uses_the_callers_scope(base):
    p = _project(base, site_id=None)
    ids = {e["id"] for e in requests.get(f"{base}/projects/{p['id']}/inventory", headers=A).json()["equipment"]}
    assert ids == {fx.AHU, fx.VAV, fx.MTR}  # org-a's prefixes, never org-b's


def test_run_round_trip(base, spec):
    p = _project(base, settings={"window_hours": 48})
    r = requests.post(f"{base}/projects/{p['id']}/runs", json={}, headers=A)
    assert r.status_code == 202, r.text
    run = r.json()
    assert_matches_schema(run, _schema(spec, "/projects/{id}/runs", "post", 202), spec)
    assert run["status"] == "queued" and run["summary"] is None and run["finished_at"] is None
    assert run["phase"] == "startup"
    assert run["scope"] == {"caller": {"prefixes": ["fbf/a", "fbf/a2"], "exclude": []}, "site_prefixes": ["fbf/a"]}
    assert run["window_start"] == "2026-11-04T12:00:00+00:00"

    got = _wait(base, run["id"])
    assert_matches_schema(got.json(), _schema(spec, "/runs/{id}", "get", 200), spec)
    done = got.json()["run"]
    assert done["status"] == "completed" and done["error"] is None and done["progress"] is None
    assert done["phase"] == "startup" and done["summary"]["phase"] == "startup" and done["summary"]["window_hours"] == 48
    assert done["finished_at"] == "2026-11-06T12:00:00+00:00"
    results = got.json()["results"]
    assert {r["status"] for r in results} == {"pass", "fail", "inconclusive"}
    # 48h window: RAT (3 days old) still stale
    assert next(r for r in results if r["subject"] == fx.pt(fx.AHU, "RAT"))["status"] == "fail"

    only_fail = requests.get(f"{base}/runs/{run['id']}", params={"status": "fail", "check_id": "template:"}, headers=A).json()["results"]
    assert only_fail and all(r["status"] == "fail" and r["check_id"].startswith("template:") for r in only_fail)

    listed = requests.get(f"{base}/projects/{p['id']}/runs", headers=A).json()
    assert [(x["id"], x["status"]) for x in listed] == [(run["id"], "completed")]
    assert requests.get(f"{base}/runs/{run['id']}", headers=B).status_code == 404
    assert requests.get(f"{base}/runs/missing", headers=A).status_code == 404
    events = requests.get(f"{base}/projects/{p['id']}/events", headers=A).json()
    assert [e["action"] for e in events[:2]] == ["run.complete", "run.create"]
    assert events[0]["detail"]["by_status"] == done["summary"]["by_status"] and events[0]["user_email"] == "a@example.com"


class _Gate:
    """engine.run that waits until released, then runs (or raises)."""

    def __init__(self, error: Exception | None = None):
        self.started = threading.Event()
        self.release = threading.Event()
        self.error = error

    def __call__(self, *args, **kwargs):
        self.started.set()
        assert self.release.wait(10)
        if self.error:
            raise self.error
        return engine.run(*args, **kwargs)


def test_one_run_per_project_and_running_status(monkeypatch, repo, spec):
    gate = _Gate()
    server = _serve(monkeypatch, repo, runner.RunWorker(repo, lambda: fx.NOW, run_fn=gate))
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        p = _project(base)
        first = requests.post(f"{base}/projects/{p['id']}/runs", json={}, headers=A).json()
        assert gate.started.wait(5)
        running = requests.get(f"{base}/runs/{first['id']}", headers=A).json()
        assert_matches_schema(running, _schema(spec, "/runs/{id}", "get", 200), spec)
        assert running["run"]["status"] == "running" and running["results"] == []
        assert running["run"]["progress"]["stage"] == "starting"
        again = requests.post(f"{base}/projects/{p['id']}/runs", json={}, headers=A)
        assert again.status_code == 409 and again.json()["run_id"] == first["id"]
        assert_matches_schema(again.json(), _schema(spec, "/projects/{id}/runs", "post", 409), spec)
        other = _project(base, name="Other")  # another project isn't blocked
        assert requests.post(f"{base}/projects/{other['id']}/runs", json={}, headers=A).status_code == 202
        gate.release.set()
        assert _wait(base, first["id"]).json()["run"]["status"] == "completed"
        # done, so the project can run again
        assert requests.post(f"{base}/projects/{p['id']}/runs", json={}, headers=A).status_code == 202
    finally:
        gate.release.set()
        server.shutdown()


def test_failed_run_records_the_error(monkeypatch, repo, spec):
    gate = _Gate(error=RuntimeError("oxigraph unreachable"))
    gate.release.set()
    server = _serve(monkeypatch, repo, runner.RunWorker(repo, lambda: fx.NOW, run_fn=gate))
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        p = _project(base)
        run = requests.post(f"{base}/projects/{p['id']}/runs", json={}, headers=A).json()
        got = _wait(base, run["id"])
        assert_matches_schema(got.json(), _schema(spec, "/runs/{id}", "get", 200), spec)
        failed = got.json()["run"]
        assert failed["status"] == "failed" and failed["error"] == "RuntimeError: oxigraph unreachable"
        assert failed["finished_at"] is not None and failed["summary"] is None and got.json()["results"] == []
        ev = requests.get(f"{base}/projects/{p['id']}/events", headers=A).json()[0]
        assert ev["action"] == "run.fail" and ev["detail"]["run_id"] == run["id"]
        # a failed run doesn't block the next
        assert requests.post(f"{base}/projects/{p['id']}/runs", json={}, headers=A).status_code == 202
    finally:
        server.shutdown()


def test_run_rejects_bad_window(base):
    p = _project(base)
    assert requests.post(f"{base}/projects/{p['id']}/runs", json={"window_hours": -5}, headers=A).status_code == 400


def test_patch_and_archive(base, spec):
    p = _project(base)
    r = requests.patch(f"{base}/projects/{p['id']}", json={"name": "Renamed", "settings": {"window_hours": 12}}, headers=A)
    assert r.status_code == 200 and r.json()["name"] == "Renamed" and r.json()["settings"] == {"window_hours": 12}
    assert requests.patch(f"{base}/projects/{p['id']}", json={"name": ""}, headers=A).status_code == 400
    assert requests.delete(f"{base}/projects/{p['id']}", headers=A).status_code == 204
    assert requests.get(f"{base}/projects", headers=A).json() == []
    assert [x["id"] for x in requests.get(f"{base}/projects", params={"include_archived": "true"}, headers=A).json()] == [p["id"]]
    assert requests.post(f"{base}/projects/{p['id']}/runs", json={}, headers=A).status_code == 409


def test_templates(base, spec):
    r = requests.get(f"{base}/templates", headers=A)
    assert r.status_code == 200
    assert_matches_schema(r.json(), _schema(spec, "/templates", "get", 200), spec)
    assert len(r.json()) == 6


# ------------------------------------------------------------ Cx 5 (#41)


def _completed_run(base, headers=A, **project):
    p = _project(base, headers=headers, **project)
    run = requests.post(f"{base}/projects/{p['id']}/runs", json={}, headers=headers).json()
    _wait(base, run["id"], headers)
    return p, run


def test_report_json_and_html(base, spec):
    _, run = _completed_run(base)
    r = requests.get(f"{base}/runs/{run['id']}/report", headers=A)
    assert r.status_code == 200
    assert_matches_schema(r.json(), _schema(spec, "/runs/{id}/report", "get", 200), spec)
    eq = r.json()["equipment"]
    assert {e["equip_uri"] for e in eq} == {fx.AHU, fx.VAV, fx.MTR}
    order = {"fail": 0, "inconclusive": 1, "pass": 2, "not_applicable": 3}
    assert [order[e["status"]] for e in eq] == sorted(order[e["status"]] for e in eq)  # failures first
    h = requests.get(f"{base}/runs/{run['id']}/report", params={"format": "html"}, headers=A)
    assert h.status_code == 200 and h.headers["Content-Type"].startswith("text/html")
    assert "default-src 'none'" in h.headers["Content-Security-Policy"]
    assert "<script" not in h.text and "Building A" in h.text and fx.AHU in h.text
    assert requests.get(f"{base}/runs/{run['id']}/report", params={"format": "pdf"}, headers=A).status_code == 400
    assert requests.get(f"{base}/runs/{run['id']}/report", headers=B).status_code == 404


def test_report_html_escapes(base):
    _, run = _completed_run(base, name="<script>alert(1)</script>")
    h = requests.get(f"{base}/runs/{run['id']}/report", params={"format": "html"}, headers=A).text
    assert "<script>alert" not in h and "&lt;script&gt;" in h


def test_equipment_status(base, spec):
    assert requests.get(f"{base}/equipment-status", headers=A).json() == {}
    _, run = _completed_run(base)
    r = requests.get(f"{base}/equipment-status", headers=A)
    assert_matches_schema(r.json(), _schema(spec, "/equipment-status", "get", 200), spec)
    got = r.json()
    assert set(got) == {fx.AHU, fx.VAV, fx.MTR}
    assert got[fx.AHU]["run_id"] == run["id"] and got[fx.AHU]["project"] == "Building A"
    assert got[fx.AHU]["status"] == "fail"  # RAT is stale
    assert requests.get(f"{base}/equipment-status", headers=B).json() == {}
    assert requests.get(f"{base}/equipment-status", headers=A2_ONLY).json() == {}


def test_gaps(base, spec):
    p = _project(base)
    r = requests.get(f"{base}/projects/{p['id']}/gaps", headers=A)
    assert r.status_code == 200
    assert_matches_schema(r.json(), _schema(spec, "/projects/{id}/gaps", "get", 200), spec)
    rows = {(g["equip_uri"], g["template"]): g for g in r.json()["equipment"]}
    ahu = rows[(fx.AHU, "AHU_ExpectedPoints")]
    assert ahu["matched"] < ahu["expected"] and ahu["missing"]
    assert [w["id"] for w in r.json()["without_template"]] == [fx.MTR]
    assert requests.get(f"{base}/projects/{p['id']}/gaps", headers=B).status_code == 404

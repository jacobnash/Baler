"""commissioning_api (#37): the spec is valid OpenAPI, live responses
match it, and tenancy holds - org and site checked against the caller's
own JWT, graph reads scoped, results filtered by the reader's scope.
Runs the real handler on MemoryRepo: no services."""

from __future__ import annotations

from http.server import ThreadingHTTPServer
from threading import Thread

import cx_fixtures as fx
import jwt as pyjwt
import pytest
import requests
import yaml
from conftest import assert_matches_schema, load_spec
from openapi_spec_validator import validate

from baler.commissioning import db
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


@pytest.fixture
def base(monkeypatch, repo):
    monkeypatch.setenv("BALER_GATEWAY_SECRET", GATEWAY)
    monkeypatch.setenv("BALER_JWT_SECRET", JWT)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(repo, clock=lambda: fx.NOW))
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


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
    assert p["current_phase"] == "startup" and p["org_id"] == "org-a"
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
    assert r.status_code == 201, r.text
    run = r.json()
    assert_matches_schema(run, _schema(spec, "/projects/{id}/runs", "post", 201), spec)
    assert run["phase"] == "startup" and run["summary"]["window_hours"] == 48
    assert run["scope"] == {"caller": {"prefixes": ["fbf/a", "fbf/a2"], "exclude": []}, "site_prefixes": ["fbf/a"]}
    assert run["window_start"] == "2026-11-04T12:00:00+00:00"

    got = requests.get(f"{base}/runs/{run['id']}", headers=A)
    assert got.status_code == 200
    assert_matches_schema(got.json(), _schema(spec, "/runs/{id}", "get", 200), spec)
    results = got.json()["results"]
    assert {r["status"] for r in results} == {"pass", "fail", "inconclusive"}
    # 48h window: RAT (3 days old) still stale
    assert next(r for r in results if r["subject"] == fx.pt(fx.AHU, "RAT"))["status"] == "fail"

    only_fail = requests.get(f"{base}/runs/{run['id']}", params={"status": "fail", "check_id": "template:"}, headers=A).json()["results"]
    assert only_fail and all(r["status"] == "fail" and r["check_id"].startswith("template:") for r in only_fail)

    listed = requests.get(f"{base}/projects/{p['id']}/runs", headers=A).json()
    assert [x["id"] for x in listed] == [run["id"]]
    assert requests.get(f"{base}/runs/{run['id']}", headers=B).status_code == 404
    assert requests.get(f"{base}/runs/missing", headers=A).status_code == 404
    assert requests.get(f"{base}/projects/{p['id']}/events", headers=A).json()[0]["action"] == "run.create"


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

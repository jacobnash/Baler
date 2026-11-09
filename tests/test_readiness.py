"""Readiness per space / suite (#210): the rules, and the report over the API."""

from __future__ import annotations

from datetime import timedelta

import cx_api_harness as hx
import cx_fixtures as fx
import pytest
import requests
from rdflib import URIRef

from baler.commissioning import readiness
from baler.store import BRICK


def test_no_evidence_is_never_ready():
    assert readiness.requirements_criterion([])["status"] == "inconclusive"
    assert readiness.tab_criterion([])["status"] == "inconclusive"
    assert readiness.tests_criterion([], [], {})["status"] == "inconclusive"
    assert readiness.verdict(readiness.worst(["pass", "inconclusive"])) == "incomplete"
    assert readiness.verdict(readiness.worst(["pass", "fail", "inconclusive"])) == "not_ready"
    assert readiness.verdict(readiness.worst(["pass", "pass"])) == "ready"


def test_issues_risk_accepted_passes_open_fails():
    issues = [{"id": "i1", "status": "risk_accepted", "equip_uri": fx.VAV, "subject": "x"},
              {"id": "i2", "status": "closed", "equip_uri": fx.VAV, "subject": "x"},
              {"id": "i3", "status": "open", "equip_uri": fx.MTR, "subject": "x"}]
    assert readiness.issues_criterion(issues, {fx.VAV}, set())["status"] == "pass"
    assert readiness.issues_criterion(issues, {fx.VAV, fx.MTR}, set())["status"] == "fail"
    # an issue on the room's own point counts too
    assert readiness.issues_criterion([{"id": "i4", "status": "in_progress", "equip_uri": None, "subject": "urn:point:r"}],
                                      set(), {"urn:point:r"})["status"] == "fail"


@pytest.fixture
def api(monkeypatch):
    store = fx.store()
    store.graph.add((URIRef(fx.AHU), BRICK.feeds, URIRef(fx.VAV)))
    repo = hx.repo(store=store)
    base, server = hx.serve(monkeypatch, repo)
    saved = list(readiness.REQUIREMENT_SOURCES)
    yield base, repo
    server.shutdown()
    readiness.REQUIREMENT_SOURCES[:] = saved


def _space(base, pid, **body):
    r = requests.post(f"{base}/projects/{pid}/spaces", json=body, headers=hx.A)
    assert r.status_code == 201, r.text
    return r.json()


def _passed_test(repo, pid, uris, status="pass"):
    proc = {"id": f"p-{status}", "project_id": pid, "name": "VAV start-up", "kind": "startup", "status": "active",
            "created_at": fx.NOW, "steps": []}
    repo.create_procedure(proc)
    repo.create_test_run({"id": f"tr-{status}", "procedure_id": proc["id"], "project_id": pid, "status": "completed",
                          "requested_at": fx.NOW - timedelta(hours=1), "finished_at": fx.NOW,
                          "equipment": [{"equip_uri": u, "status": status, "steps": []} for u in uris]})


def test_suite_readiness_report(api):
    base, repo = api
    pid = hx.project(base)["id"]
    suite = _space(base, pid, name="Surgery suite", kind="suite")
    or3 = _space(base, pid, name="OR 3", space_type="operating_room", parent_id=suite["id"])
    requests.put(f"{base}/spaces/{or3['id']}/serving", json={"equip_uris": [fx.VAV]}, headers=hx.A)

    rep = hx.check(requests.get(f"{base}/spaces/{suite['id']}/readiness", headers=hx.A), "/spaces/{id}/readiness", "get", 200)
    room = rep["rooms"][0]
    assert rep["verdict"] == "incomplete" and room["name"] == "OR 3"
    assert {k: v["status"] for k, v in room["criteria"].items()} == {
        "requirements": "inconclusive", "tests": "inconclusive", "tab_report": "inconclusive", "issues": "pass"}
    assert [c["equip_uri"] for c in room["serving_chain"]] == [fx.VAV, fx.AHU]
    assert any("requirement pack" in x for x in rep["limitations"])

    # evidence arrives: both serving units tested, TAB linked, a requirement source passes
    _passed_test(repo, pid, [fx.VAV, fx.AHU])
    requests.patch(f"{base}/spaces/{or3['id']}", json={"attributes": {"tab_report": {"title": "TAB report L3, rev 2"}}}, headers=hx.A)
    readiness.REQUIREMENT_SOURCES.append(lambda repo, project, space, now: [
        {"id": "r1", "title": "Pressure to adjacent", "citation": "test", "status": "pass", "evidence": {}}])
    rep = requests.get(f"{base}/spaces/{suite['id']}/readiness", headers=hx.A).json()
    assert rep["verdict"] == "ready", rep["rooms"][0]["criteria"]

    # an open issue on the AHU feeding the OR makes it not ready
    repo.create_issue({"id": "iss", "project_id": pid, "equip_uri": fx.AHU, "check_id": None, "subject": fx.AHU,
                       "status": "open", "title": "SAT hunting", "created_at": fx.NOW})
    rep = requests.get(f"{base}/spaces/{or3['id']}/readiness", headers=hx.A).json()
    assert rep["verdict"] == "not_ready" and rep["rooms"][0]["criteria"]["issues"]["items"][0]["issue_id"] == "iss"

    page = requests.get(f"{base}/spaces/{suite['id']}/readiness?format=html", headers=hx.A)
    assert page.status_code == 200 and "default-src 'none'" in page.headers["Content-Security-Policy"]
    assert "NOT READY" in page.text and "SAT hunting" in page.text
    assert requests.get(f"{base}/spaces/{suite['id']}/readiness", headers=hx.B).status_code == 404


def test_failed_test_fails_the_room(api):
    base, repo = api
    pid = hx.project(base)["id"]
    room = _space(base, pid, name="Lab 1", space_type="laboratory")
    requests.put(f"{base}/spaces/{room['id']}/serving", json={"equip_uris": [fx.VAV]}, headers=hx.A)
    _passed_test(repo, pid, [fx.VAV, fx.AHU], status="fail")
    rep = requests.get(f"{base}/spaces/{room['id']}/readiness", headers=hx.A).json()
    assert rep["rooms"][0]["criteria"]["tests"]["status"] == "fail" and rep["verdict"] == "not_ready"

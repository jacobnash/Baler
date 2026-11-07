"""Cx 8 (#44): opt-in fault mirroring with real URIs, and faults into the
issues log - two orgs side by side (cx_fixtures), MemoryRepo, no services."""

from __future__ import annotations

from datetime import timedelta

import cx_fixtures as fx
import pytest
import requests
import test_commissioning_openapi as api
from conftest import assert_matches_schema, load_spec

from baler.commissioning import db, mirror, runner
from baler.commissioning_api import OPENAPI_SPEC_PATH

RAT = fx.pt(fx.AHU, "RAT")


def _r(check, subject, status, equip=fx.AHU):
    return {"equip_uri": equip, "check_id": check, "subject": subject, "status": status, "symptom": "s"}


def test_plan():
    results = [_r("exists", RAT, "fail"), _r("exists", fx.pt(fx.AHU, "SAT"), "pass"), _r("live", fx.pt(fx.AHU, "X"), "inconclusive"),
               _r("pair:fan_cmd_status", f"{fx.pt(fx.AHU, 'SF-C')}|{fx.pt(fx.AHU, 'SF-S')}", "fail"),
               _r("template:AHU_SAT", fx.AHU, "fail"), _r("exists", fx.pt(fx.VAV, "ZNT"), "fail", fx.VAV)]
    opened, closed = mirror.plan(results, {}, None, set(), {("cx:exists", fx.pt(fx.AHU, "SAT"))})
    assert {(r, u) for r, u, _ in opened} == {("cx:exists", RAT), ("cx:pair:fan_cmd_status", fx.pt(fx.AHU, "SF-C")),
                                             ("cx:exists", fx.pt(fx.VAV, "ZNT"))}  # template: not mirrored by default
    assert closed == [("cx:exists", fx.pt(fx.AHU, "SAT"), "check passed")]
    assert all(u.startswith(("urn:point:", "urn:equip:")) for _, u, _ in opened)
    # confirmed equipment only, and a risk suppresses (and resolves an open one)
    opened, closed = mirror.plan(results, {}, {fx.VAV}, {("exists", fx.pt(fx.VAV, "ZNT"))}, {("cx:exists", fx.pt(fx.VAV, "ZNT"))})
    assert opened == [] and closed == [("cx:exists", fx.pt(fx.VAV, "ZNT"), "accepted risk")]
    opened, _ = mirror.plan(results, {"checks": ["template:"]}, None, set(), set())
    assert [(r, u) for r, u, _ in opened] == [("cx:template:AHU_SAT", fx.AHU)]


@pytest.mark.parametrize("bad", [[], {"enabled": "yes"}, {"equipment": "some"}, {"checks": []}, {"severity": "bad"}, {"x": 1}])
def test_validate(bad):
    with pytest.raises((ValueError, TypeError)):
        mirror.validate_settings(bad)


# ------------------------------------------------------------------- api


@pytest.fixture(scope="module")
def oas():
    return load_spec(OPENAPI_SPEC_PATH)


@pytest.fixture
def repo():
    return db.MemoryRepo(store=fx.store(), history=fx.history(), sites=api.SITES, scope_fn=api._scope, users={"u-a": "a@example.com"})


@pytest.fixture
def served(monkeypatch, repo):
    worker = runner.RunWorker(repo, lambda: fx.NOW)
    sent = []
    worker.notify = lambda *a: sent.append(a)
    server = api._serve(monkeypatch, repo, worker)
    _workers.append(worker)
    yield f"http://127.0.0.1:{server.server_address[1]}", sent
    _workers.remove(worker)
    server.shutdown()


_workers: list = []  # the served fixture's RunWorker, for _run to join


def _run(base, pid, headers=api.A):
    run = requests.post(f"{base}/projects/{pid}/runs", json={}, headers=headers).json()
    done = api._wait(base, run["id"], headers).json()
    # The run reads "completed" before the runner mirrors its faults
    # (runner.execute: complete_run, then mirror.apply), so a test that
    # checks repo.faults right after the status flips races the mirror.
    # Joining the worker waits for the whole job, mirroring included.
    for worker in _workers:
        worker.join()
    return done


def test_mirroring_opens_real_uri_faults_and_resolves_them(served, repo, oas):
    base, sent = served
    p = api._project(base, settings={"fault_mirroring": {"enabled": True, "equipment": "all"}})
    _run(base, p["id"])
    open_ = [f for f in repo.faults if f["ended_at"] is None]
    assert open_ and all(f["rule_id"].startswith("cx:") for f in open_)
    assert all(f["point_uri"].startswith(("urn:point:fbf/a/", "urn:equip:fbf/a/")) for f in open_)  # never cx:, never org B
    rat = next(f for f in open_ if f["point_uri"] == RAT)
    assert rat["rule_id"] == "cx:exists" and rat["detail"]["project_id"] == p["id"] and rat["detail"]["source"] == "commissioning"
    assert ("fault.opened", "cx:exists", RAT) in [(e, r, u) for e, r, u, *_ in sent]
    listed = requests.get(f"{base}/projects/{p['id']}/faults", headers=api.A)
    assert_matches_schema(listed.json(), api._schema(oas, "/projects/{id}/faults", "get", 200), oas)
    assert RAT in {f["point_uri"] for f in listed.json()}
    assert requests.get(f"{base}/projects/{p['id']}/faults", headers=api.B).status_code == 404
    # a second run doesn't duplicate; RAT reporting again resolves its fault
    before = len(repo.faults)
    repo._history.add(RAT, fx.NOW - timedelta(minutes=1), 70.0)
    _run(base, p["id"])
    assert next(f for f in repo.faults if f["id"] == rat["id"])["status"] == "resolved"
    assert len(repo.faults) == before
    assert ("fault.resolved", "cx:exists", RAT) in [(e, r, u) for e, r, u, *_ in sent]


def test_confirmed_only_and_risk_suppression(served, repo):
    base, _ = served
    p = api._project(base, settings={"fault_mirroring": {"enabled": True}})
    _run(base, p["id"])
    assert repo.faults == []  # nothing confirmed against the spec yet
    repo.put_rows("cx_mappings", [{"id": "m1", "project_id": p["id"], "spec_item_id": "s", "equip_uri": fx.AHU, "confidence": "confirmed",
                                   "basis": [], "alternatives": [], "status": "confirmed", "created_at": fx.NOW}])
    run = _run(base, p["id"])
    assert {f["point_uri"] for f in repo.faults} == {RAT}  # AHU only, VAV not confirmed
    issue = requests.post(f"{base}/projects/{p['id']}/issues", headers=api.A,
                          json={"from_result": {"run_id": run["run"]["id"], "check_id": "exists", "subject": RAT}}).json()
    assert requests.post(f"{base}/issues/{issue['id']}/risk", json={"owner": "Controls Co", "retest_trigger": "sensor replaced", "reason": "on order", "review_by": "2027-01-01",
                                                                         "accepted_name": "Pat Admin"}, headers=api.A).status_code == 201
    _run(base, p["id"])
    assert repo.faults[0]["status"] == "resolved"  # the accepted risk suppresses the mirror


def test_disabled_by_default(served, repo):
    base, _ = served
    p = api._project(base)
    _run(base, p["id"])
    assert repo.faults == []
    assert requests.patch(f"{base}/projects/{p['id']}", json={"settings": {"fault_mirroring": {"enabled": 1}}}, headers=api.A).status_code == 400


def test_ingest_fault_detector_faults_two_orgs(served, repo, oas):
    base, _ = served
    p = api._project(base)
    mine = repo.open_fault("stale-zone", fx.pt(fx.VAV, "ZNT"), fx.NOW, {"message": "zone temp stale"}, "critical")
    repo.open_fault("stale-zone", fx.pt(fx.AHU_B, "SAT"), fx.NOW, {}, "warning")  # org B's point
    repo.open_fault("cx:exists", RAT, fx.NOW, {"project_id": "other"}, "warning")  # a Cx mirror: never ingested
    r = requests.post(f"{base}/projects/{p['id']}/faults/ingest", json={"source": "fault_detector"}, headers=api.A)
    assert r.status_code == 200, r.text
    assert_matches_schema(r.json(), api._schema(oas, "/projects/{id}/faults/ingest", "post", 200), oas)
    assert (r.json()["created"], r.json()["outside_project"]) == (1, 1)  # org B's point; the cx: mirror is skipped outright
    [issue] = requests.get(f"{base}/projects/{p['id']}/issues", headers=api.A).json()
    assert issue["source"] == "fdd:fault_detector" and issue["source_id"] == str(mine) and issue["equip_uri"] == fx.VAV
    assert issue["title"] == "zone temp stale" and issue["severity"] == "critical"
    again = requests.post(f"{base}/projects/{p['id']}/faults/ingest", json={"source": "fault_detector"}, headers=api.A).json()
    assert again["created"] == 0
    repo.close_fault("stale-zone", fx.pt(fx.VAV, "ZNT"), fx.NOW)
    done = requests.post(f"{base}/projects/{p['id']}/faults/ingest", json={"source": "fault_detector"}, headers=api.A).json()
    assert done["resolved"] == 1
    assert requests.get(f"{base}/issues/{issue['id']}", headers=api.A).json()["status"] == "closed"
    # org B ingests nothing of org A's
    pb = api._project(base, headers=api.B, site_id="site-b")
    rb = requests.post(f"{base}/projects/{pb['id']}/faults/ingest", json={"source": "fault_detector"}, headers=api.B).json()
    assert rb["created"] == 1  # only its own point
    assert requests.get(f"{base}/projects/{pb['id']}/issues", headers=api.B).json()[0]["equip_uri"] == fx.AHU_B


def test_ingest_external_fdd(served):
    base, _ = served
    p = api._project(base)
    items = [{"id": "K-1", "point_uri": fx.pt(fx.AHU, "SAT"), "title": "SAT hunting", "severity": "warning"},
             {"id": "K-2", "equip_uri": fx.VAV, "message": "damper stuck"},
             {"id": "K-3", "point_uri": fx.pt(fx.AHU_B, "SAT")}]
    r = requests.post(f"{base}/projects/{p['id']}/faults/ingest", json={"source": "kgs", "items": items}, headers=api.A).json()
    assert (r["source"], r["created"], r["outside_project"]) == ("fdd:kgs", 2, 1)
    assert requests.post(f"{base}/projects/{p['id']}/faults/ingest", json={"source": "kgs", "items": items}, headers=api.A).json()["created"] == 0
    for bad in ({"source": "KGS!"}, {"source": "kgs"}, {"source": "kgs", "items": [{"id": "x"}]},
                {"source": "fault_detector", "items": []}):
        assert requests.post(f"{base}/projects/{p['id']}/faults/ingest", json=bad, headers=api.A).status_code == 400

"""Phase gates (Baler#86): moving into start-up, functional or acceptance
checks the records - points reporting, procedures passing, issues closed,
inconclusives waived - and warns, blocks or stays out of the way per
settings.phase_gates. No services."""

from __future__ import annotations

from datetime import timedelta

import cx_fixtures as fx
import pytest
import requests
import test_commissioning_issues as ti
from conftest import assert_matches_schema, load_spec

from baler.commissioning import db, gates
from baler.commissioning_api import OPENAPI_SPEC_PATH

A = ti.A
RUN = {"id": "r1"}


@pytest.fixture(scope="module")
def spec() -> dict:
    return load_spec(OPENAPI_SPEC_PATH)


@pytest.fixture
def repo():
    return db.MemoryRepo(store=fx.store(), history=fx.history(), sites=ti.api.SITES, scope_fn=ti.api._scope,
                         users={"u-a": "a@example.com"})


@pytest.fixture
def base(monkeypatch, repo):
    server = ti.api._serve(monkeypatch, repo)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _res(subject, status, check="exists", **kw):
    return {"check_id": check, "subject": subject, "status": status, "equip_uri": "urn:equip:x", **kw}


def test_rules_per_phase():
    assert gates.rules_for("construction") == []
    assert gates.rules_for("startup") == ["verification_run", "points_reporting"]
    assert "early_procedures_pass" in gates.rules_for("functional")
    assert set(gates.rules_for("acceptance")) >= {"procedures_pass", "no_open_issues", "no_unwaived_inconclusive"}


def test_no_run_is_unmet():
    g = gates.evaluate("startup", None, [], [], {}, [], {})
    assert not g["met"] and [u["rule"] for u in g["unmet"]] == ["verification_run"]


def test_startup_needs_every_point_reporting_unless_waived():
    res = [_res("a", "pass"), _res("b", "inconclusive", symptom="no samples have ever been recorded")]
    g = gates.evaluate("startup", RUN, res, [], {}, [], {})
    assert not g["met"] and g["unmet"][0]["items"][0]["subject"] == "b"
    waived = [{"id": "i1", "check_id": "exists", "subject": "b", "status": "risk_accepted", "title": "t"}]
    assert gates.evaluate("startup", RUN, res, [], {}, waived, {"i1": {"id": "k1"}})["met"]


def test_functional_needs_early_procedures_passing():
    procs = [{"id": "p1", "name": "AHU power-on", "kind": "power_on", "status": "active"},
             {"id": "p2", "name": "AHU functional", "kind": "functional", "status": "active"},
             {"id": "p3", "name": "old", "kind": "startup", "status": "archived"}]
    latest = {"p1": {"id": "t1", "summary": {"by_status": {"fail": 1, "pass": 2}}}, "p2": None}
    g = gates.evaluate("functional", RUN, [], procs, latest, [], {})
    [u] = g["unmet"]
    assert u["rule"] == "early_procedures_pass" and u["items"] == [
        {"procedure_id": "p1", "procedure": "AHU power-on", "test_run_id": "t1", "status": "1 fail"}]
    latest["p1"] = {"id": "t2", "summary": {"by_status": {"pass": 3}}}
    assert gates.evaluate("functional", RUN, [], procs, latest, [], {})["met"]  # functional's own procedure isn't required yet


def test_acceptance_needs_zero_unwaived_inconclusive_and_no_open_issues():
    procs = [{"id": "p1", "name": "AHU start-up", "kind": "startup", "status": "active"}]
    latest = {"p1": {"id": "t1", "summary": {"by_status": {"pass": 1, "inconclusive": 1}}}}
    res = [_res("a", "pass"), _res("b", "inconclusive", check="live")]
    open_issue = [{"id": "i2", "check_id": "exists", "subject": "c", "status": "ready_for_retest", "title": "resolved"}]
    g = gates.evaluate("acceptance", RUN, res, procs, latest, open_issue, {})
    assert {u["rule"] for u in g["unmet"]} == {"procedures_pass", "no_open_issues", "no_unwaived_inconclusive"}
    # a resolved-but-not-closed issue still blocks acceptance: a person closes it
    assert "person" in next(u for u in g["unmet"] if u["rule"] == "no_open_issues")["message"]
    waiver = [{"id": "i3", "check_id": "live", "subject": "b", "status": "risk_accepted", "title": "w"}]
    latest["p1"]["summary"]["by_status"] = {"pass": 2}
    assert gates.evaluate("acceptance", RUN, res, procs, latest, waiver, {"i3": {"id": "k3"}})["met"]


@pytest.mark.parametrize("bad", ["strict", True, None])
def test_mode_validation(bad):
    with pytest.raises(ValueError):
        gates.validate_mode(bad)
    assert gates.mode({"phase_gates": "block"}) == "block" and gates.mode(None) == "warn"


# ------------------------------------------------------------------ API


def test_warn_moves_and_records_what_was_unmet(base, spec):
    p = ti._project(base)  # phase startup, no runs yet
    pre = requests.get(f"{base}/projects/{p['id']}/gate", params={"to": "functional"}, headers=A)
    assert pre.status_code == 200 and pre.json()["mode"] == "warn" and not pre.json()["gate"]["met"]
    r = requests.post(f"{base}/projects/{p['id']}/phase", headers=A, json={"key": "functional"})
    assert r.status_code == 200 and r.json()["current_phase"] == "functional" and r.json()["gate"]["met"] is False
    assert_matches_schema(r.json(), ti._schema(spec, "/projects/{id}/phase", "post", 200), spec)
    # moving back is never gated
    r = requests.post(f"{base}/projects/{p['id']}/phase", headers=A, json={"key": "startup"})
    assert r.status_code == 200 and r.json()["gate"] is None


def test_block_refuses_unless_overridden_with_a_note(base):
    p = ti._project(base)
    assert requests.patch(f"{base}/projects/{p['id']}", headers=A, json={"settings": {"phase_gates": "block"}}).status_code == 200
    r = requests.post(f"{base}/projects/{p['id']}/phase", headers=A, json={"key": "acceptance"})
    assert r.status_code == 409 and not r.json()["gate"]["met"]
    assert requests.post(f"{base}/projects/{p['id']}/phase", headers=A, json={"key": "acceptance", "override": True}).status_code == 400
    r = requests.post(f"{base}/projects/{p['id']}/phase", headers=A,
                      json={"key": "acceptance", "override": True, "note": "owner accepts; retest at changeover"})
    assert r.status_code == 200 and r.json()["current_phase"] == "acceptance"
    ev = requests.get(f"{base}/projects/{p['id']}/events", headers=A).json()[0]
    assert ev["detail"]["override"] is True and ev["detail"]["gate_mode"] == "block" and ev["actor"] == "user"
    assert requests.patch(f"{base}/projects/{p['id']}", headers=A, json={"settings": {"phase_gates": "nope"}}).status_code == 400


def test_a_clean_project_passes_the_startup_gate(base, repo):
    p, run = ti._completed_run(base)
    for r in repo.get_results(run["id"]):
        if r["check_id"] == "exists" and r["status"] != "pass":
            repo._history.add(r["subject"], fx.NOW - timedelta(minutes=1), 1.0)
    run2 = requests.post(f"{base}/projects/{p['id']}/runs", json={}, headers=A).json()
    ti._wait(base, run2["id"])
    requests.post(f"{base}/projects/{p['id']}/phase", headers=A, json={"key": "construction"})
    g = requests.get(f"{base}/projects/{p['id']}/gate", params={"to": "startup"}, headers=A).json()["gate"]
    assert g["met"], g


def test_gate_preview_validates_target(base):
    p = ti._project(base)
    assert requests.get(f"{base}/projects/{p['id']}/gate", params={"to": "nope"}, headers=A).status_code == 400


# ------------------------------------------------------------- Cx report (#87)


def test_project_report_tells_the_whole_story(base, repo, spec):
    p, run = ti._completed_run(base)
    issue = ti._rat_issue(base, run)
    assert issue["severity"] == "major" and issue["title"].startswith(ti.RAT.replace("urn:point:", "", 1))
    requests.post(f"{base}/projects/{p['id']}/phase", headers=A, json={"key": "functional", "note": "started"})
    r = requests.get(f"{base}/projects/{p['id']}/report", headers=A)
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["latest_run"]["run"]["id"] == run["id"]
    assert any("Passive only" in x for x in rep["limitations"])
    assert rep["phase_history"][0]["detail"]["to"] == "functional"
    [logged] = [i for i in rep["issues"] if i["id"] == issue["id"]]
    assert logged["events"][0]["action"] == "create" and logged["events"][0]["actor"] == "user"
    page = requests.get(f"{base}/projects/{p['id']}/report", params={"format": "html"}, headers=A)
    assert page.status_code == 200 and page.headers["Content-Type"].startswith("text/html")
    body = page.text
    for section in ("Scope and limitations", "Phase history", "Specification vs installed", "Procedure / functional test results",
                    "Issues log", "Sign-off", "his.html?expr="):
        assert section in body, section
    assert "<script" not in body


def test_run_report_never_calls_incomplete_equipment_a_pass(base):
    _p, run = ti._completed_run(base)
    rep = requests.get(f"{base}/runs/{run['id']}/report", headers=A).json()
    for e in rep["equipment"]:
        if e["counts"].get("inconclusive") and not e["counts"].get("fail"):
            assert e["status"] == "inconclusive" and e["verdict"] == "incomplete"
    for e in rep["equipment"]:
        for r in e["results"]:
            if r["status"] == "inconclusive":
                assert r["reason"]  # never an empty cell
    page = requests.get(f"{base}/runs/{run['id']}/report", params={"format": "html"}, headers=A).text
    assert "verification ladder" in page and " h)" in page  # ladder explained; window length in human units


def test_an_inconclusive_check_can_be_waived_for_acceptance(base, repo):
    """#96: rung 3 on read-only controllers can never pass; a waiver
    (an accepted risk on an issue opened from the inconclusive result) has
    to be possible, or the acceptance gate is unreachable."""
    p, run = ti._completed_run(base)
    inc = next(r for r in repo.get_results(run["id"]) if r["status"] == "inconclusive")
    r = requests.post(f"{base}/projects/{p['id']}/issues", headers=A,
                      json={"from_result": {"run_id": run["id"], "check_id": inc["check_id"], "subject": inc["subject"]}})
    assert r.status_code == 201, r.text
    bulk = requests.post(f"{base}/runs/{run['id']}/issues", headers=A, json={"include_inconclusive": True}).json()
    assert bulk["already_tracked"] >= 1  # the one above
    before = requests.get(f"{base}/projects/{p['id']}/gate", params={"to": "acceptance"}, headers=A).json()["gate"]
    unwaived = next(u for u in before["unmet"] if u["rule"] == "no_unwaived_inconclusive")["count"]
    risk = requests.post(f"{base}/issues/{r.json()['id']}/risk", headers=ti.ADMIN,
                         json={"reason": "read-only controller; retest when writes are enabled",
                               "review_by": (fx.NOW.date() + timedelta(days=90)).isoformat()})
    assert risk.status_code in (200, 201), risk.text
    after = requests.get(f"{base}/projects/{p['id']}/gate", params={"to": "acceptance"}, headers=A).json()["gate"]
    left = next((u["count"] for u in after["unmet"] if u["rule"] == "no_unwaived_inconclusive"), 0)
    assert left == unwaived - 1

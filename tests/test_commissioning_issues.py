"""Cx 4 (#40): the issues log, accepted risks, import/export - the pure
rules in commissioning.issues and the routes on MemoryRepo (no services)."""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta

import cx_fixtures as fx
import pytest
import requests
import test_commissioning_openapi as api
from conftest import assert_matches_schema, load_spec

from baler.commissioning import db, issues
from baler.commissioning_api import OPENAPI_SPEC_PATH

A, A2_ONLY, B = api.A, api.A2_ONLY, api.B
_completed_run, _project, _schema, _wait = api._completed_run, api._project, api._schema, api._wait


@pytest.fixture(scope="module")
def spec() -> dict:
    return load_spec(OPENAPI_SPEC_PATH)


@pytest.fixture
def repo():
    return db.MemoryRepo(store=fx.store(), history=fx.history(), sites=api.SITES, scope_fn=api._scope,
                         users={"u-a": "a@example.com"})


@pytest.fixture
def base(monkeypatch, repo):
    server = api._serve(monkeypatch, repo)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()

RAT = fx.pt(fx.AHU, "RAT")
ADMIN = api._h()  # the gateway enforces roles; the backend sees the same claims


# ------------------------------------------------------------------ pure


def _issue(**kw):
    base_ = {"id": "i1", "project_id": "p", "check_id": "exists", "subject": RAT, "status": "open", "title": "t"}
    return base_ | kw


def test_transition_rules():
    issues.transition(_issue(), "in_progress")
    issues.transition(_issue(status="in_progress"), "ready_for_retest")
    with pytest.raises(issues.Conflict, match="ready_for_retest first"):
        issues.transition(_issue(), "closed")
    with pytest.raises(issues.Conflict, match="no passing retest"):
        issues.transition(_issue(status="ready_for_retest"), "closed")
    issues.transition(_issue(status="ready_for_retest", retest={"run_id": "r3"}), "closed")  # a person, citing the retest
    issues.transition(_issue(check_id=None, subject=None), "closed")  # manual
    with pytest.raises(issues.Conflict):
        issues.transition(_issue(), "risk_accepted")
    with pytest.raises(issues.Conflict, match="revoke"):
        issues.transition(_issue(status="risk_accepted"), "open")
    with pytest.raises(ValueError):
        issues.transition(_issue(), "nope")


def test_reconcile():
    now = fx.NOW
    today = now.date()
    res = [{"check_id": "exists", "subject": "a", "status": "pass"},
           {"check_id": "exists", "subject": "b", "status": "fail", "symptom": "s"},
           {"check_id": "exists", "subject": "c", "status": "inconclusive"},
           {"check_id": "exists", "subject": "d", "status": "fail"},
           {"check_id": "exists", "subject": "e", "status": "fail"}]
    active = [_issue(id="1", subject="a"), _issue(id="2", subject="b", status="ready_for_retest"),
              _issue(id="3", subject="c"), _issue(id="4", subject="d", status="risk_accepted"),
              _issue(id="5", subject="e", status="risk_accepted"), _issue(id="6", check_id=None, subject=None)]
    risks = {"4": {"id": "k4", "review_by": today + timedelta(days=5)}, "5": {"id": "k5", "review_by": today - timedelta(days=1)}}
    got = {i: (c, a) for i, c, a, _ in issues.reconcile(active, risks, res, "r2", now, today)}
    # evidence resolves, never closes (#86)
    assert got["1"][1] == "resolve.evidence" and got["1"][0]["status"] == "ready_for_retest"
    assert got["1"][0]["resolved_by"] == {"run_id": "r2"} and "closed_run" not in got["1"][0]
    assert got["2"][1] == "retest.failed" and got["2"][0]["status"] == "open"
    assert "3" not in got  # inconclusive never closes
    assert got["4"][1] == "risk.covered" and "status" not in got["4"][0]
    assert got["5"][1] == "still.failing"  # risk lapsed first (separate entry), then judged as open
    acts = [a for i, _, a, _ in issues.reconcile(active, risks, res, "r2", now, today) if i == "5"]
    assert acts == ["risk.expired", "still.failing"]
    assert "6" not in got


def test_retest_must_rest_on_data_from_after_the_fix():
    now = fx.NOW
    fixed = now - timedelta(hours=1)
    issue = _issue(id="1", subject="a", status="ready_for_retest", resolved_at=fixed)
    old = {"check_id": "exists", "subject": "a", "status": "pass", "evidence": {"last_ts": (fixed - timedelta(minutes=5)).isoformat()}}
    new = {**old, "evidence": {"last_ts": (fixed + timedelta(minutes=5)).isoformat()}}
    # a 24 h verification window over data from before the fix: not a retest
    assert issues.reconcile([issue], {}, [old], "r3", now, now.date(), now - timedelta(hours=24)) == []
    # the point has reported since the fix: a retest
    [(_, ch, act, _det)] = issues.reconcile([issue], {}, [new], "r3", now, now.date(), now - timedelta(hours=24))
    assert act == "retest.passed" and ch["retest"]["run_id"] == "r3" and "status" not in ch
    # a procedure test run whose window starts after the fix
    [(_, ch, act, _)] = issues.reconcile([issue], {}, [old], "t1", now, now.date(), fixed + timedelta(minutes=1), "test_run")
    assert act == "retest.passed" and ch["retest"] == {"test_run_id": "t1", "kind": "test_run", "at": now.isoformat(),
                                                       "judged_from": (fixed + timedelta(minutes=1)).isoformat()}
    assert "last_seen_run" not in ch  # test runs don't count as the verification run that last saw it


def test_parse_import_csv_aliases_and_status():
    text = "﻿Issue #,Title,Description,Status,Responsible Party,Asset,Priority,Floor\n" \
           "CX-1,Fan won't start,VFD fault,In Progress,Mech Co,AHU-1,High,2\n" \
           ",,,,,,,\n" \
           "CX-2,Damper stuck,,Closed,,VAV-9,low,3\n"
    source, rows = issues.parse_import({"source": "Facility Grid", "csv": text})
    assert source == "facilitygrid" and len(rows) == 2
    assert (rows[0]["source_id"], rows[0]["status"], rows[0]["assignee"], rows[0]["severity"], rows[0]["equipment"]) == \
        ("CX-1", "in_progress", "Mech Co", "major", "AHU-1")
    assert rows[0]["extra"] == {"Floor": "2"}
    assert rows[1]["status"] == "closed" and rows[1]["severity"] == "info"
    with pytest.raises(ValueError):
        issues.parse_import({"source": "baler", "items": []})
    with pytest.raises(TypeError):
        issues.parse_import({"source": "x"})


def test_match_equipment():
    equips = [{"id": fx.AHU, "dis": "AHU-1"}, {"id": fx.VAV, "dis": "VAV 1"}]
    assert issues.match_equipment("ahu 1", equips) == fx.AHU
    assert issues.match_equipment("vav-1", equips) == fx.VAV
    assert issues.match_equipment(fx.AHU, equips) == fx.AHU
    assert issues.match_equipment("urn:equip:elsewhere", equips) is None
    assert issues.match_equipment("chiller", equips) is None


def test_export_csv_shapes_and_formula_quoting():
    i = {"id": "x", "status": "ready_for_retest", "severity": "major", "title": "=HYPERLINK(1)", "equip_uri": fx.AHU,
         "check_id": "exists", "subject": RAT, "assignee": None, "source": "baler", "source_id": None, "detail": {},
         "created_at": fx.NOW, "updated_at": fx.NOW, "closed_at": None}
    for shape in issues.EXPORT_SHAPES:
        text = issues.to_csv(issues.export_rows([i], shape), shape)
        rows = list(csv.DictReader(io.StringIO(text)))
        assert list(rows[0]) == list(issues.EXPORT_SHAPES[shape])
        assert "=HYPERLINK" not in text.replace("'=HYPERLINK", "")
    assert issues.export_rows([i], "cxalloy")[0]["Status"] == "Ready for Review"
    assert issues.export_rows([i], "facilitygrid")[0]["Priority"] == "High"
    with pytest.raises(ValueError):
        issues.export_rows([i], "nope")


# ------------------------------------------------------------------- api


def _rat_issue(base, run):
    r = requests.post(f"{base}/projects/{run['project_id']}/issues", headers=A,
                      json={"from_result": {"run_id": run["id"], "check_id": "exists", "subject": RAT}})
    assert r.status_code == 201, r.text
    return r.json()


def test_issue_from_result_closes_only_on_evidence(base, repo, spec):
    p, run = _completed_run(base)
    issue = _rat_issue(base, run)
    assert_matches_schema(issue, _schema(spec, "/projects/{id}/issues", "post", 201), spec)
    assert issue["status"] == "open" and issue["check_id"] == "exists" and issue["opened_run"] == run["id"]
    dup = requests.post(f"{base}/projects/{p['id']}/issues", headers=A,
                        json={"from_result": {"run_id": run["id"], "check_id": "exists", "subject": RAT}})
    assert dup.status_code == 409 and dup.json()["issue_id"] == issue["id"]
    # a passing result can't open one
    ok = next(r for r in requests.get(f"{base}/runs/{run['id']}", headers=A).json()["results"] if r["status"] == "pass")
    assert requests.post(f"{base}/projects/{p['id']}/issues", headers=A, json={"from_result": {
        "run_id": run["id"], "check_id": ok["check_id"], "subject": ok["subject"]}}).status_code == 400
    # no closing by hand before it is resolved
    r = requests.patch(f"{base}/issues/{issue['id']}", json={"status": "closed"}, headers=A)
    assert r.status_code == 409 and "ready_for_retest first" in r.json()["error"]
    # the sensor reports again; the next run passes exists for RAT: resolved, not closed (#86)
    repo._history.add(RAT, fx.NOW - timedelta(minutes=5), 70.0)
    run2 = requests.post(f"{base}/projects/{p['id']}/runs", json={}, headers=A).json()
    _wait(base, run2["id"])
    got = requests.get(f"{base}/issues/{issue['id']}", headers=A).json()
    assert got["status"] == "ready_for_retest" and got["resolved_by"] == {"run_id": run2["id"]} and got["closed_run"] is None
    assert got["events"][0]["action"] == "resolve.evidence" and got["events"][0]["actor"] == "system"
    # resolving isn't a retest: a person still can't close it
    r = requests.patch(f"{base}/issues/{issue['id']}", json={"status": "closed"}, headers=A)
    assert r.status_code == 409 and "no passing retest" in r.json()["error"]
    # a later run sees the point report after the fix: the retest is recorded
    repo._history.add(RAT, fx.NOW, 70.5)
    run3 = requests.post(f"{base}/projects/{p['id']}/runs", json={}, headers=A).json()
    _wait(base, run3["id"])
    got = requests.get(f"{base}/issues/{issue['id']}", headers=A).json()
    assert got["status"] == "ready_for_retest" and got["retest"]["run_id"] == run3["id"]
    # only now can a named person close it, and the close cites the retest
    r = requests.patch(f"{base}/issues/{issue['id']}", json={"status": "closed", "note": "verified on site"}, headers=A)
    assert r.status_code == 200 and r.json()["status"] == "closed" and r.json()["closed_run"] == run3["id"]
    got = requests.get(f"{base}/issues/{issue['id']}", headers=A)
    assert_matches_schema(got.json(), _schema(spec, "/issues/{id}", "get", 200), spec)
    ev = got.json()["events"]
    assert [e["action"] for e in ev][:4] == ["close", "retest.passed", "resolve.evidence", "create"]
    assert ev[0]["actor"] == "user" and ev[0]["detail"]["retest"]["run_id"] == run3["id"]
    assert ev[0]["detail"]["note"] == "verified on site"
    proj_events = [e["action"] for e in requests.get(f"{base}/projects/{p['id']}/events", headers=A).json()]
    assert "issues.reconcile" in proj_events


def test_claimed_fix_needs_a_retest_after_the_claim(base, repo):
    p, run = _completed_run(base)
    issue = _rat_issue(base, run)
    r = requests.patch(f"{base}/issues/{issue['id']}", json={"status": "ready_for_retest", "note": "replaced sensor"}, headers=A)
    assert r.status_code == 200 and r.json()["status"] == "ready_for_retest" and r.json()["resolved_at"]
    ev = requests.get(f"{base}/issues/{issue['id']}", headers=A).json()["events"]
    assert ev[0]["action"] == "fix.claimed" and ev[0]["detail"]["note"] == "replaced sensor" and ev[0]["actor"] == "user"
    # a pass on data from before the claim is not a retest
    repo._history.add(RAT, fx.NOW - timedelta(minutes=5), 70.0)
    run2 = requests.post(f"{base}/projects/{p['id']}/runs", json={}, headers=A).json()
    _wait(base, run2["id"])
    assert requests.get(f"{base}/issues/{issue['id']}", headers=A).json()["retest"] is None
    # once the point reports after the claim, it is
    repo._history.add(RAT, fx.NOW, 70.5)
    run3 = requests.post(f"{base}/projects/{p['id']}/runs", json={}, headers=A).json()
    _wait(base, run3["id"])
    assert requests.get(f"{base}/issues/{issue['id']}", headers=A).json()["retest"]["run_id"] == run3["id"]


def test_retest_failure_reopens(base, repo):
    p, run = _completed_run(base)
    issue = _rat_issue(base, run)
    requests.patch(f"{base}/issues/{issue['id']}", json={"status": "ready_for_retest"}, headers=A)
    run2 = requests.post(f"{base}/projects/{p['id']}/runs", json={}, headers=A).json()
    _wait(base, run2["id"])
    got = requests.get(f"{base}/issues/{issue['id']}", headers=A).json()
    assert got["status"] == "open" and got["last_seen_run"] == run2["id"] and got["events"][0]["action"] == "retest.failed"


def test_manual_issue_and_filters(base, spec):
    p = _project(base)
    r = requests.post(f"{base}/projects/{p['id']}/issues", json={"title": "Label the AHU-1 panel", "equip_uri": fx.AHU,
                                                               "severity": "minor", "assignee": "Controls Co"}, headers=A)
    assert r.status_code == 201 and r.json()["check_id"] is None
    iid = r.json()["id"]
    assert requests.post(f"{base}/projects/{p['id']}/issues", json={"title": "x", "equip_uri": fx.AHU_B}, headers=A).status_code == 400
    assert requests.post(f"{base}/projects/{p['id']}/issues", json={"title": "x", "severity": "huge"}, headers=A).status_code == 400
    assert requests.patch(f"{base}/issues/{iid}", json={"status": "closed"}, headers=A).json()["status"] == "closed"
    listed = requests.get(f"{base}/projects/{p['id']}/issues", params={"status": "open,in_progress"}, headers=A)
    assert_matches_schema(listed.json(), _schema(spec, "/projects/{id}/issues", "get", 200), spec)
    assert listed.json() == []
    assert [i["id"] for i in requests.get(f"{base}/projects/{p['id']}/issues", headers=A).json()] == [iid]
    assert requests.get(f"{base}/issues/{iid}", headers=B).status_code == 404
    assert requests.patch(f"{base}/issues/{iid}", json={}, headers=A).status_code == 400


def test_issues_from_run_bulk(base):
    p, run = _completed_run(base)
    r = requests.post(f"{base}/runs/{run['id']}/issues", json={"check_prefix": "template:"}, headers=A)
    assert r.status_code == 200 and r.json()["opened"] and r.json()["already_tracked"] == 0
    again = requests.post(f"{base}/runs/{run['id']}/issues", json={"check_prefix": "template:"}, headers=A).json()
    assert again["opened"] == [] and again["already_tracked"] == len(r.json()["opened"])
    listed = requests.get(f"{base}/projects/{p['id']}/issues", headers=A).json()
    assert all(i["check_id"].startswith("template:") for i in listed)
    assert requests.post(f"{base}/runs/{run['id']}/issues", json={}, headers=B).status_code == 404


def test_risk_accept_cover_expire_revoke(base, repo, spec):
    p, run = _completed_run(base)
    issue = _rat_issue(base, run)
    today = fx.NOW.date()
    for bad in ({"reason": "", "review_by": "2027-01-01"}, {"reason": "x", "review_by": str(today)}, {"reason": "x", "review_by": "soon"}):
        assert requests.post(f"{base}/issues/{issue['id']}/risk", json=bad, headers=ADMIN).status_code == 400
    r = requests.post(f"{base}/issues/{issue['id']}/risk", json={"reason": "sensor replacement on order",
                                                                "review_by": str(today + timedelta(days=30))}, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert_matches_schema(r.json(), _schema(spec, "/issues/{id}/risk", "post", 201), spec)
    risk = r.json()
    assert requests.post(f"{base}/issues/{issue['id']}/risk", json={"reason": "x", "review_by": "2030-01-01"}, headers=ADMIN).status_code == 409
    assert requests.patch(f"{base}/issues/{issue['id']}", json={"status": "open"}, headers=A).status_code == 409
    # still failing: covered, not reopened
    run2 = requests.post(f"{base}/projects/{p['id']}/runs", json={}, headers=A).json()
    _wait(base, run2["id"])
    got = requests.get(f"{base}/issues/{issue['id']}", headers=A).json()
    assert got["status"] == "risk_accepted" and got["risk"]["id"] == risk["id"] and got["events"][0]["action"] == "risk.covered"
    risks = requests.get(f"{base}/projects/{p['id']}/risks", headers=A)
    assert_matches_schema(risks.json(), _schema(spec, "/projects/{id}/risks", "get", 200), spec)
    assert [x["id"] for x in risks.json()] == [risk["id"]]
    # review_by passes: reopened on the next read, risk lapsed
    repo.risks[risk["id"]]["review_by"] = date(2026, 1, 1)
    listed = requests.get(f"{base}/projects/{p['id']}/issues", headers=A).json()
    assert listed[0]["status"] == "open" and listed[0]["risk"] is None
    assert requests.get(f"{base}/projects/{p['id']}/risks", headers=A).json() == []
    # accept again, then revoke by hand
    r = requests.post(f"{base}/issues/{issue['id']}/risk", json={"reason": "again", "review_by": "2027-06-01"}, headers=ADMIN).json()
    assert requests.delete(f"{base}/risks/{r['id']}", headers=B).status_code == 404
    assert requests.delete(f"{base}/risks/{r['id']}", headers=ADMIN).status_code == 204
    got = requests.get(f"{base}/issues/{issue['id']}", headers=A).json()
    assert got["status"] == "open" and len(got["risks"]) == 2 and all(k["revoked_at"] for k in got["risks"])


def test_import_upserts_and_export(base, spec):
    p = _project(base)
    text = "ID,Name,Description,Status,Asset,Assigned To\nA-1,Fan noisy,,Open,AHU-1,Mech\nA-2,Missing label,,Closed,Chiller-7,\n"
    r = requests.post(f"{base}/projects/{p['id']}/issues/import", json={"source": "CxAlloy", "csv": text}, headers=A)
    assert r.status_code == 200, r.text
    assert_matches_schema(r.json(), _schema(spec, "/projects/{id}/issues/import", "post", 200), spec)
    assert (r.json()["created"], r.json()["updated"], r.json()["unmatched_equipment"]) == (2, 0, ["Chiller-7"])
    listed = {i["source_id"]: i for i in requests.get(f"{base}/projects/{p['id']}/issues", headers=A).json()}
    assert listed["A-1"]["equip_uri"] == fx.AHU and listed["A-1"]["source"] == "cxalloy" and listed["A-2"]["status"] == "closed"
    text2 = text.replace("Fan noisy,,Open", "Fan noisy,,In Progress")
    r = requests.post(f"{base}/projects/{p['id']}/issues/import", json={"source": "cxalloy", "csv": text2}, headers=A).json()
    assert (r["created"], r["updated"], r["unchanged"]) == (0, 1, 1)
    items = requests.post(f"{base}/projects/{p['id']}/issues/import",
                          json={"source": "sheet", "items": [{"title": "Punch: insulate CHW valve", "location": "Plant"}]}, headers=A)
    assert items.json()["created"] == 1
    assert requests.post(f"{base}/projects/{p['id']}/issues/import", json={"source": "x"}, headers=A).status_code == 400
    csv_out = requests.get(f"{base}/projects/{p['id']}/issues/export", params={"shape": "cxalloy"}, headers=A)
    assert csv_out.headers["Content-Type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(csv_out.text)))
    assert len(rows) == 3 and {r["Status"] for r in rows} == {"In Progress", "Closed", "Open"}
    js = requests.get(f"{base}/projects/{p['id']}/issues/export", params={"format": "json", "shape": "facilitygrid"}, headers=A).json()
    assert {r["Issue #"] for r in js} >= {"A-1", "A-2"}
    assert requests.get(f"{base}/projects/{p['id']}/issues/export", params={"format": "xml"}, headers=A).status_code == 400
    # a site-restricted login on another site sees nothing of it
    assert requests.get(f"{base}/projects/{p['id']}/issues", headers=A2_ONLY).status_code == 404


def test_report_carries_issues(base):
    _, run = _completed_run(base)
    _rat_issue(base, run)
    rep = requests.get(f"{base}/runs/{run['id']}/report", headers=A).json()
    assert [i["check_id"] for i in rep["issues"]] == ["exists"]
    assert "Issues log" in requests.get(f"{base}/runs/{run['id']}/report", params={"format": "html"}, headers=A).text

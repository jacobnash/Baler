"""Traditional and automated commissioning in one procedure (Baler#275,
slices a+b): a manual step, a passive step and an active step in one
procedure, the gate reflecting all three; manual fail -> issue -> retest ->
close with a citation and closed_by; a person contradicting the data;
offline resends stored once; nobody records for someone else; coverage by
class and method. A fake controller behind point_write (the Cx 9 test
site), no services, no real equipment."""

from __future__ import annotations

import base64
import threading
from datetime import timedelta
from http.server import ThreadingHTTPServer

import jwt as pyjwt
import pytest
import requests
import test_commissioning_active as act
import test_commissioning_openapi as api
from conftest import assert_matches_schema, load_spec

from baler.commissioning import active, coverage, db, manual, procedures, runner
from baler.commissioning_api import OPENAPI_SPEC_PATH, make_handler

T0 = act.T0
AHU, CMD, STS, SAT = act.AHU, act.CMD, act.STS, act.SAT

PROC = {"name": "AHU-7 functional (mixed)", "kind": "functional", "equip_class": "Air_Handling_Unit", "window_hours": 1, "steps": [
    {"id": "visual", "title": "Belts, filters and access doors", "instruction": "Look at it.", "methods": ["manual"],
     "witness_required": False},
    {"id": "sat-plausible", "title": "Supply air temperature plausible", "methods": ["passive", "manual"],
     "criteria": [{"range": {"min": 40, "max": 110, "unit": "F"}, "point_class": "Supply_Air_Temperature_Sensor"}]},
    {"id": "fan-response", "title": "Fan status follows command", "methods": ["active"], "active": {"kind": "command_response"}},
]}


def _hdr(role="operator", sub="u-a", kind="session"):
    tok = pyjwt.encode({"sub": sub, "org_id": "org-a", "site_ids": [], "role": role, "kind": kind}, api.JWT, algorithm="HS256")
    return {"X-Gateway-Secret": api.GATEWAY, "Authorization": f"Bearer {tok}"}


ADMIN, OP, OTHER = _hdr("admin", "u-adm"), _hdr(), _hdr(sub="u-b")


@pytest.fixture
def served(monkeypatch):
    site = act.Site()
    for m in range(0, 60, 5):
        site.history.add(SAT, T0 - timedelta(minutes=60 - m), 55.0)
    repo = db.MemoryRepo(store=act.store(), history=site.history, sites=api.SITES, scope_fn=api._scope,
                         users={"u-a": "a@example.com", "u-adm": "adm@example.com", "u-b": "b@example.com"},
                         names={"u-a": "Alex CxA"})
    ex = active.Executor(repo, active.Writer(repo, site.commander), site.clock, site.sleep)
    monkeypatch.setenv("BALER_GATEWAY_SECRET", api.GATEWAY)
    monkeypatch.setenv("BALER_JWT_SECRET", api.JWT)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(repo, clock=site.clock, worker=runner.RunWorker(repo, site.clock),
                                                                executor=ex))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", repo, ex, site
    server.shutdown()


@pytest.fixture(scope="module")
def oas():
    return load_spec(OPENAPI_SPEC_PATH)


def _test_run(base, proc_id, methods=None):
    body = {"methods": methods} if methods else {}
    tr = requests.post(f"{base}/procedures/{proc_id}/runs", json=body, headers=OP)
    assert tr.status_code == 202, tr.text
    for _ in range(500):
        got = requests.get(f"{base}/test-runs/{tr.json()['id']}", headers=OP).json()
        if got["status"] in ("completed", "failed"):
            assert got["status"] == "completed", got
            return got
        threading.Event().wait(0.01)
    raise AssertionError("test run never finished")


def _step(tr, sid):
    return next(s for s in tr["equipment"][0]["steps"] if s["id"] == sid)


def _record(base, tr_id, sid, status, headers=OP, **kw):
    return requests.post(f"{base}/test-runs/{tr_id}/manual-results", headers=headers,
                         json={"equip_uri": AHU, "step_id": sid, "status": status} | kw)


def _setup(base):
    p = api._project(base, headers=ADMIN, phase="functional", settings={"phase_gates": "warn"})
    proc = requests.post(f"{base}/projects/{p['id']}/procedures", headers=OP, json=PROC)
    assert proc.status_code == 201, proc.text
    return p, proc.json()


# ------------------------------------------------------------------ pure


def test_step_methods_validation():
    out = procedures.normalize(PROC)
    assert out["steps"][0] == {"id": "visual", "title": "Belts, filters and access doors", "instruction": "Look at it.",
                               "criteria": [], "methods": ["manual"]}
    assert "methods" not in procedures.normalize({**PROC, "steps": [PROC["steps"][1] | {"methods": ["passive"]}]})["steps"][0]
    for bad in ({"methods": []}, {"methods": ["vibes"]}, {"methods": ["manual", "manual"]},
                {"methods": ["passive"], "criteria": []}, {"methods": ["active"], "active": {"kind": "chaos"}},
                {"methods": ["manual"], "active": {"kind": "command_response"}},
                {"methods": ["passive"], "witness_required": True, "criteria": PROC["steps"][1]["criteria"]}):
        with pytest.raises((ValueError, TypeError)):
            procedures.normalize({**PROC, "steps": [{"id": "x"} | bad]})
    proc = procedures.normalize(PROC)
    assert procedures.choose_methods(proc) == {"visual": "manual", "sat-plausible": "passive", "fan-response": "active"}
    assert procedures.choose_methods(proc, {"sat-plausible": "manual"})["sat-plausible"] == "manual"
    for bad in ({"visual": "passive"}, {"nope": "manual"}):
        with pytest.raises(ValueError):
            procedures.choose_methods(proc, bad)


def test_a_procedure_without_methods_still_runs_exactly_as_before():
    """Every existing procedure (the demo's, live's) is passive-only: same
    statuses, method passive, nothing manual."""
    per = [{"equip_uri": AHU, "dis": "x", "status": "pass", "steps": [
        {"id": "s", "title": "s", "status": "fail", "criteria": [{"status": "fail"}]}]}]
    got = manual.apply(per, {"steps": [{"id": "s", "criteria": [{}]}]}, {"s": "passive"}, [], [], T0)
    assert got[0]["steps"][0]["status"] == "fail" and got[0]["steps"][0]["method"] == "passive"
    assert got[0]["status"] == "fail" and not got[0]["steps"][0]["contradiction"]
    assert manual.issue_results(got, "p") == []


# ------------------------------------------------------- one procedure, three methods


def test_manual_passive_and_active_steps_feed_the_gate(served, oas, monkeypatch):
    base, repo, ex, site = served
    p, proc = _setup(base)
    tr = _test_run(base, proc["id"])
    assert [(s["id"], s["method"], s["status"]) for s in tr["equipment"][0]["steps"]] == [
        ("visual", "manual", "inconclusive"), ("sat-plausible", "passive", "pass"), ("fan-response", "active", "inconclusive")]
    assert _step(tr, "visual")["reason"] == "awaiting a manual result"
    gate = requests.get(f"{base}/projects/{p['id']}/gate", params={"to": "acceptance"}, headers=OP).json()["gate"]
    assert any(u["rule"] == "procedures_pass" for u in gate["unmet"])

    # the manual step: a person, with a witness and a photo
    photo = base64.b64encode(b"\x89PNG fake photo").decode()
    r = _record(base, tr["id"], "visual", "pass", note="belts tight, filters new",
                witness={"name": "Pat Owner", "org": "Owner", "role": "owner's rep"},
                attachments=[{"filename": "belts.png", "media_type": "image/png", "data": photo}])
    assert r.status_code == 201, r.text
    assert_matches_schema(r.json(), api._schema(oas, "/test-runs/{id}/manual-results", "post", 201), oas)
    m = r.json()
    assert (m["recorded_by"], m["recorded_email"], m["recorded_name"], m["recorded_role"]) == ("u-a", "a@example.com", "Alex CxA", "operator")
    att = requests.get(f"{base}/manual-attachments/{m['attachments'][0]['id']}", headers=_hdr("viewer"))
    assert att.status_code == 200 and att.content == b"\x89PNG fake photo" and att.headers["Content-Type"] == "image/png"

    # the active step: a Cx 9 run under every safeguard, unchanged
    monkeypatch.setenv("BALER_CX_ALLOW_WRITES", "1")
    plan = requests.post(f"{base}/projects/{p['id']}/active-runs", headers=ADMIN, json={
        "equip_uri": AHU, "window_start": site.clock().isoformat(), "window_end": (site.clock() + timedelta(hours=1)).isoformat()}).json()
    assert requests.post(f"{base}/active-runs/{plan['id']}/confirm", json={"confirm": plan["id"]}, headers=ADMIN).status_code == 202
    ex.threads[plan["id"]].join(10)
    assert repo.levels == {}

    got = requests.get(f"{base}/test-runs/{tr['id']}", headers=OP).json()
    assert [(s["id"], s["status"]) for s in got["equipment"][0]["steps"]] == [
        ("visual", "pass"), ("sat-plausible", "pass"), ("fan-response", "pass")]
    assert _step(got, "visual")["manual"]["witness"]["name"] == "Pat Owner"
    assert _step(got, "fan-response")["active"]["active_run_id"] == plan["id"]
    assert got["summary"]["by_status"]["pass"] == 1
    assert {k: v["pass"] for k, v in got["summary"]["steps_by_method"].items()} == {"manual": 1, "passive": 1, "active": 1}
    gate = requests.get(f"{base}/projects/{p['id']}/gate", params={"to": "acceptance"}, headers=OP).json()["gate"]
    assert not any(u["rule"] == "procedures_pass" for u in gate["unmet"])

    cov = requests.get(f"{base}/projects/{p['id']}/coverage", headers=_hdr("viewer"))
    assert_matches_schema(cov.json(), api._schema(oas, "/projects/{id}/coverage", "get", 200), oas)
    row = cov.json()[0]
    assert (row["equip_class"], row["sampled"], row["tested"], row["passed"], row["automatic"], row["witnessed"]) == \
        ("Air_Handling_Unit", 1, 1, 1, 1, 1)
    assert row["by_method"] == {"manual": 1, "passive": 1, "active": 1}
    assert row["summary"] == "1 of 1 tested: 1 automatically, 1 manually, 1 witnessed"
    html = requests.get(f"{base}/projects/{p['id']}/report", params={"format": "html"}, headers=OP).text
    assert "Test coverage" in html and "witness Pat Owner" in html and "checked by Alex CxA" in html


def test_manual_fail_opens_an_issue_and_a_retest_closes_it_with_a_citation(served):
    base, _, _, site = served
    p, proc = _setup(base)
    tr = _test_run(base, proc["id"])
    assert _record(base, tr["id"], "visual", "fail", note="belt frayed").status_code == 201
    issues = requests.get(f"{base}/projects/{p['id']}/issues", headers=OP).json()
    issue = next(i for i in issues if i["check_id"] == f"manual:{proc['id']}/visual")
    assert issue["status"] == "open" and issue["subject"] == AHU and "belt frayed" in issue["title"]
    assert issue["detail"]["evidence"]["method"] == "manual"
    # re-reading the run doesn't open it twice or add events
    requests.get(f"{base}/test-runs/{tr['id']}", headers=OP)
    assert len([i for i in requests.get(f"{base}/projects/{p['id']}/issues", headers=OP).json() if i["check_id"] == issue["check_id"]]) == 1
    # no retest yet: can't close
    assert requests.patch(f"{base}/issues/{issue['id']}", json={"status": "closed"}, headers=OP).status_code == 409
    # the contractor fixes it and claims it; the CxA re-checks later
    site.now += timedelta(minutes=10)
    assert requests.patch(f"{base}/issues/{issue['id']}", json={"status": "ready_for_retest", "note": "belt replaced"},
                          headers=OP).status_code == 200
    site.now += timedelta(minutes=10)
    r = _record(base, tr["id"], "visual", "pass", note="new belt")
    assert r.status_code == 201
    got = requests.get(f"{base}/issues/{issue['id']}", headers=OP).json()
    assert got["status"] == "ready_for_retest" and got["retest"]["test_run_id"] == tr["id"]
    closed = requests.patch(f"{base}/issues/{issue['id']}", json={"status": "closed"}, headers=ADMIN).json()
    assert closed["status"] == "closed" and closed["closed_run"] == tr["id"] and closed["closed_by"] == "u-adm"
    events = [e["action"] for e in requests.get(f"{base}/issues/{issue['id']}/events", headers=OP).json()]
    assert events[0] == "close" and "retest.passed" in events and "open.method" in events
    # both results stay on the record
    assert [m["status"] for m in requests.get(f"{base}/test-runs/{tr['id']}/manual-results", headers=OP).json()] == ["fail", "pass"]


def test_a_person_contradicting_the_data_is_flagged_and_opens_an_issue(served):
    base = served[0]
    p, proc = _setup(base)
    tr = _test_run(base, proc["id"], methods={"sat-plausible": "manual"})
    assert _step(tr, "sat-plausible")["machine_status"] == "pass"
    _record(base, tr["id"], "sat-plausible", "fail", note="reads 20F high against my probe")
    got = requests.get(f"{base}/test-runs/{tr['id']}", headers=OP).json()
    st = _step(got, "sat-plausible")
    assert st["contradiction"] and st["status"] == "fail" and st["machine_status"] == "pass"
    assert st["criteria"][0]["status"] == "pass"  # computed evidence never overwritten by a typed value
    assert got["summary"]["contradictions"] == 1
    checks = {i["check_id"] for i in requests.get(f"{base}/projects/{p['id']}/issues", headers=OP).json()}
    assert f"contradiction:{proc['id']}/sat-plausible" in checks
    html = requests.get(f"{base}/projects/{p['id']}/report", params={"format": "html"}, headers=OP).text
    assert "contradiction: a person recorded fail, the data says pass" in html


def test_offline_resend_is_stored_once_and_nobody_records_for_anyone_else(served):
    base, repo, _, site = served
    _, proc = _setup(base)
    tr = _test_run(base, proc["id"])
    observed = (site.clock() - timedelta(hours=2)).isoformat()  # checked while the phone was offline
    first = _record(base, tr["id"], "visual", "pass", client_id="phone-1:draft-0001", observed_at=observed)
    again = _record(base, tr["id"], "visual", "pass", client_id="phone-1:draft-0001", observed_at=observed)
    assert first.status_code == 201 and again.status_code == 200 and again.json()["id"] == first.json()["id"]
    assert len(repo.find_rows(manual.RESULTS, test_run_id=tr["id"])) == 1
    assert first.json()["observed_at"] == observed
    # recorded as the caller: no field names someone else, keys can't, other people can't edit
    assert _record(base, tr["id"], "visual", "pass", recorded_by="u-b").status_code == 400
    assert _record(base, tr["id"], "visual", "pass", headers=_hdr("admin", kind="api_key")).status_code == 403
    assert _record(base, tr["id"], "visual", "pass", headers=_hdr("viewer")).status_code in (403,)
    mid = first.json()["id"]
    assert requests.patch(f"{base}/manual-results/{mid}", json={"status": "fail", "reason": "mine now"}, headers=OTHER).status_code == 403
    assert requests.patch(f"{base}/manual-results/{mid}", json={"status": "fail"}, headers=OP).status_code == 400  # why?
    ed = requests.patch(f"{base}/manual-results/{mid}", json={"status": "fail", "note": "missed a frayed belt",
                                                             "reason": "second look"}, headers=OP)
    assert ed.status_code == 200 and ed.json()["status"] == "fail"
    h = ed.json()["history"]
    assert h[0]["status"] == "pass" and h[0]["reason"] == "second look" and h[0]["replaced_by"] == "u-a"
    for bad in ({"step_id": "sat-plausible-x"}, {"step_id": "fan-response"}, {"status": "maybe"},
                {"equip_uri": "urn:equip:nope"}, {"witness": {"org": "x"}},
                {"attachments": [{"filename": "a.exe", "media_type": "application/x-msdownload", "data": "AA=="}]},
                {"observed_at": (site.clock() + timedelta(days=1)).isoformat()}):
        body = {"equip_uri": AHU, "step_id": "visual", "status": "pass"} | bad
        assert requests.post(f"{base}/test-runs/{tr['id']}/manual-results", json=body, headers=OP).status_code == 400, bad


def test_witness_required_keeps_the_step_open_without_one(served):
    base = served[0]
    p = api._project(base, headers=ADMIN, phase="functional")
    body = {**PROC, "steps": [{**PROC["steps"][0], "witness_required": True}]}
    proc = requests.post(f"{base}/projects/{p['id']}/procedures", headers=OP, json=body).json()
    tr = _test_run(base, proc["id"])
    assert _record(base, tr["id"], "visual", "pass").status_code == 400
    assert _record(base, tr["id"], "visual", "pass", witness={"name": "Pat"}).status_code == 201


def test_coverage_counts_sampled_and_untested():
    procs = [{"id": "p1", "status": "active", "equip_class": "Air_Handling_Unit", "equip_uris": ["urn:equip:a1"], "name": "x"}]
    equips = [{"id": f"urn:equip:a{i}", "brickClasses": ["Air_Handling_Unit"], "points": []} for i in range(1, 4)]
    tr = {"id": "t1", "equipment": [{"equip_uri": "urn:equip:a1", "steps": [
        {"id": "s", "status": "pass", "method": "passive"}, {"id": "m", "status": "inconclusive", "method": "manual"}]}]}
    row = coverage.compute(procs, {"p1": tr}, equips)[0]
    assert (row["in_class"], row["sampled"], row["tested"], row["passed"], row["untested"]) == (3, 1, 1, 0, 0)
    assert coverage.sentence(row) == "1 of 1 tested: 1 automatically (sampled 1 of 3)"

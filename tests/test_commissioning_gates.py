"""Phase gates (Baler#86): moving into start-up, functional or acceptance
checks the records - points reporting, procedures passing, issues closed,
inconclusives waived - and warns, blocks or stays out of the way per
settings.phase_gates. No services."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import cx_fixtures as fx
import pytest
import requests
import test_commissioning_issues as ti
from conftest import assert_matches_schema, load_spec

from baler.commissioning import db, gates, issues, report
from baler.commissioning_api import OPENAPI_SPEC_PATH

A = ti.A
RUN = {"id": "r1"}


@pytest.fixture(scope="module")
def spec() -> dict:
    return load_spec(OPENAPI_SPEC_PATH)


@pytest.fixture
def repo():
    return db.MemoryRepo(store=fx.store(), history=fx.history(), sites=ti.api.SITES, scope_fn=ti.api._scope,
                         users={"u-a": "a@example.com"}, names={"u-a": "Jacob Nash"})


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
    assert any("Passive only" in x["text"] for x in rep["limitations"])
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
                         json={"reason": "read-only controller", "owner": "Controls Co", "retest_trigger": "writes enabled",
                               "review_by": (fx.NOW.date() + timedelta(days=90)).isoformat()})
    assert risk.status_code in (200, 201), risk.text
    after = requests.get(f"{base}/projects/{p['id']}/gate", params={"to": "acceptance"}, headers=A).json()["gate"]
    left = next((u["count"] for u in after["unmet"] if u["rule"] == "no_unwaived_inconclusive"), 0)
    assert left == unwaived - 1


def test_report_round_two_waivers_sequences_and_criteria(base, repo):
    """#99: waived is first-class (open vs waived; accepted-by, owner and
    retest trigger as columns), sequences reported per criterion wherever
    they ran, and an acceptance-criteria section built from the data."""
    p, run = ti._completed_run(base)
    inc = next(r for r in repo.get_results(run["id"]) if r["status"] == "inconclusive")
    issue = requests.post(f"{base}/projects/{p['id']}/issues", headers=A,
                          json={"from_result": {"run_id": run["id"], "check_id": inc["check_id"], "subject": inc["subject"]}}).json()
    risk = requests.post(f"{base}/issues/{issue['id']}/risk", headers=ti.ADMIN,
                         json={"reason": "read-only controller", "owner": "Controls Co",
                               "retest_trigger": "when writes are enabled",
                               "review_by": (fx.NOW.date() + timedelta(days=60)).isoformat()})
    assert risk.status_code == 201 and risk.json()["owner"] == "Controls Co"
    # a start-up procedure carrying an S3-style availability criterion
    proc = requests.post(f"{base}/projects/{p['id']}/procedures", headers=A, json={
        "name": "AHU start-up + S3", "kind": "startup", "equip_class": "Air_Handling_Unit",
        "steps": [{"id": "s3", "title": "S3 availability",
                   "criteria": [{"availability": {"min_pct": 95, "max_gap_minutes": 10},
                                 "point_class": "Supply_Air_Temperature_Sensor"}]}]}).json()
    rep = requests.get(f"{base}/projects/{p['id']}/report", headers=A).json()
    assert rep["issue_counts"]["waived"] == 1
    assert rep["inconclusive"]["waived"] >= 1
    [sq] = [s for s in rep["sequences"] if s["procedure_id"] == proc["id"]]
    assert sq["step"] == "S3 availability" and sq["status"] == "not_evaluated" and "never been run" in sq["reason"]
    kinds = {x["kind"] for x in rep["limitations"]}
    assert "sequence_not_evaluated" in kinds and "no_sequences" not in kinds
    assert any("waived" in x["text"] for x in rep["limitations"] if x["kind"] == "inconclusive")
    page = requests.get(f"{base}/projects/{p['id']}/report", params={"format": "html"}, headers=A).text
    for want in ("Accepted by", "Owner / responsible", "Retest trigger", "Controls Co", "when writes are enabled",
                 "Acceptance criteria and results", "available at least 95%", "Sequences of operation", "UTC",
                 'id="waivers"', f'id="issue-{issue["id"]}"', "waived (risk accepted)"):
        assert want in page, want


def test_report_review_100_rules_as_judged_waived_badges_and_acceptance(base, repo):
    """#100 review: (1) rules print as the run judged them, and an edit after
    the run is flagged; (2) acceptance availability is data; (3) waived
    results carry a text badge and a per-equipment count; (4) accepted-by is
    a name labelled CxA, owner and retest trigger are required; explicit
    sequence tags; thead/scope."""
    p, run = ti._completed_run(base)
    assert requests.patch(f"{base}/projects/{p['id']}", headers=A, json={"settings": {
        "cxa_name": "Jacob Nash", "acceptance": {"min_pct": 90, "max_gap_minutes": 10, "settle_minutes": 30}}}).status_code == 200
    inc = next(r for r in repo.get_results(run["id"]) if r["status"] == "inconclusive")
    issue = requests.post(f"{base}/projects/{p['id']}/issues", headers=A,
                          json={"from_result": {"run_id": run["id"], "check_id": inc["check_id"], "subject": inc["subject"]}}).json()
    review = (fx.NOW.date() + timedelta(days=60)).isoformat()
    bad = requests.post(f"{base}/issues/{issue['id']}/risk", headers=ti.ADMIN, json={"reason": "x", "review_by": review})
    assert bad.status_code == 400 and "owner" in bad.json()["error"]
    assert requests.post(f"{base}/issues/{issue['id']}/risk", headers=ti.ADMIN, json={
        "reason": "read-only", "owner": "Controls Co", "retest_trigger": "writes enabled", "accepted_name": "Jacob Nash",
        "review_by": review}).status_code == 201
    # a plain range explicitly tagged S2 counts as a sequence
    proc = requests.post(f"{base}/projects/{p['id']}/procedures", headers=A, json={
        "name": "AHU seq", "kind": "startup", "equip_class": "Air_Handling_Unit",
        "steps": [{"id": "s2", "title": "Supply air band",
                   "criteria": [{"range": {"min": 40, "max": 80, "unit": "F"}, "point_class": "Discharge_Air_Temperature_Sensor",
                                 "sequence": "S2"}]}]}).json()
    tr = requests.post(f"{base}/procedures/{proc['id']}/runs", headers=A,
                       json={"started_at": (fx.NOW - timedelta(hours=2)).isoformat(), "hours": 2})
    assert tr.status_code in (200, 201, 202), tr.text
    for _ in range(100):
        if repo.list_test_runs(proc["id"])[0]["status"] in ("completed", "failed"):
            break
        import time as _t
        _t.sleep(0.05)
    # edit the threshold after the run
    assert requests.patch(f"{base}/procedures/{proc['id']}", headers=A, json={"steps": [{"id": "s2", "title": "Supply air band",
        "criteria": [{"range": {"min": 50, "max": 60, "unit": "F"}, "point_class": "Discharge_Air_Temperature_Sensor",
                      "sequence": "S2"}]}]}).status_code == 200
    rep = requests.get(f"{base}/projects/{p['id']}/report", headers=A).json()
    [sq] = [s for s in rep["sequences"] if s["procedure_id"] == proc["id"]]
    assert sq["sequence"] == "S2" and sq["procedure_edited"] is True and "40..80" in sq["rule"]  # the rule the run judged
    assert "no_sequences" not in {x["kind"] for x in rep["limitations"]}
    assert "procedure_edited" in {x["kind"] for x in rep["limitations"]}
    acc = rep["acceptance"]
    assert acc["min_pct"] == 90 and acc["settle_minutes"] == 30 and acc["points"]
    assert all({"samples", "expected", "availability_pct", "pass"} <= set(pt) for pt in acc["points"])
    assert rep["issue_counts"]["total"] == sum(rep["issue_counts"][k] for k in ("open", "waived", "closed", "other"))
    waived_eq = [e for e in rep["latest_run"]["equipment"] if e["waived"]]
    assert waived_eq and any(r["waived_by"] == issue["id"] for e in waived_eq for r in e["results"])
    page = requests.get(f"{base}/projects/{p['id']}/report", params={"format": "html"}, headers=A).text
    for want in ("procedure edited after this run", "Per-point availability", "settle 30 min", ">waived<",
                 f'href="#issue-{issue["id"]}"', "Jacob Nash, CxA", "via ", "<thead>", 'scope="col"', "<caption>"):
        assert want in page, want


def test_report_says_when_equipment_names_are_unavailable(base, monkeypatch):
    p, _run = ti._completed_run(base)
    from baler.commissioning import inventory

    def boom(*a, **k):
        raise RuntimeError("store down")

    monkeypatch.setattr(inventory, "build", boom)
    rep = requests.get(f"{base}/projects/{p['id']}/report", headers=A).json()
    assert rep["equipment_names"] is False and "equipment_names_unavailable" in {x["kind"] for x in rep["limitations"]}


# ------------------------------------------------- Cx report fixes (#118-#125)

_RANGE_40_80 = {"range": {"min": 40, "max": 80, "unit": "F"}, "point_class": "Zone_Air_Temperature_Sensor"}
_RANGE_50_60 = {"range": {"min": 50, "max": 60, "unit": "F"}, "point_class": "Zone_Air_Temperature_Sensor"}
_EXISTS = {"check": "exists"}


def _tr(steps, verdicts, embed=True, snapshot=True):
    """A completed test run: `steps` is the procedure as run (snapshot),
    `verdicts` {step_id: [status per criterion]} for one equipment."""
    judged = [{"id": sid, "title": sid, "status": "pass",
               "criteria": [{**({"criterion": next(st for st in steps if st["id"] == sid)["criteria"][i]} if embed else {}),
                             "status": s, "counts": {}, "failing": [], "observed": []} for i, s in enumerate(ss)]}
              for sid, ss in verdicts.items()]
    return {"id": "tr1", "status": "completed", "started_at": "2026-11-06T10:00:00+00:00",
            "window_end": "2026-11-06T12:00:00+00:00", "procedure": {"steps": steps} if snapshot else {},
            "equipment": [{"equip_uri": fx.VAV, "steps": judged}], "summary": {"by_status": {}}}


def test_118_report_prints_the_rule_the_run_judged_not_the_current_procedure():
    """Edit a threshold and insert a criterion ahead of it after the run:
    the report still prints 40..80 beside that verdict (never 50..60, never
    the inserted rule's text), from the snapshot the run stored, and flags
    the edit - even with no rule embedded in the verdicts."""
    as_run = [{"id": "s1", "title": "Zone band", "criteria": [_RANGE_40_80, _EXISTS]}]
    now = [{"id": "s1", "title": "Zone band", "criteria": [{"check": "live"}, _RANGE_50_60, _EXISTS]}]
    tr = _tr(as_run, {"s1": ["fail", "pass"]}, embed=False)
    rows, edited = report.recorded_criteria({"steps": now}, tr)
    assert edited is True
    assert [(report._rule_text(r["criterion"]), r["judged"][0]["status"]) for r in rows] == [
        ("Zone_Air_Temperature_Sensor within 40..80 F", "fail"), ("check exists", "pass")]
    assert all(r["recorded"] for r in rows)
    page = report._criteria_html([{"name": "VAV FPT", "steps": now, "test_run": tr}])
    assert "40..80" in page and "50..60" not in page and "check live" not in page
    assert "procedure edited after this run" in page
    # unchanged procedure: not flagged
    assert report.recorded_criteria({"steps": as_run}, tr)[1] is False


def test_118_an_old_run_without_recorded_rules_still_renders_flagged():
    """A run from before rules were stored (no snapshot, no rule on the
    verdict) renders with today's rule, marked as not recorded - never
    "check None", never claimed as the rule the run judged."""
    steps = [{"id": "s1", "title": "Zone band", "criteria": [_RANGE_40_80]}]
    tr = _tr(steps, {"s1": ["pass"]}, embed=False, snapshot=False)
    rows, edited = report.recorded_criteria({"steps": steps}, tr)
    assert [r["recorded"] for r in rows] == [False] and rows[0]["criterion"] == _RANGE_40_80 and edited is False
    page = report._criteria_html([{"name": "VAV FPT", "steps": steps, "test_run": tr}])
    assert "rule not recorded with this run" in page and "check None" not in page
    proc = {"id": "p1", "name": "VAV FPT", "status": "active", "steps": steps}
    kinds = {x["kind"] for x in report.limitations(None, [], [proc], {"p1": tr}, [], True)}
    assert "rule_unrecorded" in kinds
    # no snapshot but each verdict carries its rule: that rule, recorded
    tr2 = _tr(steps, {"s1": ["pass"]}, embed=True, snapshot=False)
    rows2, edited2 = report.recorded_criteria({"steps": [{"id": "s1", "criteria": [_RANGE_50_60]}]}, tr2)
    assert rows2[0]["recorded"] and rows2[0]["criterion"] == _RANGE_40_80 and edited2 is True


def test_118_live_report_after_editing_a_procedure(base, repo):
    """Acceptance: run a procedure, change a threshold, regenerate the
    report: the original rule prints and the edit is flagged."""
    p, _run = ti._completed_run(base)
    steps = [{"id": "band", "title": "Zone band", "criteria": [_RANGE_40_80]}]
    proc = requests.post(f"{base}/projects/{p['id']}/procedures", headers=A, json={
        "name": "VAV band", "kind": "functional", "equip_class": "Variable_Air_Volume_Box", "steps": steps}).json()
    assert requests.post(f"{base}/procedures/{proc['id']}/runs", headers=A,
                         json={"started_at": (fx.NOW - timedelta(hours=2)).isoformat(), "hours": 2}).status_code in (200, 201, 202)
    _wait_test_run(repo, proc["id"])
    assert requests.patch(f"{base}/procedures/{proc['id']}", headers=A, json={
        "steps": [{"id": "band", "title": "Zone band", "criteria": [_RANGE_50_60]}]}).status_code == 200
    page = requests.get(f"{base}/projects/{p['id']}/report", params={"format": "html"}, headers=A).text
    criteria = page.split("<h3>Procedure criteria</h3>", 1)[1].split("<h2>", 1)[0]
    assert "40..80" in criteria and "50..60" not in criteria and "procedure edited after this run" in criteria
    lim = requests.get(f"{base}/projects/{p['id']}/report", headers=A).json()["limitations"]
    assert any(x["kind"] == "procedure_edited" and "VAV band" in x["text"] for x in lim)


def _wait_test_run(repo, proc_id):
    import time as _t
    for _ in range(200):
        runs = repo.list_test_runs(proc_id)
        if runs and runs[0]["status"] in ("completed", "failed"):
            assert runs[0]["status"] == "completed", runs[0].get("error")
            return runs[0]
        _t.sleep(0.05)
    raise AssertionError("test run did not finish")


def test_119_acceptance_availability_is_data_with_window_settle_and_counts():
    """Window, settle, threshold and samples/expected per point - passing
    points too - and a point reporting every max_gap reads exactly 100%."""
    start = datetime(2026, 11, 6, 13, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=40)
    judged_from = start + timedelta(minutes=3)
    every = [judged_from + timedelta(minutes=i) for i in range(37)]
    series = {"urn:point:full": [(t, 1.0) for t in every],
              "urn:point:zone": [(t, 1.0) for i, t in enumerate(every) if i != 20]}
    points = {"urn:point:full": {"dis": "full", "equip_uri": "urn:equip:e"},
              "urn:point:zone": {"dis": "bacnet/zone_temp", "equip_uri": "urn:equip:e"}}
    from baler.commissioning.history import InMemoryHistory

    measured = InMemoryHistory(series).availability(list(points), judged_from, end, timedelta(minutes=1))
    acc = report.acceptance_availability(measured, points, start, end, 3, 95, 1)
    by = {r["dis"]: r for r in acc["points"]}
    assert (by["full"]["samples"], by["full"]["expected"], by["full"]["availability_pct"]) == (37, 37, 100.0)
    assert (by["bacnet/zone_temp"]["samples"], by["bacnet/zone_temp"]["expected"]) == (36, 37)
    assert by["bacnet/zone_temp"]["pass"] is True and 97 <= by["bacnet/zone_temp"]["availability_pct"] < 100
    html = report._acceptance_html(acc, "run12345")
    for want in ("36 / 37", "37 / 37", "settle 3 min", "2026-11-06 13:00 UTC", "2026-11-06 13:40 UTC", "40 min",
                 "at least 95%", "2 of 2 point(s) meet it"):
        assert want in html, want


def test_119_equipment_verdicts_carry_the_acceptance_window_and_availability(base):
    p, _run = ti._completed_run(base)
    requests.patch(f"{base}/projects/{p['id']}", headers=A, json={"settings": {
        "acceptance": {"min_pct": 95, "max_gap_minutes": 5, "settle_minutes": 0.5}}})
    rep = requests.get(f"{base}/projects/{p['id']}/report", headers=A).json()
    assert all(pt["equip_uri"] for pt in rep["acceptance"]["points"])
    page = report.project_html(rep)
    eq = page.split("<h2>Equipment verdicts (latest verification run)</h2>", 1)[1].split("<h2>", 1)[0]
    assert "<th scope=\"col\">Availability</th>" in eq and "settle" in eq and "point(s) at least 95%" in eq


def test_120_waived_results_are_badged_and_linked_where_they_appear(base, repo):
    """The run report and the project report: each waived result carries a
    'waived' badge linking to its issue row (which exists in the same page),
    each equipment its waived count - and it still reads inconclusive."""
    p, run = ti._completed_run(base)
    inc = next(r for r in repo.get_results(run["id"]) if r["status"] == "inconclusive")
    issue = requests.post(f"{base}/projects/{p['id']}/issues", headers=A,
                          json={"from_result": {"run_id": run["id"], "check_id": inc["check_id"], "subject": inc["subject"]}}).json()
    assert requests.post(f"{base}/issues/{issue['id']}/risk", headers=ti.ADMIN, json={
        "reason": "read-only", "owner": "Controls Co", "retest_trigger": "writes enabled",
        "review_by": (fx.NOW.date() + timedelta(days=30)).isoformat()}).status_code == 201
    for page in (requests.get(f"{base}/runs/{run['id']}/report", params={"format": "html"}, headers=A).text,
                 requests.get(f"{base}/projects/{p['id']}/report", params={"format": "html"}, headers=A).text):
        badge = f'<a class="s waived" href="#issue-{issue["id"]}">waived</a>'
        assert badge in page and f'id="issue-{issue["id"]}"' in page
        row = next(r for r in page.split("<tr>") if badge in r)
        assert 'class="s inconclusive">inconclusive<' in row and ">pass<" not in row  # waived never reads as a pass
        assert "1 waived</span>" in page


def test_121_waiver_names_the_accepter_from_their_account():
    """risk_extras: owner and retest trigger required; the account's display
    name wins over a typed one (nobody signs as someone else); with no
    display name a typed name is required and kept as typed."""
    body = {"owner": "Controls Co", "retest_trigger": "writes enabled", "accepted_name": "Somebody Else"}
    with pytest.raises(ValueError, match="owner"):
        issues.risk_extras({**body, "owner": " "}, "Jacob Nash")
    with pytest.raises(ValueError, match="retest_trigger"):
        issues.risk_extras({**body, "retest_trigger": ""}, "Jacob Nash")
    assert issues.risk_extras(body, "Jacob Nash") == {"owner": "Controls Co", "retest_trigger": "writes enabled",
                                                      "accepted_name": "Jacob Nash", "accepted_name_source": "account"}
    assert issues.risk_extras(body, None)["accepted_name_source"] == "typed"
    with pytest.raises(ValueError, match="accepted_name"):
        issues.risk_extras({k: v for k, v in body.items() if k != "accepted_name"}, None)


def test_121_cxa_label_only_for_a_whole_name_from_the_account():
    assert report.is_cxa("Jacob Nash", "Jacob Nash, PE (Nash Cx)") and report.is_cxa("jacob  nash", "Jacob Nash")
    assert report.is_cxa("Jacob Nash", "Jacob Nash (Nash Cx)")
    assert not report.is_cxa("Jacob", "Jacob Nash") and not report.is_cxa("J", "Jacob Nash")
    assert not report.is_cxa("Jacob Nashville", "Jacob Nash") and not report.is_cxa("Jacob Nash", None)
    acct = {"accepted_name": "Jacob Nash", "accepted_name_source": "account", "accepted_email": "demo@example.com"}
    assert report._accepted_by(acct, "Jacob Nash, PE") == "Jacob Nash, CxA<br><span class=muted>via demo@example.com</span>"
    typed = {**acct, "accepted_name_source": "typed"}
    assert ", CxA" not in report._accepted_by(typed, "Jacob Nash") and "(name as typed)" in report._accepted_by(typed, "Jacob Nash")
    legacy = {"accepted_name": "Jacob Nash", "accepted_email": "demo@example.com"}  # #100-era: no source
    assert ", CxA" not in report._accepted_by(legacy, "Jacob Nash")
    assert report._accepted_by({"accepted_email": "demo@example.com"}, "Jacob Nash") == "demo@example.com"  # pre-#100


def test_121_api_requires_owner_and_trigger_and_report_names_the_accepter(base, repo):
    p, run = ti._completed_run(base)
    requests.patch(f"{base}/projects/{p['id']}", headers=A, json={"settings": {"cxa_name": "Jacob Nash, PE"}})
    inc = [r for r in repo.get_results(run["id"]) if r["status"] == "inconclusive"]
    ids = [requests.post(f"{base}/projects/{p['id']}/issues", headers=A, json={"from_result": {
        "run_id": run["id"], "check_id": r["check_id"], "subject": r["subject"]}}).json()["id"] for r in inc[:2]]
    review = (fx.NOW.date() + timedelta(days=30)).isoformat()
    for missing in ("owner", "retest_trigger"):
        body = {"reason": "x", "owner": "Controls Co", "retest_trigger": "writes enabled", "review_by": review}
        body[missing] = ""
        bad = requests.post(f"{base}/issues/{ids[0]}/risk", headers=ti.ADMIN, json=body)
        assert bad.status_code == 400 and missing in bad.json()["error"]
    ok = requests.post(f"{base}/issues/{ids[0]}/risk", headers=ti.ADMIN, json={
        "reason": "x", "owner": "Controls Co", "retest_trigger": "writes enabled", "review_by": review,
        "accepted_name": "Not Me"})
    assert ok.status_code == 201 and ok.json()["accepted_name"] == "Jacob Nash" and ok.json()["accepted_name_source"] == "account"
    # an account with no display name must type one, and it isn't the CxA
    repo.names.clear()
    assert requests.post(f"{base}/issues/{ids[1]}/risk", headers=ti.ADMIN, json={
        "reason": "x", "owner": "Controls Co", "retest_trigger": "writes enabled", "review_by": review}).status_code == 400
    assert requests.post(f"{base}/issues/{ids[1]}/risk", headers=ti.ADMIN, json={
        "reason": "x", "owner": "Controls Co", "retest_trigger": "writes enabled", "review_by": review,
        "accepted_name": "Jacob Nash"}).status_code == 201
    page = requests.get(f"{base}/projects/{p['id']}/report", params={"format": "html"}, headers=A).text
    waivers = page.split('id="waivers"', 1)[1]
    assert waivers.count("Jacob Nash, CxA") == 1 and "(name as typed)" in waivers and "via a@example.com" in waivers
    assert "<td>-</td>" not in waivers


def test_122_a_step_labelled_s2_is_a_sequence_without_hours_or_a_tag():
    """A plain range (no hours, no tag) whose step the integrator titled S2
    appears in the Sequences table, tagged S2; an ordinary plain range
    doesn't."""
    steps = [{"id": "s2", "title": "S2: zone within 65-80 F", "criteria": [
                {"range": {"min": 65, "max": 80, "unit": "F"}, "point_class": "Zone_Air_Temperature_Sensor"}]},
             {"id": "plaus", "title": "Plausible", "criteria": [_RANGE_40_80]},
             {"id": "tagged", "title": "Zone band", "criteria": [{**_RANGE_40_80, "sequence": "S3"}]}]
    proc = {"id": "p1", "name": "VAV seq", "status": "active", "steps": steps}
    seqs = report.sequences([proc], {"p1": None})
    assert [(s["sequence"], s["step"]) for s in seqs] == [("S2", "S2: zone within 65-80 F"), ("S3", "Zone band")]
    kinds = {x["kind"] for x in report.limitations(None, [], [proc], {"p1": None}, [], True)}
    assert "no_sequences" not in kinds
    assert report.sequence_tag({}, "S12 - occupied") == "S12" and report.sequence_tag({}, "SAT check") is None
    assert report.sequence_tag({}, "S2.1 setup") is None


def test_123_equipment_names_do_not_depend_on_who_generates_the_report(base, repo, monkeypatch):
    """Names come from the project's own scope: a caller whose scope is
    narrower than the project's site gets the same names as an admin, and
    their results stay filtered to their scope."""
    p, _run = ti._completed_run(base)
    narrow = ti.api._h(sites=["site-a"], sub="u-narrow")
    real = repo._scope_fn
    monkeypatch.setattr(repo, "_scope_fn", lambda c: db.Scope(("fbf/a/vav-1",)) if c.get("sub") == "u-narrow" else real(c))
    from baler.commissioning import inventory
    real_build, seen = inventory.build, []
    monkeypatch.setattr(inventory, "build", lambda store, scopes: seen.append(scopes) or real_build(store, scopes))
    admin = requests.get(f"{base}/projects/{p['id']}/report", headers=A).json()
    seen.clear()
    mine = requests.get(f"{base}/projects/{p['id']}/report", headers=narrow).json()
    # the names were read in the project's scope, which reaches the AHU the caller can't
    assert seen and any(all(sc.allows(fx.AHU) for sc in scopes) for scopes in seen)
    assert mine["equipment"] == {fx.VAV: admin["equipment"][fx.VAV]}  # the same names, only what they may read
    assert admin["equipment"][fx.AHU]["dis"]
    assert {e["equip_uri"] for e in mine["latest_run"]["equipment"]} == {fx.VAV}  # results still the caller's
    assert {pt["equip_uri"] for pt in mine["acceptance"]["points"]} == {fx.VAV}


def test_123_equipment_missing_from_the_inventory_is_stated(base, monkeypatch):
    p, _run = ti._completed_run(base)
    from baler.commissioning import inventory
    real = inventory.build
    monkeypatch.setattr(inventory, "build", lambda store, scopes: [e for e in real(store, scopes) if e["id"] != fx.MTR])
    rep = requests.get(f"{base}/projects/{p['id']}/report", headers=A).json()
    lim = next(x for x in rep["limitations"] if x["kind"] == "equipment_names_missing")
    assert "fbf/a/mtr-1" in lim["text"]


def test_125_timestamps_naive_or_aware_are_utc_and_durations_work():
    naive, aware = "2026-11-06T10:00:00", "2026-11-06T12:30:00+02:00"
    assert report._dt(naive).tzinfo is not None and report._when(naive) == "2026-11-06 10:00 UTC"
    assert report._when(aware) == "2026-11-06 10:30 UTC"
    assert report._duration(naive, aware) == "30 min"  # mixing them used to raise TypeError


def test_125_issue_counts_sum_to_the_total():
    rep = report.project_build({"id": "p", "name": "P"}, fx.NOW, None, None, [], [], {},
                               [{"id": "1", "status": "open"}, {"id": "2", "status": "closed"},
                                {"id": "3", "status": "risk_accepted"}, {"id": "4", "status": "imported_weird"}],
                               [], None, [], [], True)
    c = rep["issue_counts"]
    assert c == {"open": 1, "waived": 1, "closed": 1, "other": 1, "total": 4}
    assert "1 other (of 4)" in report.project_html(rep)


def test_125_print_layout_tables_and_links(base, repo):
    """Rows (not whole tables) avoid page breaks and the sign-off stays
    together; every table has a caption, a repeating <thead> and th
    scope; trend links are absolute to the given origin (a root-relative
    link means nothing in the UI's blob: page or a saved file), run ids
    aren't links that 401, and a bad origin is refused."""
    p, run = ti._completed_run(base)
    origin = "https://baler.example.com"
    for path in (f"/runs/{run['id']}/report", f"/projects/{p['id']}/report"):
        page = requests.get(f"{base}{path}", params={"format": "html", "origin": origin}, headers=A).text
        css = page.split("<style>", 1)[1].split("</style>", 1)[0]
        assert "tr, .signoff { break-inside: avoid; }" in css and "table { break-inside" not in css
        assert page.count("<table") == page.count("<caption>") == page.count("<thead>"), path
        assert "<th>" not in page and 'scope="col"' in page
        hrefs = [h.split('"', 1)[0] for h in page.split('href="')[1:]]
        assert hrefs and all(h.startswith(("#", f"{origin}/ui/his.html?")) for h in hrefs), hrefs
        assert any(h.startswith(origin) for h in hrefs)
    assert requests.get(f"{base}/projects/{p['id']}/report", params={"format": "html", "origin": "javascript:alert(1)"},
                        headers=A).status_code == 400


def test_ui_paths_for_inconclusive_issues_341(base, repo):
    """#341: the calls the Cx UI now makes. The bulk action opens issues for
    inconclusive results only when include_inconclusive is sent (the UI's
    checkbox, off by default); waiving every one meets the acceptance
    gate's no_unwaived_inconclusive rule, and revoking a waiver unmeets it."""
    p, run = ti._completed_run(base)
    results = repo.get_results(run["id"])
    inconclusive = {(r["check_id"], r["subject"]) for r in results if r["status"] == "inconclusive"}
    failing = {(r["check_id"], r["subject"]) for r in results if r["status"] == "fail"}
    assert inconclusive and failing

    def tracked():
        got = requests.get(f"{base}/projects/{p['id']}/issues", headers=A).json()
        return {(i["check_id"], i["subject"]): i for i in got if i.get("check_id")}

    def unwaived():
        gate = requests.get(f"{base}/projects/{p['id']}/gate", params={"to": "acceptance"}, headers=A).json()["gate"]
        return next((u["count"] for u in gate["unmet"] if u["rule"] == "no_unwaived_inconclusive"), 0)

    # checkbox unchecked: the UI sends {} and only failures get issues
    plain = requests.post(f"{base}/runs/{run['id']}/issues", headers=A, json={}).json()
    assert len(plain["opened"]) == len(failing) and set(tracked()) == failing
    assert unwaived() == len(inconclusive)
    # checkbox checked: the inconclusive ones too, failures already tracked
    more = requests.post(f"{base}/runs/{run['id']}/issues", headers=A, json={"include_inconclusive": True}).json()
    assert len(more["opened"]) == len(inconclusive) and more["already_tracked"] == len(failing)
    issues = tracked()
    assert set(issues) == failing | inconclusive

    review = (fx.NOW.date() + timedelta(days=90)).isoformat()
    risks = []
    for key in inconclusive:
        r = requests.post(f"{base}/issues/{issues[key]['id']}/risk", headers=ti.ADMIN, json={
            "reason": "read-only controller", "owner": "Controls Co", "retest_trigger": "writes enabled", "review_by": review})
        assert r.status_code == 201, r.text
        risks.append(r.json())
    assert unwaived() == 0  # every inconclusive waived: the rule is met

    assert requests.delete(f"{base}/risks/{risks[0]['id']}", headers=ti.ADMIN).status_code == 204
    assert unwaived() == 1  # revoked: unmet again
    assert requests.get(f"{base}/issues/{risks[0]['issue_id']}", headers=A).json()["status"] == "open"

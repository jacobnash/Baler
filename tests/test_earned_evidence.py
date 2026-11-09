"""Earned evidence (#218): schedule activities linked to evidence from
BACnet data, Cx tests and a person's sign-off, and the report."""

from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone

import cx_api_harness as hx
import cx_fixtures as fx
import pytest
import requests

from baler.commissioning import earned_evidence

AS_OF = datetime(2027, 2, 15, 12, 0, tzinfo=timezone.utc)
XER = os.path.join(os.path.dirname(__file__), "fixtures", "schedules", "sample.xer")


def test_params_are_checked():
    with pytest.raises(ValueError):
        earned_evidence.check_params("vibes", {})
    with pytest.raises(ValueError):
        earned_evidence.check_params("points_online", {"equip_uris": []})
    assert earned_evidence.check_params("form_signed", {"title": " TAB report "}) == {"title": "TAB report"}


@pytest.fixture
def api(monkeypatch):
    hist = fx.history()
    hist.add(fx.pt(fx.VAV, "ZNT"), AS_OF - timedelta(hours=1), 72.0)  # one of VAV-1's two points is reporting
    repo = hx.repo(history=hist)
    base, server = hx.serve(monkeypatch, repo)
    yield base, repo
    server.shutdown()


def _link(base, pid, code, kind, params):
    r = requests.post(f"{base}/projects/{pid}/schedule-links", json={"activity_code": code, "kind": kind, "params": params},
                      headers=hx.A)
    return hx.check(r, "/projects/{id}/schedule-links", "post", 201)


def test_earned_evidence_report(api):
    base, repo = api
    pid = hx.project(base)["id"]
    with open(XER, "rb") as f:
        sched = requests.post(f"{base}/projects/{pid}/construction-schedules", json={"data": base64.b64encode(f.read()).decode()},
                              headers=hx.A).json()
    proc = requests.post(f"{base}/projects/{pid}/procedures", json={"template": "ahu_startup", "status": "active"},
                         headers=hx.A).json()

    # proposals, never applied: "AHU-1 start-up" names AHU-1 and start-up
    sugg = hx.check(requests.get(f"{base}/construction-schedules/{sched['id']}/link-suggestions", headers=hx.A),
                    "/construction-schedules/{id}/link-suggestions", "get", 200)
    assert [(s["activity_code"], s["kind"], s["params"]["procedure_id"]) for s in sugg] == [("M-1000", "procedure_passed", proc["id"])]
    assert requests.get(f"{base}/projects/{pid}/schedule-links", headers=hx.A).json() == []

    s = sugg[0]
    _link(base, pid, s["activity_code"], s["kind"], s["params"])
    _link(base, pid, "M-1010", "points_online", {"equip_uris": [fx.VAV], "min_pct": 95, "within_hours": 24})
    tab = _link(base, pid, "M-1020", "form_signed", {"title": "TAB report linked, Level 3"})

    url = f"{base}/construction-schedules/{sched['id']}/earned-evidence?as_of={AS_OF.isoformat().replace('+', '%2B')}"
    rep = hx.check(requests.get(url, headers=hx.A), "/construction-schedules/{id}/earned-evidence", "get", 200)
    assert rep["counts"] == {"done_by_data": 0, "due_not_evidenced": 2, "marked_done_no_evidence": 1,
                             "ahead_of_schedule_status": 0, "not_linked": 0}
    assert [r["code"] for r in rep["marked_done_no_evidence"]] == ["M-1000"]  # schedule says done, no test passed
    vav = next(r for r in rep["due_not_evidenced"] if r["code"] == "M-1010")
    assert "1 of 2 points" in vav["evidence"][0]["detail"]

    # the evidence arrives: the start-up test passes, the TAB report is signed off
    repo.create_test_run({"id": "tr1", "procedure_id": proc["id"], "project_id": pid, "status": "completed",
                          "requested_at": AS_OF - timedelta(days=12), "finished_at": AS_OF - timedelta(days=11),
                          "equipment": [{"equip_uri": fx.AHU, "status": "pass", "steps": []}]})
    hx.check(requests.post(f"{base}/schedule-links/{tab['id']}/signoff", json={"document": {"title": "TAB-L3 rev 2"}},
                           headers=hx.A), "/schedule-links/{id}/signoff", "post", 201)
    rep = requests.get(url, headers=hx.A).json()
    done = {r["code"]: r for r in rep["done_by_data"]}
    assert set(done) == {"M-1000", "M-1020"}
    assert [r["code"] for r in rep["ahead_of_schedule_status"]] == ["M-1020"]  # signed off, schedule says not started
    assert "a@example.com" in done["M-1020"]["evidence"][0]["detail"]
    assert rep["counts"]["due_not_evidenced"] == 1 and rep["counts"]["marked_done_no_evidence"] == 0

    assert requests.get(url, headers=hx.B).status_code == 404
    r = requests.post(f"{base}/schedule-links/{rep['done_by_data'][0]['evidence'][0]['link_id']}/signoff", headers=hx.A)
    assert r.status_code == 400  # data-judged links aren't signed off by hand

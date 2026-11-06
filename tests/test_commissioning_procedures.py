"""Cx 6 (#42): start-up / power-on / functional test procedures - seeds,
validation, passive evaluation on the hospital fixture, the run-to-run
diff, the API and the scheduler. No services."""

from __future__ import annotations

import time
from datetime import timedelta

import cx_hospital as hx
import pytest
import requests
import test_commissioning_openapi as api
from conftest import assert_matches_schema, load_spec

from baler.commissioning import db, engine, inventory, procedures, runner
from baler.commissioning_api import OPENAPI_SPEC_PATH
from baler.scope import Scope

DAY = timedelta(hours=24)
SITES = {"site-h": {"id": "site-h", "org_id": "org-a", "name": "Hospital", "topic_prefixes": [hx.SITE]}}


def _equips():
    return inventory.build(hx.store(), [Scope.everything()])


def _evaluate(template, history=None, start=None, end=None):
    proc = procedures.from_seed(template)
    end = end or hx.NOW
    start = start or end - timedelta(hours=proc["window_hours"])
    hist = procedures.ClippedHistory(history or hx.history(), end)
    site = _equips()
    equips = [e for e in site if procedures.applies(proc, e)]
    _, results = engine.run(hx.store(), hist, [Scope.everything()], end, (end - start).total_seconds() / 3600,
                            equips=equips, site_equips=site)
    ids = runner._range_points(proc, site)
    series = hist.window(ids, start, end + timedelta(seconds=1))
    return procedures.evaluate(proc, equips, results, series)


def _step(per, equip, step):
    e = next(x for x in per if x["equip_uri"] == equip)
    return next(s for s in e["steps"] if s["id"] == step)


# ------------------------------------------------------------------ pure


def test_every_seed_is_valid_and_covers_the_six_classes():
    assert len(procedures.SEEDS) == 12
    classes = {s["equip_class"] for s in procedures.SEEDS}
    assert classes == {"Air_Handling_Unit", "Variable_Air_Volume_Box", "Fan_Coil_Unit", "Chiller", "Boiler", "Pump"}
    for t in procedures.SEED_BY_ID:
        p = procedures.from_seed(t)
        assert p["kind"] in ("power_on", "startup") and p["steps"] and p["template"] == t


@pytest.mark.parametrize("bad", [
    {"name": "x", "kind": "nope", "equip_class": "Boiler", "steps": [{"criteria": [{"check": "exists"}]}]},
    {"name": "x", "kind": "startup", "equip_class": "Boiler", "steps": []},
    {"name": "x", "kind": "startup", "equip_class": "Boiler", "steps": [{"criteria": []}]},
    {"name": "x", "kind": "startup", "equip_class": "Boiler", "steps": [{"criteria": [{"check": "exists", "range": {}}]}]},
    {"name": "x", "kind": "startup", "equip_class": "Boiler", "steps": [{"criteria": [{"range": {"min": 1}}]}]},
    {"name": "x", "kind": "startup", "equip_class": "Boiler", "steps": [{"criteria": [{"range": {"min": 5, "max": 1}, "point_class": "X"}]}]},
    {"name": "x", "kind": "startup", "equip_class": "Boiler", "steps": [{"criteria": [{"check": "exists", "bogus": 1}]}]},
    {"name": "x", "kind": "startup", "equip_class": "Boiler", "steps": [{"criteria": [{"check": "exists"}]}], "schedule": {"every_hours": 0}},
    {"name": "x", "kind": "startup", "equip_class": "Boiler", "steps": [{"id": "a", "criteria": [{"check": "exists"}]},
                                                                      {"id": "a", "criteria": [{"check": "live"}]}]},
])
def test_normalize_rejects(bad):
    with pytest.raises((ValueError, TypeError)):
        procedures.normalize(bad)


def test_boiler_startup_on_the_hospital():
    per = _evaluate("boiler_startup")
    assert [e["equip_uri"] for e in per] == [hx.BOILER]
    assert _step(per, hx.BOILER, "no-stale")["status"] == "pass"
    hw = _step(per, hx.BOILER, "hw-temp")
    assert hw["status"] == "pass" and hw["criteria"][0]["counts"]["pass"] == 1
    assert _step(per, hx.BOILER, "control")["status"] == "not_applicable"  # no sensor/setpoint pair on the boiler


def test_ahu_power_on_strict_and_range():
    per = _evaluate("ahu_power_on")
    assert _step(per, hx.AHU, "points-appear")["status"] == "pass"
    assert _step(per, hx.AHU, "plausible")["status"] == "pass"
    req = _step(per, hx.AHU, "required-points")
    assert req["status"] in ("pass", "fail")  # judged, not skipped (required: true)


def test_never_reported_point_fails_power_on_but_is_only_inconclusive_otherwise():
    h = hx.history()
    h.series.pop(hx.pt(hx.BOILER, "hw-ret-temp"))
    per = _evaluate("boiler_power_on", h)
    appear = _step(per, hx.BOILER, "points-appear")
    assert appear["status"] == "fail" and appear["criteria"][0]["failing"][0]["subject"] == hx.pt(hx.BOILER, "hw-ret-temp")
    stale = _step(_evaluate("boiler_startup", h), hx.BOILER, "no-stale")
    assert stale["status"] == "inconclusive"


def test_out_of_range_value_fails_plausibility():
    h = hx.history()
    h.add(hx.pt(hx.BOILER, "hw-sup-temp"), hx.NOW - timedelta(minutes=1), -40.0)
    hw = _step(_evaluate("boiler_startup", h), hx.BOILER, "hw-temp")
    assert hw["status"] == "fail" and "-40" in hw["criteria"][0]["failing"][0]["symptom"]


def test_a_past_window_is_judged_on_what_was_known_then():
    """ClippedHistory: points that only appear after the window don't count."""
    h = hx.history()
    late = hx.pt(hx.BOILER, "hw-ret-temp")
    h.series[late] = [(t, v) for t, v in h.series[late] if t > hx.NOW - timedelta(hours=2)]
    end = hx.NOW - timedelta(hours=6)
    per = _evaluate("boiler_power_on", h, start=end - timedelta(hours=2), end=end)
    assert _step(per, hx.BOILER, "points-appear")["status"] == "fail"


def test_diff_states():
    prev = [{"equip_uri": "e1", "steps": [{"id": "a", "status": "fail"}, {"id": "b", "status": "pass"},
                                          {"id": "c", "status": "fail"}, {"id": "d", "status": "pass"}]}]
    cur = [{"equip_uri": "e1", "steps": [{"id": "a", "status": "pass"}, {"id": "b", "status": "fail"},
                                         {"id": "c", "status": "fail"}, {"id": "e", "status": "pass"}]}]
    d = procedures.diff(prev, cur)
    assert d["counts"] == {"resolved": 1, "regressed": 1, "still_failing": 1, "gone": 1, "new": 1}
    assert procedures.diff(None, cur)["counts"] == {"new": 4}


def test_diff_results_for_runs():
    a = [{"equip_uri": "e", "check_id": "exists", "subject": "p1", "status": "fail", "symptom": "s"},
         {"equip_uri": "e", "check_id": "exists", "subject": "p2", "status": "pass"}]
    b = [{"equip_uri": "e", "check_id": "exists", "subject": "p1", "status": "pass"},
         {"equip_uri": "e", "check_id": "exists", "subject": "p2", "status": "fail", "symptom": "gone quiet"}]
    d = procedures.diff_results(a, b)
    assert {(c["subject"], c["state"]) for c in d["changes"]} == {("p1", "resolved"), ("p2", "regressed")}
    assert next(c for c in d["changes"] if c["subject"] == "p2")["symptom"] == "gone quiet"


# ------------------------------------------------------------------- api


@pytest.fixture(scope="module")
def spec():
    return load_spec(OPENAPI_SPEC_PATH)


@pytest.fixture
def repo():
    def scope(claims):
        return Scope((hx.SITE,)) if claims and claims.get("org_id") == "org-a" else Scope.nothing()
    return db.MemoryRepo(store=hx.store(), history=hx.history(), sites=SITES, scope_fn=scope, users={"u-a": "a@example.com"})


@pytest.fixture
def base(monkeypatch, repo):
    server = api._serve(monkeypatch, repo)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _wait_test(base, tr_id):
    deadline = time.monotonic() + 15
    while True:
        got = requests.get(f"{base}/test-runs/{tr_id}", headers=api.A)
        assert got.status_code == 200, got.text
        if got.json()["status"] in ("completed", "failed"):
            return got.json()
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_procedure_api_round_trip(base, spec, repo):
    tpls = requests.get(f"{base}/procedure-templates", headers=api.A)
    assert_matches_schema(tpls.json(), api._schema(spec, "/procedure-templates", "get", 200), spec)
    assert len(tpls.json()) == 12
    p = api._project(base, site_id="site-h")
    seeded = requests.post(f"{base}/projects/{p['id']}/procedures", json={"seed": "all"}, headers=api.A)
    assert seeded.status_code == 201 and len(seeded.json()) == 12
    again = requests.post(f"{base}/projects/{p['id']}/procedures", json={"seed": "all"}, headers=api.A).json()
    assert again == []  # already there
    listed = requests.get(f"{base}/projects/{p['id']}/procedures", headers=api.A)
    assert_matches_schema(listed.json(), api._schema(spec, "/projects/{id}/procedures", "get", 200), spec)
    boiler = next(x for x in listed.json() if x["template"] == "boiler_startup")

    started = (hx.NOW - timedelta(hours=20)).isoformat()
    r = requests.post(f"{base}/procedures/{boiler['id']}/runs", json={"started_at": started, "hours": 12}, headers=api.A)
    assert r.status_code == 202, r.text
    assert_matches_schema(r.json(), api._schema(spec, "/procedures/{id}/runs", "post", 202), spec)
    done = _wait_test(base, r.json()["id"])
    assert_matches_schema(done, api._schema(spec, "/test-runs/{id}", "get", 200), spec)
    assert done["status"] == "completed" and done["phase"] == "startup" and done["previous_id"] is None
    assert done["window_end"] == (hx.NOW - timedelta(hours=8)).isoformat()
    assert [e["equip_uri"] for e in done["equipment"]] == [hx.BOILER]
    assert done["diff"]["counts"].get("new")

    # break the HW temperature and run again: the diff says what regressed
    repo._history.add(hx.pt(hx.BOILER, "hw-sup-temp"), hx.NOW - timedelta(minutes=1), 400.0)
    r2 = requests.post(f"{base}/procedures/{boiler['id']}/runs", json={}, headers=api.A).json()
    d2 = _wait_test(base, r2["id"])
    assert d2["previous_id"] == done["id"]
    assert {(c["step"], c["state"]) for c in d2["diff"]["changes"]} >= {("hw-temp", "regressed")}
    runs = requests.get(f"{base}/procedures/{boiler['id']}/runs", headers=api.A).json()
    assert {x["id"] for x in runs} == {r2["id"], r.json()["id"]} and "equipment" not in runs[0]

    # validation and tenancy
    assert requests.post(f"{base}/procedures/{boiler['id']}/runs", json={"started_at": "2099-01-01T00:00:00Z"}, headers=api.A).status_code == 400
    assert requests.post(f"{base}/procedures/{boiler['id']}/runs", json={"started_at": "2026-11-06T00:00:00"}, headers=api.A).status_code == 400
    assert requests.get(f"{base}/procedures/{boiler['id']}", headers=api.B).status_code == 404
    assert requests.get(f"{base}/test-runs/{r2['id']}", headers=api.B).status_code == 404
    events = [e["action"] for e in requests.get(f"{base}/projects/{p['id']}/events", headers=api.A).json()]
    assert {"procedure.create", "test_run.create", "test_run.complete"} <= set(events)


def test_procedure_create_patch_archive(base, spec):
    p = api._project(base, site_id="site-h")
    body = {"name": "Boiler seasonal", "kind": "seasonal", "equip_class": "Boiler",
            "steps": [{"id": "hw", "title": "HW in band", "criteria": [
                {"range": {"min": 120, "max": 200, "unit": "F"}, "point": hx.pt(hx.BOILER, "hw-sup-temp")}]}]}
    r = requests.post(f"{base}/projects/{p['id']}/procedures", json=body, headers=api.A)
    assert r.status_code == 201, r.text
    assert_matches_schema(r.json(), api._schema(spec, "/projects/{id}/procedures", "post", 201), spec)
    pid = r.json()["id"]
    assert r.json()["window_hours"] == 168 and r.json()["status"] == "active"
    r = requests.patch(f"{base}/procedures/{pid}", json={"schedule": {"every_hours": 24}, "name": "Boiler weekly"}, headers=api.A)
    assert r.status_code == 200 and r.json()["schedule"] == {"every_hours": 24} and r.json()["name"] == "Boiler weekly"
    assert requests.patch(f"{base}/procedures/{pid}", json={"kind": "x"}, headers=api.A).status_code == 400
    tr = requests.post(f"{base}/procedures/{pid}/runs", json={"hours": 24}, headers=api.A).json()
    assert _wait_test(base, tr["id"])["equipment"][0]["steps"][0]["status"] == "pass"
    assert requests.delete(f"{base}/procedures/{pid}", headers=api.A).status_code == 204
    assert requests.get(f"{base}/projects/{p['id']}/procedures", headers=api.A).json() == []
    assert requests.post(f"{base}/procedures/{pid}/runs", json={}, headers=api.A).status_code == 409
    one = requests.post(f"{base}/projects/{p['id']}/procedures", json={"template": "pump_power_on", "window_hours": 1}, headers=api.A)
    assert one.status_code == 201 and one.json()["template"] == "pump_power_on" and one.json()["window_hours"] == 1


def test_run_diff_route(base, spec, repo):
    p = api._project(base, site_id="site-h")
    first = requests.post(f"{base}/projects/{p['id']}/runs", json={}, headers=api.A).json()
    api._wait(base, first["id"])
    repo._history.series.pop(hx.pt(hx.BOILER, "hw-ret-temp"))
    second = requests.post(f"{base}/projects/{p['id']}/runs", json={}, headers=api.A).json()
    api._wait(base, second["id"])
    d = requests.get(f"{base}/runs/{second['id']}/diff", headers=api.A)
    assert_matches_schema(d.json(), api._schema(spec, "/runs/{id}/diff", "get", 200), spec)
    assert d.json()["against"] == first["id"]
    assert any(c["subject"] == hx.pt(hx.BOILER, "hw-ret-temp") and c["state"] == "changed" for c in d.json()["changes"])
    assert requests.get(f"{base}/runs/{second['id']}/diff", params={"against": "nope"}, headers=api.A).status_code == 400


def test_schedule_tick_queues_due_procedures_and_projects(repo):
    now = hx.NOW
    worker = runner.RunWorker(repo, lambda: now)
    repo.sites.update(SITES)
    from baler.commissioning import phases
    repo.create_project({"id": "pj", "org_id": "org-a", "site_id": "site-h", "name": "H", "created_by": "u",
                         "settings": {"schedule": {"every_hours": 6}}, "created_at": now, "updated_at": now,
                         "phases": phases.set_current(phases.default_phases(), "warranty", now)})
    proc = {"id": "pr", "project_id": "pj", **procedures.from_seed("boiler_startup"), "created_by": "u",
            "created_at": now, "updated_at": now}
    proc["schedule"] = {"every_hours": 24}
    repo.create_procedure(proc)
    unscheduled = {**proc, "id": "pr2", "schedule": None}
    repo.create_procedure(unscheduled)
    queued = runner.schedule_tick(repo, worker, now)
    worker.join()
    assert len(queued) == 2
    tr = repo.list_test_runs("pr")[0]
    assert tr["triggered_by"] == "schedule" and tr["status"] == "completed" and tr["phase"] == "warranty"
    assert repo.list_test_runs("pr2") == []
    assert repo.list_runs("pj")[0]["triggered_by"] == "schedule"
    assert runner.schedule_tick(repo, worker, now + timedelta(hours=1)) == []  # not due yet
    later = runner.schedule_tick(repo, worker, now + timedelta(hours=7))
    worker.join()
    assert len(later) == 1  # the project run (6 h), not the procedure (24 h)

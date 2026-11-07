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
    truncated: set[str] = set()
    series = hist.window(ids, start, end + timedelta(seconds=1), truncated=truncated)
    return procedures.evaluate(proc, equips, results, series, truncated)


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


def _limited(limit):
    """The hospital history behind PostgresHistory's per-point read limit."""
    h = hx.history()
    return type(h)(h.series, per_point_limit=limit)


def test_latest_is_the_newest_sample_even_past_the_read_limit():
    """Baler#88: 24 h of 5-minute samples is 288 rows; with a 50-row read
    limit, 'latest' read through a window was the 50th-oldest sample (20 h
    stale) and every point failed 'every point appears'."""
    per = _evaluate("boiler_power_on", _limited(50))
    assert _step(per, hx.BOILER, "points-appear")["status"] == "pass"


def test_a_truncated_window_cannot_pass_a_range_but_can_fail_it():
    hw = _step(_evaluate("boiler_startup", _limited(50)), hx.BOILER, "hw-temp")
    assert hw["status"] == "inconclusive"
    assert "window truncated at 50 samples" in hw["criteria"][0]["failing"][0]["symptom"]

    h = _limited(50)
    h.add(hx.pt(hx.BOILER, "hw-sup-temp"), hx.NOW - timedelta(hours=23, minutes=59), -40.0)  # inside the read prefix
    hw = _step(_evaluate("boiler_startup", h), hx.BOILER, "hw-temp")
    assert hw["status"] == "fail" and "-40" in hw["criteria"][0]["failing"][0]["symptom"]


def test_empty_readings_are_not_reporting():
    """Baler#85: a bridge publishing null while the device is down must fail
    'every point appears', not pass on the null rows."""
    h = hx.history()
    dead = hx.pt(hx.BOILER, "hw-ret-temp")
    h.series[dead] = [(t, v) for t, v in h.series[dead] if t < hx.NOW - timedelta(hours=3)]
    for m in range(0, 120, 5):
        h.add(dead, hx.NOW - timedelta(minutes=m), None)
    appear = _step(_evaluate("boiler_power_on", h), hx.BOILER, "points-appear")
    assert appear["status"] == "fail"
    [f] = appear["criteria"][0]["failing"]
    assert f["subject"] == dead and "empty readings" in f["symptom"]


def test_empty_readings_with_no_real_value_ever_say_so():
    h = hx.history()
    dead = hx.pt(hx.BOILER, "hw-ret-temp")
    h.series[dead] = [(hx.NOW - timedelta(minutes=m), None) for m in range(0, 60, 5)]
    [f] = _step(_evaluate("boiler_power_on", h), hx.BOILER, "points-appear")["criteria"][0]["failing"]
    assert "no real value ever" in f["symptom"]


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


# ------------------------------------------------- sequence rules S1-S3 (#86)


def _run_proc(steps, equip_class, history=None, hours=24.0):
    proc = procedures.normalize({"name": "seq", "kind": "functional", "equip_class": equip_class, "window_hours": hours,
                                 "steps": [{"id": f"s{i}", "title": f"S{i}", "criteria": [c]} for i, c in enumerate(steps, 1)]})
    end = hx.NOW
    start = end - timedelta(hours=hours)
    hist = procedures.ClippedHistory(history or hx.history(), end)
    site = _equips()
    equips = [e for e in site if procedures.applies(proc, e)]
    _, results = engine.run(hx.store(), hist, [Scope.everything()], end, hours, equips=equips, site_equips=site)
    ids = runner._range_points(proc, site)
    series = hist.window(ids, start, end + timedelta(seconds=1))
    return procedures.evaluate(proc, equips, results, series, set(), (start, end))


S2 = {"range": {"min": 65, "max": 80, "unit": "F", "hours": {"from": 7, "to": 18, "days": [0, 1, 2, 3, 4]}},
      "point_class": "Zone_Air_Temperature_Sensor"}


def test_s2_band_is_judged_in_occupied_hours_only():
    h = hx.history()
    zone = hx.pt(hx.VAV2, "zone-air-temp")
    h.add(zone, hx.NOW.replace(hour=3, minute=1), 90.0)  # unoccupied: not judged
    [crit] = _step(_run_proc([S2], "Variable_Air_Volume_Box", h), hx.VAV2, "s1")["criteria"]
    assert crit["status"] == "pass"
    obs = next(o for o in crit["observed"] if o["subject"] == zone)
    assert obs["max"] == 71.5 and obs["samples"] > 0  # evidence on a passing point too
    h.add(zone, hx.NOW.replace(hour=10, minute=1), 90.0)  # occupied: judged
    [crit] = _step(_run_proc([S2], "Variable_Air_Volume_Box", h), hx.VAV2, "s1")["criteria"]
    assert crit["status"] == "fail" and "90" in crit["failing"][0]["symptom"]


S3 = {"availability": {"min_pct": 99, "max_gap_minutes": 5}, "point_class": "Hot_Water_Supply_Temperature_Sensor"}


def test_s3_availability():
    [crit] = _step(_run_proc([S3], "Boiler"), hx.BOILER, "s1")["criteria"]
    assert crit["status"] == "pass" and crit["observed"][0]["availability_pct"] >= 99
    h = hx.history()
    sup = hx.pt(hx.BOILER, "hw-sup-temp")
    h.series[sup] = [(t, v) for t, v in h.series[sup] if not (hx.NOW - timedelta(hours=6) <= t < hx.NOW - timedelta(hours=4))]
    [crit] = _step(_run_proc([S3], "Boiler", h), hx.BOILER, "s1")["criteria"]
    assert crit["status"] == "fail" and 90 < crit["observed"][0]["availability_pct"] < 99
    assert "available" in crit["failing"][0]["symptom"]


def test_availability_pct_math():
    t0 = hx.NOW - timedelta(hours=1)
    every5 = [t0 + timedelta(minutes=5 * i) for i in range(12)]
    assert procedures.availability_pct(every5, t0, hx.NOW, timedelta(minutes=5)) == 100.0
    assert procedures.availability_pct([], t0, hx.NOW, timedelta(minutes=5)) == 0.0
    half = every5[:6]  # silent for the last 30 min
    assert procedures.availability_pct(half, t0, hx.NOW, timedelta(minutes=5)) == 50.0


S1 = {"response": {"when": {"point_class": "Run_Status", "equals": "active"},
                   "then": {"point_class": "Hot_Water_Supply_Temperature_Sensor", "changes_by": 0.5, "within_minutes": 60}}}


def test_s1_response_after_start():
    """The hospital boiler starts at 02:00. Make its supply temperature flat
    (fails), then rise after the start (passes)."""
    h = hx.history()
    sup = hx.pt(hx.BOILER, "hw-sup-temp")
    h.series[sup] = [(t, 150.0) for t, _ in h.series[sup]]
    [crit] = _step(_run_proc([S1], "Boiler", h), hx.BOILER, "s1")["criteria"]
    assert crit["status"] == "fail" and "moved 0" in crit["failing"][0]["symptom"]
    start = hx.NOW.replace(hour=2, minute=0)
    h.series[sup] = [(t, 150.0 + (min((t - start).total_seconds() / 600, 3) if t >= start else 0)) for t, _ in h.series[sup]]
    [crit] = _step(_run_proc([S1], "Boiler", h), hx.BOILER, "s1")["criteria"]
    assert crit["status"] == "pass" and crit["observed"][0]["moved"] >= 0.5


def test_s1_never_exercised_is_inconclusive():
    h = hx.history()
    run = hx.pt(hx.BOILER, "boiler-run-status")
    h.series[run] = [(t, "active") for t, _ in h.series[run]]
    [crit] = _step(_run_proc([S1], "Boiler", h), hx.BOILER, "s1")["criteria"]
    assert crit["status"] == "inconclusive" and "never started" in crit["failing"][0]["symptom"]


@pytest.mark.parametrize("bad", [
    {"availability": {"min_pct": 0, "max_gap_minutes": 5}, "point_class": "X"},
    {"availability": {"min_pct": 99}, "point_class": "X"},
    {"range": {"min": 1, "hours": {"from": 18, "to": 7}}, "point_class": "X"},
    {"range": {"min": 1, "hours": {"tz": "Mars/Olympus"}}, "point_class": "X"},
    {"response": {"when": {"point_class": "X"}, "then": {"point_class": "Y", "changes_by": 1, "within_minutes": 5}}},
    {"response": {"when": {"point_class": "X", "equals": 1}, "then": {"point_class": "Y", "changes_by": 0, "within_minutes": 5}}},
    {"range": {"min": 1}, "availability": {"min_pct": 9, "max_gap_minutes": 1}, "point_class": "X"},
])
def test_sequence_criteria_validation(bad):
    with pytest.raises((ValueError, TypeError)):
        procedures.normalize({"name": "x", "kind": "functional", "equip_class": "Boiler", "steps": [{"criteria": [bad]}]})

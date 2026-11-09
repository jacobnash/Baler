"""Passive override tests (Baler#212): BACnet schedules evaluated in time,
override detection (minus Baler's own writes and the schedule's), the
schedule-aware verdict, the refresh from a mock Windrow, and the results
flowing through runs, procedures and issues. No services, no devices."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from rdflib import RDF, Literal, URIRef

from baler.commissioning import (
    db,
    engine,
    inventory,
    issues,
    overrides,
    procedures,
    runner,
)
from baler.commissioning import schedules as sch
from baler.commissioning.history import InMemoryHistory
from baler.scope import Scope
from baler.store import BRICK, HAYSTACK, TD, Store

UTC = timezone.utc
CHI = ZoneInfo("America/Chicago")
NOW = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)  # a Wednesday, 12:00 in Chicago
VAV = "urn:equip:fbf/a/vav-3"
ZNT = "urn:point:fbf/a/vav-3/znt"
ZSP = "urn:point:fbf/a/vav-3/zsp"
OCC = "urn:point:fbf/a/vav-3/occ"
AHU = "urn:equip:fbf/a/ahu-2"
FAN_C = "urn:point:fbf/a/ahu-2/sf-c"
FAN_S = "urn:point:fbf/a/ahu-2/sf-s"

OFFICE = {  # occupied 07:00-18:00 weekdays, a holiday calendar
    "object": "schedule,1", "name": "Office hours", "default": "inactive", "present_value": "active",
    "priority_for_writing": 16, "effective_period": None,
    "weekly": {d: ([{"time": "07:00:00", "value": "active"}, {"time": "18:00:00", "value": "inactive"}]
                   if d not in ("saturday", "sunday") else []) for d in sch.WEEKDAYS},
    "exceptions": [{"period": {"calendar": "calendar,1"}, "priority": 10,
                    "times": [{"time": "00:00:00", "value": "inactive"}]}],
    "controls": [{"object": "binary-value,1", "property": "present-value", "label": "occ"}],
}
HOLIDAYS = {"object": "calendar,1", "name": "Holidays", "present_value": False,
            "dates": [{"date": "*-12-25"}, {"week_n_day": {"month": 11, "week": 4, "weekday": "thursday"}}]}


def store() -> Store:
    s = Store()
    g = s.graph
    for equip, cls, points in ((VAV, "Variable_Air_Volume_Box", ((ZNT, "Zone_Air_Temperature_Sensor", False),
                                                                (ZSP, "Zone_Air_Temperature_Setpoint", True),
                                                                (OCC, "Occupancy_Command", True))),
                               (AHU, "Air_Handling_Unit", ((FAN_C, "Fan_Command", True), (FAN_S, "Fan_Status", False)))):
        e = URIRef(equip)
        g.add((e, RDF.type, BRICK[cls]))
        for uri, pcls, writable in points:
            p = URIRef(uri)
            g.add((p, RDF.type, TD.RawPoint))
            g.add((p, RDF.type, BRICK[pcls]))
            g.add((e, BRICK.hasPoint, p))
            g.add((p, BRICK.isPointOf, e))
            if writable:
                g.add((p, HAYSTACK.hasTag, Literal("writable")))
    return s


def equips():
    return {e["id"]: e for e in inventory.build(store(), [Scope.everything()])}


def minutes(start: datetime, n: int, value_at) -> list:
    return [(start + timedelta(minutes=m), value_at(m)) for m in range(n)]


def override_series(reaches: bool, override_at=None, hold=timedelta(hours=1), now=NOW):
    """Setpoint 72 -> 68 at override_at for `hold`; the zone either follows
    over ~12 minutes or sits at 72."""
    override_at = override_at or now - timedelta(hours=3)
    start = now - timedelta(hours=6)
    span = int((now - start).total_seconds() // 60)
    t_over = int((override_at - start).total_seconds() // 60)
    t_back = t_over + int(hold.total_seconds() // 60)

    def sp(m):
        return 68.0 if t_over <= m < t_back else 72.0

    def zn(m):
        if not reaches or m < t_over:
            return 72.0
        if m < t_back:
            return max(68.2, 72.0 - (m - t_over) * 0.33)
        return 72.0

    return {ZSP: minutes(start, span, sp), ZNT: minutes(start, span, zn)}


CFG = {"enabled": True, "tz": "America/Chicago"}


def _run(series, cfg=None, ctx=None, now=NOW):
    return overrides.run([equips()[VAV], equips()[AHU]], series, now, {**CFG, **(cfg or {})}, ctx or overrides.Context())


# ---------------------------------------------------------------- schedules


def test_weekly_schedule_default_and_holiday_calendar():
    cals = {"calendar,1": HOLIDAYS}

    def at(y, m, d, hh, mm=0):
        return sch.value_at(OFFICE, datetime(y, m, d, hh, mm, tzinfo=CHI), CHI, cals)

    assert at(2026, 11, 4, 6, 59)["value"] == "inactive" and at(2026, 11, 4, 6, 59)["source"] == "default"
    assert at(2026, 11, 4, 7)["value"] == "active" and at(2026, 11, 4, 7)["source"] == "weekly"
    assert at(2026, 11, 4, 18)["value"] == "inactive"
    assert at(2026, 11, 7, 12)["value"] == "inactive"  # Saturday
    thanksgiving = at(2026, 11, 26, 12)  # 4th Thursday of November
    assert thanksgiving["value"] == "inactive" and thanksgiving["source"] == "exception"
    assert at(2026, 12, 25, 12)["source"] == "exception" and at(2026, 12, 24, 12)["value"] == "active"
    # the same moment in UTC is judged on Chicago's wall clock
    assert sch.value_at(OFFICE, datetime(2026, 11, 4, 12, 30, tzinfo=UTC), CHI, cals)["value"] == "inactive"  # 06:30
    assert sch.value_at(OFFICE, datetime(2026, 11, 4, 13, 30, tzinfo=UTC), CHI, cals)["value"] == "active"


def test_exception_priority_relinquish_and_effective_period():
    s = {**OFFICE, "exceptions": [
        {"period": {"date_range": ["2026-11-02", "2026-11-06"]}, "priority": 12,
         "times": [{"time": "05:00:00", "value": "active"}]},
        {"period": {"date": "2026-11-04"}, "priority": 3,
         "times": [{"time": "09:00:00", "value": "inactive"}, {"time": "10:00:00", "value": None}]},
    ]}
    v = lambda hh: sch.value_at(s, datetime(2026, 11, 4, hh, tzinfo=CHI), CHI)
    assert v(6)["value"] == "active" and v(6)["priority"] == 12  # before the priority-3 event starts
    assert v(9)["value"] == "inactive" and v(9)["priority"] == 3
    assert v(11)["value"] == "active" and v(11)["priority"] == 12  # null relinquished down to the next
    limited = {**OFFICE, "effective_period": ["2027-01-01", "2027-12-31"]}
    assert sch.value_at(limited, datetime(2026, 11, 4, 12, tzinfo=CHI), CHI)["source"] == "not_in_effect"


def test_transitions_and_wildcards():
    got = sch.transitions(OFFICE, datetime(2026, 11, 4, 0, tzinfo=CHI), datetime(2026, 11, 5, 23, tzinfo=CHI), CHI)
    assert [(t.astimezone(CHI).strftime("%d %H:%M"), v) for t, v in got] == [
        ("04 07:00", "active"), ("04 18:00", "inactive"), ("05 07:00", "active"), ("05 18:00", "inactive")]
    assert sch.date_matches("*-*-last", date(2026, 2, 28))
    assert sch.date_matches("2026-odd-*", date(2026, 11, 3)) and not sch.date_matches("*-even-*", date(2026, 11, 3))
    assert sch.week_n_day_matches({"month": None, "week": 6, "weekday": "monday"}, date(2026, 11, 30))
    assert sch.is_unoccupied("Inactive") and sch.is_unoccupied(0) and not sch.is_unoccupied(1) and not sch.is_unoccupied(None)
    assert sch.controlled_points(OFFICE, "fbf/a/vav-3") == [OCC]


# ---------------------------------------------------------------- detection


def test_an_override_the_zone_follows_passes():
    got = _run(override_series(reaches=True))
    r = next(x for x in got if x["check_id"] == "override:zone_air_temperature")
    assert r["status"] == "pass" and r["subject"] == ZSP and r["equip_uri"] == VAV
    ov = r["evidence"]["overrides"][0]
    assert ov["value"] == 68.0 and ov["previous"] == 72.0 and ov["basis"] == "history_step"
    assert 10 <= ov["reached_after_minutes"] <= 14 and r["evidence"]["judged"] == 1


def test_an_override_the_zone_ignores_fails_with_both_values_in_the_symptom():
    r = next(x for x in _run(override_series(reaches=False)) if x["check_id"] == "override:zone_air_temperature")
    assert r["status"] == "fail"
    assert "from 72 to 68" in r["symptom"] and "no closer than 72" in r["symptom"] and "within 30 min" in r["symptom"]
    assert any("higher-priority writer" in c["cause"] for c in r["candidates"])


def test_scheduled_unoccupied_is_not_a_failure():
    # Saturday: the office schedule (controlling the VAV's occupancy point) had it off
    sat = datetime(2026, 11, 7, 18, 0, tzinfo=UTC)
    series = override_series(reaches=False, now=sat)
    ctx = overrides.Context(schedules=[{"id": "s1", "name": "Office hours", "body": OFFICE, "points": [OCC],
                                        "calendars": {"calendar,1": HOLIDAYS}}])
    r = next(x for x in _run(series, ctx=ctx, now=sat) if x["check_id"] == "override:zone_air_temperature")
    assert r["status"] == "not_applicable" and "scheduled unoccupied" in r["symptom"] and "Office hours" in r["symptom"]
    # without the schedule it is a failure; with the occupancy point's own history it's exempt again
    assert next(x for x in _run(series, now=sat) if x["check_id"] == "override:zone_air_temperature")["status"] == "fail"
    series[OCC] = [(sat - timedelta(hours=12), "inactive")]
    assert next(x for x in _run(series, now=sat) if x["check_id"] == "override:zone_air_temperature")["status"] == "not_applicable"


def test_baler_writes_schedule_changes_and_reset_sequences_are_not_overrides():
    series = override_series(reaches=False)
    at = NOW - timedelta(hours=3)
    assert not [x for x in _run(series, ctx=overrides.Context(baler_writes={ZSP: [at + timedelta(seconds=30)]}))
                if x["check_id"].startswith("override:zone")]
    sp_sched = {**OFFICE, "name": "Setpoint schedule",
                "weekly": {d: [{"time": at.astimezone(CHI).strftime("%H:%M:00"), "value": 68.0}] for d in sch.WEEKDAYS},
                "exceptions": [], "default": 72.0}
    ctx = overrides.Context(schedules=[{"id": "s2", "name": "Setpoint schedule", "body": sp_sched, "points": [ZSP]}])
    got = [x for x in _run(series, ctx=ctx) if x["check_id"].startswith("override:zone")]
    assert got == []
    # a 5-minute blip (a reset sequence, not an override) never counts
    blip = override_series(reaches=False, hold=timedelta(minutes=5))
    assert not [x for x in _run(blip) if x["check_id"].startswith("override:zone")]


def test_pending_override_is_not_judged_and_release_early_is_inconclusive():
    # 10 minutes before the end of history, zone hasn't reached yet: pending, no result
    late = override_series(reaches=False, override_at=NOW - timedelta(minutes=15), hold=timedelta(hours=2))
    assert not [x for x in _run(late) if x["check_id"].startswith("override:zone")]
    # released after 15 min without reaching: inconclusive, says why
    short = override_series(reaches=False, hold=timedelta(minutes=15))
    r = next(x for x in _run(short) if x["check_id"] == "override:zone_air_temperature")
    assert r["status"] == "inconclusive" and "released after 15 min" in r["symptom"]


def test_priority_array_basis_and_level_annotation():
    series = override_series(reaches=True)
    at = NOW - timedelta(hours=3)
    assert not [x for x in _run(series, cfg={"basis": "priority_array"}) if x["check_id"].startswith("override:zone")]
    ctx = overrides.Context(priority={ZSP: [
        {"observed_at": at - timedelta(minutes=30), "levels": {"16": {"value": 72.0, "baler": False}}},
        {"observed_at": at + timedelta(minutes=5), "levels": {"8": {"value": 68.0, "baler": False},
                                                             "16": {"value": 72.0, "baler": False}}}]})
    r = next(x for x in _run(series, cfg={"basis": "priority_array"}, ctx=ctx) if x["check_id"].startswith("override:zone"))
    ov = r["evidence"]["overrides"][0]
    assert r["status"] == "pass" and ov["basis"] == "priority_array" and ov["level"]["level"] == 8
    # a level Baler holds is not someone else's override
    ctx.priority[ZSP][1]["levels"]["8"]["baler"] = True
    assert not [x for x in _run(series, cfg={"basis": "priority_array"}, ctx=ctx) if x["check_id"].startswith("override:zone")]


def test_a_fan_command_override_is_judged_even_when_scheduled_off():
    start = NOW - timedelta(hours=2)
    series = {FAN_C: minutes(start, 120, lambda m: 1.0 if m >= 30 else 0.0),
              FAN_S: minutes(start, 120, lambda m: 1.0 if m >= 33 else 0.0)}
    ctx = overrides.Context(schedules=[{"id": "s1", "name": "Office hours", "body": OFFICE, "points": [OCC]}])
    r = next(x for x in _run(series, ctx=ctx) if x["check_id"] == "override:fan_cmd_status")
    assert r["status"] == "pass" and r["evidence"]["overrides"][0]["reached_after_minutes"] == 3.0
    series[FAN_S] = minutes(start, 120, lambda m: 0.0)
    r = next(x for x in _run(series) if x["check_id"] == "override:fan_cmd_status")
    assert r["status"] == "fail" and "didn't follow" in r["symptom"]


def test_settings_validation():
    overrides.validate_settings({"enabled": True, "within_minutes": 45, "tolerances": {"zone_air_temperature": 0.5},
                                 "basis": "priority_array", "tz": "America/Chicago", "unoccupied_values": [0, "unocc"]})
    for bad in ({"enabled": "yes"}, {"within_minutes": 0}, {"basis": "vibes"}, {"tz": "Mars/Base"},
                {"tolerances": {"nope": 1}}, {"min_step": {"air_flow": -1}}, {"surprise": 1}, []):
        with pytest.raises((ValueError, TypeError)):
            overrides.validate_settings(bad)


# ---------------------------------------------------------- runs and issues


def _repo(series):
    return db.MemoryRepo(store=store(), history=InMemoryHistory(series))


def test_engine_adds_override_results_only_when_enabled():
    repo = _repo(override_series(reaches=False))
    with repo.history() as h:
        _, plain = engine.run(repo.store, h, [Scope.everything()], NOW, 12)
        _, withit = engine.run(repo.store, h, [Scope.everything()], NOW, 12, override_settings=CFG, override_source=repo)
    assert not [r for r in plain if r["check_id"].startswith("override:")]
    got = [r for r in withit if r["check_id"].startswith("override:")]
    assert [(r["check_id"], r["status"]) for r in got] == [("override:zone_air_temperature", "fail")]
    assert runner.engine_kwargs({"overrides": CFG}) == {"override_settings": CFG}
    assert runner.engine_kwargs({"overrides": {"enabled": False}}) == {}


def test_procedure_criterion_and_issue_lifecycle():
    """An override fail is a Cx result like any other: a procedure step
    citing override: fails, an issue opens from the run, and a later run
    where the zone follows resolves it for a person to close."""
    repo = _repo(override_series(reaches=False))
    with repo.history() as h:
        _, results = engine.run(repo.store, h, [Scope.everything()], NOW, 12, override_settings=CFG, override_source=repo)
    proc = procedures.normalize({"name": "Overrides answer", "kind": "functional", "equip_class": "Variable_Air_Volume_Box",
                                 "steps": [{"id": "overrides", "criteria": [{"check": "override:"}]}]})
    per = procedures.evaluate(proc, [equips()[VAV]], results, {})
    assert per[0]["steps"][0]["status"] == "fail"
    fail = next(r for r in results if r["check_id"] == "override:zone_air_temperature")
    issue = issues.from_result(fail, "run-1", "p1", db.new_id, NOW, "cxa")
    repo.create_issue(issue)
    later = NOW + timedelta(hours=6)
    passing = [{**fail, "status": "pass", "symptom": None}]
    changes = runner.reconcile_issues(repo, "p1", "run-2", passing, later, judged_from=later - timedelta(hours=12))
    assert [c[2] for c in changes] == ["resolve.evidence"]
    assert repo.get_issue(issue["id"])["status"] == "ready_for_retest"


# ------------------------------------------------------- refresh (mock Windrow)


class MockWindrow:
    """The command channel of one Windrow instance with one connection.
    Records every action so the test can prove refresh only reads."""

    def __init__(self, priority=True):
        self.actions: list[str] = []
        self.priority = priority

    def statuses(self):
        return {("site-a", "edge-1"): {"state": "online"}, ("site-z", "edge-9"): {"state": "offline"}}

    def command(self, site, instance, action, body=None, timeout=None, suffix=None):
        self.actions.append(action)
        if action == "list_connections":
            return [{"id": "c1", "topic_prefix": "fbf/a/vav-3"}, {"id": "c2", "topic_prefix": "fbf/other/x"}]
        if action == "connection_schedules":
            assert body == {"id": "c1"}
            return {"schedules": [OFFICE], "calendars": [HOLIDAYS]}
        if action == "read_priority_arrays":
            if not self.priority:
                raise RuntimeError("site refused read_priority_arrays: unknown action")
            return {"points": {"zsp": {"levels": {"8": 68.0, "16": 72.0}}}}
        raise AssertionError(f"unexpected action {action}")


def test_refresh_reads_schedules_and_priority_arrays_and_never_writes():
    repo = _repo({})
    repo.levels[(ZSP, 16)] = {"val": 72.0, "who": "cx:old", "expires_at": None}
    w = MockWindrow()
    got = overrides.refresh(repo, w, [Scope(("fbf/a",))], NOW)
    assert set(w.actions) == {"list_connections", "connection_schedules", "read_priority_arrays"}
    assert got["schedules"] == 1 and got["calendars"] == 1 and got["priority_points"] == 1 and not got["errors"]
    found = repo.schedules_for_points([OCC])
    assert [s["name"] for s in found] == ["Office hours"] and "calendar,1" in found[0]["calendars"]
    obs = repo.priority_observations([ZSP], NOW - timedelta(hours=1), NOW)[ZSP]
    assert obs[0]["levels"] == {"8": {"value": 68.0, "baler": False}, "16": {"value": 72.0, "baler": True}}
    # unchanged levels add no row; a Windrow without the action still refreshes schedules
    assert overrides.refresh(repo, w, [Scope(("fbf/a",))], NOW + timedelta(minutes=15))["priority_points"] == 0
    old = MockWindrow(priority=False)
    got = overrides.refresh(repo, old, [Scope(("fbf/a",))], NOW)
    assert got["instances"][0]["priority_arrays"] == "unsupported" and got["schedules"] == 1


def test_refresher_ticks_only_enabled_projects_at_their_interval():
    repo = _repo({})
    for pid, cfg in (("on", {"enabled": True, "refresh_minutes": 15}), ("off", {"enabled": False}),
                     ("manual", {"enabled": True, "refresh_minutes": 0})):
        repo.create_project({"id": pid, "org_id": "o", "name": pid, "settings": {"overrides": cfg}, "phases": [],
                             "created_at": NOW, "updated_at": NOW})
    r = overrides.Refresher(repo, MockWindrow(), lambda p: [Scope.everything()])
    assert r.tick(NOW) == ["on"] and r.tick(NOW + timedelta(minutes=5)) == [] and r.tick(NOW + timedelta(minutes=16)) == ["on"]


def test_baler_writes_come_from_the_active_run_log():
    repo = _repo({})
    repo.put_rows("cx_active_runs", [{"id": "r1", "project_id": "p", "writes": [
        {"action": "write", "point": ZSP, "at": (NOW - timedelta(hours=1)).isoformat()},
        {"action": "relinquish", "point": ZSP, "at": (NOW - timedelta(minutes=50)).isoformat()}]}])
    got = repo.baler_writes([ZSP], NOW - timedelta(hours=2), NOW)
    assert got == {ZSP: [NOW - timedelta(hours=1), NOW - timedelta(minutes=50)]}


# ------------------------------------------------------------------- api


@pytest.fixture
def served(monkeypatch):
    import threading
    from http.server import ThreadingHTTPServer

    import test_commissioning_openapi as api

    from baler.commissioning_api import make_handler

    repo = db.MemoryRepo(store=store(), history=InMemoryHistory(override_series(reaches=False)), sites=api.SITES,
                         scope_fn=api._scope, users={"u-a": "a@example.com"})
    monkeypatch.setenv("BALER_GATEWAY_SECRET", api.GATEWAY)
    monkeypatch.setenv("BALER_JWT_SECRET", api.JWT)
    servers = []

    def start(commander):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(repo, clock=lambda: NOW, worker=runner.RunWorker(repo, lambda: NOW),
                                                                    commander=commander))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start, repo, api
    for s in servers:
        s.shutdown()


def test_api_settings_schedules_refresh_and_a_run(served):
    import requests
    from conftest import assert_matches_schema, load_spec

    from baler.commissioning_api import OPENAPI_SPEC_PATH

    start, _repo_unused, api = served
    oas = load_spec(OPENAPI_SPEC_PATH)
    base = start(None)
    assert requests.post(f"{base}/projects", headers=api.A, json={"name": "x", "phase": "functional", "site_id": "site-a",
                                                                  "settings": {"overrides": {"basis": "nope"}}}).status_code == 400
    p = api._project(base, phase="functional", settings={"overrides": CFG, "window_hours": 12})
    assert requests.post(f"{base}/projects/{p['id']}/schedules/refresh", headers=api.A).status_code == 503
    w = MockWindrow()
    base = start(w)
    r = requests.post(f"{base}/projects/{p['id']}/schedules/refresh", headers=api.A)
    assert r.status_code == 200, r.text
    assert_matches_schema(r.json(), api._schema(oas, "/projects/{id}/schedules/refresh", "post", 200), oas)
    assert "point_write" not in w.actions
    got = requests.get(f"{base}/projects/{p['id']}/schedules", headers=api.A)
    assert_matches_schema(got.json(), api._schema(oas, "/projects/{id}/schedules", "get", 200), oas)
    office = next(s for s in got.json()["schedules"] if s["kind"] == "schedule")
    assert office["points"] == [OCC] and office["now"]["value"] == "active" and office["now"]["unoccupied"] is False
    assert got.json()["tz"] == "America/Chicago"
    assert requests.get(f"{base}/projects/{p['id']}/schedules", headers=api.B).status_code == 404
    run = requests.post(f"{base}/projects/{p['id']}/runs", json={}, headers=api.A).json()
    results = api._wait(base, run["id"]).json()["results"]
    ov = [x for x in results if x["check_id"].startswith("override:")]
    assert [(x["check_id"], x["status"]) for x in ov] == [("override:zone_air_temperature", "fail")]
    events = [e["action"] for e in requests.get(f"{base}/projects/{p['id']}/events", headers=api.A).json()]
    assert "schedules.refresh" in events

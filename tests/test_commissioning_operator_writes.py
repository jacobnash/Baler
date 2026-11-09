"""Audited operator writes and schedule/calendar writes (Baler#213),
against a mock Windrow: point writes land in Baler's priority array with
who, why, level and expiry, and end on time by themselves; schedule writes
go over the Windrow#26 command contract and revert. No services, no
devices."""

from __future__ import annotations

import copy
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import jwt as pyjwt
import pytest
import requests
import test_commissioning_openapi as api
from conftest import assert_matches_schema, load_spec
from rdflib import RDF, Literal, URIRef

from baler.commissioning import active, db, inventory, runner
from baler.commissioning import operator_writes as ow
from baler.commissioning.history import InMemoryHistory
from baler.commissioning_api import OPENAPI_SPEC_PATH, make_handler
from baler.scope import Scope
from baler.store import BRICK, HAYSTACK, TD, Store

T0 = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
VAV = "urn:equip:fbf/a/vav-7"
ZSP = "urn:point:fbf/a/vav-7/zsp"
ZNT = "urn:point:fbf/a/vav-7/znt"

WEEK = {d: [{"time": "07:00:00", "value": "active"}, {"time": "18:00:00", "value": "inactive"}]
        for d in ("monday", "tuesday", "wednesday", "thursday", "friday")} | {"saturday": [], "sunday": []}


def store() -> Store:
    s = Store()
    g = s.graph
    e = URIRef(VAV)
    g.add((e, RDF.type, BRICK["Variable_Air_Volume_Box"]))
    for uri, cls, writable in ((ZSP, "Zone_Air_Temperature_Setpoint", True), (ZNT, "Zone_Air_Temperature_Sensor", False)):
        p = URIRef(uri)
        g.add((p, RDF.type, TD.RawPoint))
        g.add((p, RDF.type, BRICK[cls]))
        g.add((e, BRICK.hasPoint, p))
        g.add((p, BRICK.isPointOf, e))
        if writable:
            g.add((p, HAYSTACK.hasTag, Literal("writable")))
    return s


class MockWindrow:
    """One Windrow instance: point_write (MemoryRepo calls it as
    commander(point, value, level)) and the command channel, holding one
    schedule and one calendar the way bacnet_schedules reads them, and
    implementing write_schedule / write_calendar per the Windrow#26
    contract."""

    def __init__(self):
        self.objects = {"schedule,1": {"object": "schedule,1", "name": "Office", "default": "inactive", "weekly": copy.deepcopy(WEEK),
                                       "exceptions": [], "effective_period": None, "priority_for_writing": 16,
                                       "present_value": "active", "controls": []},
                        "calendar,1": {"object": "calendar,1", "name": "Holidays", "present_value": False, "dates": [{"date": "*-12-25"}]}}
        self.sent: list[tuple] = []
        self.refuse: set[str] = set()

    def __call__(self, point, value, level):
        self.sent.append(("point_write", point, value, level))
        if "point_write" in self.refuse:
            raise RuntimeError("site refused the write")

    def command(self, site, instance, action, body=None, timeout=None, suffix=None):
        self.sent.append((action, copy.deepcopy(body)))
        if action in self.refuse:
            raise RuntimeError(f"site refused {action}")
        if action == "list_connections":
            return [{"id": "c1", "topic_prefix": "fbf/a/vav-7"}, {"id": "c9", "topic_prefix": "fbf/b/elsewhere"}]
        if action == "connection_schedules":
            return {"schedules": [copy.deepcopy(self.objects["schedule,1"])], "calendars": [copy.deepcopy(self.objects["calendar,1"])]}
        if action in ("write_schedule", "write_calendar"):
            assert suffix == action.replace("_", "-") and body["id"] == "c1"
            obj = self.objects[body["object"]]
            obj.update({k: v for k, v in body.items() if k not in ("id", "object", "who", "reason")})
            return copy.deepcopy(obj)
        raise AssertionError(f"unexpected action {action}")


def _equips():
    return inventory.build(store(), [Scope.everything()])


def _hdr(role="operator", sub="u-a", kind="session", org="org-a"):
    tok = pyjwt.encode({"sub": sub, "org_id": org, "site_ids": [], "role": role, "kind": kind}, api.JWT, algorithm="HS256")
    return {"X-Gateway-Secret": api.GATEWAY, "Authorization": f"Bearer {tok}"}


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def served(monkeypatch):
    clock, w = Clock(), MockWindrow()
    repo = db.MemoryRepo(store=store(), history=InMemoryHistory({ZSP: [(T0 - timedelta(minutes=5), 72.0)]}), sites=api.SITES,
                         scope_fn=api._scope, users={"u-a": "a@example.com", "u-adm": "adm@example.com"})
    monkeypatch.setenv("BALER_GATEWAY_SECRET", api.GATEWAY)
    monkeypatch.setenv("BALER_JWT_SECRET", api.JWT)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(repo, clock=clock, worker=runner.RunWorker(repo, clock), commander=w))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", repo, w, clock
    server.shutdown()


@pytest.fixture(scope="module")
def oas():
    return load_spec(OPENAPI_SPEC_PATH)


# ------------------------------------------------------------------ pure


def test_point_write_validation():
    eq = _equips()
    ok = ow.point_write({"point": ZSP, "value": 68, "reason": "tenant too warm", "hold_minutes": 60}, eq, T0)
    assert ok["level"] == 8 and ok["expires_at"] == T0 + timedelta(hours=1) and ok["equip_uri"] == VAV
    base = {"point": ZSP, "value": 68, "reason": "tenant too warm", "hold_minutes": 60}
    for bad in ({"reason": None}, {"reason": "  x "}, {"hold_minutes": None}, {"hold_minutes": 60 * 25}, {"hold_minutes": 0},
                {"level": 5}, {"level": 17}, {"level": "8"}, {"point": ZNT}, {"point": "urn:point:elsewhere"},
                {"value": "warm"}, {"value": None}, {"replace": "yes"},
                {"hold_minutes": None, "expires_at": (T0 + timedelta(days=2)).isoformat()},
                {"hold_minutes": None, "expires_at": "2026-11-09T16:00:00"}):
        with pytest.raises((ValueError, TypeError)):
            ow.point_write({k: v for k, v in (base | bad).items() if v is not None}, eq, T0)
    assert ow.point_write(base | {"hold_minutes": None, "expires_at": (T0 + timedelta(hours=2)).isoformat()} | {"level": 16},
                          eq, T0)["level"] == 16


def test_schedule_write_validation():
    base = {"site": "site-a", "instance": "edge-1", "connection_id": "c1", "object": "schedule,1", "reason": "open house Saturday",
            "revert_at": (T0 + timedelta(days=2)).isoformat(),
            "exceptions": [{"period": {"date": "2026-11-14"}, "priority": 10,
                            "times": [{"time": "09:00", "value": "active"}, {"time": "13:00", "value": None}]}]}
    got = ow.schedule_write(base, T0)
    assert got["fields"]["exceptions"][0]["times"][0] == {"time": "09:00:00", "value": "active"}
    assert ow.schedule_write({**base, "revert_at": None, "permanent": True}, T0)["permanent"]
    cal = ow.schedule_write({**base, "object": "calendar,1", "kind": "calendar", "exceptions": None,
                             "dates": [{"date": "*-12-25"}, {"week_n_day": {"month": 11, "week": 4, "weekday": "thursday"}}]}, T0)
    assert len(cal["fields"]["dates"]) == 2 and "exceptions" not in cal["fields"]
    for bad in ({"revert_at": None}, {"permanent": True}, {"reason": ""}, {"object": "analog-value,1"}, {"site": ""},
                {"revert_at": (T0 + timedelta(days=40)).isoformat()},
                {"exceptions": [{"period": {"date": "2026-13-01"}, "times": []}]},
                {"exceptions": [{"period": {"date": "2026-11-14"}, "priority": 0, "times": []}]},
                {"exceptions": [{"period": {"date": "2026-11-14"}, "times": [{"time": "25:00", "value": 1}]}]},
                {"exceptions": None, "weekly": {"monday": []}}, {"exceptions": None}, {"kind": "trend"}):
        body = {k: v for k, v in (base | bad).items() if v is not None}
        with pytest.raises((ValueError, TypeError)):
            ow.schedule_write(body, T0)


# ------------------------------------------------------------ point writes


def test_point_write_round_trip_audited_and_relinquished(served, oas):
    base, repo, w, _clock = served
    p = api._project(base, headers=_hdr(), phase="functional")
    body = {"point": ZSP, "value": 68, "level": 8, "reason": "tenant in 4012 too warm", "hold_minutes": 90}
    r = requests.post(f"{base}/projects/{p['id']}/writes", json=body, headers=_hdr())
    assert r.status_code == 201, r.text
    assert_matches_schema(r.json(), api._schema(oas, "/projects/{id}/writes", "post", 201), oas)
    row = r.json()
    assert (row["status"], row["who"], row["user_sub"], row["role"], row["level"], row["reason"]) == \
        ("active", "a@example.com", "u-a", "operator", 8, "tenant in 4012 too warm")
    assert row["expires_at"] == (T0 + timedelta(minutes=90)).isoformat() and row["before"]["latest"]["value"] == 72.0
    assert w.sent == [("point_write", ZSP, 68, 8)]
    assert repo.levels[(ZSP, 8)]["who"] == f"op:{row['id']}" and repo.levels[(ZSP, 8)]["expires_at"] == T0 + timedelta(minutes=90)
    listed = requests.get(f"{base}/projects/{p['id']}/writes", params={"point": ZSP}, headers=_hdr("viewer"))
    assert_matches_schema(listed.json(), api._schema(oas, "/projects/{id}/writes", "get", 200), oas)
    assert [x["id"] for x in listed.json()["writes"]] == [row["id"]] and listed.json()["priority_array"]["8"]["val"] == 68
    # relinquish needs a reason too
    assert requests.post(f"{base}/writes/{row['id']}/relinquish", json={}, headers=_hdr()).status_code == 400
    rel = requests.post(f"{base}/writes/{row['id']}/relinquish", json={"reason": "tenant left"}, headers=_hdr())
    assert rel.status_code == 200 and rel.json()["status"] == "relinquished" and rel.json()["end_reason"] == "tenant left"
    assert (ZSP, 8) not in repo.levels and w.sent[-1] == ("point_write", ZSP, None, 8)
    assert requests.post(f"{base}/writes/{row['id']}/relinquish", json={"reason": "again"}, headers=_hdr()).status_code == 409
    events = [e["action"] for e in requests.get(f"{base}/projects/{p['id']}/events", headers=_hdr()).json()]
    assert events[:2] == ["operator_write.relinquished", "operator_write.point"]


def test_who_can_write(served):
    base, _repo, w, _ = served
    p = api._project(base, headers=_hdr(), phase="functional")
    body = {"point": ZSP, "value": 68, "reason": "testing reach", "hold_minutes": 10}
    url = f"{base}/projects/{p['id']}/writes"
    assert requests.post(url, json=body, headers=_hdr("viewer")).status_code == 403
    assert requests.post(url, json=body, headers=_hdr("admin", kind="api_key")).status_code == 403  # a person, not a key
    assert requests.post(url, json=body, headers=_hdr(org="org-b", sub="u-b")).status_code == 404
    for bad in ({"reason": ""}, {"hold_minutes": None}, {"level": 1}):
        assert requests.post(url, json={k: v for k, v in (body | bad).items() if v is not None}, headers=_hdr()).status_code == 400
    assert w.sent == []


def test_a_cx_test_level_is_never_taken_and_a_persons_only_with_replace(served):
    base, repo, w, _ = served
    p = api._project(base, headers=_hdr(), phase="functional")
    url = f"{base}/projects/{p['id']}/writes"
    body = {"point": ZSP, "value": 68, "reason": "testing reach", "hold_minutes": 10}
    repo.levels[(ZSP, 8)] = {"val": 1, "who": "cx:run-1", "expires_at": T0 + timedelta(minutes=5)}
    r = requests.post(url, json=body, headers=_hdr())
    assert r.status_code == 409 and "Cx active test" in r.text and w.sent == []
    assert requests.post(url, json=body | {"replace": True}, headers=_hdr()).status_code == 409
    repo.levels[(ZSP, 8)] = {"val": 70, "who": "bob@example.com", "expires_at": None}
    assert requests.post(url, json=body, headers=_hdr()).status_code == 409
    took = requests.post(url, json=body | {"replace": True}, headers=_hdr()).json()
    assert took["before"]["priority_array"]["8"] == {"val": 70, "who": "bob@example.com"}
    # a second operator write supersedes the first one cleanly
    again = requests.post(url, json=body | {"value": 67}, headers=_hdr()).json()
    assert repo.get_row(ow.TABLE, took["id"])["status"] == "superseded" and again["status"] == "active"


def test_refused_write_is_recorded_failed(served):
    base, repo, w, _ = served
    p = api._project(base, headers=_hdr(), phase="functional")
    w.refuse.add("point_write")
    r = requests.post(f"{base}/projects/{p['id']}/writes", json={"point": ZSP, "value": 68, "reason": "x y z", "hold_minutes": 5},
                      headers=_hdr())
    assert r.status_code == 502
    row = repo.find_rows(ow.TABLE, project_id=p["id"])[0]
    assert row["status"] == "failed" and "refused" in row["error"] and (ZSP, 8) not in repo.levels


def test_sweeper_auto_relinquishes_retries_and_leaves_others_alone():
    w = MockWindrow()
    repo = db.MemoryRepo(store=store(), history=InMemoryHistory())
    claims = {"sub": "u-a", "role": "operator"}

    def write(value, minutes):
        req = ow.point_write({"point": ZSP, "value": value, "reason": "hold for TAB", "hold_minutes": minutes}, _equips(), T0)
        row = ow.new_row("p", "point", ZSP, claims, "a@example.com", req["reason"], T0, level=8, value=value,
                         expires_at=req["expires_at"])
        return ow.write_point(repo, w, row, False, T0)

    row = write(68, 30)
    assert ow.sweep(repo, w, T0 + timedelta(minutes=29)) == []
    w.refuse.add("point_write")  # the site is down at expiry: stays active, logged, retried
    assert ow.sweep(repo, w, T0 + timedelta(minutes=31))[0][1].startswith("retry")
    assert repo.get_row(ow.TABLE, row["id"])["status"] == "active" and (ZSP, 8) in repo.levels
    w.refuse.clear()
    assert ow.sweep(repo, w, T0 + timedelta(minutes=32)) == [(row["id"], "expired")]
    got = repo.get_row(ow.TABLE, row["id"])
    assert (ZSP, 8) not in repo.levels and got["end_reason"].startswith("expired") and got["ended_by"] is None
    assert [e["action"] for e in got["log"]] == ["write", "relinquish", "relinquish"]
    # someone took the level since: the sweeper ends the record but sends nothing
    row2 = write(66, 10)
    repo.levels[(ZSP, 8)] = {"val": 75, "who": "bob@example.com", "expires_at": None}
    n = len(w.sent)
    assert ow.sweep(repo, w, T0 + timedelta(minutes=11)) == [(row2["id"], "expired")]
    assert len(w.sent) == n and repo.levels[(ZSP, 8)]["who"] == "bob@example.com"
    # Cx 9's sweeper never touches an operator's level, nor this one a Cx level
    del repo.levels[(ZSP, 8)]
    row3 = write(65, 10)
    assert active.sweep(repo, active.Writer(repo, w), T0 + timedelta(hours=5)) == []
    assert repo.levels[(ZSP, 8)]["who"] == f"op:{row3['id']}"
    repo.levels[(ZNT, 8)] = {"val": 1, "who": "cx:run-9", "expires_at": T0}
    ow.sweep(repo, w, T0 + timedelta(minutes=11))
    assert (ZNT, 8) in repo.levels


# --------------------------------------------------------- schedule writes


def _sched_body(**kw):
    return {"site": "site-a", "instance": "edge-1", "connection_id": "c1", "object": "schedule,1", "reason": "open house Saturday",
            "revert_at": (T0 + timedelta(days=6)).isoformat(),
            "exceptions": [{"period": {"date": "2026-11-14"}, "priority": 10,
                            "times": [{"time": "09:00", "value": "active"}, {"time": "13:00", "value": None}]}]} | kw


def test_schedule_write_admin_only_audited_and_reverted(served, oas):
    base, repo, w, _clock = served
    admin = _hdr("admin", sub="u-adm")
    p = api._project(base, headers=admin, phase="functional")
    url = f"{base}/projects/{p['id']}/schedule-writes"
    assert requests.post(url, json=_sched_body(), headers=_hdr()).status_code == 403  # operator
    assert requests.post(url, json=_sched_body(connection_id="c9"), headers=admin).status_code == 404  # out of scope
    assert requests.post(url, json=_sched_body(object="schedule,7"), headers=admin).status_code == 400
    targets = requests.get(f"{base}/projects/{p['id']}/schedule-targets", params={"site": "site-a", "instance": "edge-1"},
                           headers=_hdr())
    assert targets.status_code == 200 and [t["connection_id"] for t in targets.json()] == ["c1"]
    r = requests.post(url, json=_sched_body(), headers=admin)
    assert r.status_code == 201, r.text
    assert_matches_schema(r.json(), api._schema(oas, "/projects/{id}/schedule-writes", "post", 201), oas)
    row = r.json()
    assert row["status"] == "active" and row["before"]["exceptions"] == [] and row["who"] == "adm@example.com"
    sent = [s for s in w.sent if s[0] == "write_schedule"]
    assert sent == [("write_schedule", {"id": "c1", "object": "schedule,1", "exceptions": _norm_exceptions(),
                                         "who": "adm@example.com", "reason": "open house Saturday"})]
    assert w.objects["schedule,1"]["exceptions"] == _norm_exceptions()
    # the sweeper writes the old exceptions back at revert_at
    assert ow.sweep(repo, w, T0 + timedelta(days=5)) == []
    assert ow.sweep(repo, w, T0 + timedelta(days=6, minutes=1)) == [(row["id"], "reverted")]
    assert w.objects["schedule,1"]["exceptions"] == [] and w.objects["schedule,1"]["weekly"] == WEEK


def _norm_exceptions():
    return [{"period": {"date": "2026-11-14"}, "priority": 10,
             "times": [{"time": "09:00:00", "value": "active"}, {"time": "13:00:00", "value": None}]}]


def test_schedule_revert_leaves_someone_elses_change_alone_and_permanent_writes(served):
    base, repo, w, _ = served
    admin = _hdr("admin", sub="u-adm")
    p = api._project(base, headers=admin, phase="functional")
    row = requests.post(f"{base}/projects/{p['id']}/schedule-writes", json=_sched_body(), headers=admin).json()
    w.objects["schedule,1"]["exceptions"] = []  # someone changed it in the vendor tool
    assert ow.sweep(repo, w, T0 + timedelta(days=7)) == [(row["id"], "conflict")]
    assert w.objects["schedule,1"]["exceptions"] == [] and repo.get_row(ow.TABLE, row["id"])["end_reason"].endswith("since this write")
    perm = requests.post(f"{base}/projects/{p['id']}/schedule-writes", headers=admin,
                         json=_sched_body(object="calendar,1", kind="calendar", exceptions=None, revert_at=None, permanent=True,
                                          dates=[{"date": "*-12-25"}, {"date": "*-01-01"}],
                                          reason="add New Year to holidays") | {"exceptions": None})
    body = perm.request.body
    assert perm.status_code == 201, (perm.text, body)
    assert perm.json()["status"] == "permanent" and w.objects["calendar,1"]["dates"][-1] == {"date": "*-01-01"}
    assert ow.sweep(repo, w, T0 + timedelta(days=60)) == []  # never reverted by itself
    rv = requests.post(f"{base}/writes/{perm.json()['id']}/revert", json={"reason": "wrong year"}, headers=admin)
    assert rv.status_code == 200 and rv.json()["status"] == "reverted" and w.objects["calendar,1"]["dates"] == [{"date": "*-12-25"}]
    assert requests.post(f"{base}/writes/{perm.json()['id']}/revert", json={"reason": "x y z"}, headers=_hdr()).status_code == 403


def test_without_a_command_channel_writes_are_unavailable(monkeypatch):
    repo = db.MemoryRepo(store=store(), history=InMemoryHistory(), sites=api.SITES, scope_fn=api._scope)
    monkeypatch.setenv("BALER_GATEWAY_SECRET", api.GATEWAY)
    monkeypatch.setenv("BALER_JWT_SECRET", api.JWT)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(repo, clock=lambda: T0, worker=runner.RunWorker(repo, lambda: T0)))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        p = api._project(base, headers=_hdr(), phase="functional")
        r = requests.post(f"{base}/projects/{p['id']}/writes", json={"point": ZSP, "value": 1, "reason": "abc", "hold_minutes": 1},
                          headers=_hdr())
        assert r.status_code == 503
    finally:
        server.shutdown()

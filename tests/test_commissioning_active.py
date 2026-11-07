"""Cx 9 (#45): active tests through point_write inside the safety
envelope. A fake site: writing the fan command makes the fan status
follow (or not), on a fake clock. No services, no MQTT."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
import requests
import test_commissioning_openapi as api
from conftest import assert_matches_schema, load_spec
from rdflib import RDF, Literal, URIRef

from baler.commissioning import active, db, inventory
from baler.commissioning.history import InMemoryHistory
from baler.commissioning_api import OPENAPI_SPEC_PATH
from baler.scope import Scope
from baler.store import BRICK, HAYSTACK, TD, Store

T0 = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
AHU = "urn:equip:fbf/a/ahu-7"
CMD = "urn:point:fbf/a/ahu-7/sf-c"
STS = "urn:point:fbf/a/ahu-7/sf-s"
SAT = "urn:point:fbf/a/ahu-7/sat"


def store():
    s = Store()
    g = s.graph
    e = URIRef(AHU)
    g.add((e, RDF.type, BRICK["Air_Handling_Unit"]))
    for uri, cls, writable in ((CMD, "Fan_Command", True), (STS, "Fan_Status", False), (SAT, "Supply_Air_Temperature_Sensor", False)):
        p = URIRef(uri)
        g.add((p, RDF.type, TD.RawPoint))
        g.add((p, RDF.type, BRICK[cls]))
        g.add((e, BRICK.hasPoint, p))
        g.add((p, BRICK.isPointOf, e))
        if writable:
            g.add((p, HAYSTACK.hasTag, Literal("writable")))
    return s


class Site:
    """A fake clock and a fake controller behind point_write."""

    def __init__(self, follows=True):
        self.now = T0
        self.follows = follows
        self.history = InMemoryHistory({STS: [(T0 - timedelta(minutes=5), 0.0)], CMD: [(T0 - timedelta(minutes=5), 0.0)]})
        self.fail_on = None
        self.lock = threading.Lock()

    def clock(self):
        with self.lock:
            return self.now

    def sleep(self, s):
        with self.lock:
            self.now += timedelta(seconds=max(s, 1))

    def commander(self, point, value, level):
        if self.fail_on == (point, value):
            raise RuntimeError("site refused the write")
        if value is not None and point == CMD:
            self.history.add(CMD, self.now, float(value))
            if self.follows:
                self.history.add(STS, self.now + timedelta(seconds=30), float(value))


def _equip():
    return inventory.build(store(), [Scope.everything()])[0]


def _setup(follows=True):
    site = Site(follows)
    repo = db.MemoryRepo(store=store(), history=site.history)
    writer = active.Writer(repo, site.commander)
    ex = active.Executor(repo, writer, site.clock, site.sleep, poll_seconds=10)
    return site, repo, ex


def _planned(repo, steps=None, minutes=60):
    equip = _equip()
    steps = active.validate_steps(steps or active.command_response_steps(equip, 2), equip)
    run = active.new_run("p", AHU, "command_response", steps, T0, T0 + timedelta(minutes=minutes), "admin", T0)
    run["status"] = "confirmed"
    repo.put_rows("cx_active_runs", [run])
    return run


# ------------------------------------------------------------------ pure


def test_command_response_steps_and_validation():
    equip = _equip()
    steps = active.command_response_steps(equip, 3)
    assert [s["id"] for s in steps] == ["fan_cmd_status-on", "fan_cmd_status-off"]
    assert steps[0]["writes"] == [{"point": CMD, "value": 1}] and steps[0]["expect"] == [{"point": STS, "equals": 1}]
    for bad in ([{"writes": [{"point": STS, "value": 1}]}],  # not writable
                [{"writes": [{"point": "urn:point:elsewhere", "value": 1}]}],
                [{"writes": [{"point": CMD, "value": "on"}]}],
                [{"writes": [{"point": CMD, "value": 1}], "hold_minutes": 2, "within_minutes": 5}],
                [{"writes": [{"point": CMD, "value": 1}], "hold_minutes": 999}],
                [{"writes": [{"point": CMD, "value": 1}], "expect": [{"point": STS}]}], []):
        with pytest.raises((ValueError, TypeError)):
            active.validate_steps(bad, equip)


def test_plan_lists_every_write_and_caps_expiry_at_the_window():
    steps = active.validate_steps(active.command_response_steps(_equip(), 2), _equip())
    p = active.plan(steps, T0, T0 + timedelta(minutes=4))
    assert [(w["point"], w["value"], w["level"]) for w in p] == [(CMD, 1, 8), (CMD, 0, 8)]
    assert [datetime.fromisoformat(w["expires_at"]) for w in p] == [T0 + timedelta(minutes=2), T0 + timedelta(minutes=4)]
    with pytest.raises(ValueError, match="longer than the window"):
        active.plan(steps, T0, T0 + timedelta(minutes=3))
    with pytest.raises(ValueError):
        active.check_window(T0, T0 + timedelta(hours=9), T0)
    with pytest.raises(ValueError):
        active.check_window(T0, T0 - timedelta(minutes=1), T0)


def test_writes_allowed_needs_exactly_1(monkeypatch):
    monkeypatch.delenv("BALER_CX_ALLOW_WRITES", raising=False)
    assert not active.writes_allowed()
    monkeypatch.setenv("BALER_CX_ALLOW_WRITES", "true")
    assert not active.writes_allowed()
    monkeypatch.setenv("BALER_CX_ALLOW_WRITES", "1")
    assert active.writes_allowed()


# -------------------------------------------------------------- executing


def test_status_follows_command_passes_and_everything_is_relinquished():
    _, repo, ex = _setup()
    run = _planned(repo)
    ex.execute(run["id"])
    got = repo.get_row("cx_active_runs", run["id"])
    assert got["status"] == "completed", got
    assert [r["status"] for r in got["results"]] == ["pass", "pass"]
    actions = [(w["action"], w["point"], w.get("value")) for w in got["writes"]]
    assert actions[:2] == [("write", CMD, 1), ("relinquish", CMD, None)]
    assert ("write", CMD, 0) in actions and actions[-1][0] == "relinquish"
    assert repo.levels == {}  # nothing left held
    assert all(c["level"] == 8 and c["who"] == f"cx:{run['id']}" for c in repo.commands)
    assert all(w["expires_at"] for w in got["writes"] if w["action"] == "write")


def test_status_not_following_fails_but_still_relinquishes():
    _, repo, ex = _setup(follows=False)
    run = _planned(repo)
    ex.execute(run["id"])
    got = repo.get_row("cx_active_runs", run["id"])
    assert got["status"] == "completed" and got["results"][0]["status"] == "fail"
    assert got["results"][0]["expect"][0]["observed"] == 0.0
    assert repo.levels == {}


def test_a_refused_write_fails_the_run_and_relinquishes_what_was_written():
    site, repo, ex = _setup()
    site.fail_on = (CMD, 0)  # the second step's write
    run = _planned(repo)
    ex.execute(run["id"])
    got = repo.get_row("cx_active_runs", run["id"])
    assert got["status"] == "failed" and "refused" in got["error"]
    assert repo.levels == {} and got["writes"][-1]["action"] == "relinquish"


def test_abort_stops_between_polls():
    site, repo, ex = _setup()
    steps = [{"id": "hold", "writes": [{"point": CMD, "value": 1}], "hold_minutes": 30}]
    run = _planned(repo, steps)
    real_sleep = site.sleep

    def sleep(s):
        real_sleep(s)
        if site.now - T0 > timedelta(minutes=5):
            repo.update_row("cx_active_runs", run["id"], {"abort_requested": True})
    ex.sleep = sleep
    ex.execute(run["id"])
    got = repo.get_row("cx_active_runs", run["id"])
    assert got["status"] == "aborted" and repo.levels == {}
    assert site.now - T0 < timedelta(minutes=7)


def test_window_end_stops_the_run():
    site, repo, ex = _setup(follows=False)
    run = _planned(repo, minutes=4)
    site.now = T0 + timedelta(minutes=3)  # started late: step 1 runs to the window end, step 2 can't start
    ex.execute(run["id"])
    assert repo.get_row("cx_active_runs", run["id"])["status"] == "expired" and repo.levels == {}


def test_sweeper_relinquishes_orphans_only():
    _, repo, ex = _setup()
    running = _planned(repo)
    repo.update_row("cx_active_runs", running["id"], {"status": "running"})
    repo.command_point(CMD, 1, 8, f"cx:{running['id']}", None, T0 + timedelta(minutes=5))
    repo.command_point(SAT, 50, 8, "cx:gone", None, T0 + timedelta(minutes=5))  # its run doesn't exist
    repo.command_point(STS, 1, 8, "alice@example.com", None, None)  # a person's level: never touched
    assert active.sweep(repo, ex.writer, T0) == [(SAT, "cx:gone")]
    assert active.sweep(repo, ex.writer, T0 + timedelta(minutes=6)) == [(CMD, f"cx:{running['id']}")]  # expired
    assert list(repo.levels) == [(STS, 8)]


# ------------------------------------------------------------------- api


@pytest.fixture(scope="module")
def oas():
    return load_spec(OPENAPI_SPEC_PATH)


def _hdr(role, org="org-a"):
    import jwt as pyjwt
    tok = pyjwt.encode({"sub": "u-a", "org_id": org, "site_ids": [], "role": role, "kind": "session"}, api.JWT, algorithm="HS256")
    return {"X-Gateway-Secret": api.GATEWAY, "Authorization": f"Bearer {tok}"}


@pytest.fixture
def served(monkeypatch):
    from http.server import ThreadingHTTPServer

    from baler.commissioning import runner
    from baler.commissioning_api import make_handler

    site = Site()
    repo = db.MemoryRepo(store=store(), history=site.history, sites=api.SITES, scope_fn=api._scope, users={"u-a": "a@example.com"})
    ex = active.Executor(repo, active.Writer(repo, site.commander), site.clock, site.sleep)
    monkeypatch.setenv("BALER_GATEWAY_SECRET", api.GATEWAY)
    monkeypatch.setenv("BALER_JWT_SECRET", api.JWT)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(repo, clock=site.clock, worker=runner.RunWorker(repo, site.clock), executor=ex))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", repo, ex, site
    server.shutdown()


def _plan(base, pid, headers, **body):
    body = {"equip_uri": AHU, "window_start": T0.isoformat(), "window_end": (T0 + timedelta(hours=1)).isoformat()} | body
    return requests.post(f"{base}/projects/{pid}/active-runs", json=body, headers=headers)


def test_active_api_envelope(served, oas, monkeypatch):
    base, repo, ex, _ = served
    admin, operator = _hdr("admin"), _hdr("operator")
    p = api._project(base, headers=admin)
    assert _plan(base, p["id"], operator).status_code == 403  # admin only, in the backend too
    r = _plan(base, p["id"], admin)
    assert r.status_code == 201, r.text
    assert_matches_schema(r.json(), api._schema(oas, "/projects/{id}/active-runs", "post", 201), oas)
    run = r.json()
    assert run["status"] == "planned" and [w["value"] for w in run["plan"]] == [1, 0] and repo.commands == []  # dry run only
    rid = run["id"]
    # the switch is off
    monkeypatch.delenv("BALER_CX_ALLOW_WRITES", raising=False)
    assert requests.post(f"{base}/active-runs/{rid}/confirm", json={"confirm": rid}, headers=admin).status_code == 403
    # switch off answers first: a wrong confirm or an unknown run is still 403, not 400/404
    assert requests.post(f"{base}/active-runs/{rid}/confirm", json={"confirm": "nope"}, headers=admin).status_code == 403
    assert requests.post(f"{base}/active-runs/no-such-run/confirm", json={}, headers=admin).status_code == 403
    monkeypatch.setenv("BALER_CX_ALLOW_WRITES", "1")
    st = requests.get(f"{base}/active/status", headers=admin).json()
    assert st["writes_allowed"] is True and st["level"] == 8
    assert requests.post(f"{base}/active-runs/{rid}/confirm", json={"confirm": "yes"}, headers=admin).status_code == 400
    assert requests.post(f"{base}/active-runs/{rid}/confirm", json={"confirm": rid}, headers=operator).status_code == 403
    assert requests.post(f"{base}/active-runs/{rid}/confirm", json={"confirm": rid}, headers=_hdr("admin", "org-b")).status_code == 404
    c = requests.post(f"{base}/active-runs/{rid}/confirm", json={"confirm": rid}, headers=admin)
    assert c.status_code == 202, c.text
    assert c.json()["before"]["latest"][STS]["value"] == 0.0 and CMD in c.json()["before"]["priority_arrays"]
    ex.threads[rid].join(10)
    got = requests.get(f"{base}/active-runs/{rid}", headers=admin)
    assert_matches_schema(got.json(), api._schema(oas, "/active-runs/{id}", "get", 200), oas)
    assert got.json()["status"] == "completed" and repo.levels == {}
    assert requests.post(f"{base}/active-runs/{rid}/confirm", json={"confirm": rid}, headers=admin).status_code == 409
    events = [e["action"] for e in requests.get(f"{base}/projects/{p['id']}/events", headers=admin).json()]
    assert {"active_run.plan", "active_run.confirm", "active_run.completed"} <= set(events)
    assert [x["id"] for x in requests.get(f"{base}/projects/{p['id']}/active-runs", headers=admin).json()] == [rid]
    # the run report shows exactly what was commanded
    vr = requests.post(f"{base}/projects/{p['id']}/runs", json={}, headers=admin).json()
    api._wait(base, vr["id"], admin)
    html = requests.get(f"{base}/runs/{vr['id']}/report", params={"format": "html"}, headers=admin).text
    assert "everything commanded" in html and CMD in html


def test_active_api_window_cancel_and_validation(served, monkeypatch):
    base = served[0]
    admin = _hdr("admin")
    monkeypatch.setenv("BALER_CX_ALLOW_WRITES", "1")
    p = api._project(base, headers=admin)
    later = _plan(base, p["id"], admin, window_start=(T0 + timedelta(hours=2)).isoformat(),
                  window_end=(T0 + timedelta(hours=3)).isoformat()).json()
    assert requests.post(f"{base}/active-runs/{later['id']}/confirm", json={"confirm": later["id"]}, headers=admin).status_code == 409
    a = requests.post(f"{base}/active-runs/{later['id']}/abort", json={}, headers=_hdr("operator"))
    assert a.status_code == 202 and a.json()["status"] == "cancelled"
    for bad in ({"equip_uri": "urn:equip:nope"}, {"window_end": T0.isoformat()}, {"kind": "chaos"},
                {"kind": "custom", "steps": [{"writes": [{"point": STS, "value": 1}]}]},
                {"window_start": "soon"}):
        assert _plan(base, p["id"], admin, **bad).status_code == 400, bad
    k = requests.post(f"{base}/active/kill", json={}, headers=admin)
    assert k.status_code == 200 and k.json()["aborted"] == []
    assert requests.post(f"{base}/active/kill", json={}, headers=_hdr("operator")).status_code == 403

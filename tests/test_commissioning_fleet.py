"""Fleet active runs (Baler#275 slice d): one active test across many mock
terminal units in one window. Every #45 safeguard still applies per write
(each member is an ordinary active run); on top of that: per-system
concurrency and a stagger, an automatic stop at a failure threshold,
retest-failures-only, a dry run listing every write across the fleet, a
typed confirmation that is the plan's hash, and relinquish-everything on
kill, on the threshold and on a crash. A fake site and a fake clock only:
no services, no MQTT, no equipment."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from itertools import pairwise

import pytest
import requests
import test_commissioning_openapi as api
from conftest import assert_matches_schema, load_spec
from rdflib import RDF, Literal, URIRef

from baler.commissioning import active, db, fleet, inventory, runner
from baler.commissioning.history import InMemoryHistory
from baler.commissioning_api import OPENAPI_SPEC_PATH
from baler.scope import Scope
from baler.store import BRICK, HAYSTACK, TD, Store

T0 = datetime(2026, 11, 9, 22, 0, tzinfo=timezone.utc)


def ahu(a: int) -> str:
    return f"urn:equip:fbf/a/ahu-{a}"


def vav(a: int, i: int) -> str:
    return f"urn:equip:fbf/a/vav-{a}-{i}"


def cmd(equip: str) -> str:
    return equip.replace("urn:equip:", "urn:point:") + "/fan-c"


def sts(equip: str) -> str:
    return equip.replace("urn:equip:", "urn:point:") + "/fan-s"


def build_store(n_ahu: int, per_ahu: int) -> Store:
    """Fan-powered terminal units, each fed by one of n_ahu AHUs."""
    s = Store()
    g = s.graph
    for a in range(n_ahu):
        g.add((URIRef(ahu(a)), RDF.type, BRICK["Air_Handling_Unit"]))
        for i in range(per_ahu):
            e = URIRef(vav(a, i))
            g.add((e, RDF.type, BRICK["Variable_Air_Volume_Box"]))
            g.add((URIRef(ahu(a)), BRICK.feeds, e))
            for uri, cls, writable in ((cmd(str(e)), "Fan_Command", True), (sts(str(e)), "Fan_Status", False)):
                p = URIRef(uri)
                g.add((p, RDF.type, TD.RawPoint))
                g.add((p, RDF.type, BRICK[cls]))
                g.add((e, BRICK.hasPoint, p))
                g.add((p, BRICK.isPointOf, e))
                if writable:
                    g.add((p, HAYSTACK.hasTag, Literal("writable")))
    return s


class Site:
    """A fake clock and fake terminal units behind point_write: a unit's fan
    status follows its command 30 s later, unless the unit is in `bad`."""

    def __init__(self, store: Store, bad=()):
        self.now = T0
        self.bad = set(bad)
        self.lock = threading.Lock()
        equips = [str(e) for e in store.graph.subjects(RDF.type, BRICK["Variable_Air_Volume_Box"])]
        self.history = InMemoryHistory({p: [(T0 - timedelta(minutes=5), 0.0)] for e in equips for p in (cmd(e), sts(e))})
        self.on_write = None
        self.delay = timedelta(seconds=30)

    def clock(self):
        with self.lock:
            return self.now

    def sleep(self, s):
        with self.lock:
            self.now += timedelta(seconds=max(s, 1))
        time.sleep(0.0005)  # let the other threads in

    def commander(self, point, value, level):
        if value is None or not point.endswith("/fan-c"):
            return
        if self.on_write:
            self.on_write(point)
        equip = point.replace("urn:point:", "urn:equip:")[: -len("/fan-c")]
        with self.lock:
            now = self.now
        self.history.add(point, now, float(value))
        if equip not in self.bad:
            self.history.add(sts(equip), now + self.delay, float(value))


def setup(n_ahu=3, per_ahu=4, bad=()):
    st = build_store(n_ahu, per_ahu)
    site = Site(st, bad)
    repo = db.MemoryRepo(store=st, history=site.history)
    writer = active.Writer(repo, site.commander)
    ex = fleet.ChildExecutor(repo, writer, site.clock, site.sleep, poll_seconds=10)
    fr = fleet.FleetRunner(repo, ex, site.clock, site.sleep, poll_seconds=10)
    return site, repo, ex, fr


def equips(repo):
    return inventory.build(repo.store, [Scope.everything()])


def confirmed(repo, site, **body):
    body = {"equip_class": "Variable_Air_Volume_Box", "window_start": T0.isoformat(),
            "window_end": (T0 + timedelta(hours=8)).isoformat()} | body
    f = fleet.plan(repo, {"id": "p"}, equips(repo), body, "admin", site.clock(), body.pop("previous", None))
    f.update(status="confirmed", confirmed_by="admin", confirmed_at=site.clock(), heartbeat_at=site.clock())
    repo.put_rows(fleet.TABLE, [f])
    return f


def run_to_end(fr, ex, fid):
    fr.run(fid)
    for t in list(ex.threads.values()):
        t.join(30)
        assert not t.is_alive()


def children(repo, fid):
    return repo.find_rows("cx_active_runs", fleet_id=fid)


@pytest.fixture
def writes_on(monkeypatch):
    monkeypatch.setenv("BALER_CX_ALLOW_WRITES", "1")


# ------------------------------------------------------------------- pure


def test_schedule_obeys_per_system_and_global_limits_and_the_stagger():
    members = [{"equip_uri": f"e{s}{i}", "system": s, "minutes": 10} for s in "ABC" for i in range(4)]
    members = fleet.interleave(members)
    assert [m["system"] for m in members[:6]] == ["A", "B", "C", "A", "B", "C"]
    fleet.schedule(members, T0, T0 + timedelta(hours=8), 1, 2, timedelta(minutes=1))
    spans = [(datetime.fromisoformat(m["slot_start"]), datetime.fromisoformat(m["slot_end"]), m["system"]) for m in members]
    starts = [s for s, _, _ in spans]
    assert starts == sorted(starts) and all(b - a >= timedelta(minutes=1) for a, b in pairwise(starts))
    for t, _, _ in spans:
        live = [sys for s, e, sys in spans if s <= t < e]
        assert len(live) <= 2 and all(live.count(x) <= 1 for x in live)
    with pytest.raises(ValueError, match="doesn't fit the window"):
        fleet.schedule(fleet.interleave(members), T0, T0 + timedelta(minutes=30), 1, 2, timedelta(minutes=1))


def test_outcomes():
    assert fleet.outcome({"status": "running"}) is None
    assert fleet.outcome({"status": "completed", "results": [{"status": "pass"}, {"status": "fail"}]}) == "fail"
    assert fleet.outcome({"status": "completed", "results": [{"status": "pass"}]}) == "pass"
    assert fleet.outcome({"status": "completed", "results": [{"status": "inconclusive"}]}) == "inconclusive"
    assert fleet.outcome({"status": "failed", "error": "RuntimeError: refused"}) == "fail"
    assert fleet.outcome({"status": "failed", "error": db.INTERRUPTED}) == "aborted"
    assert fleet.outcome({"status": "aborted"}) == "aborted"


# -------------------------------------------------------------- executing


def test_real_run_obeys_concurrency_and_stagger_and_relinquishes_everything(writes_on):
    site, repo, ex, fr = setup(3, 4)
    site.delay = timedelta(seconds=90)  # each member runs ~3 min, so members 60 s apart overlap
    f = confirmed(repo, site, concurrency_per_system=1, max_concurrent=2, stagger_seconds=60)
    peak = {"global": 0, "system": 0}
    systems = {m["equip_uri"]: m["system"] for m in f["members"]}

    def on_write(point):
        live = [r for r in children(repo, f["id"]) if r["status"] in ("confirmed", "running")]
        by = [systems[r["equip_uri"]] for r in live]
        peak["global"] = max(peak["global"], len(live))
        peak["system"] = max([peak["system"]] + [by.count(s) for s in by])
    site.on_write = on_write
    run_to_end(fr, ex, f["id"])
    got = repo.get_row(fleet.TABLE, f["id"])
    assert got["status"] == "completed", got
    assert peak["global"] == 2 and peak["system"] == 1
    kids = children(repo, f["id"])
    assert len(kids) == 12 and all(k["status"] == "completed" for k in kids)
    assert all(m["outcome"] == "pass" for m in got["members"])
    starts = sorted(k["created_at"] for k in kids)
    assert all(b - a >= timedelta(seconds=60) for a, b in pairwise(starts))
    # every member is an ordinary Cx 9 run: priority 8 as cx:<its id>, every write expiring, before-state captured
    assert all(c["level"] == 8 and c["who"].startswith("cx:") for c in repo.commands)
    assert all(w["expires_at"] for k in kids for w in k["writes"] if w["action"] == "write")
    assert all(k["before"] and cmd(k["equip_uri"]) in k["before"]["priority_arrays"] for k in kids)
    assert repo.levels == {}
    assert {"fleet_run.completed"} <= {e["action"] for e in repo.events}


def test_failure_threshold_stops_the_fleet_and_relinquishes_everything(writes_on):
    bad = [vav(0, i) for i in range(4)] + [vav(1, i) for i in range(4)]
    site, repo, ex, fr = setup(3, 4, bad=bad)
    f = confirmed(repo, site, concurrency_per_system=1, max_concurrent=3, stagger_seconds=0, max_failures=2)
    run_to_end(fr, ex, f["id"])
    got = repo.get_row(fleet.TABLE, f["id"])
    assert got["status"] == "stopped" and "failure threshold" in got["stop_reason"]
    assert got["failures"] >= 2
    not_run = [m["equip_uri"] for m in got["members"] if m["state"] == "not_run"]
    assert not_run, "the fleet stopped before every member ran"
    commanded = {c["point"] for c in repo.commands}
    assert not any(cmd(e) in commanded for e in not_run)  # nothing after the stop touched them
    assert repo.levels == {}
    assert "fleet_run.stop" in {e["action"] for e in repo.events}


def test_switch_off_mid_fleet_stops_it(monkeypatch, writes_on):
    site, repo, ex, fr = setup(2, 3)
    f = confirmed(repo, site, concurrency_per_system=1, max_concurrent=1, stagger_seconds=0)
    site.on_write = lambda point: monkeypatch.setenv("BALER_CX_ALLOW_WRITES", "0")
    run_to_end(fr, ex, f["id"])
    got = repo.get_row(fleet.TABLE, f["id"])
    assert got["status"] == "stopped" and "BALER_CX_ALLOW_WRITES" in got["stop_reason"]
    assert len(children(repo, f["id"])) == 1 and repo.levels == {}


def test_a_member_started_after_the_fleet_is_flagged_never_writes(writes_on):
    """The race the kill switch could lose: a coordinator that starts a
    member just after the fleet was flagged. ChildExecutor checks the
    fleet before the first write."""
    site, repo, ex, _ = setup(1, 1)
    f = confirmed(repo, site)
    m = f["members"][0]
    child = active.new_run("p", m["equip_uri"], f["kind"], m["steps"], T0, T0 + timedelta(hours=1), "admin", T0)
    child.update(status="confirmed", fleet_id=f["id"])
    repo.put_rows("cx_active_runs", [child])
    repo.update_row(fleet.TABLE, f["id"], {"abort_requested": True})
    ex.execute(child["id"])
    assert repo.get_row("cx_active_runs", child["id"])["status"] == "aborted" and repo.commands == []


def test_retest_failures_only_touches_only_equipment_that_failed(writes_on):
    bad = [vav(0, 1), vav(2, 3)]
    site, repo, ex, fr = setup(3, 4, bad=bad)
    first = confirmed(repo, site, concurrency_per_system=2, max_concurrent=6, stagger_seconds=0, max_failures=10)
    run_to_end(fr, ex, first["id"])
    first = repo.get_row(fleet.TABLE, first["id"])
    assert first["status"] == "completed" and sorted(fleet.failed_members(first)) == sorted(bad)
    site.bad.clear()  # the contractor fixed them
    before = len(repo.commands)
    retest = confirmed(repo, site, window_start=site.clock().isoformat(),
                       window_end=(site.clock() + timedelta(hours=2)).isoformat(), previous=first)
    assert retest["retest_of"] == first["id"] and sorted(m["equip_uri"] for m in retest["members"]) == sorted(bad)
    assert {w["equip_uri"] for w in retest["plan"]} == set(bad)
    ex.threads.clear()
    run_to_end(fr, ex, retest["id"])
    got = repo.get_row(fleet.TABLE, retest["id"])
    assert got["status"] == "completed" and all(m["outcome"] == "pass" for m in got["members"])
    touched = {c["point"] for c in repo.commands[before:]}
    assert touched == {cmd(e) for e in bad}
    assert repo.levels == {}
    # nothing failed this time: nothing to retest
    with pytest.raises(ValueError, match="nothing to retest"):
        confirmed(repo, site, window_start=site.clock().isoformat(),
                  window_end=(site.clock() + timedelta(hours=1)).isoformat(), previous=got)


class Frozen(fleet.ChildExecutor):
    """A member that has written its first step and then the process died:
    the levels stay held and the row stays `running`."""

    def start(self, run_id):
        run = self.repo.get_row("cx_active_runs", run_id)
        self.repo.update_row("cx_active_runs", run_id, {"status": "running", "started_at": self.clock()})
        for w in run["steps"][0]["writes"]:
            self.writer.write(w["point"], w["value"], run_id, "admin", self.clock() + timedelta(minutes=2))


def _crashed(n_members=3):
    site, repo, _, _ = setup(1, n_members)
    writer = active.Writer(repo, site.commander)
    frozen = Frozen(repo, writer, site.clock, site.sleep)
    fr = fleet.FleetRunner(repo, frozen, site.clock, site.sleep)
    f = confirmed(repo, site, concurrency_per_system=n_members, max_concurrent=n_members, stagger_seconds=0)
    repo.update_row(fleet.TABLE, f["id"], {"status": "running"})
    assert fr.tick(f["id"]) is None
    assert len(repo.levels) == n_members  # every member holds a level; the coordinator is "gone" now
    return site, repo, writer, f


def test_process_crash_recovery_relinquishes_from_persisted_fleet_state(writes_on):
    site, repo, _, f = _crashed()
    # a new process: a new writer, only what was persisted
    writer = active.Writer(repo, site.commander)
    hit = fleet.recover(repo, writer, site.clock())
    assert [x["id"] for x in hit] == [f["id"]]
    got = repo.get_row(fleet.TABLE, f["id"])
    assert got["status"] == "failed" and got["error"] == db.INTERRUPTED and got["abort_requested"]
    assert all(k["status"] == "failed" for k in children(repo, f["id"]))
    assert repo.levels == {}


def test_a_coordinator_that_stops_beating_is_swept(writes_on):
    site, repo, writer, f = _crashed()
    assert fleet.sweep(repo, writer, site.clock() + timedelta(minutes=1)) == []  # still within its heartbeat
    assert len(repo.levels) == 3
    done = fleet.sweep(repo, writer, site.clock() + fleet.HEARTBEAT_STALE + timedelta(seconds=1))
    assert len(done) == 3 and repo.levels == {}
    got = repo.get_row(fleet.TABLE, f["id"])
    assert got["status"] == "failed" and "stopped responding" in got["error"]
    assert all(k["abort_requested"] for k in children(repo, f["id"]))


def test_the_scheduler_tick_runs_every_sweeper_even_if_one_fails():
    calls = []

    def boom(now):
        raise RuntimeError("one sweeper failing")
    s = runner.Scheduler(db.MemoryRepo(), None, lambda: T0, every_s=3600, extra=[boom, calls.append])
    s.stop()
    s.tick()
    assert calls == [T0]


# ------------------------------------------------------------------- api


@pytest.fixture(scope="module")
def oas():
    return load_spec(OPENAPI_SPEC_PATH)


def _hdr(role, org="org-a"):
    import jwt as pyjwt
    tok = pyjwt.encode({"sub": "u-a", "org_id": org, "site_ids": [], "role": role, "kind": "session"}, api.JWT, algorithm="HS256")
    return {"X-Gateway-Secret": api.GATEWAY, "Authorization": f"Bearer {tok}"}


def _served(monkeypatch, n_ahu, per_ahu, bad=()):
    from http.server import ThreadingHTTPServer

    from baler.commissioning_api import make_handler

    st = build_store(n_ahu, per_ahu)
    site = Site(st, bad)
    repo = db.MemoryRepo(store=st, history=site.history, sites=api.SITES, scope_fn=api._scope, users={"u-a": "a@example.com"})
    writer = active.Writer(repo, site.commander)
    ex = active.Executor(repo, writer, site.clock, site.sleep)
    fr = fleet.FleetRunner(repo, fleet.ChildExecutor(repo, writer, site.clock, site.sleep), site.clock, site.sleep)
    monkeypatch.setenv("BALER_GATEWAY_SECRET", api.GATEWAY)
    monkeypatch.setenv("BALER_JWT_SECRET", api.JWT)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(repo, clock=site.clock, worker=runner.RunWorker(repo, site.clock),
                                                                executor=ex, fleet_runner=fr))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", repo, fr, site


@pytest.fixture
def big(monkeypatch):
    server, base, repo, fr, site = _served(monkeypatch, 4, 30)  # 120 terminal units
    yield base, repo, fr, site
    server.shutdown()


@pytest.fixture
def small(monkeypatch):
    server, base, repo, fr, site = _served(monkeypatch, 2, 3, bad=[vav(a, i) for a in range(2) for i in range(3)])
    yield base, repo, fr, site
    server.shutdown()


def _plan(base, pid, headers, **body):
    body = {"equip_class": "Variable_Air_Volume_Box", "window_start": T0.isoformat(),
            "window_end": (T0 + timedelta(hours=8)).isoformat()} | body
    return requests.post(f"{base}/projects/{pid}/fleet-runs", json=body, headers=headers)


def test_dry_run_over_120_terminal_units_lists_every_write(big, oas, monkeypatch):
    base, repo, _, _ = big
    monkeypatch.delenv("BALER_CX_ALLOW_WRITES", raising=False)  # planning is allowed with the switch off
    admin = _hdr("admin")
    p = api._project(base, headers=admin)
    assert _plan(base, p["id"], _hdr("operator")).status_code == 403
    r = _plan(base, p["id"], admin, concurrency_per_system=2, max_concurrent=8, stagger_seconds=30)
    assert r.status_code == 201, r.text
    assert_matches_schema(r.json(), api._schema(oas, "/projects/{id}/fleet-runs", "post", 201), oas)
    f = r.json()
    assert f["status"] == "planned" and f["writes_allowed"] is False and repo.commands == []  # nothing commanded
    assert len(f["members"]) == 120 and f["skipped"] == []
    want = {(vav(a, i), cmd(vav(a, i)), v) for a in range(4) for i in range(30) for v in (1, 0)}
    assert {(w["equip_uri"], w["point"], w["value"]) for w in f["plan"]} == want and len(f["plan"]) == 240
    assert all(w["level"] == 8 and w["expires_at"] <= f["window_end"] and w["not_before"] for w in f["plan"])
    assert {m["system"] for m in f["members"]} == {ahu(a) for a in range(4)}
    assert f["plan_hash"] == fleet.plan_hash(repo.get_row(fleet.TABLE, f["id"]))
    # the schedule keeps the limits
    spans = [(m["slot_start"], m["slot_end"], m["system"]) for m in f["members"]]
    for t, _, _ in spans:
        live = [sys for s, e, sys in spans if s <= t < e]
        assert len(live) <= 8 and all(live.count(x) <= 2 for x in live)
    listed = requests.get(f"{base}/projects/{p['id']}/fleet-runs", headers=_hdr("viewer")).json()
    assert [x["id"] for x in listed] == [f["id"]]
    got = requests.get(f"{base}/fleet-runs/{f['id']}", headers=_hdr("viewer"))
    assert_matches_schema(got.json(), api._schema(oas, "/fleet-runs/{id}", "get", 200), oas)
    assert requests.get(f"{base}/fleet-runs/{f['id']}", headers=_hdr("admin", "org-b")).status_code == 404
    for bad in ({"window_end": (T0 + timedelta(minutes=30)).isoformat()},  # doesn't fit
                {"kind": "custom"}, {"equip_class": None}, {"equip_uris": ["urn:equip:nope"]},
                {"max_failures": 0}, {"concurrency_per_system": 0}, {"stagger_seconds": -1}, {"kind": "library", "test": "nope"}):
        assert _plan(base, p["id"], admin, **bad).status_code == 400, bad
    assert "fleet_run.plan" in {e["action"] for e in repo.events}


def _wait(cond, timeout=20.0):
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def test_the_typed_confirmation_is_the_exact_plan(big, monkeypatch):
    base, repo, fr, _ = big
    admin = _hdr("admin")
    p = api._project(base, headers=admin)
    members = [vav(a, 0) for a in range(4)]
    f = _plan(base, p["id"], admin, equip_class=None, equip_uris=members, stagger_seconds=0).json()
    fid, code = f["id"], f["plan_hash"]
    confirm = lambda body, h=admin: requests.post(f"{base}/fleet-runs/{fid}/confirm", json=body, headers=h)
    monkeypatch.delenv("BALER_CX_ALLOW_WRITES", raising=False)
    assert confirm({"confirm": code}).status_code == 403  # the switch answers first
    monkeypatch.setenv("BALER_CX_ALLOW_WRITES", "1")
    assert confirm({"confirm": code}, _hdr("operator")).status_code == 403
    assert confirm({"confirm": code}, _hdr("admin", "org-b")).status_code == 404
    assert confirm({"confirm": fid}).status_code == 400  # the id isn't the plan
    assert confirm({"confirm": code[:12]}).status_code == 400
    # the stored plan changes: the code the admin read no longer confirms it
    row = repo.get_row(fleet.TABLE, fid)
    changed = [dict(m) for m in row["members"]]
    changed[0] = {**changed[0], "steps": [{**changed[0]["steps"][0], "hold_minutes": 30.0, "within_minutes": 30.0}]
                  + changed[0]["steps"][1:]}
    repo.update_row(fleet.TABLE, fid, {"members": changed})
    r = confirm({"confirm": code})
    assert r.status_code == 409 and "plan changed" in r.text
    repo.update_row(fleet.TABLE, fid, {"members": row["members"]})
    # the inventory changes: a member's command isn't writable any more
    tag = (URIRef(cmd(members[2])), HAYSTACK.hasTag, Literal("writable"))
    repo.store.graph.remove(tag)
    r = confirm({"confirm": code})
    assert r.status_code == 409 and members[2] in r.text
    repo.store.graph.add(tag)
    assert repo.commands == []
    c = confirm({"confirm": code})
    assert c.status_code == 202, c.text
    fr.threads[fid].join(30)
    got = repo.get_row(fleet.TABLE, fid)
    assert got["status"] == "completed" and got["confirmed_by"] == "u-a" and repo.levels == {}
    assert confirm({"confirm": code}).status_code == 409
    events = [e for e in repo.events if e["action"] == "fleet_run.confirm"]
    assert len(events) == 1 and events[0]["detail"]["plan_hash"] == code


def _held(repo, fid):
    ids = {k["id"] for k in children(repo, fid)}
    return [k for k, v in repo.levels.items() if v["who"][3:] in ids]


def test_kill_switch_stops_the_fleet_and_relinquishes_everything(small, monkeypatch):
    base, repo, fr, _ = small  # nothing follows: every member holds its write for the whole response time
    monkeypatch.setenv("BALER_CX_ALLOW_WRITES", "1")
    admin = _hdr("admin")
    p = api._project(base, headers=admin)
    f = _plan(base, p["id"], admin, response_minutes=30, concurrency_per_system=1, max_concurrent=2, stagger_seconds=0,
              max_failures=6).json()
    assert requests.post(f"{base}/fleet-runs/{f['id']}/confirm", json={"confirm": f["plan_hash"]}, headers=admin).status_code == 202
    _wait(lambda: _held(repo, f["id"]))
    k = requests.post(f"{base}/active/kill", json={}, headers=admin)
    assert k.status_code == 200 and k.json()["fleets"] == [f["id"]] and k.json()["relinquished"]
    started = len(children(repo, f["id"]))
    fr.threads[f["id"]].join(30)
    got = repo.get_row(fleet.TABLE, f["id"])
    assert got["status"] == "aborted" and got["stop_reason"] == "kill switch"
    assert len(children(repo, f["id"])) == started  # nothing started after the kill
    assert repo.levels == {}
    assert {"fleet_run.kill", "fleet_run.aborted"} <= {e["action"] for e in repo.events}


def test_abort_one_fleet_relinquishes_everything(small, monkeypatch):
    base, repo, fr, _ = small
    monkeypatch.setenv("BALER_CX_ALLOW_WRITES", "1")
    admin = _hdr("admin")
    p = api._project(base, headers=admin)
    f = _plan(base, p["id"], admin, response_minutes=30, concurrency_per_system=1, max_concurrent=2, stagger_seconds=0,
              max_failures=6).json()
    requests.post(f"{base}/fleet-runs/{f['id']}/confirm", json={"confirm": f["plan_hash"]}, headers=admin)
    _wait(lambda: _held(repo, f["id"]))
    a = requests.post(f"{base}/fleet-runs/{f['id']}/abort", json={}, headers=_hdr("operator"))
    assert a.status_code == 202 and a.json()["abort_requested"] is True
    assert _held(repo, f["id"]) == []  # relinquished at once, not at the next poll
    fr.threads[f["id"]].join(30)
    assert repo.get_row(fleet.TABLE, f["id"])["status"] == "aborted" and repo.levels == {}
    # a planned fleet is cancelled; a finished one can't be confirmed
    g = _plan(base, p["id"], admin).json()
    assert requests.post(f"{base}/fleet-runs/{g['id']}/abort", json={}, headers=admin).json()["status"] == "cancelled"
    assert requests.post(f"{base}/fleet-runs/{g['id']}/confirm", json={"confirm": g["plan_hash"]}, headers=admin).status_code == 409


def test_retest_failures_only_over_the_api(small, monkeypatch):
    base, repo, fr, site = small
    monkeypatch.setenv("BALER_CX_ALLOW_WRITES", "1")
    site.bad = {vav(1, 2)}
    admin = _hdr("admin")
    p = api._project(base, headers=admin)
    f = _plan(base, p["id"], admin, concurrency_per_system=3, max_concurrent=6, stagger_seconds=0).json()
    requests.post(f"{base}/fleet-runs/{f['id']}/confirm", json={"confirm": f["plan_hash"]}, headers=admin)
    fr.threads[f["id"]].join(30)
    assert repo.get_row(fleet.TABLE, f["id"])["status"] == "completed"
    r = _plan(base, p["id"], admin, equip_class=None, retest_of=f["id"], window_start=site.clock().isoformat(),
              window_end=(site.clock() + timedelta(hours=1)).isoformat())
    assert r.status_code == 201, r.text
    assert [m["equip_uri"] for m in r.json()["members"]] == [vav(1, 2)] and r.json()["retest_of"] == f["id"]
    assert {w["point"] for w in r.json()["plan"]} == {cmd(vav(1, 2))}
    # a fleet still planned can't be retested
    assert _plan(base, p["id"], admin, retest_of=r.json()["id"]).status_code == 400

"""The standard active-test library as data (Baler#275, slice c): every test
states its limits and expected results, compiles to concrete Cx 9 steps
against the latest values, refuses anything outside its limits, and runs
through the unchanged active-test envelope. A mock controller only."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import jwt as pyjwt
import pytest
import requests
import test_commissioning_openapi as api
from rdflib import RDF, Literal, URIRef

from baler.commissioning import active, db, inventory, procedures, runner, test_library
from baler.commissioning.history import InMemoryHistory
from baler.commissioning_api import make_handler
from baler.scope import Scope
from baler.store import BRICK, HAYSTACK, TD, Store

T0 = datetime(2026, 11, 9, 14, 0, tzinfo=timezone.utc)
AHU = "urn:equip:fbf/a/ahu-9"
VAV = "urn:equip:fbf/a/vav-9"
P = {k: f"urn:point:fbf/a/{k}" for k in ("ahu-9/sf-c", "ahu-9/sf-s", "ahu-9/sat-sp", "ahu-9/sat",
                                         "vav-9/dmp", "vav-9/rhv", "vav-9/flow", "vav-9/dat")}
POINTS = {AHU: (("ahu-9/sf-c", "Fan_Command", True), ("ahu-9/sf-s", "Fan_Status", False),
                ("ahu-9/sat-sp", "Supply_Air_Temperature_Setpoint", True), ("ahu-9/sat", "Supply_Air_Temperature_Sensor", False)),
          VAV: (("vav-9/dmp", "Damper_Position_Command", True), ("vav-9/rhv", "Valve_Command", True),
                ("vav-9/flow", "Air_Flow_Sensor", False), ("vav-9/dat", "Discharge_Air_Temperature_Sensor", False))}


def store() -> Store:
    s = Store()
    g = s.graph
    for equip, cls in ((AHU, "Air_Handling_Unit"), (VAV, "Variable_Air_Volume_Box")):
        e = URIRef(equip)
        g.add((e, RDF.type, BRICK[cls]))
        for key, pcls, writable in POINTS[equip]:
            p = URIRef(P[key])
            g.add((p, RDF.type, TD.RawPoint))
            g.add((p, RDF.type, BRICK[pcls]))
            g.add((e, BRICK.hasPoint, p))
            g.add((p, BRICK.isPointOf, e))
            if writable:
                g.add((p, HAYSTACK.hasTag, Literal("writable")))
    return s


def equip(uri):
    return next(e for e in inventory.build(store(), [Scope.everything()]) if e["id"] == uri)


class Plant:
    """A fake clock and a fake AHU behind point_write: the fan status
    follows its command, supply air settles near its setpoint a minute
    after a write."""

    def __init__(self):
        self.now = T0
        self.lock = threading.Lock()
        self.history = InMemoryHistory({P["ahu-9/sf-s"]: [(T0 - timedelta(minutes=5), 0.0)], P["ahu-9/sat-sp"]: [(T0 - timedelta(minutes=5), 58.0)],
                                        P["ahu-9/sat"]: [(T0 - timedelta(minutes=5), 58.4)], P["ahu-9/sf-c"]: [(T0 - timedelta(minutes=5), 0.0)]})

    def clock(self):
        with self.lock:
            return self.now

    def sleep(self, s):
        with self.lock:
            self.now += timedelta(seconds=max(s, 1))

    def commander(self, point, value, level):
        if value is None:
            return
        self.history.add(point, self.now, float(value))
        if point == P["ahu-9/sf-c"]:
            self.history.add(P["ahu-9/sf-s"], self.now + timedelta(seconds=20), float(value))
        if point == P["ahu-9/sat-sp"]:
            self.history.add(P["ahu-9/sat"], self.now + timedelta(minutes=1), float(value) + 0.5)


# ------------------------------------------------------------------ pure


def test_every_library_test_states_limits_and_expected_results():
    test_library.check_library()
    assert {t["id"] for t in test_library.TESTS} >= {"vav_reheat", "ahu_basic", "chw_loop"}
    for t in test_library.TESTS:
        assert t["expected"] and t["limits"]["writes"] and t["limits"]["max_hold_minutes"] <= active.MAX_HOLD_MINUTES
    broken = [{**test_library.BY_ID["ahu_basic"], "limits": {"writes": {}, "max_hold_minutes": 20}}]
    with pytest.raises(ValueError, match="stated limit"):
        test_library.check_library(broken)
    too_long = [{**test_library.BY_ID["ahu_basic"], "limits": {**test_library.BY_ID["ahu_basic"]["limits"], "max_hold_minutes": 90}}]
    with pytest.raises(ValueError, match="60"):
        test_library.check_library(too_long)


def test_compile_resolves_relative_values_and_enforces_limits():
    latest = {P["vav-9/flow"]: (T0, 200.0), P["vav-9/dat"]: (T0, 60.0)}
    steps = active.validate_steps(test_library.compile_steps("vav_reheat", equip(VAV), latest), equip(VAV))
    assert steps[0]["writes"] == [{"point": P["vav-9/dmp"], "value": 100}] and steps[0]["expect"] == [{"point": P["vav-9/flow"], "min": 250.0}]
    assert steps[1]["expect"] == [{"point": P["vav-9/dat"], "min": 65.0}]
    ahu = test_library.compile_steps("ahu_basic", equip(AHU), {P["ahu-9/sat-sp"]: (T0, 58.0)})
    assert ahu[1]["writes"][1] == {"point": P["ahu-9/sat-sp"], "value": 55}
    assert ahu[1]["expect"] == [{"point": P["ahu-9/sat"], "min": 53, "max": 57}]
    with pytest.raises(ValueError, match="outside its safety limit 50..65"):
        test_library.compile_steps("ahu_basic", equip(AHU), {P["ahu-9/sat-sp"]: (T0, 52.0)})  # 52 - 3 = 49
    with pytest.raises(ValueError, match="needs a recent value"):
        test_library.compile_steps("ahu_basic", equip(AHU), {})
    with pytest.raises(ValueError, match="no single Pump_Command"):
        test_library.compile_steps("chw_loop", equip(AHU), {})
    assert test_library.applies("vav_reheat", equip(VAV)) and not test_library.applies("vav_reheat", equip(AHU))


def test_a_procedure_step_can_name_a_library_test():
    proc = procedures.normalize({"name": "x", "kind": "functional", "equip_class": "Air_Handling_Unit", "steps": [
        {"id": "a", "methods": ["active"], "active": {"kind": "library", "test": "ahu_basic"}}]})
    assert proc["steps"][0]["active"] == {"kind": "library", "test": "ahu_basic"}
    for bad in ({"kind": "library"}, {"kind": "library", "test": "nope"}, {"kind": "command_response", "test": "ahu_basic"}):
        with pytest.raises(ValueError):
            procedures.normalize({"name": "x", "kind": "functional", "equip_class": "Air_Handling_Unit",
                                  "steps": [{"id": "a", "methods": ["active"], "active": bad}]})


# ------------------------------------------------------------------- api


def _hdr(role="admin", sub="u-adm"):
    tok = pyjwt.encode({"sub": sub, "org_id": "org-a", "site_ids": [], "role": role, "kind": "session"}, api.JWT, algorithm="HS256")
    return {"X-Gateway-Secret": api.GATEWAY, "Authorization": f"Bearer {tok}"}


@pytest.fixture
def served(monkeypatch):
    plant = Plant()
    repo = db.MemoryRepo(store=store(), history=plant.history, sites=api.SITES, scope_fn=api._scope)
    ex = active.Executor(repo, active.Writer(repo, plant.commander), plant.clock, plant.sleep)
    monkeypatch.setenv("BALER_GATEWAY_SECRET", api.GATEWAY)
    monkeypatch.setenv("BALER_JWT_SECRET", api.JWT)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(repo, clock=plant.clock, worker=runner.RunWorker(repo, plant.clock),
                                                                executor=ex))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", repo, ex, plant
    server.shutdown()


def test_library_test_runs_through_the_unchanged_envelope_and_judges_a_procedure_step(served, monkeypatch):
    base, repo, ex, plant = served
    admin = _hdr()
    lib = requests.get(f"{base}/active/library", headers=_hdr("viewer", "u-v")).json()
    assert {t["id"] for t in lib} >= {"vav_reheat", "ahu_basic", "chw_loop"}
    p = api._project(base, headers=admin, phase="functional")
    proc = requests.post(f"{base}/projects/{p['id']}/procedures", headers=admin, json={
        "name": "AHU-9 FPT", "kind": "functional", "equip_class": "Air_Handling_Unit", "window_hours": 1,
        "steps": [{"id": "std", "title": "Standard AHU test", "methods": ["active"], "active": {"kind": "library", "test": "ahu_basic"}}]}).json()
    window = {"window_start": plant.clock().isoformat(), "window_end": (plant.clock() + timedelta(hours=1)).isoformat()}
    url = f"{base}/projects/{p['id']}/active-runs"
    assert requests.post(url, json={"equip_uri": AHU, "kind": "library", "test": "vav_reheat"} | window, headers=admin).status_code == 400
    assert requests.post(url, json={"equip_uri": AHU, "kind": "library", "test": "ahu_basic"} | window,
                         headers=_hdr("operator", "u-a")).status_code == 403
    tr = requests.post(f"{base}/procedures/{proc['id']}/runs", json={}, headers=admin).json()
    plan = requests.post(url, json={"equip_uri": AHU, "kind": "library", "test": "ahu_basic"} | window, headers=admin)
    assert plan.status_code == 201, plan.text
    run = plan.json()
    assert run["kind"] == "library:ahu_basic" and repo.commands == []  # the dry run lists, writes nothing
    assert [(w["point"], w["value"], w["level"]) for w in run["plan"]] == [
        (P["ahu-9/sf-c"], 1, 8), (P["ahu-9/sf-c"], 1, 8), (P["ahu-9/sat-sp"], 55, 8)]
    monkeypatch.delenv("BALER_CX_ALLOW_WRITES", raising=False)
    assert requests.post(f"{base}/active-runs/{run['id']}/confirm", json={"confirm": run["id"]}, headers=admin).status_code == 403
    monkeypatch.setenv("BALER_CX_ALLOW_WRITES", "1")
    assert requests.post(f"{base}/active-runs/{run['id']}/confirm", json={"confirm": run["id"]}, headers=admin).status_code == 202
    ex.threads[run["id"]].join(10)
    done = repo.get_row("cx_active_runs", run["id"])
    assert done["status"] == "completed" and [r["status"] for r in done["results"]] == ["pass", "pass"]
    assert repo.levels == {}  # everything relinquished
    got = requests.get(f"{base}/test-runs/{tr['id']}", headers=admin).json()
    step = got["equipment"][0]["steps"][0]
    assert (step["method"], step["status"], step["active"]["active_run_id"]) == ("active", "pass", run["id"])

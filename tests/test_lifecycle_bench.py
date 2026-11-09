"""Bench-test records on assets (#211), no services."""

from datetime import datetime, timezone

import pytest

from baler.lifecycle import bench, routes, rows

POS = "urn:equip:fbf/hosp/ahu-1/chw-valve"


class Client:
    def __init__(self):
        self.rows = rows.MemoryRows()
        self.now = datetime(2026, 6, 1, tzinfo=timezone.utc)

    def __call__(self, method, path, body=None, **query):
        req = routes.Request(method=method, path=path, rows=self.rows, org_id="org1", body=body, now=self.now,
                             query={k: str(v) for k, v in query.items()}, who={"sub": "u1"})
        return routes.dispatch(req)

    def ok(self, *a, **kw):
        st, out = self(*a, **kw)
        assert st in (200, 201), out
        return out


@pytest.fixture
def c():
    return Client()


def _proc(c):
    t = bench.TEMPLATES["actuator-stroke"]
    steps = [dict(s) for s in t["steps"]]
    for s in steps:
        if s["id"] == "stroke-time":
            s["criterion"] = {"range": {"max": 95}}
        if s["id"] == "feedback-0":
            s["criterion"] = {"range": {"min": -2, "max": 2}}
        if s["id"] == "feedback-100":
            s["criterion"] = {"range": {"min": 98, "max": 102}}
    return c.ok("POST", "/bench/procedures", {"template": "actuator-stroke", "steps": steps})


GOOD = {"stroke-open": True, "stroke-closed": True, "stroke-time": 88, "feedback-0": 0.4, "feedback-100": 99.1}


def test_first_passing_bench_test_before_install_is_the_baseline(c):
    p = _proc(c)
    a = c.ok("POST", "/assets", {"asset_type": "actuator", "serial": "ACT-1"})
    bad = c.ok("POST", f"/assets/{a['id']}/bench-tests", {"procedure_id": p["id"], "tested_at": "2026-02-01",
                                                          "measurements": {**GOOD, "stroke-time": 140}})
    assert bad["verdict"] == "fail" and bad["before_install"]
    assert next(r for r in bad["results"] if r["step_id"] == "stroke-time")["why"] == "outside ..95.0"
    good = c.ok("POST", f"/assets/{a['id']}/bench-tests", {"procedure_id": p["id"], "tested_at": "2026-02-02",
                                                           "measurements": GOOD, "technician": "J. Tech",
                                                           "instruments": "Fluke 789 #123"})
    assert good["verdict"] == "pass"
    c.ok("POST", f"/assets/{a['id']}/install", {"position_uri": POS, "at": "2026-03-01"})
    later = c.ok("POST", f"/assets/{a['id']}/bench-tests", {"procedure_id": p["id"], "tested_at": "2026-05-01",
                                                            "measurements": GOOD})
    assert not later["before_install"]
    base = c.ok("GET", f"/assets/{a['id']}/baseline")
    assert base["bench"]["id"] == good["id"] and base["first_install"].startswith("2026-03-01")
    flags = {t["id"]: t["baseline"] for t in c.ok("GET", f"/assets/{a['id']}/bench-tests")}
    assert flags == {bad["id"]: False, good["id"]: True, later["id"]: False}


def test_the_procedure_is_snapshotted_so_edits_dont_rewrite_history(c):
    p = _proc(c)
    a = c.ok("POST", "/assets", {"serial": "ACT-2"})
    t = c.ok("POST", f"/assets/{a['id']}/bench-tests", {"procedure_id": p["id"], "measurements": GOOD})
    c.ok("PATCH", f"/bench/procedures/{p['id']}", {"steps": [{"id": "x", "title": "x", "criterion": {"manual": True}}]})
    again = c.ok("GET", f"/assets/{a['id']}/bench-tests")[0]
    assert again["procedure"]["steps"] == t["procedure"]["steps"] and len(again["procedure"]["steps"]) == 5


def test_template_limits_are_blank_so_results_are_inconclusive_until_set(c):
    p = c.ok("POST", "/bench/procedures", {"template": "temperature-sensor"})
    a = c.ok("POST", "/assets", {"serial": "TS-1"})
    t = c.ok("POST", f"/assets/{a['id']}/bench-tests", {"procedure_id": p["id"],
                                                        "measurements": {"low-point": 0.1, "high-point": -0.2}})
    assert t["verdict"] == "inconclusive" and {r["why"] for r in t["results"]} == {"no limits set for this step"}
    assert c.ok("GET", f"/assets/{a['id']}/baseline")["bench"] is None


def test_one_off_procedure_and_validation(c):
    a = c.ok("POST", "/assets", {"serial": "X"})
    t = c.ok("POST", f"/assets/{a['id']}/bench-tests", {
        "procedure": {"name": "Quick check", "steps": [{"id": "on", "title": "Powers on", "criterion": {"manual": True}},
                                                       {"id": "fw", "title": "Firmware", "criterion": {"equals": "3.2"}}]},
        "measurements": {"on": True, "fw": "3.1"}})
    assert t["verdict"] == "fail" and t["procedure_id"] is None
    assert c("POST", f"/assets/{a['id']}/bench-tests", {"procedure": {"name": "q", "steps": []}})[0] == 400
    assert c("POST", f"/assets/{a['id']}/bench-tests", {"procedure": {"name": "q", "steps": [
        {"id": "on", "title": "On", "criterion": {"manual": True}}]}, "measurements": {"nope": 1}})[0] == 400
    assert c("POST", f"/assets/{a['id']}/bench-tests", {"procedure": {"name": "q", "steps": [
        {"id": "on", "title": "On", "criterion": {"manual": True}}]}, "measurements": {"on": "yes"}})[0] == 400
    with pytest.raises(ValueError):
        bench.normalize_steps([{"id": "a", "title": "a", "criterion": {"range": {"min": 5, "max": 1}}}])
    assert len(c.ok("GET", "/bench/templates")) == len(bench.TEMPLATES)

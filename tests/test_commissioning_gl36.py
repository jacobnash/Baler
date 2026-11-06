"""Cx 3 (#39): ConStrain items over Baler history via commissioning.gl36,
on the mock-hospital-shaped fixture. Needs pandas (the cx-gl36 extra)."""

from __future__ import annotations

from datetime import timedelta

import cx_hospital as hx
import pytest

pytest.importorskip("pandas")

from baler.commissioning import (
    engine,
    gl36,
    inventory,
)
from baler.scope import Scope

DAY = timedelta(hours=24)


@pytest.fixture(scope="module")
def equips():
    return inventory.build(hx.store(), [inventory.site_scope([hx.SITE])])


def _one(results, equip):
    [r] = [r for r in results if r["equip_uri"] == equip]
    return r


def test_hw_reset_passes_when_the_boiler_follows_its_reset(equips):
    [r] = gl36.run(equips, hx.history(), hx.NOW, DAY, {"hw_reset": hx.HW_RESET_F})
    assert r["check_id"] == "gl36:hw_reset" and r["subject"] == hx.BOILER and r["status"] == "pass", r
    ev = r["evidence"]
    assert ev["points"] == {"temperature_air_outdoor": hx.pt(hx.AHU, "outside-air-temp"),  # site-wide OAT
                            "temperature_water_hot": hx.pt(hx.BOILER, "hw-sup-temp"),
                            "flow_mass_water_hot": hx.pt(hx.BOILER, "boiler-run-status")}
    assert "run status stands in" in ev["proxies"]["flow_mass_water_hot"]
    assert ev["params_c"]["temperature_water_hot_setpoint_max"] == pytest.approx(82.222, abs=1e-3)  # 180 F
    assert ev["samples"] == 289 and ev["failed"] == 0 and ev["passed"] == 289
    assert ev["upstream"] == "constrain 0.8.0 (vendored)"


def test_hw_reset_fails_without_a_reset(equips):
    [r] = gl36.run(equips, hx.history(reset=False), hx.NOW, DAY, {"hw_reset": hx.HW_RESET_F})
    ev = r["evidence"]
    assert r["status"] == "fail" and 0 < ev["failed"] < ev["samples"]
    assert "6.5.4.4" in r["symptom"] and r["candidates"]
    # failures are the warm afternoon (OAT >= 60 F), never while the boiler is off
    first = ev["first_failures"][0]
    assert hx.oat_f(hx.NOW.fromisoformat(first)) >= 60 - 0.9


def test_zone_deadband_runs_where_both_setpoints_exist(equips):
    out = gl36.run(equips, hx.history(), hx.NOW, DAY, {"zone_deadband": {}})
    assert {r["equip_uri"] for r in out} == {hx.VAV, hx.VAV2}
    vav2 = _one(out, hx.VAV2)
    assert vav2["status"] == "fail" and vav2["evidence"]["failed"] == 24  # 06:00-08:00, 5-min steps
    # hospital VAVs carry occupied/unoccupied setpoints, not cooling/heating
    vav = _one(out, hx.VAV)
    assert vav["status"] == "inconclusive"
    assert [m["point"] for m in vav["evidence"]["missing_points"]] == ["temperature_air_zone_cool_setpoint",
                                                                      "temperature_air_zone_heat_setpoint"]


def test_sat_reset_is_inconclusive_on_hospital_ahus(equips):
    [r] = gl36.run(equips, hx.history(), hx.NOW, DAY, {"sat_reset": {"temperature_air_zone_design_cool_setpoint": 24}})
    assert r["equip_uri"] == hx.AHU and r["status"] == "inconclusive"
    assert r["evidence"]["missing_points"][0]["classes"] == ["Supply_Air_Temperature_Setpoint",
                                                            "Discharge_Air_Temperature_Setpoint"]


def test_missing_parameters_are_inconclusive_not_pass(equips):
    [r] = gl36.run(equips, hx.history(), hx.NOW, DAY, {"hw_reset": {"temperature_air_outdoor_min": -6}})
    assert r["status"] == "inconclusive"
    assert set(r["evidence"]["missing_params"]) == {"temperature_air_outdoor_max", "temperature_water_hot_setpoint_max",
                                                    "temperature_water_hot_setpoint_min"}


def test_chw_reset_passes_a_constant_chw_temperature(equips):
    """Upstream's CHWReset only checks CHW stays inside [min, max]: a
    chiller that never resets passes. Recorded in the spike write-up."""
    params = {"unit": "F", "temperature_air_outdoor_min": 20, "temperature_air_outdoor_max": 60,
              "temperature_water_chilled_setpoint_min": 42, "temperature_water_chilled_setpoint_max": 50}
    [r] = gl36.run(equips, hx.history(), hx.NOW, DAY, {"chw_reset": params})
    assert r["equip_uri"] == hx.CHILLER and r["status"] == "pass"


def test_no_data_is_inconclusive(equips):
    from baler.commissioning.history import InMemoryHistory

    [r] = gl36.run(equips, InMemoryHistory(), hx.NOW, DAY, {"hw_reset": hx.HW_RESET_F})
    assert r["status"] == "inconclusive" and r["evidence"]["samples"] == 0


@pytest.mark.parametrize("bad", [
    [], {"nope": {}}, {"hw_reset": []}, {"hw_reset": {"x": 1}}, {"hw_reset": {"temperature_air_outdoor_min": "cold"}},
    {"hw_reset": {"unit": "K"}}, {"zone_deadband": {"step_minutes": 0}},
])
def test_validate_settings_rejects(bad):
    with pytest.raises((TypeError, ValueError)):
        gl36.validate_settings(bad)


def test_engine_adds_gl36_results_when_enabled():
    summary, results = engine.run(hx.store(), hx.history(), [Scope.everything()], hx.NOW,
                                  gl36_settings={"hw_reset": hx.HW_RESET_F})
    gl = [r for r in results if r["check_id"].startswith("gl36:")]
    assert [(r["check_id"], r["status"]) for r in gl] == [("gl36:hw_reset", "pass")]
    assert summary["by_check"]["gl36"]["pass"] == 1
    _, plain = engine.run(hx.store(), hx.history(), [Scope.everything()], hx.NOW)
    assert not [r for r in plain if r["check_id"].startswith("gl36:")]


def test_binary_and_unit_conversion():
    assert gl36._number("active", "binary", False) == 1.0 and gl36._number("Off", "binary", False) == 0.0
    assert gl36._number(True, "number", False) == 1.0 and gl36._number("n/a", "number", False) is None
    assert gl36._number(212, "temperature", True) == pytest.approx(100.0)
    assert gl36.is_fahrenheit("°F") and gl36.is_fahrenheit("degreesFahrenheit") and not gl36.is_fahrenheit("°C")


def test_project_settings_and_background_run_carry_gl36():
    from baler.commissioning import db, phases, runner
    from baler.commissioning_api import _check_settings

    settings = _check_settings({"window_hours": 24, "gl36": {"hw_reset": hx.HW_RESET_F}})
    with pytest.raises(ValueError):
        _check_settings({"gl36": {"economizer_high_limit": {}}})  # not in ConStrain 0.8.0's library
    repo = db.MemoryRepo(store=hx.store(), history=hx.history())
    repo.create_project({"id": "p", "org_id": "o", "site_id": None, "name": "H", "settings": settings, "created_by": "u",
                         "created_at": hx.NOW, "updated_at": hx.NOW,
                         "phases": phases.set_current(phases.default_phases(), "functional", hx.NOW)})
    repo.create_run({"id": "r", "project_id": "p", "phase": "functional", "status": "queued", "started_at": hx.NOW,
                     "window_start": hx.NOW - DAY, "window_end": hx.NOW, "scope": {}})
    runner.execute(repo, runner.RunJob("r", "p", "functional", [Scope.everything()], hx.NOW, 24.0,
                                       engine_kwargs={"gl36_settings": settings["gl36"]}), lambda: hx.NOW)
    assert repo.get_run("r")["status"] == "completed"
    [r] = [r for r in repo.get_results("r") if r["check_id"] == "gl36:hw_reset"]
    assert r["status"] == "pass"


def test_a_crashing_item_is_inconclusive_not_fatal(equips, monkeypatch):
    def boom(*a, **k):
        raise KeyError("bad frame")

    monkeypatch.setattr(gl36, "check", boom)
    [r] = gl36.run(equips, hx.history(), hx.NOW, DAY, {"hw_reset": hx.HW_RESET_F})
    assert r["status"] == "inconclusive" and "KeyError" in r["evidence"]["error"]


def test_project_pass_threshold(equips):
    """settings.gl36.<item>.max_fail_fraction: 0 is ConStrain's own rule;
    above the share of failing steps, the item passes with a note."""
    strict = gl36.run(equips, hx.history(reset=False), hx.NOW, DAY, {"hw_reset": hx.HW_RESET_F})[0]
    frac = strict["evidence"]["tested_fail_fraction"]
    assert strict["status"] == "fail" and strict["evidence"]["max_fail_fraction"] == 0 and 0 < frac < 0.5
    loose = gl36.run(equips, hx.history(reset=False), hx.NOW, DAY, {"hw_reset": hx.HW_RESET_F | {"max_fail_fraction": 0.5}})[0]
    assert loose["status"] == "pass" and loose["evidence"]["constrain_verdict"] is False and "max_fail_fraction" in loose["evidence"]["note"]
    tight = gl36.run(equips, hx.history(reset=False), hx.NOW, DAY, {"hw_reset": hx.HW_RESET_F | {"max_fail_fraction": frac / 2}})[0]
    assert tight["status"] == "fail"
    with pytest.raises(ValueError):
        gl36.validate_settings({"hw_reset": {"max_fail_fraction": 1}})

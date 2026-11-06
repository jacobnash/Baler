"""
The ladder, rungs 1-3 (Cx 2, #38) - PR #5's test_commissioning_ladder.py
scenarios ported onto Brick classes: rungs climb in order, a failure
falls to the rung below when re-checked, and every failure names the
symptom with candidate causes (each with what would tell it apart) -
never a diagnosis. Rung 4 (command response) and #5's plan_command_tests
move to Cx 6 / Cx 9.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from cx_fixtures import NOW

from baler.commissioning import checks, ladder
from baler.commissioning.history import InMemoryHistory

E = "urn:equip:fbf/a/vav-1"
WINDOW = timedelta(hours=24)


def equip(points: dict) -> dict:
    """{name: brickClass or (brickClass, unit) or {"tags": [...]}} -> inventory row."""
    pts = []
    for name, spec in points.items():
        cls, unit, tags = spec, None, []
        if isinstance(spec, tuple):
            cls, unit = spec
        if isinstance(spec, dict):
            cls, tags = None, spec["tags"]
        pts.append({"id": f"urn:point:fbf/a/vav-1/{name}", "dis": name, "brickClass": cls, "unit": unit, "tags": tags})
    return {"id": E, "points": pts, "brickClasses": ["Variable_Air_Volume_Box"]}


def uri(name):
    return f"urn:point:fbf/a/vav-1/{name}"


def climb(e, hist):
    latest = hist.latest([p["id"] for p in e["points"]])
    exists = checks.existence(e, latest, NOW, WINDOW)
    series = hist.window([p["id"] for p in e["points"]], NOW - WINDOW, NOW + timedelta(seconds=1))
    out = exists + ladder.climb(e, exists, series, NOW, WINDOW)
    return out, next(r for r in out if r["check_id"] == "ladder")


def by(out, check, subject=None):
    return next(r for r in out if r["check_id"] == check and (subject is None or r["subject"] == subject))


VAV = {"ZNT": "Zone_Air_Temperature_Sensor", "ZNT-SP": "Zone_Air_Temperature_Setpoint",
       "DMP": "Damper_Position_Command", "FLOW": "Air_Flow_Sensor"}


def test_all_rungs_pass_for_a_healthy_vav_with_tracking():
    hist = InMemoryHistory()
    for i in range(24):
        t = NOW - timedelta(hours=23 - i)
        hist.add(uri("ZNT-SP"), t, 72.0 if i < 22 else 70.0)
        hist.add(uri("ZNT"), t, 72.0 + (i % 2) * 0.3 if i <= 22 else 71.0)
        hist.add(uri("DMP"), t, 40.0 + i)
        hist.add(uri("FLOW"), t, 350.0 + i)
    out, lad = climb(equip(VAV), hist)
    assert lad["status"] == "pass" and lad["evidence"]["highest_rung_passed"] == 3
    assert lad["evidence"]["rungs"] == {"1": "pass", "2": "pass", "3": "pass"}
    assert by(out, "live", uri("ZNT"))["status"] == "pass"
    zone = by(out, "pair:zone_air_temperature")
    assert zone["status"] == "pass" and zone["subject"] == f"{uri('ZNT')}|{uri('ZNT-SP')}"


def test_no_samples_at_all_is_inconclusive_and_nothing_above_is_attempted():
    # in the graph but nothing has ever reported: "I don't know", not "it isn't there"
    out, lad = climb(equip(VAV), InMemoryHistory())
    assert {r["status"] for r in out if r["check_id"] == "exists"} == {"inconclusive"}
    assert lad["status"] == "inconclusive" and lad["evidence"]["highest_rung_passed"] == 0
    assert lad["evidence"]["rungs"]["2"] == "not_attempted" and lad["evidence"]["rungs"]["3"] == "not_attempted"
    assert not [r for r in out if r["check_id"] in ("live",) or r["check_id"].startswith("pair:")]


def test_device_gone_quiet_fails_rung1_with_candidates():
    hist = InMemoryHistory()
    for i in range(6):
        hist.add(uri("ZNT"), NOW - timedelta(days=3, hours=i), 72.0 + i * 0.1)
    out, lad = climb(equip(VAV), hist)
    r1 = by(out, "exists", uri("ZNT"))
    assert r1["status"] == "fail" and "older than" in r1["symptom"]
    assert r1["candidates"] and all(c["distinguish"] for c in r1["candidates"])  # never a bare failure
    assert lad["status"] == "fail" and lad["evidence"]["highest_rung_passed"] == 0
    assert lad["evidence"]["rungs"]["2"] == "not_attempted"


def test_flat_sensor_fails_liveness_with_symptom_and_candidates():
    hist = InMemoryHistory()
    for i in range(12):
        t = NOW - timedelta(hours=11 - i)
        hist.add(uri("ZNT"), t, 72.0)  # frozen
        hist.add(uri("ZNT-SP"), t, 72.0)
        hist.add(uri("FLOW"), t, 350.0 + i)
    out, lad = climb(equip(VAV), hist)
    r2 = by(out, "live", uri("ZNT"))
    assert r2["status"] == "fail" and "constant 72" in r2["symptom"] and "Zone_Air_Temperature_Sensor" in r2["symptom"]
    causes = " ".join(c["cause"] for c in r2["candidates"]).lower()
    assert "sensor" in causes and "overrid" in causes
    assert all(c["distinguish"] for c in r2["candidates"])
    assert lad["evidence"]["highest_rung_passed"] == 1 and lad["evidence"]["rungs"]["3"] == "not_attempted"


def test_rung3_failure_that_is_really_a_frozen_sensor_falls_to_rung2():
    ahu = equip({"SAT": "Supply_Air_Temperature_Sensor", "SAT-SP": "Supply_Air_Temperature_Setpoint",
                 "SF-S": "Fan_Status", "SF-C": "Fan_Command"})
    hist = InMemoryHistory()
    for i in range(24):
        t = NOW - timedelta(hours=23 - i)
        live = i < 12
        hist.add(uri("SAT-SP"), t, 55.0)
        hist.add(uri("SAT"), t, 55.0 + (i % 3) * 0.4 if live else 65.0)  # froze at a 10 degF error
        hist.add(uri("SF-S"), t, 1)
        hist.add(uri("SF-C"), t, 1)
    out, lad = climb(ahu, hist)
    assert by(out, "live", uri("SAT"))["status"] == "pass"  # moved earlier in the window
    r3 = by(out, "pair:supply_air_temperature")
    assert r3["status"] == "fail" and r3["evidence"]["fell_to_rung"] == 2, r3
    assert "froze" in r3["evidence"]["recheck"]
    assert lad["evidence"]["fell_to_rung"] == 2 and lad["evidence"]["highest_rung_passed"] == 1
    assert by(out, "pair:fan_cmd_status")["status"] == "pass"


def test_command_status_disagreement_is_a_responsiveness_failure():
    e = equip({"SF-S": "Fan_Status", "SF-C": "Fan_Command", "SAT": "Supply_Air_Temperature_Sensor"})
    hist = InMemoryHistory()
    for i in range(12):
        t = NOW - timedelta(hours=11 - i)
        hist.add(uri("SF-C"), t, True)
        hist.add(uri("SF-S"), t, "inactive")  # commanded on, never proves
        hist.add(uri("SAT"), t, 55.0 + (i % 2))
    out, lad = climb(e, hist)
    r3 = by(out, "pair:fan_cmd_status")
    assert r3["status"] == "fail" and "fan status" in r3["symptom"] and "command" in r3["symptom"]
    assert r3["evidence"]["agreement"] == 0.0
    assert any("starter" in c["cause"] or "belt" in c["cause"] for c in r3["candidates"])
    assert lad["status"] == "fail" and lad["evidence"]["highest_rung_passed"] == 2


def test_celsius_sensor_against_fahrenheit_setpoint():
    e = equip({"ZNT": ("Zone_Air_Temperature_Sensor", "°C"), "ZNT-SP": ("Zone_Air_Temperature_Setpoint", "°F")})
    hist = InMemoryHistory()
    for i in range(6):
        t = NOW - timedelta(hours=5 - i)
        hist.add(uri("ZNT"), t, 22.2 + (i % 2) * 0.2)  # ~72 degF
        hist.add(uri("ZNT-SP"), t, 72.0)
    out, _ = climb(e, hist)
    assert by(out, "pair:zone_air_temperature")["status"] == "pass"


def test_ambiguous_setpoints_are_not_guessed():
    e = equip({"ZNT": "Zone_Air_Temperature_Sensor", "HTG-SP": "Heating_Zone_Air_Temperature_Setpoint",
               "CLG-SP": "Cooling_Zone_Air_Temperature_Setpoint"})
    hist = InMemoryHistory()
    for i in range(6):
        t = NOW - timedelta(hours=5 - i)
        hist.add(uri("ZNT"), t, 72.0 + i * 0.1)
        hist.add(uri("HTG-SP"), t, 70.0)
        hist.add(uri("CLG-SP"), t, 74.0)
    out, _ = climb(e, hist)
    r = by(out, "pair:zone_air_temperature")
    assert r["status"] == "inconclusive" and "ambiguous" in r["evidence"]["reason"]


def test_thin_data_is_inconclusive_not_pass():
    hist = InMemoryHistory()
    hist.add(uri("ZNT"), NOW - timedelta(minutes=5), 72.0)
    out, lad = climb(equip(VAV), hist)
    assert by(out, "live", uri("ZNT"))["status"] == "inconclusive"
    assert lad["status"] != "pass"


# --- point roles: PR #5's test_commissioning_points expectations, read
# through Brick classes (the class a mapping rule would mint for that
# function) instead of #5's own name vocabulary. Name -> class
# classification itself belongs to spec alignment (Cx 7).
@pytest.mark.parametrize("brick_class,role", [
    ("Supply_Air_Temperature_Sensor", "sensor"),
    ("Discharge_Air_Temperature_Sensor", "sensor"),
    ("Supply_Air_Temperature_Setpoint", "setpoint"),
    ("Return_Air_Temperature_Sensor", "sensor"),
    ("Mixed_Air_Temperature_Sensor", "sensor"),
    ("Outside_Air_Temperature_Sensor", "sensor"),
    ("Zone_Air_Temperature_Sensor", "sensor"),
    ("Zone_Air_Temperature_Setpoint", "setpoint"),
    ("Fan_Status", "status"),
    ("Fan_Command", "command"),
    ("Supply_Air_Static_Pressure_Sensor", "sensor"),
    ("Damper_Position_Command", "command"),
    ("Air_Flow_Sensor", "sensor"),
    ("Chilled_Water_Supply_Temperature_Sensor", "sensor"),
    ("Chilled_Water_Return_Temperature_Sensor", "sensor"),
    ("Enable_Command", "command"),
    ("Run_Status", "status"),
    ("Filter_Differential_Pressure_Sensor", "sensor"),
    ("Air_Handling_Unit", None),
])
def test_role_from_brick_class(brick_class, role):
    assert ladder.role_of({"brickClass": brick_class}) == role


@pytest.mark.parametrize("tags,role", [
    (["zone", "air", "temp", "sensor", "point"], "sensor"),
    (["zone", "air", "temp", "sp", "point"], "setpoint"),
    (["damper", "cmd", "point"], "command"),
    (["point"], None),
])
def test_role_from_haystack_markers_when_no_brick_class(tags, role):
    assert ladder.role_of({"brickClass": None, "tags": tags}) == role

"""Baler's own economizer checks (cx:econ_*), the per-project ladder
settings, and gl36's per-project pass threshold. No services, no pandas
for the economizer part."""

from __future__ import annotations

from datetime import timedelta

import cx_hospital as hx
import pytest
from rdflib import RDF, Literal, URIRef

from baler.commissioning import economizer, engine, inventory, ladder
from baler.commissioning.history import InMemoryHistory
from baler.scope import Scope
from baler.store import BRICK, HAYSTACK, TD, Store

DAY = timedelta(hours=24)
AHU = "urn:equip:fbf/econ/ahu-1"
F = "degreesFahrenheit"
POINTS = {"oat": ("Outside_Air_Temperature_Sensor", F), "rat": ("Return_Air_Temperature_Sensor", F),
          "oa-dmpr-cmd": ("Damper_Position_Command", "%"), "chw-vlv-cmd": ("Valve_Position_Command", "%")}
CFG = {"high_limit": 60, "unit": "F", "min_position": 20}


def pt(name):
    return AHU.replace("urn:equip:", "urn:point:") + "/" + name


def store(extra_damper=False):
    s = Store()
    g = s.graph
    e = URIRef(AHU)
    g.add((e, RDF.type, BRICK["Air_Handling_Unit"]))
    pts = dict(POINTS)
    if extra_damper:
        pts["oa-dmpr-pos"] = ("Damper_Position_Sensor", "%")  # command and feedback: which one?
    for name, (cls, unit) in pts.items():
        p = URIRef(pt(name))
        g.add((p, RDF.type, TD.RawPoint))
        g.add((p, RDF.type, BRICK[cls]))
        g.add((p, HAYSTACK.unit, Literal(unit)))
        g.add((e, BRICK.hasPoint, p))
        g.add((p, BRICK.isPointOf, e))
    return s


def history(damper):
    """OAT 15-65 F over the day (hospital curve); cooling on above 50 F.
    damper(oat, cooling_on) -> % open."""
    h = InMemoryHistory()
    t = hx.NOW - DAY
    while t <= hx.NOW:
        oat = hx.oat_f(t)
        cooling = oat > 50
        h.add(pt("oat"), t, round(oat, 2))
        h.add(pt("rat"), t, 72.0)
        h.add(pt("chw-vlv-cmd"), t, 60.0 if cooling else 0.0)
        h.add(pt("oa-dmpr-cmd"), t, damper(oat, cooling))
        t += timedelta(minutes=5)
    return h


def good(oat, cooling):
    if oat > 60:
        return 20.0
    return 80.0 if cooling else 35.0


def _equips(s=None):
    return inventory.build(s or store(), [Scope.everything()])


def _by(results):
    return {r["check_id"]: r for r in results}


def test_good_sequence_passes_both():
    out = _by(economizer.run(_equips(), history(good), hx.NOW, DAY, CFG))
    hl, ig = out["cx:econ_high_limit"], out["cx:econ_integrated"]
    assert hl["status"] == "pass" and hl["evidence"]["tested"] > 0, hl
    assert ig["status"] == "pass" and ig["evidence"]["tested"] > 0, ig
    assert hl["evidence"]["points"]["oa_damper"] == pt("oa-dmpr-cmd")
    assert ig["evidence"]["points"]["cooling"] == pt("chw-vlv-cmd") and ig["evidence"]["points"]["rat"] == pt("rat")
    assert "named outside air" in ig["evidence"]["binding"]["oa_damper"]


def test_damper_open_above_high_limit_fails():
    out = _by(economizer.run(_equips(), history(lambda oat, c: 100.0), hx.NOW, DAY, CFG))
    r = out["cx:econ_high_limit"]
    assert r["status"] == "fail" and "high limit" in r["symptom"] and r["candidates"]
    assert all(hx.oat_f(hx.NOW.fromisoformat(t)) > 60 for t in r["evidence"]["first_failures"])
    assert out["cx:econ_integrated"]["status"] == "pass"


def test_economizer_shut_when_cooling_starts_fails_integrated():
    out = _by(economizer.run(_equips(), history(lambda oat, c: 20.0), hx.NOW, DAY, CFG))
    r = out["cx:econ_integrated"]
    assert r["status"] == "fail" and r["evidence"]["failed"] == r["evidence"]["tested"]
    assert out["cx:econ_high_limit"]["status"] == "pass"


def test_threshold_relaxes_the_verdict():
    def mostly_good(oat, cooling):
        return 100.0 if 61 < oat < 61.5 else good(oat, cooling)
    strict = _by(economizer.run(_equips(), history(mostly_good), hx.NOW, DAY, CFG))["cx:econ_high_limit"]
    assert strict["status"] == "fail" and 0 < strict["evidence"]["fail_fraction"] < 0.2
    loose = _by(economizer.run(_equips(), history(mostly_good), hx.NOW, DAY, CFG | {"max_fail_fraction": 0.2}))
    assert loose["cx:econ_high_limit"]["status"] == "pass" and "within" in loose["cx:econ_high_limit"]["evidence"]["note"]


def test_ambiguous_damper_is_inconclusive_until_bound():
    s = store(extra_damper=True)
    out = _by(economizer.run(_equips(s), history(good), hx.NOW, DAY, CFG))
    assert out["cx:econ_high_limit"]["status"] == "inconclusive"
    assert out["cx:econ_high_limit"]["evidence"]["missing_points"] == ["oa_damper"]
    bound = CFG | {"points": {AHU: {"oa_damper": pt("oa-dmpr-cmd")}}}
    h = history(good)
    out = _by(economizer.run(_equips(s), h, hx.NOW, DAY, bound))
    assert out["cx:econ_high_limit"]["status"] == "pass"
    assert out["cx:econ_high_limit"]["evidence"]["binding"]["oa_damper"] == "project setting"


def test_no_weather_to_judge_is_inconclusive():
    out = _by(economizer.run(_equips(), history(good), hx.NOW, DAY, CFG | {"high_limit": 90}))
    assert out["cx:econ_high_limit"]["status"] == "inconclusive" and "never above" in out["cx:econ_high_limit"]["symptom"]


def test_celsius_settings_convert():
    out = _by(economizer.run(_equips(), history(lambda oat, c: 100.0), hx.NOW, DAY,
                             {"high_limit": 15.56, "unit": "C", "min_position": 20}))
    assert out["cx:econ_high_limit"]["status"] == "fail"


@pytest.mark.parametrize("bad", [{}, {"high_limit": "x"}, {"high_limit": 70, "unit": "K"}, {"high_limit": 70, "checks": []},
                                 {"high_limit": 70, "nope": 1}, {"high_limit": 70, "points": {AHU: {"fan": "urn:point:x"}}},
                                 {"high_limit": 70, "max_fail_fraction": 1}])
def test_validate_rejects(bad):
    with pytest.raises((ValueError, TypeError)):
        economizer.validate_settings(bad)


def test_engine_runs_economizer_when_enabled():
    summary, results = engine.run(store(), history(good), [Scope.everything()], hx.NOW, 24, economizer_settings=CFG)
    assert {r["check_id"] for r in results} >= {"cx:econ_high_limit", "cx:econ_integrated"}
    assert summary["by_check"]["cx"]["pass"] == 2


# ----------------------------------------------------------------- ladder


@pytest.mark.parametrize("bad", [[], {"nope": 1}, {"min_samples": 0}, {"status_agreement_min": 2},
                                 {"tolerances": {"nope": 1}}, {"tolerances": {"zone_air_temperature": -1}}])
def test_ladder_settings_validate(bad):
    with pytest.raises((ValueError, TypeError)):
        ladder.validate_settings(bad)


def test_ladder_effective_merges_overrides():
    eff = ladder.effective({"tolerances": {"zone_air_temperature": 1.0}, "min_samples": 5})
    assert eff["min_samples"] == 5 and eff["tolerances"]["zone_air_temperature"] == 1.0
    assert eff["tolerances"]["supply_air_temperature"] == 3.0  # untouched default


def test_ladder_tolerance_changes_the_verdict():
    from datetime import datetime, timezone
    now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    equip = {"id": "urn:equip:z", "points": [
        {"id": "urn:point:z/t", "brickClass": "Zone_Air_Temperature_Sensor", "unit": "°F", "tags": []},
        {"id": "urn:point:z/sp", "brickClass": "Zone_Air_Temperature_Setpoint", "unit": "°F", "tags": []}]}
    series = {"urn:point:z/t": [(now - timedelta(minutes=10 * i), 72.0 + (i % 3) * 0.1) for i in range(20)],
              "urn:point:z/sp": [(now - timedelta(minutes=10 * i), 71.0) for i in range(20)]}
    get = lambda cfg: next(r for r in ladder.responsiveness(equip, series, ladder.effective(cfg), now, DAY, set())
                           if r["check_id"] == "pair:zone_air_temperature")
    assert get(None)["status"] == "pass"
    tight = get({"tolerances": {"zone_air_temperature": 0.5}})
    assert tight["status"] == "fail" and tight["evidence"]["tolerance"] == 0.5


def test_project_settings_reach_the_engine():
    from baler.commissioning_api import _check_settings, engine_kwargs

    s = {"ladder": {"tolerances": {"air_flow": 0.1}}, "economizer": CFG, "window_hours": 12}
    assert _check_settings(s) is s
    assert engine_kwargs(s) == {"ladder_config": s["ladder"], "economizer_settings": CFG}
    assert engine_kwargs({}) == {}
    with pytest.raises(ValueError):
        _check_settings({"economizer": {"unit": "F"}})
    with pytest.raises(ValueError):
        _check_settings({"ladder": {"min_samples": -1}})

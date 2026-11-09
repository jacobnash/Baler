"""
The standard active-test library, as data (Baler#275, slice c).

Each test is a short functional sequence for one kind of equipment - VAV
reheat, AHU, a chilled-water loop - written against Brick classes, with
its safety limits and expected results stated next to it. `compile` turns
one into the concrete steps of a Cx 9 active run for one equipment, and
the run is then planned, dry-run, confirmed and executed exactly as any
other: active.validate_steps, active.plan, the typed confirmation,
BALER_CX_ALLOW_WRITES, the window, expiring writes, guaranteed relinquish,
the sweeper and the kill switch all apply unchanged. Nothing here writes.

A test:

  id, name, equip_class      what it is and what it applies to
  description                what it proves, in a CxA's words
  limits                     safety limits, checked when it is compiled:
    writes: {point_class: {min, max}}   every value it may command
    max_hold_minutes                    no step holds longer (<= Cx 9's 60)
  steps:
    id, title
    writes: [{point_class, value | offset}]
            value: an absolute command; offset: relative to the point's
            latest value (a setpoint step down 3 F)
    expect: [{point_class, equals | min | max | near | rise_by | fall_by}]
            near {target_from, tolerance}: within tolerance of what the
            step wrote to target_from (the setpoint); rise_by / fall_by:
            relative to the point's latest value before the test
    hold_minutes, within_minutes
  expected                   the expected result, in words, for the report

Relative values are resolved against the latest samples when the run is
planned, so the dry run lists the exact numbers that will be written and
expected - and a resolved value outside the limits refuses the plan.
"""

from __future__ import annotations

import copy

from baler.commissioning import active, ladder

TESTS: list[dict] = [
    {
        "id": "vav_reheat", "name": "VAV airflow and reheat", "equip_class": "Variable_Air_Volume_Box",
        "description": "Damper opens and airflow follows; the reheat valve opens and the discharge air warms.",
        "limits": {"writes": {"Damper_Position_Command": {"min": 0, "max": 100}, "Valve_Command": {"min": 0, "max": 100}},
                   "max_hold_minutes": 15},
        "steps": [
            {"id": "damper-open", "title": "Damper to 100%: airflow rises",
             "writes": [{"point_class": "Damper_Position_Command", "value": 100}],
             "expect": [{"point_class": "Air_Flow_Sensor", "rise_by": 50}], "hold_minutes": 5, "within_minutes": 5},
            {"id": "reheat-open", "title": "Reheat valve to 100%: discharge air warms",
             "writes": [{"point_class": "Valve_Command", "value": 100}],
             "expect": [{"point_class": "Discharge_Air_Temperature_Sensor", "rise_by": 5}], "hold_minutes": 15,
             "within_minutes": 15},
        ],
        "expected": "Airflow rises by at least 50 cfm within 5 min of the damper opening; discharge air rises at least "
                    "5 F within 15 min of the reheat valve opening. Both are relinquished after their hold.",
    },
    {
        "id": "ahu_basic", "name": "AHU fan and supply air temperature", "equip_class": "Air_Handling_Unit",
        "description": "The supply fan starts on command, and supply air follows a setpoint step down.",
        "limits": {"writes": {"Fan_Command": {"min": 0, "max": 1}, "Supply_Air_Temperature_Setpoint": {"min": 50, "max": 65}},
                   "max_hold_minutes": 20},
        "steps": [
            {"id": "fan-on", "title": "Supply fan on: status follows",
             "writes": [{"point_class": "Fan_Command", "value": 1}],
             "expect": [{"point_class": "Fan_Status", "equals": 1}], "hold_minutes": 3, "within_minutes": 2},
            {"id": "sat-step", "title": "Supply air setpoint down 3 F: supply air follows",
             "writes": [{"point_class": "Fan_Command", "value": 1}, {"point_class": "Supply_Air_Temperature_Setpoint", "offset": -3}],
             "expect": [{"point_class": "Supply_Air_Temperature_Sensor", "near": {"target_from": "Supply_Air_Temperature_Setpoint",
                                                                                  "tolerance": 2}}],
             "hold_minutes": 20, "within_minutes": 20},
        ],
        "expected": "Fan status on within 2 min; supply air within 2 F of a setpoint 3 F lower within 20 min (the setpoint "
                    "stays within 50-65 F). The fan command is held through the step and relinquished after.",
    },
    {
        "id": "chw_loop", "name": "Chilled-water loop pump and supply temperature", "equip_class": "Chilled_Water_System",
        "description": "The loop pump starts on command, and chilled water supply follows a setpoint step down.",
        "limits": {"writes": {"Pump_Command": {"min": 0, "max": 1},
                              "Supply_Chilled_Water_Temperature_Setpoint": {"min": 40, "max": 50}},
                   "max_hold_minutes": 30},
        "steps": [
            {"id": "pump-on", "title": "Pump on: status follows",
             "writes": [{"point_class": "Pump_Command", "value": 1}],
             "expect": [{"point_class": "Pump_Status", "equals": 1}], "hold_minutes": 3, "within_minutes": 2},
            {"id": "chws-step", "title": "Chilled water setpoint down 2 F: supply follows",
             "writes": [{"point_class": "Pump_Command", "value": 1},
                        {"point_class": "Supply_Chilled_Water_Temperature_Setpoint", "offset": -2}],
             "expect": [{"point_class": "Chilled_Water_Supply_Temperature_Sensor",
                         "near": {"target_from": "Supply_Chilled_Water_Temperature_Setpoint", "tolerance": 1.5}}],
             "hold_minutes": 30, "within_minutes": 30},
        ],
        "expected": "Pump status on within 2 min; chilled water supply within 1.5 F of a setpoint 2 F lower within 30 min "
                    "(the setpoint stays within 40-50 F).",
    },
]
BY_ID = {t["id"]: t for t in TESTS}
_EXPECT_KEYS = ("equals", "min", "max", "near", "rise_by", "fall_by")


def check_library(tests: list[dict] = TESTS) -> None:
    """Every test states limits for everything it writes, holds no longer
    than Cx 9 allows, and fits in one active run. Raises ValueError."""
    for t in tests:
        lim = t["limits"]
        if lim["max_hold_minutes"] > active.MAX_HOLD_MINUTES:
            raise ValueError(f"{t['id']}: max_hold_minutes exceeds Cx 9's {active.MAX_HOLD_MINUTES}")
        if not 0 < len(t["steps"]) <= active.MAX_STEPS:
            raise ValueError(f"{t['id']}: 1-{active.MAX_STEPS} steps")
        if not t.get("expected"):
            raise ValueError(f"{t['id']}: states no expected result")
        for st in t["steps"]:
            if st["hold_minutes"] > lim["max_hold_minutes"] or st["within_minutes"] > st["hold_minutes"]:
                raise ValueError(f"{t['id']}/{st['id']}: hold exceeds the limit, or within exceeds hold")
            for w in st["writes"]:
                if w["point_class"] not in lim["writes"] or ("value" in w) == ("offset" in w):
                    raise ValueError(f"{t['id']}/{st['id']}: every write has a stated limit and one of value/offset")
            for e in st["expect"]:
                if sum(k in e for k in _EXPECT_KEYS) != 1:
                    raise ValueError(f"{t['id']}/{st['id']}: each expectation has one of {', '.join(_EXPECT_KEYS)}")


def public(t: dict) -> dict:
    return copy.deepcopy(t)


def _point(equip: dict, cls: str) -> dict | None:
    p, _ = ladder._pick(equip["points"], (cls,), True)
    return p


def compile_steps(test_id: str, equip: dict, latest: dict[str, tuple]) -> list[dict]:
    """The library test as concrete active-run steps for `equip`, with
    relative values resolved against `latest` ({point: (ts, value)}).
    Raises ValueError naming what's missing or out of limits; the result
    still goes through active.validate_steps at the API."""
    found = BY_ID.get(test_id)
    if found is None:
        raise ValueError(f"unknown library test {test_id!r} (known: {', '.join(BY_ID)})")
    t: dict = found
    out = []

    def now_value(p: dict, why: str) -> float:
        got = latest.get(p["id"])
        vals = ladder.numeric([got]) if got else []
        if not vals:
            raise ValueError(f"{t['id']}: {why} needs a recent value of {p.get('dis') or p['id']} and there is none")
        return vals[0][1]

    for st in t["steps"]:
        written: dict[str, float] = {}
        writes = []
        for w in st["writes"]:
            p = _point(equip, w["point_class"])
            if p is None:
                raise ValueError(f"{t['id']}/{st['id']}: no single {w['point_class']} on {equip.get('dis') or equip['id']}")
            value = float(w["value"]) if "value" in w else now_value(p, f"step {st['id']}") + float(w["offset"])
            lim = t["limits"]["writes"][w["point_class"]]
            if not lim["min"] <= value <= lim["max"]:
                raise ValueError(f"{t['id']}/{st['id']}: {w['point_class']} = {value:g} is outside its safety limit "
                                 f"{lim['min']:g}..{lim['max']:g}")
            value = int(value) if value.is_integer() else round(value, 2)
            written[w["point_class"]] = value
            writes.append({"point": p["id"], "value": value})
        expect = []
        for e in st["expect"]:
            p = _point(equip, e["point_class"])
            if p is None:
                raise ValueError(f"{t['id']}/{st['id']}: no single {e['point_class']} on {equip.get('dis') or equip['id']}")
            if "near" in e:
                target = written[e["near"]["target_from"]]
                expect.append({"point": p["id"], "min": target - e["near"]["tolerance"], "max": target + e["near"]["tolerance"]})
            elif "rise_by" in e:
                expect.append({"point": p["id"], "min": round(now_value(p, f"step {st['id']}") + e["rise_by"], 2)})
            elif "fall_by" in e:
                expect.append({"point": p["id"], "max": round(now_value(p, f"step {st['id']}") - e["fall_by"], 2)})
            else:
                expect.append({"point": p["id"], **{k: e[k] for k in ("equals", "min", "max") if k in e}})
        out.append({"id": st["id"], "title": st["title"], "writes": writes, "expect": expect,
                    "hold_minutes": st["hold_minutes"], "within_minutes": st["within_minutes"]})
    return out


def applies(test_id: str, equip: dict) -> bool:
    from baler.commissioning import procedures

    t = BY_ID.get(test_id)
    return t is not None and procedures.applies({"equip_class": t["equip_class"]}, equip)


check_library()

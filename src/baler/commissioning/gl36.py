"""
Sequence verification over Baler history with PNNL ConStrain's items
(Cx 3, Baler#39). Spike scope: todo/cx3-constrain-gl36-spike.md.

The pipeline for one item on one piece of equipment:

  1. Resolve each ConStrain data point the item needs ("role") to a Baler
     point through the Cx inventory: Brick class first, subclass-aware
     (brick_vocab), Haystack markers second (the same rule templates.py
     uses). A role can look on the equipment itself or, for building-wide
     quantities like outdoor air temperature, anywhere in the inventory.
     Some roles have a documented proxy (a run status standing in for a
     flow sensor), recorded in the evidence when used.
  2. Build the dataframe ConStrain expects from point_history through the
     Cx History seam: one column per role on a regular grid (last value
     per step, held forward for a few steps), temperatures converted to
     deg C (ConStrain's unit), binary states to 0/1.
  3. Add the item's design parameters (project settings, `gl36`) as
     constant columns, as ConStrain's own `params` does, and run the
     vendored item (commissioning.vendor_constrain).
  4. Report one Cx result, check id `gl36:<item>`, subject the equipment.
     ConStrain's verdict is kept as is: any failing sample fails the item.
     Missing points or parameters, or no aligned samples, are
     `inconclusive` - never a pass.

Needs pandas: `pip install -e ".[cx-gl36]"`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from baler import brick_vocab
from baler.commissioning import templates as tpl
from baler.commissioning.history import History

FAHRENHEIT = {"°f", "degf", "deg f", "f", "fahrenheit", "degreesfahrenheit", "℉"}
TRUE = {"true", "on", "active", "1", "yes", "run", "running"}
FALSE = {"false", "off", "inactive", "0", "no", "stop", "stopped"}
DEFAULT_STEP_MINUTES = 5
HOLD_STEPS = 3  # a value carries forward this many steps, no further
MAX_FAILURES_LISTED = 5


@dataclass(frozen=True)
class Role:
    point: str  # ConStrain's data point name (a dataframe column)
    classes: tuple[str, ...]  # Brick classes, in order of preference
    kind: str = "number"  # number | temperature | binary
    where: str = "equip"  # equip | equip_or_site
    proxy: Role | None = None  # used when nothing matches `classes`
    proxy_note: str = ""


@dataclass(frozen=True)
class Item:
    id: str
    cls: str  # vendored ConStrain class name
    requirement: str
    equip_classes: tuple[str, ...]
    roles: tuple[Role, ...]
    params: tuple[str, ...] = ()
    temperature_params: tuple[str, ...] = field(default=())


def _run_status(kind_note: str) -> Role:
    return Role("_status", ("Run_Status", "On_Off_Status", "Status"), "binary", "equip",
                proxy_note=f"no {kind_note} flow sensor: the equipment's own run status stands in (on = flow)")


OAT = Role("temperature_air_outdoor", ("Outside_Air_Temperature_Sensor",), "temperature", "equip_or_site")

ITEMS: dict[str, Item] = {item.id: item for item in (
    Item("zone_deadband", "ZoneTempControl", "ASHRAE 90.1-2019 6.4.3.1.2 Dead Band (>= 5 F between heating and cooling setpoints)",
         ("Terminal_Unit",),
         (Role("temperature_air_zone_cool_setpoint", ("Zone_Air_Cooling_Temperature_Setpoint",), "temperature"),
          Role("temperature_air_zone_heat_setpoint", ("Zone_Air_Heating_Temperature_Setpoint",), "temperature"))),
    Item("sat_reset", "SupplyAirTempReset", "ASHRAE 90.1-2016 6.5.3.5 Supply Air Temperature Reset",
         ("Air_Handling_Unit",),
         (Role("temperature_air_supply_setpoint", ("Supply_Air_Temperature_Setpoint", "Discharge_Air_Temperature_Setpoint"),
               "temperature"),),
         params=("temperature_air_zone_design_cool_setpoint",),
         temperature_params=("temperature_air_zone_design_cool_setpoint",)),
    Item("chw_reset", "CHWReset", "ASHRAE 90.1-2016 6.5.4.4 Chilled-Water Temperature Reset",
         ("Chiller",),
         (OAT,
          Role("temperature_water_chilled", ("Chilled_Water_Supply_Temperature_Sensor",), "temperature"),
          Role("flow_mass_water_chilled", ("Chilled_Water_Flow_Sensor",), proxy=_run_status("chilled water"))),
         params=("temperature_air_outdoor_max", "temperature_air_outdoor_min",
                 "temperature_water_chilled_setpoint_max", "temperature_water_chilled_setpoint_min"),
         temperature_params=("temperature_air_outdoor_max", "temperature_air_outdoor_min",
                             "temperature_water_chilled_setpoint_max", "temperature_water_chilled_setpoint_min")),
    Item("hw_reset", "HWReset", "ASHRAE 90.1-2016 6.5.4.4 Hot-Water Temperature Reset",
         ("Boiler",),
         (OAT,
          Role("temperature_water_hot", ("Hot_Water_Supply_Temperature_Sensor",), "temperature"),
          Role("flow_mass_water_hot", ("Hot_Water_Flow_Sensor",), proxy=_run_status("hot water"))),
         params=("temperature_air_outdoor_max", "temperature_air_outdoor_min",
                 "temperature_water_hot_setpoint_max", "temperature_water_hot_setpoint_min"),
         temperature_params=("temperature_air_outdoor_max", "temperature_air_outdoor_min",
                             "temperature_water_hot_setpoint_max", "temperature_water_hot_setpoint_min")),
)}


def available() -> bool:
    try:
        import pandas  # noqa: F401
    except ImportError:
        return False
    return True


def validate_settings(settings) -> dict:
    """Project settings `gl36`: {item_id: {param: number, ..., "unit": "C"|"F",
    "step_minutes": n, "max_fail_fraction": f}}. max_fail_fraction is the
    project's pass threshold: the item still passes when no more than that
    share of tested steps failed. 0 (the default) is ConStrain's own rule,
    any failing step fails the item.
    A key enables that item. Raises ValueError/TypeError."""
    if not isinstance(settings, dict):
        raise TypeError("gl36 must be an object of {item: parameters}")
    for key, params in settings.items():
        item = ITEMS.get(key)
        if item is None:
            raise ValueError(f"unknown gl36 item {key!r} (known: {', '.join(sorted(ITEMS))})")
        if not isinstance(params, dict):
            raise TypeError(f"gl36.{key} must be an object")
        for name, value in params.items():
            if name == "unit":
                if value not in ("C", "F"):
                    raise ValueError(f"gl36.{key}.unit must be C or F")
            elif name == "step_minutes":
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 60:
                    raise ValueError(f"gl36.{key}.step_minutes must be a number in [1, 60]")
            elif name == "max_fail_fraction":
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < 1:
                    raise ValueError(f"gl36.{key}.max_fail_fraction must be a number in [0, 1)")
            elif name not in item.params:
                raise ValueError(f"gl36.{key} takes {', '.join(item.params) or 'no parameters'}, not {name!r}")
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"gl36.{key}.{name} must be a number")
    if settings and not available():
        raise ValueError("gl36 items need pandas: install baler with the cx-gl36 extra")
    return settings


# ------------------------------------------------------------- resolving


def _family(classes) -> list[set[str]]:
    vocab = brick_vocab.load()
    return [vocab.descendants(c) or {c} for c in classes]


def _match(points: list[dict], classes) -> dict | None:
    """First point meeting the role: exact class order first, then the
    classes' Brick descendants, then Haystack markers."""
    for c in classes:
        hit = next((p for p in points if p.get("brickClass") == c), None)
        if hit:
            return hit
    for fam in _family(classes):
        hit = next((p for p in points if p.get("brickClass") in fam), None)
        if hit:
            return hit
    need = tpl._markers_for(classes)
    if need:
        return next((p for p in points if not p.get("brickClass") and need <= set(p.get("tags") or ())), None)
    return None


def applies(item: Item, equip: dict) -> bool:
    fams = _family(item.equip_classes)
    return any(c in fam for fam in fams for c in equip.get("brickClasses") or [])


def resolve(item: Item, equip: dict, equips: list[dict]) -> tuple[dict[str, dict], dict[str, str], list[dict]]:
    """(bound role -> point row, proxies used, missing roles)."""
    site_points = [p for e in equips for p in e["points"]]
    bound: dict[str, dict] = {}
    proxies: dict[str, str] = {}
    missing: list[dict] = []
    for role in item.roles:
        hit = _match(equip["points"], role.classes)
        if hit is None and role.where == "equip_or_site":
            hit = _match(site_points, role.classes)
        if hit is None and role.proxy is not None:
            hit = _match(equip["points"], role.proxy.classes)
            if hit is not None:
                proxies[role.point] = role.proxy.proxy_note
        if hit is None:
            missing.append({"point": role.point, "classes": list(role.classes),
                            "proxy_classes": list(role.proxy.classes) if role.proxy else []})
        else:
            bound[role.point] = hit
    return bound, proxies, missing


# ------------------------------------------------------------ dataframe


def is_fahrenheit(unit) -> bool:
    return str(unit or "").strip().lower().replace(" ", "") in {u.replace(" ", "") for u in FAHRENHEIT}


def _number(value, kind: str, fahrenheit: bool):
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        v = float(value)
    else:
        s = str(value).strip().lower()
        if kind == "binary" and s in TRUE:
            return 1.0
        if kind == "binary" and s in FALSE:
            return 0.0
        try:
            v = float(s)
        except ValueError:
            return None
    if kind == "binary":
        return 1.0 if v else 0.0
    if kind == "temperature" and fahrenheit:
        return (v - 32.0) * 5.0 / 9.0
    return v


def frame(item: Item, bound: dict[str, dict], history: History, start: datetime, end: datetime,
          step_minutes: float = DEFAULT_STEP_MINUTES, binary: frozenset[str] = frozenset()):
    """The dataframe ConStrain's item reads: a column per bound role on a
    regular grid, NaN rows (a role with no value close enough) dropped.
    `binary` names roles bound to a status proxy (read as 0/1)."""
    import pandas as pd

    roles = {r.point: r for r in item.roles}
    series = history.window(sorted({p["id"] for p in bound.values()}), start, end)
    cols = {}
    for name, point in bound.items():
        role = roles[name]
        kind = "binary" if name in binary else role.kind
        f = is_fahrenheit(point.get("unit"))
        rows = [(ts, _number(v, kind, f)) for ts, v in series.get(point["id"], [])]
        rows = [(ts, v) for ts, v in rows if v is not None]
        cols[name] = pd.Series([v for _, v in rows], index=pd.DatetimeIndex([ts for ts, _ in rows]), dtype="float64")
    step = f"{step_minutes:g}min"
    grid = {k: s.resample(step).last().ffill(limit=HOLD_STEPS) for k, s in cols.items() if len(s)}
    if len(grid) < len(bound):
        return pd.DataFrame(columns=list(bound))
    return pd.concat(grid, axis=1).dropna()


# ------------------------------------------------------------------ run


def _params(item: Item, settings: dict) -> tuple[dict, list[str]]:
    unit = settings.get("unit", "C")
    out = {}
    for name in item.params:
        if name in settings:
            v = float(settings[name])
            out[name] = (v - 32.0) * 5.0 / 9.0 if unit == "F" and name in item.temperature_params else v
    return out, [n for n in item.params if n not in out]


def _result(equip_id: str, item: Item, status: str, evidence: dict, symptom=None, candidates=None) -> dict:
    return {"equip_uri": equip_id, "check_id": f"gl36:{item.id}", "subject": equip_id, "status": status,
            "severity": None, "symptom": symptom, "candidates": candidates or [], "evidence": evidence}


def check(item: Item, equip: dict, equips: list[dict], history: History, now: datetime, window: timedelta,
          settings: dict) -> dict:
    from baler.commissioning import vendor_constrain
    from baler.commissioning.vendor_constrain import library

    bound, proxies, missing = resolve(item, equip, equips)
    params, missing_params = _params(item, settings)
    ev: dict = {"item": item.id, "requirement": item.requirement, "constrain_class": item.cls,
                "upstream": f"{vendor_constrain.UPSTREAM} (vendored)",
                "points": {k: p["id"] for k, p in bound.items()}, "proxies": proxies, "params_c": params}
    if missing or missing_params:
        ev.update(missing_points=missing, missing_params=missing_params)
        what = [m["point"] for m in missing] + missing_params
        return _result(equip["id"], item, "inconclusive", ev, symptom=f"can't run {item.id}: no {', '.join(what)}",
                       candidates=[{"cause": "point not classified or not on this equipment",
                                    "distinguish": "evidence.missing_points lists the Brick classes looked for"},
                                   {"cause": "design parameters not entered",
                                    "distinguish": f"project settings gl36.{item.id} needs {', '.join(item.params)}"}])
    step = float(settings.get("step_minutes", DEFAULT_STEP_MINUTES))
    df = frame(item, bound, history, now - window, now + timedelta(seconds=1), step, frozenset(proxies))
    ev.update(step_minutes=step, samples=len(df))
    if df.empty:
        return _result(equip["id"], item, "inconclusive", ev,
                       symptom="no time steps where every point had a value in the window")
    checker = getattr(library, item.cls)(df, params=params, tolerances=vendor_constrain.TOLERANCES)
    verdict, detail = checker.get_checks
    result = checker.result
    failed = result[result == False]  # element-wise, as upstream does
    threshold = float(settings.get("max_fail_fraction", 0))
    ev.update(passed=detail["Pass #"], failed=detail["Fail #"], untested=detail["Untested #"],
              fail_fraction=round(detail["Fail #"] / max(detail["Sample #"], 1), 4),
              first_failures=[ts.isoformat() for ts in list(failed.index)[:MAX_FAILURES_LISTED]],
              tested_fail_fraction=round(detail["Fail #"] / max(detail["Pass #"] + detail["Fail #"], 1), 4),
              constrain_verdict=verdict if isinstance(verdict, bool) else str(verdict), max_fail_fraction=threshold)
    if verdict is True:
        return _result(equip["id"], item, "pass", ev)
    if verdict is False and detail["Pass #"] and ev["tested_fail_fraction"] <= threshold:
        ev["note"] = (f"ConStrain fails it ({detail['Fail #']} failing steps); within this project's "
                      f"max_fail_fraction {threshold:g}, so it passes here")
        return _result(equip["id"], item, "pass", ev)
    if verdict is False:
        return _result(equip["id"], item, "fail", ev,
                       symptom=f"{detail['Fail #']} of {detail['Sample #']} time steps break {item.requirement}",
                       candidates=[{"cause": "sequence not programmed or overridden",
                                    "distinguish": "trend the points in evidence.points around evidence.first_failures"},
                                   {"cause": "design parameters entered for the check differ from the sequence's",
                                    "distinguish": "compare evidence.params_c with the sequence of operations"},
                                   {"cause": "sensor reading wrong",
                                    "distinguish": "rung-2 liveness/stuck results for the same points"}])
    return _result(equip["id"], item, "inconclusive", ev, symptom="ConStrain reported every sample untested")


def run(equips: list[dict], history: History, now: datetime, window: timedelta, settings: dict,
        site: list[dict] | None = None) -> list[dict]:
    """One gl36:<item> result per enabled item per equipment it applies to.
    `site` (default `equips`) is where building-wide roles like outdoor
    air temperature are looked for."""
    out = []
    for item_id in sorted(settings):
        item = ITEMS[item_id]
        for e in equips:
            if not applies(item, e):
                continue
            try:
                out.append(check(item, e, site or equips, history, now, window, settings[item_id] or {}))
            except Exception as exc:  # noqa: BLE001 - one item's bad data mustn't sink the whole run
                out.append(_result(e["id"], item, "inconclusive", {"item": item.id, "error": f"{type(exc).__name__}: {exc}"[:500]},
                                   symptom="the check itself failed on this equipment's data"))
    return out

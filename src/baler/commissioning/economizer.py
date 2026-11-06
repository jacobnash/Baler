"""
Air-side economizer checks, Baler's own (`cx:` check ids). PNNL
ConStrain 0.8.0 has no economizer high-limit or integrated-economizer
item (todo/cx3-constrain-gl36-spike.md, finding 4), so these are written
here, not vendored. Pure Python over the Cx History seam - no pandas.

  cx:econ_high_limit  ASHRAE 90.1 6.5.1.1.3 (fixed dry-bulb high limit):
                      above the high limit the economizer is locked out,
                      so the outdoor-air damper sits at its minimum
                      position.
  cx:econ_integrated  ASHRAE 90.1 6.5.1.3 (integrated economizer
                      control): while mechanical cooling runs and outdoor
                      air is still useful (below the high limit and, when a
                      return-air sensor is bound, cooler than return air),
                      the economizer stays open above minimum instead of
                      being shut off when cooling starts.

Enabled per project with settings.economizer:

  {"high_limit": 75, "unit": "F", "min_position": 20,
   "position_tolerance": 5, "temperature_tolerance": 1,
   "max_fail_fraction": 0, "step_minutes": 5,
   "checks": ["high_limit", "integrated"],
   "points": {"<urn:equip:...>": {"oa_damper": "<urn:point:...>", ...}}}

high_limit is the design value for the climate zone (90.1 table
6.5.1.1.3: 75 F in the dry zones, 70 F in 5A/6A, 65 F in 1A-4A). The
damper is in percent open. A result has the same shape as gl36:
first_failures, fail_fraction, the bound points. Same verdict rule as
ConStrain by default (any failing step fails), relaxed only by the
project's max_fail_fraction.
"""

from __future__ import annotations

import bisect
import re
from datetime import datetime, timedelta

from baler import brick_vocab
from baler.commissioning.gl36 import is_fahrenheit
from baler.commissioning.history import History
from baler.commissioning.ladder import numeric

CHECKS = ("high_limit", "integrated")
ROLES = ("oat", "oa_damper", "cooling", "rat")
_NUM = {"high_limit": (-50, 150), "min_position": (0, 100), "position_tolerance": (0, 50),
        "temperature_tolerance": (0, 20), "max_fail_fraction": (0, 0.999), "step_minutes": (1, 60),
        "cooling_threshold": (0, 100)}
DEFAULTS = {"unit": "F", "min_position": 20.0, "position_tolerance": 5.0, "temperature_tolerance": 1.0,
            "max_fail_fraction": 0.0, "step_minutes": 5.0, "cooling_threshold": 5.0, "checks": list(CHECKS)}
HOLD_STEPS = 3
MAX_FAILURES_LISTED = 5

_OA_NAME = re.compile(r"(^|[^a-z])(oa|osa|outside|outdoor)([^a-z]|$)", re.IGNORECASE)
_COOL_NAME = re.compile(r"(chw|clg|cool|dx)", re.IGNORECASE)


def validate_settings(cfg) -> dict:
    if not isinstance(cfg, dict):
        raise TypeError("economizer must be an object")
    if "high_limit" not in cfg:
        raise ValueError("economizer.high_limit is required (the climate zone's fixed dry-bulb limit)")
    for k, v in cfg.items():
        if k == "unit":
            if v not in ("C", "F"):
                raise ValueError("economizer.unit must be C or F")
        elif k == "checks":
            if not isinstance(v, list) or not v or any(c not in CHECKS for c in v):
                raise ValueError(f"economizer.checks must be a non-empty list of {', '.join(CHECKS)}")
        elif k == "points":
            if not isinstance(v, dict) or any(not isinstance(m, dict) for m in v.values()):
                raise TypeError("economizer.points must be {equip uri: {role: point uri}}")
            for m in v.values():
                for role, uri in m.items():
                    if role not in ROLES or not isinstance(uri, str) or not uri.startswith("urn:point:"):
                        raise ValueError(f"economizer.points roles are {', '.join(ROLES)}, each a urn:point: URI")
        elif k in _NUM:
            lo, hi = _NUM[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise ValueError(f"economizer.{k} must be a number in [{lo:g}, {hi:g}]")
        else:
            raise ValueError(f"unknown economizer setting {k!r}")
    return cfg


# ------------------------------------------------------------- binding


def _fam(*classes) -> set[str]:
    vocab = brick_vocab.load()
    out: set[str] = set()
    for c in classes:
        out |= vocab.descendants(c) or {c}
    return out


def _named(p: dict, rx) -> bool:
    return bool(rx.search(str(p.get("dis") or "")) or rx.search(str(p.get("id") or "").rsplit("/", 1)[-1]))


def applies(equip: dict) -> bool:
    return any(c in _fam("Air_Handling_Unit") for c in equip.get("brickClasses") or [])


def bind(equip: dict, equips: list[dict], overrides: dict | None = None) -> tuple[dict[str, dict], dict[str, str]]:
    """(role -> point row, role -> how it was chosen). Explicit project
    bindings first; then Brick class, Haystack markers, and as a last
    resort the point's name - recorded in `how`, so a guess is visible."""
    by_id = {p["id"]: p for e in equips for p in e["points"]}
    bound: dict[str, dict] = {}
    how: dict[str, str] = {}
    for role, uri in (overrides or {}).items():
        if uri in by_id:
            bound[role], how[role] = by_id[uri], "project setting"
    pts = equip["points"]
    site_pts = [p for e in equips for p in e["points"]]

    if "oat" not in bound:
        fam = _fam("Outside_Air_Temperature_Sensor")
        hit = next((p for p in pts if p.get("brickClass") in fam), None) or next((p for p in site_pts if p.get("brickClass") in fam), None)
        if hit is None:
            hit = next((p for p in pts + site_pts if not p.get("brickClass") and {"outside", "air", "temp", "sensor"} <= set(p.get("tags") or ())), None)
        if hit:
            bound["oat"], how["oat"] = hit, "Brick class / markers" + ("" if hit in pts else " (elsewhere on the site)")

    if "rat" not in bound:
        fam = _fam("Return_Air_Temperature_Sensor")
        hit = next((p for p in pts if p.get("brickClass") in fam), None)
        if hit:
            bound["rat"], how["rat"] = hit, "Brick class"

    if "oa_damper" not in bound:
        fam = _fam("Damper_Position_Command", "Damper_Position_Sensor")
        dampers = [p for p in pts if p.get("brickClass") in fam or (not p.get("brickClass") and "damper" in (p.get("tags") or ()))]
        oa = [p for p in dampers if "outside" in (p.get("tags") or ()) or _named(p, _OA_NAME)]
        if len(oa) == 1:
            bound["oa_damper"], how["oa_damper"] = oa[0], "damper point marked or named outside air"
        elif len(dampers) == 1:
            bound["oa_damper"], how["oa_damper"] = dampers[0], "the equipment's only damper point (assumed outside air)"

    if "cooling" not in bound:
        direct = _fam("Cooling_Command", "Cooling_Enable_Command", "Cooling_Demand_Sensor")
        hit = next((p for p in pts if p.get("brickClass") in direct), None)
        if hit:
            bound["cooling"], how["cooling"] = hit, "Brick class"
        else:
            valves = _fam("Valve_Position_Command", "Valve_Command", "Valve_Position_Sensor")
            cool = [p for p in pts if (p.get("brickClass") in valves or (not p.get("brickClass") and "valve" in (p.get("tags") or ())))
                    and ({"cool", "chilled"} & set(p.get("tags") or ()) or _named(p, _COOL_NAME))]
            if len(cool) == 1:
                bound["cooling"], how["cooling"] = cool[0], "valve point marked or named cooling"
    return bound, how


# ---------------------------------------------------------------- grid


def _sampler(samples: list[tuple[datetime, float]], hold: timedelta):
    times = [t for t, _ in samples]

    def at(t: datetime) -> float | None:
        i = bisect.bisect_right(times, t) - 1
        if i < 0 or t - times[i] > hold:
            return None
        return samples[i][1]
    return at


def grid(bound: dict[str, dict], history: History, start: datetime, end: datetime, step: timedelta,
         to_unit: str) -> list[tuple[datetime, dict[str, float]]]:
    """Rows on a regular grid where every bound role has a value no older
    than HOLD_STEPS steps. Temperatures converted to `to_unit`."""
    series = history.window(sorted({p["id"] for p in bound.values()}), start, end)
    samplers = {}
    for role, p in bound.items():
        s = numeric(series.get(p["id"], []))
        if role in ("oat", "rat"):
            f = is_fahrenheit(p.get("unit"))
            if to_unit == "F" and not f:
                s = [(t, v * 9 / 5 + 32) for t, v in s]
            elif to_unit == "C" and f:
                s = [(t, (v - 32) * 5 / 9) for t, v in s]
        samplers[role] = _sampler(s, step * HOLD_STEPS)
    rows = []
    t = start
    while t <= end:
        vals = {r: f(t) for r, f in samplers.items()}
        if all(v is not None for v in vals.values()):
            rows.append((t, vals))  # type: ignore[arg-type]
        t += step
    return rows


# --------------------------------------------------------------- checks


def _result(equip_id, check, status, evidence, symptom=None, candidates=None) -> dict:
    return {"equip_uri": equip_id, "check_id": f"cx:econ_{check}", "subject": equip_id, "status": status,
            "severity": None, "symptom": symptom, "candidates": candidates or [], "evidence": evidence}


REQUIREMENT = {"high_limit": "ASHRAE 90.1 6.5.1.1.3 economizer high-limit shutoff",
               "integrated": "ASHRAE 90.1 6.5.1.3 integrated economizer control"}
NEEDS = {"high_limit": ("oat", "oa_damper"), "integrated": ("oat", "oa_damper", "cooling")}

CANDIDATES = {
    "high_limit": [
        {"cause": "high-limit setpoint in the controller differs from the design value",
         "distinguish": "compare the controller's economizer lockout setpoint with settings.economizer.high_limit"},
        {"cause": "outdoor air sensor reading low (sun, placement, failed)",
         "distinguish": "rung-2 liveness for the OAT point; compare with a weather source"},
        {"cause": "damper stuck open or actuator linkage slipped",
         "distinguish": "command the damper to minimum and watch position feedback / mixed-air temperature"},
    ],
    "integrated": [
        {"cause": "sequence closes the economizer when mechanical cooling starts (non-integrated)",
         "distinguish": "trend damper and cooling output across a cooling start in mild weather"},
        {"cause": "low-limit / freeze protection or mixed-air low limit overriding the damper",
         "distinguish": "mixed-air temperature and low-limit status at evidence.first_failures"},
        {"cause": "the cooling point bound is not the AHU's cooling output",
         "distinguish": "evidence.binding says how each point was chosen; set settings.economizer.points if wrong"},
    ],
}


def check(kind: str, equip: dict, equips: list[dict], history: History, now: datetime, window: timedelta,
          cfg: dict) -> dict:
    c = {**DEFAULTS, **cfg}
    bound, how = bind(equip, equips, (c.get("points") or {}).get(equip["id"]))
    need = NEEDS[kind]
    roles = {r: bound[r] for r in need if r in bound}
    if kind == "integrated" and "rat" in bound:
        roles["rat"] = bound["rat"]
    unit = c["unit"]
    ev: dict = {"requirement": REQUIREMENT[kind], "points": {r: p["id"] for r, p in roles.items()},
                "binding": {r: how[r] for r in roles}, "params": {k: c[k] for k in ("high_limit", "unit", "min_position",
                "position_tolerance", "temperature_tolerance", "max_fail_fraction") if k in c}}
    missing = [r for r in need if r not in bound]
    if missing:
        ev["missing_points"] = missing
        return _result(equip["id"], kind, "inconclusive", ev, symptom=f"can't run: no {', '.join(missing)} point bound",
                       candidates=[{"cause": "point not classified, or several candidates",
                                    "distinguish": "bind it explicitly with settings.economizer.points"}])
    step = timedelta(minutes=float(c["step_minutes"]))
    rows = grid(roles, history, now - window, now, step, unit)
    hl, tt = float(c["high_limit"]), float(c["temperature_tolerance"])
    floor = float(c["min_position"]) + float(c["position_tolerance"])
    tested, failed = 0, []
    if kind == "high_limit":
        for t, v in rows:
            if v["oat"] > hl + tt:
                tested += 1
                if v["oa_damper"] > floor:
                    failed.append(t)
    else:
        cooling_vals = [v["cooling"] for _, v in rows]
        binary = bool(cooling_vals) and all(x in (0.0, 1.0) for x in cooling_vals)
        on = 0.5 if binary else float(c["cooling_threshold"])
        for t, v in rows:
            useful = v["oat"] < hl - tt and ("rat" not in v or v["oat"] < v["rat"] - tt)
            if v["cooling"] > on and useful:
                tested += 1
                if v["oa_damper"] <= floor:
                    failed.append(t)
    ev.update(samples=len(rows), tested=tested, failed=len(failed), step_minutes=float(c["step_minutes"]),
              fail_fraction=round(len(failed) / tested, 4) if tested else 0.0,
              first_failures=[t.isoformat() for t in failed[:MAX_FAILURES_LISTED]])
    if not tested:
        why = ("outdoor air never above the high limit in the window" if kind == "high_limit"
               else "no step with mechanical cooling on while outdoor air was useful")
        return _result(equip["id"], kind, "inconclusive", ev, symptom=f"nothing to judge: {why} ({len(rows)} aligned steps)")
    if not failed:
        return _result(equip["id"], kind, "pass", ev)
    if ev["fail_fraction"] <= float(c["max_fail_fraction"]):
        ev["note"] = f"{len(failed)} failing steps, within this project's max_fail_fraction {c['max_fail_fraction']:g}"
        return _result(equip["id"], kind, "pass", ev)
    what = (f"outdoor-air damper above minimum ({floor:g}%) with outdoor air over the {hl:g} {unit} high limit"
            if kind == "high_limit" else
            f"outdoor-air damper at minimum (<= {floor:g}%) while mechanical cooling ran in economizer weather")
    return _result(equip["id"], kind, "fail", ev, symptom=f"{len(failed)} of {tested} steps: {what}",
                   candidates=CANDIDATES[kind])


def run(equips: list[dict], history: History, now: datetime, window: timedelta, cfg: dict) -> list[dict]:
    out = []
    kinds = cfg.get("checks") or list(CHECKS)
    for e in equips:
        if not applies(e):
            continue
        for kind in kinds:
            try:
                out.append(check(kind, e, equips, history, now, window, cfg))
            except Exception as exc:  # noqa: BLE001 - one AHU's bad data mustn't sink the run
                out.append(_result(e["id"], kind, "inconclusive", {"error": f"{type(exc).__name__}: {exc}"[:500]},
                                   symptom="the check itself failed on this equipment's data"))
    return out

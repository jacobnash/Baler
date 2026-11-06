"""
Start-up / power-on and functional test procedures as data (Cx 6,
Baler#42). Jacob: "think about Power on Testing and all that start-up".

A procedure is an ordered list of steps for one kind of equipment (a
Brick class, subclass-aware). Each step has acceptance criteria, and
each criterion is a Cx check the engine already runs:

  {"check": "exists", "strict": true}       every point reported (strict:
                                            never-reported fails, as it
                                            must after power-on)
  {"check": "live"}                         sensors move
  {"check": "pair:"}                        status follows command, sensor
                                            tracks setpoint (prefix match)
  {"check": "template:", "severity": "required"}
  {"check": "gl36:hw_reset"} / {"check": "cx:econ_*"}
  {"range": {"min": 40, "max": 110, "unit": "F"}, "point_class": "Temperature_Sensor"}
                                            values plausible over the window
  {"range": {...}, "point": "urn:point:..."}
                                            one named point - e.g. a
                                            derivation output

Execution is passive: the operator says "powered on / started at T", and
the procedure is judged on history from T over the procedure's window.
Nothing is written to equipment (active tests are Cx 9).

A step's status per equipment: fail if any criterion fails; inconclusive
if any is inconclusive (no evidence is never a pass); pass otherwise. A
criterion that matches nothing on the equipment is not_applicable unless
it says "required": true.

Every test run is diffed against the procedure's previous run, per
(equipment, step), in #5's reconcile vocabulary: new, resolved,
regressed, still_failing, still_passing, changed, gone.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta

from baler import brick_vocab
from baler.commissioning.gl36 import is_fahrenheit
from baler.commissioning.history import History
from baler.commissioning.ladder import numeric

KINDS = ("power_on", "startup", "functional", "seasonal")
STATUSES = ("draft", "active", "archived")
ORDER = {"fail": 0, "inconclusive": 1, "pass": 2, "not_applicable": 3}
MAX_STEPS = 50
MAX_CRITERIA = 20
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,59}")
DEFAULT_HOURS = {"power_on": 2.0, "startup": 24.0, "functional": 24.0, "seasonal": 24.0 * 7}


def _step(sid, title, instruction, *criteria):
    return {"id": sid, "title": title, "instruction": instruction, "criteria": list(criteria)}


EXISTS_STRICT = {"check": "exists", "strict": True}
NO_STALE = {"check": "exists"}
LIVE = {"check": "live"}
CMD_STATUS = {"check": "pair:", "pair_kind": "cmd_status"}
TRACKING = {"check": "pair:", "pair_kind": "track"}
REQUIRED_POINTS = {"check": "template:", "severity": "required", "required": True}
TEMP_F = {"range": {"min": 0, "max": 250, "unit": "F"}, "point_class": "Temperature_Sensor"}
AIR_TEMP_F = {"range": {"min": 30, "max": 130, "unit": "F"}, "point_class": "Air_Temperature_Sensor"}


def _power_on(name, equip_class, label):
    return {"template": f"{name}_power_on", "name": f"{label} power-on", "kind": "power_on", "equip_class": equip_class,
            "window_hours": 2.0, "steps": [
                _step("points-appear", "Every point appears", "After power-up, every mapped point reports at least once.",
                      EXISTS_STRICT),
                _step("plausible", "Values are plausible", "Temperatures read inside physical limits (no 0, -40 or open-circuit values).",
                      TEMP_F),
                _step("status-follows", "Status follows command", "Each start/stop or fan/pump command is reflected in its status.",
                      CMD_STATUS),
                _step("required-points", "Required points are mapped", "The expected-point template's required points exist.",
                      REQUIRED_POINTS),
            ]}


def _startup(name, equip_class, label, extra=()):
    steps = [
        _step("no-stale", "No stale points", "Every point keeps reporting through the start-up window.", NO_STALE),
        _step("sensors-live", "Sensors are live", "No sensor is frozen at one value.", LIVE),
        _step("status-follows", "Status follows command", "Commands and statuses agree.", CMD_STATUS),
        _step("control", "Loops control to setpoint", "Each sensor tracks its setpoint within tolerance.", TRACKING),
        *extra,
    ]
    return {"template": f"{name}_startup", "name": f"{label} start-up", "kind": "startup", "equip_class": equip_class,
            "window_hours": 24.0, "steps": steps}


SEEDS: list[dict] = [
    _power_on("ahu", "Air_Handling_Unit", "AHU"),
    _startup("ahu", "Air_Handling_Unit", "AHU", (
        _step("air-temps", "Air temperatures plausible", "Supply, return, mixed and outside air temperatures in range.", AIR_TEMP_F),
    )),
    _power_on("vav", "Variable_Air_Volume_Box", "VAV"),
    _startup("vav", "Variable_Air_Volume_Box", "VAV", (
        _step("zone-temp", "Zone temperature plausible", "Zone air temperature in an occupiable range.",
              {"range": {"min": 50, "max": 90, "unit": "F"}, "point_class": "Zone_Air_Temperature_Sensor"}),
    )),
    _power_on("fcu", "Fan_Coil_Unit", "FCU"),
    _startup("fcu", "Fan_Coil_Unit", "FCU"),
    _power_on("chiller", "Chiller", "Chiller"),
    _startup("chiller", "Chiller", "Chiller", (
        _step("chw-temp", "Chilled water temperature plausible", "Leaving chilled water between 35 and 65 F while running.",
              {"range": {"min": 35, "max": 65, "unit": "F"}, "point_class": "Chilled_Water_Supply_Temperature_Sensor"}),
    )),
    _power_on("boiler", "Boiler", "Boiler"),
    _startup("boiler", "Boiler", "Boiler", (
        _step("hw-temp", "Hot water temperature plausible", "Supply hot water between 80 and 210 F.",
              {"range": {"min": 80, "max": 210, "unit": "F"}, "point_class": "Hot_Water_Supply_Temperature_Sensor"}),
    )),
    _power_on("pump", "Pump", "Pump"),
    _startup("pump", "Pump", "Pump"),
]
SEED_BY_ID = {s["template"]: s for s in SEEDS}


# ----------------------------------------------------------- validation


def _num(v, what) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise TypeError(f"{what} must be a number")
    return float(v)


def _criterion(c, where: str) -> dict:
    if not isinstance(c, dict):
        raise TypeError(f"{where}: a criterion is an object")
    allowed = {"check", "strict", "severity", "required", "pair_kind", "point_class", "range", "point"}
    extra = set(c) - allowed
    if extra:
        raise ValueError(f"{where}: unknown criterion keys {sorted(extra)}")
    if ("check" in c) == ("range" in c):
        raise ValueError(f"{where}: a criterion has either check or range")
    if "check" in c:
        if not isinstance(c["check"], str) or not c["check"]:
            raise ValueError(f"{where}: check must be a check id or prefix")
        if c.get("pair_kind") not in (None, "cmd_status", "track"):
            raise ValueError(f"{where}: pair_kind is cmd_status or track")
        if c.get("severity") not in (None, "required", "expected", "optional"):
            raise ValueError(f"{where}: severity is required, expected or optional")
    else:
        r = c["range"]
        if not isinstance(r, dict):
            raise TypeError(f"{where}: range is {{min, max, unit}}")
        lo = _num(r.get("min"), f"{where}: range.min") if "min" in r else None
        hi = _num(r.get("max"), f"{where}: range.max") if "max" in r else None
        if lo is None and hi is None:
            raise ValueError(f"{where}: range needs min or max")
        if lo is not None and hi is not None and lo > hi:
            raise ValueError(f"{where}: range.min is above range.max")
        if r.get("unit") not in (None, "C", "F"):
            raise ValueError(f"{where}: range.unit is C or F")
        if not c.get("point") and not c.get("point_class"):
            raise ValueError(f"{where}: a range names a point or a point_class")
        if c.get("point") is not None and (not isinstance(c["point"], str) or not c["point"].startswith("urn:point:")):
            raise ValueError(f"{where}: point must be a urn:point: URI")
    for flag in ("strict", "required"):
        if flag in c and not isinstance(c[flag], bool):
            raise ValueError(f"{where}: {flag} is true or false")
    if "point_class" in c and not isinstance(c["point_class"], str):
        raise ValueError(f"{where}: point_class is a Brick class name")
    return dict(c)


def normalize(body: dict, existing: dict | None = None) -> dict:
    """A client-supplied procedure (or a partial PATCH over `existing`)
    into the stored fields. Raises ValueError/TypeError."""
    base = copy.deepcopy(existing) if existing else {}
    for k in ("name", "kind", "equip_class", "steps", "window_hours", "schedule", "status", "equip_uris", "description"):
        if k in body:
            base[k] = body[k]
    name = base.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    if base.get("kind") not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if not isinstance(base.get("equip_class"), str) or not base["equip_class"]:
        raise ValueError("equip_class (a Brick equipment class) is required")
    uris = base.get("equip_uris")
    if uris is not None and (not isinstance(uris, list) or any(not isinstance(u, str) or not u.startswith("urn:equip:") for u in uris)):
        raise ValueError("equip_uris must be a list of urn:equip: URIs")
    steps = base.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > MAX_STEPS:
        raise ValueError(f"steps must be a list of 1-{MAX_STEPS} steps")
    seen = set()
    out_steps = []
    for i, st in enumerate(steps):
        if not isinstance(st, dict):
            raise TypeError("each step is an object")
        sid = st.get("id") or f"step-{i + 1}"
        if not isinstance(sid, str) or not _ID.fullmatch(sid) or sid in seen:
            raise ValueError(f"step id {sid!r} must be unique and match [a-z0-9][a-z0-9_-]*")
        seen.add(sid)
        crit = st.get("criteria")
        if not isinstance(crit, list) or not crit or len(crit) > MAX_CRITERIA:
            raise ValueError(f"step {sid}: criteria must be a list of 1-{MAX_CRITERIA}")
        out_steps.append({"id": sid, "title": str(st.get("title") or sid)[:200],
                          "instruction": str(st.get("instruction") or "")[:2000],
                          "criteria": [_criterion(c, f"step {sid}") for c in crit]})
    wh = _num(base.get("window_hours", DEFAULT_HOURS[base["kind"]]), "window_hours")
    if not 0 < wh <= 24 * 31:
        raise ValueError("window_hours must be in (0, 744]")
    sched = base.get("schedule")
    if sched is not None:
        if not isinstance(sched, dict) or set(sched) - {"every_hours"}:
            raise ValueError("schedule is {every_hours} or null")
        every = _num(sched.get("every_hours"), "schedule.every_hours")
        if not 1 <= every <= 24 * 31:
            raise ValueError("schedule.every_hours must be in [1, 744]")
    status = base.get("status", "active")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    return {"name": name.strip()[:200], "kind": base["kind"], "equip_class": base["equip_class"],
            "equip_uris": base.get("equip_uris"), "description": str(base.get("description") or "")[:4000],
            "steps": out_steps, "window_hours": wh, "schedule": sched, "status": status}


def from_seed(template: str) -> dict:
    seed = SEED_BY_ID.get(template)
    if seed is None:
        raise ValueError(f"unknown procedure template {template!r} (known: {', '.join(SEED_BY_ID)})")
    out = normalize({k: v for k, v in seed.items() if k != "template"})
    out["template"] = template
    return out


# ------------------------------------------------------------ evaluation


def _family(cls: str) -> set[str]:
    return brick_vocab.load().descendants(cls) or {cls}


def applies(proc: dict, equip: dict) -> bool:
    if proc.get("equip_uris"):
        return equip["id"] in proc["equip_uris"]
    fam = _family(proc["equip_class"])
    return any(c in fam for c in equip.get("brickClasses") or [])


def _matches(check_id: str, want: str) -> bool:
    if want.endswith("*"):
        return check_id.startswith(want[:-1])
    if want.endswith(":"):
        return check_id.startswith(want)
    return check_id == want or check_id.startswith(want + ":")


_CMD_PAIRS = ("pair:fan_cmd_status", "pair:pump_cmd_status", "pair:run_cmd_status")


def _point_classes(subject: str, classes: dict[str, str | None]) -> list[str | None]:
    return [classes.get(s) for s in subject.split("|")]


def eval_check(c: dict, equip: dict, results: list[dict], classes: dict[str, str | None]) -> dict:
    rs = [r for r in results if r["equip_uri"] == equip["id"] and _matches(r["check_id"], c["check"])]
    if c.get("severity"):
        rs = [r for r in rs if r.get("severity") == c["severity"]]
    if c.get("pair_kind") == "cmd_status":
        rs = [r for r in rs if r["check_id"] in _CMD_PAIRS]
    elif c.get("pair_kind") == "track":
        rs = [r for r in rs if r["check_id"].startswith("pair:") and r["check_id"] not in _CMD_PAIRS]
    if c.get("point_class"):
        fam = _family(c["point_class"])
        rs = [r for r in rs if any(k in fam for k in _point_classes(r["subject"], classes))]
    counts = {s: 0 for s in ORDER}
    for r in rs:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    if c.get("strict"):
        counts["fail"] += counts["inconclusive"]
        counts["inconclusive"] = 0
    if not rs:
        status = "inconclusive" if c.get("required") else "not_applicable"
    elif counts["fail"]:
        status = "fail"
    elif counts["inconclusive"]:
        status = "inconclusive"
    elif counts["pass"]:
        status = "pass"
    else:
        status = "not_applicable"
    failing = [{"check_id": r["check_id"], "subject": r["subject"], "status": r["status"], "symptom": r.get("symptom")}
               for r in rs if r["status"] == "fail" or (c.get("strict") and r["status"] == "inconclusive")][:10]
    return {"criterion": c, "status": status, "counts": counts, "failing": failing}


def eval_range(c: dict, equip: dict, series: dict[str, list], points: dict[str, dict]) -> dict:
    r = c["range"]
    if c.get("point"):
        targets = [points[c["point"]]] if c["point"] in points else []
    else:
        fam = _family(c["point_class"])
        targets = [p for p in equip["points"] if p.get("brickClass") in fam]
    if not targets:
        return {"criterion": c, "status": "inconclusive" if c.get("required") or c.get("point") else "not_applicable",
                "counts": {}, "failing": []}
    out, bad, unknown = [], [], 0
    for p in targets:
        vals = [v for _, v in numeric(series.get(p["id"], []))]
        if not vals:
            unknown += 1
            continue
        lo, hi = r.get("min"), r.get("max")
        unit = r.get("unit")
        f = is_fahrenheit(p.get("unit"))
        if unit == "F" and p.get("unit") and not f:
            lo, hi = [None if x is None else (x - 32) * 5 / 9 for x in (lo, hi)]
        elif unit == "C" and f:
            lo, hi = [None if x is None else x * 9 / 5 + 32 for x in (lo, hi)]
        low, high = min(vals), max(vals)
        ok = (lo is None or low >= lo) and (hi is None or high <= hi)
        out.append(p["id"])
        if not ok:
            bad.append({"subject": p["id"], "status": "fail", "check_id": "range",
                        "symptom": f"{p.get('dis') or p['id']} ranged {low:g}..{high:g} {p.get('unit') or ''}".strip()
                        + f", outside {lo if lo is not None else '-inf'}..{hi if hi is not None else 'inf'}"})
    status = "fail" if bad else ("inconclusive" if unknown or not out else "pass")
    return {"criterion": c, "status": status, "counts": {"pass": len(out) - len(bad), "fail": len(bad), "inconclusive": unknown},
            "failing": bad[:10]}


def worst(statuses) -> str:
    st = [s for s in statuses if s != "not_applicable"]
    if not st:
        return "not_applicable"
    return min(st, key=lambda s: ORDER[s])


def evaluate(proc: dict, equips: list[dict], results: list[dict], series: dict[str, list]) -> list[dict]:
    """[{equip_uri, dis, status, steps: [{id, title, status, criteria: [...]}]}]
    for every equipment the procedure applies to."""
    classes = {p["id"]: p.get("brickClass") for e in equips for p in e["points"]}
    points = {p["id"]: p for e in equips for p in e["points"]}
    out = []
    for e in equips:
        if not applies(proc, e):
            continue
        steps = []
        for st in proc["steps"]:
            crit = [eval_range(c, e, series, points) if "range" in c else eval_check(c, e, results, classes)
                    for c in st["criteria"]]
            steps.append({"id": st["id"], "title": st["title"], "status": worst(c["status"] for c in crit), "criteria": crit})
        out.append({"equip_uri": e["id"], "dis": e.get("dis"), "status": worst(s["status"] for s in steps), "steps": steps})
    return out


def summarize(per_equip: list[dict]) -> dict:
    by = {s: 0 for s in ORDER}
    for e in per_equip:
        by[e["status"]] += 1
    steps = {s: 0 for s in ORDER}
    for e in per_equip:
        for st in e["steps"]:
            steps[st["status"]] += 1
    return {"equipment": len(per_equip), "by_status": by, "steps_by_status": steps}


def diff(previous: list[dict] | None, current: list[dict]) -> dict:
    """#5's reconcile states, per (equipment, step)."""
    prev = {(e["equip_uri"], s["id"]): s["status"] for e in previous or [] for s in e["steps"]}
    cur = {(e["equip_uri"], s["id"]): s["status"] for e in current for s in e["steps"]}
    rows = []
    for key in sorted(set(prev) | set(cur)):
        a, b = prev.get(key), cur.get(key)
        if a is None:
            state = "new"
        elif b is None:
            state = "gone"
        elif a == b:
            state = "still_failing" if b == "fail" else ("still_passing" if b == "pass" else "unchanged")
        elif b == "pass" and a in ("fail", "inconclusive"):
            state = "resolved"
        elif b == "fail":
            state = "regressed"
        else:
            state = "changed"
        rows.append({"equip_uri": key[0], "step": key[1], "before": a, "after": b, "state": state})
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    return {"counts": counts, "changes": [r for r in rows if r["state"] not in ("still_passing", "unchanged")][:500]}


def diff_results(previous: list[dict], current: list[dict]) -> dict:
    """The same reconcile, for two verification runs' results, per
    (check, subject)."""
    prev = {(r["check_id"], r["subject"]): r for r in previous}
    cur = {(r["check_id"], r["subject"]): r for r in current}
    as_steps = lambda d: [{"equip_uri": f"{k[0]}|{k[1]}", "steps": [{"id": "r", "status": r["status"]}]}
                          for k, r in d.items()]
    out = diff(as_steps(prev), as_steps(cur))
    for row in out["changes"]:
        check_id, subject = row.pop("equip_uri").split("|", 1)
        row.pop("step")
        src = cur.get((check_id, subject)) or prev.get((check_id, subject)) or {}
        row.update(check_id=check_id, subject=subject, equip_uri=src.get("equip_uri"), symptom=src.get("symptom"))
    return out


class ClippedHistory:
    """History as of `end`: `latest` is the last sample at or before end
    (within `lookback`), so a past test window isn't judged on data that
    arrived later."""

    def __init__(self, history: History, end: datetime, lookback: timedelta = timedelta(days=31)):
        self.history = history
        self.end = end
        self.lookback = lookback

    def latest(self, point_uris: list[str]):
        w = self.history.window(point_uris, self.end - self.lookback, self.end + timedelta(seconds=1))
        return {u: s[-1] for u, s in w.items() if s}

    def window(self, point_uris: list[str], start: datetime, end: datetime):
        return self.history.window(point_uris, start, min(end, self.end + timedelta(seconds=1)))

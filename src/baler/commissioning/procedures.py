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
  {"range": {"min": 65, "max": 80, "unit": "F",
             "hours": {"from": 7, "to": 18, "days": [0,1,2,3,4], "tz": "America/Chicago"}},
   "point_class": "Zone_Air_Temperature_Sensor"}
                                            a band during occupied hours only (S2)
  {"availability": {"min_pct": 99, "max_gap_minutes": 5}, "point_class": "..."}
                                            the point reported at least every
                                            5 min for 99% of the window (S3)
  {"response": {"when": {"point_class": "Fan_Status", "equals": 1},
                "then": {"point_class": "Zone_Air_Temperature_Sensor",
                         "changes_by": 0.5, "within_minutes": 60}}}
                                            each time the condition starts, the
                                            other point moves by at least that
                                            much within that time (S1)

Every criterion reports what it observed per point (min/max/last, sample
count, availability, the edges it judged), not only what failed: evidence
is computed, not typed (Baler#86).

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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


KINDS_OF_CRITERION = ("check", "range", "availability", "response")


def criterion_kind(c: dict) -> str:
    return next(k for k in KINDS_OF_CRITERION if k in c)


def _target_ref(t, where: str) -> None:
    if not isinstance(t, dict) or bool(t.get("point")) == bool(t.get("point_class")):
        raise ValueError(f"{where}: names exactly one of point or point_class")
    if t.get("point") is not None and (not isinstance(t["point"], str) or not t["point"].startswith("urn:point:")):
        raise ValueError(f"{where}: point must be a urn:point: URI")
    if t.get("point_class") is not None and not isinstance(t["point_class"], str):
        raise ValueError(f"{where}: point_class is a Brick class name")


def _hours(h, where: str) -> None:
    if not isinstance(h, dict) or set(h) - {"from", "to", "days", "tz"}:
        raise ValueError(f"{where}: range.hours is {{from, to, days, tz}}")
    lo, hi = h.get("from", 0), h.get("to", 24)
    if any(isinstance(x, bool) or not isinstance(x, int) for x in (lo, hi)) or not 0 <= lo < hi <= 24:
        raise ValueError(f"{where}: range.hours from/to are whole hours, 0 <= from < to <= 24")
    days = h.get("days")
    if days is not None and (not isinstance(days, list) or not days or
                             any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in days)):
        raise ValueError(f"{where}: range.hours.days is a list of weekdays, 0 = Monday")
    if h.get("tz") is not None:
        try:
            ZoneInfo(str(h["tz"]))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"{where}: range.hours.tz {h['tz']!r} isn't a time zone") from exc


def _criterion(c, where: str) -> dict:
    if not isinstance(c, dict):
        raise TypeError(f"{where}: a criterion is an object")
    allowed = {"check", "strict", "severity", "required", "pair_kind", "point_class", "range", "point",
               "availability", "response"}
    extra = set(c) - allowed
    if extra:
        raise ValueError(f"{where}: unknown criterion keys {sorted(extra)}")
    if sum(k in c for k in KINDS_OF_CRITERION) != 1:
        raise ValueError(f"{where}: a criterion has exactly one of check, range, availability or response")
    if "check" in c:
        if not isinstance(c["check"], str) or not c["check"]:
            raise ValueError(f"{where}: check must be a check id or prefix")
        if c.get("pair_kind") not in (None, "cmd_status", "track"):
            raise ValueError(f"{where}: pair_kind is cmd_status or track")
        if c.get("severity") not in (None, "required", "expected", "optional"):
            raise ValueError(f"{where}: severity is required, expected or optional")
    elif "range" in c:
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
        if "hours" in r:
            _hours(r["hours"], where)
        if set(r) - {"min", "max", "unit", "hours"}:
            raise ValueError(f"{where}: range is {{min, max, unit, hours}}")
        _target_ref({"point": c.get("point"), "point_class": c.get("point_class")}, where)
    elif "availability" in c:
        a = c["availability"]
        if not isinstance(a, dict) or set(a) - {"min_pct", "max_gap_minutes"}:
            raise ValueError(f"{where}: availability is {{min_pct, max_gap_minutes}}")
        pct = _num(a.get("min_pct"), f"{where}: availability.min_pct")
        gap = _num(a.get("max_gap_minutes"), f"{where}: availability.max_gap_minutes")
        if not 0 < pct <= 100 or gap <= 0:
            raise ValueError(f"{where}: availability.min_pct is in (0, 100] and max_gap_minutes is positive")
        _target_ref({"point": c.get("point"), "point_class": c.get("point_class")}, where)
    else:
        r = c["response"]
        if not isinstance(r, dict) or set(r) != {"when", "then"}:
            raise ValueError(f"{where}: response is {{when, then}}")
        _target_ref({k: v for k, v in r["when"].items() if k != "equals"} if isinstance(r["when"], dict) else None,
                    f"{where}: response.when")
        if "equals" not in r["when"] or not isinstance(r["when"]["equals"], (int, float, str, bool)):
            raise ValueError(f"{where}: response.when.equals is the value that starts it (e.g. 1, true, \"active\")")
        then = r["then"]
        if not isinstance(then, dict):
            raise TypeError(f"{where}: response.then is an object")
        _target_ref({k: v for k, v in then.items() if k not in ("changes_by", "within_minutes")}, f"{where}: response.then")
        if _num(then.get("changes_by"), f"{where}: response.then.changes_by") <= 0 or \
                _num(then.get("within_minutes"), f"{where}: response.then.within_minutes") <= 0:
            raise ValueError(f"{where}: response.then.changes_by and within_minutes are positive")
        if set(r["when"]) - {"point", "point_class", "equals"} or set(then) - {"point", "point_class", "changes_by", "within_minutes"}:
            raise ValueError(f"{where}: response.when is {{point|point_class, equals}}, then is "
                             "{point|point_class, changes_by, within_minutes}")
    for flag in ("strict", "required"):
        if flag in c and not isinstance(c[flag], bool):
            raise ValueError(f"{where}: {flag} is true or false")
    if "point_class" in c and not isinstance(c["point_class"], str):
        raise ValueError(f"{where}: point_class is a Brick class name")
    return dict(c)


# Spec point-list columns that state acceptance criteria (Baler#86),
# matched with punctuation and case squeezed out.
_SPEC_LIMITS = {"pfcmin": ("pfc", "min"), "pfcmax": ("pfc", "max"), "bandmin": ("band", "min"), "bandmax": ("band", "max")}


def _squeeze(k) -> str:
    return re.sub(r"[^a-z0-9]", "", str(k or "").lower())


def spec_limits(props: dict | None) -> dict[str, dict[str, float]]:
    """{"pfc": {"min": 40.0, "max": 110.0}, "band": {...}} from a spec
    point's properties; columns that aren't numbers are ignored."""
    out: dict[str, dict[str, float]] = {}
    for k, v in (props or {}).items():
        hit = _SPEC_LIMITS.get(_squeeze(k))
        if hit is None or v in (None, ""):
            continue
        try:
            out.setdefault(hit[0], {})[hit[1]] = float(str(v).strip())
        except ValueError:
            continue
    return out


def _unit_letter(units) -> str | None:
    u = str(units or "").strip().lower().replace("°", "").replace("deg", "")
    if u in ("f", "fahrenheit", "degreesfahrenheit"):
        return "F"
    if u in ("c", "celsius", "degreescelsius"):
        return "C"
    return None


def from_spec(equip: dict, spec_equip_item: dict, rows: list[dict], point_items: dict[str, dict],
              availability: dict | None) -> dict | None:
    """A procedure seeded from accepted spec points (Baler#86): each spec
    point becomes a PFC - present (strict exists), in its pfc range, in its
    operating band, and available. `rows` is spec.points_alignment for the
    equipment; `point_items` maps spec item id -> the spec point."""
    steps = [_step("points-present", "Specified points are present and reporting",
                   "Every point the specification lists reports after power-on.", EXISTS_STRICT)]
    absent, avail_points = [], []
    for r in rows:
        if not r.get("spec_item_id"):
            continue
        item = point_items[r["spec_item_id"]]
        if r["status"] != "present":
            absent.append(item["name"])
            continue
        lim = spec_limits(item.get("properties"))
        unit = _unit_letter((item.get("properties") or {}).get("units"))
        slug = re.sub(r"[^a-z0-9]+", "-", str(item["name"]).lower()).strip("-")[:40] or "point"
        for kind, title in (("pfc", "within its PFC range"), ("band", "within its operating band")):
            if kind in lim and len(steps) < MAX_STEPS:
                rng = {**lim[kind], **({"unit": unit} if unit else {})}
                steps.append(_step(f"{kind}-{slug}"[:60], f"{item['name']} {title}",
                                   f"Spec {kind}_min/{kind}_max for {item['name']}.", {"range": rng, "point": r["point_uri"]}))
        avail_points.append(r["point_uri"])
    if availability and avail_points and len(steps) < MAX_STEPS:
        crit = [{"availability": dict(availability), "point": u} for u in avail_points[:MAX_CRITERIA]]
        steps.append({"id": "available", "title": "Specified points are available",
                      "instruction": f"Each reports at least every {availability['max_gap_minutes']:g} min for "
                                     f"{availability['min_pct']:g}% of the window.", "criteria": crit})
    if len(steps) == 1 and not absent:
        return None
    desc = f"Seeded from the specification for {spec_equip_item['tag']}."
    if absent:
        desc += f" Specified but not found in the field: {', '.join(absent)}."
    return {"name": f"{spec_equip_item['tag']} spec acceptance (PFC)", "kind": "startup",
            "equip_class": (equip.get("brickClasses") or [equip.get("brickClass") or "Equipment"])[0],
            "equip_uris": [equip["id"]], "window_hours": DEFAULT_HOURS["startup"], "description": desc, "steps": steps}


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


def _targets(ref: dict, equip: dict, points: dict[str, dict]) -> list[dict]:
    if ref.get("point"):
        return [points[ref["point"]]] if ref["point"] in points else []
    fam = _family(ref["point_class"])
    return [p for p in equip["points"] if p.get("brickClass") in fam]


def series_points(c: dict, equips: list[dict]) -> set[str]:
    """Point ids a criterion needs history for (runner reads them)."""
    kind = criterion_kind(c)
    if kind == "check":
        return set()
    refs = [c["response"]["when"], c["response"]["then"]] if kind == "response" else [c]
    out: set[str] = set()
    for ref in refs:
        if ref.get("point"):
            out.add(ref["point"])
        else:
            fam = _family(ref["point_class"])
            out |= {p["id"] for e in equips for p in e["points"] if p.get("brickClass") in fam}
    return out


def _in_hours(ts: datetime, hours: dict | None) -> bool:
    if not hours:
        return True
    local = ts.astimezone(ZoneInfo(hours["tz"])) if hours.get("tz") else ts
    if hours.get("days") is not None and local.weekday() not in hours["days"]:
        return False
    return hours.get("from", 0) <= local.hour < hours.get("to", 24)


def _iso(ts) -> str | None:
    return ts.isoformat() if isinstance(ts, datetime) else ts


def _observed(p: dict, rows: list, **extra) -> dict:
    vals = [v for _, v in rows]
    out = {"subject": p["id"], "dis": p.get("dis"), "unit": p.get("unit"), "samples": len(rows)}
    if vals:
        out.update(min=min(vals), max=max(vals), last=vals[-1], first_ts=_iso(rows[0][0]), last_ts=_iso(rows[-1][0]))
    return {**out, **extra}


def eval_range(c: dict, equip: dict, series: dict[str, list], points: dict[str, dict],
               truncated: set[str] | frozenset = frozenset()) -> dict:
    """A point whose window was cut at the read limit can still fail (a bad
    value was seen) but can't pass: the unread rest might not be in range.
    `range.hours` judges only samples inside those local hours (S2)."""
    r = c["range"]
    targets = _targets(c, equip, points)
    if not targets:
        return {"criterion": c, "status": "inconclusive" if c.get("required") or c.get("point") else "not_applicable",
                "counts": {}, "failing": [], "observed": []}
    out, bad, unknown, partial, observed = [], [], 0, [], []
    for p in targets:
        rows = [(t, v) for t, v in numeric(series.get(p["id"], [])) if _in_hours(t, r.get("hours"))]
        observed.append(_observed(p, rows))
        vals = [v for _, v in rows]
        if not vals:
            unknown += 1
            partial.append({"subject": p["id"], "status": "inconclusive", "check_id": "range",
                            "symptom": "no numeric samples" + (" inside the judged hours" if r.get("hours") else " in the window")})
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
        if not ok:
            out.append(p["id"])
            bad.append({"subject": p["id"], "status": "fail", "check_id": "range",
                        "symptom": f"{p.get('dis') or p['id']} ranged {low:g}..{high:g} {p.get('unit') or ''}".strip()
                        + f", outside {lo if lo is not None else '-inf'}..{hi if hi is not None else 'inf'}"})
        elif p["id"] in truncated:
            unknown += 1
            partial.append({"subject": p["id"], "status": "inconclusive", "check_id": "range",
                            "symptom": f"window truncated at {len(series.get(p['id'], []))} samples: "
                                       "only the start of the window was read, so the rest is unjudged"})
        else:
            out.append(p["id"])
    status = "fail" if bad else ("inconclusive" if unknown or not out else "pass")
    return {"criterion": c, "status": status, "counts": {"pass": len(out) - len(bad), "fail": len(bad), "inconclusive": unknown},
            "failing": (bad + partial)[:10], "observed": observed}


def availability_pct(times: list[datetime], start: datetime, end: datetime, max_gap: timedelta) -> float:
    """Share of [start, end) covered when each sample vouches for the point
    up to `max_gap` after it (and the window start counts as a gap)."""
    total = (end - start).total_seconds()
    if total <= 0:
        return 0.0
    covered = 0.0
    ts = sorted(t for t in times if start <= t < end)
    for i, t in enumerate(ts):
        nxt = ts[i + 1] if i + 1 < len(ts) else end
        covered += min((nxt - t).total_seconds(), max_gap.total_seconds())
    if ts:
        covered += min((ts[0] - start).total_seconds(), max_gap.total_seconds()) if ts[0] > start else 0
    return round(100.0 * min(covered, total) / total, 2)


def eval_availability(c: dict, equip: dict, series: dict[str, list], points: dict[str, dict],
                      window: tuple[datetime, datetime] | None, truncated=frozenset()) -> dict:
    a = c["availability"]
    targets = _targets(c, equip, points)
    if not targets:
        return {"criterion": c, "status": "inconclusive" if c.get("required") or c.get("point") else "not_applicable",
                "counts": {}, "failing": [], "observed": []}
    if window is None:
        return {"criterion": c, "status": "inconclusive", "counts": {"inconclusive": len(targets)}, "observed": [],
                "failing": [{"subject": equip["id"], "status": "inconclusive", "check_id": "availability",
                             "symptom": "no judged window"}]}
    gap = timedelta(minutes=a["max_gap_minutes"])
    failing, observed, passed, unknown = [], [], 0, 0
    for p in targets:
        rows = [(t, v) for t, v in series.get(p["id"], []) if v is not None]
        pct = availability_pct([t for t, _ in rows], window[0], window[1], gap)
        observed.append(_observed(p, numeric(rows), availability_pct=pct, max_gap_minutes=a["max_gap_minutes"]))
        if p["id"] in truncated:
            unknown += 1
            failing.append({"subject": p["id"], "status": "inconclusive", "check_id": "availability",
                            "symptom": f"window truncated at {len(series.get(p['id'], []))} samples"})
        elif pct + 1e-9 < a["min_pct"]:
            failing.append({"subject": p["id"], "status": "fail", "check_id": "availability",
                            "symptom": f"{p.get('dis') or p['id']} available {pct:g}% (a sample at least every "
                                       f"{a['max_gap_minutes']:g} min), below {a['min_pct']:g}%"})
        else:
            passed += 1
    nfail = sum(1 for f in failing if f["status"] == "fail")
    status = "fail" if nfail else ("inconclusive" if unknown else "pass")
    return {"criterion": c, "status": status, "counts": {"pass": passed, "fail": nfail, "inconclusive": unknown},
            "failing": failing[:10], "observed": observed}


def _value(v) -> float | None:
    got = numeric([(None, v)])
    return got[0][1] if got else None


def eval_response(c: dict, equip: dict, series: dict[str, list], points: dict[str, dict]) -> dict:
    """S1: each time `when` starts (its value becomes `equals`), every `then`
    point must move by at least changes_by within within_minutes. No start
    in the window is inconclusive - the condition was never exercised."""
    when, then = c["response"]["when"], c["response"]["then"]
    w_targets, t_targets = _targets(when, equip, points), _targets(then, equip, points)
    if not w_targets or not t_targets:
        return {"criterion": c, "status": "inconclusive" if c.get("required") else "not_applicable",
                "counts": {}, "failing": [], "observed": []}
    want = _value(when["equals"])
    within = timedelta(minutes=then["within_minutes"])
    edges = []
    for wp in w_targets:
        prev = None
        for t, v in numeric(series.get(wp["id"], [])):
            if v == want and prev is not None and prev != want:
                edges.append((t, wp))
            prev = v
    observed, failing, passed = [], [], 0
    for t, wp in edges:
        for tp in t_targets:
            vals = [v for ts, v in numeric(series.get(tp["id"], [])) if t <= ts <= t + within]
            moved = (max(vals) - min(vals)) if len(vals) >= 2 else None
            observed.append({"subject": tp["id"], "trigger": wp["id"], "at": _iso(t), "moved": moved, "samples": len(vals)})
            if moved is None or moved + 1e-9 < then["changes_by"]:
                failing.append({"subject": tp["id"], "status": "fail", "check_id": "response",
                                "symptom": f"at {t.strftime('%Y-%m-%d %H:%M')} {wp.get('dis') or wp['id']} became "
                                           f"{when['equals']}; {tp.get('dis') or tp['id']} moved "
                                           f"{'nothing measurable' if moved is None else f'{moved:g}'} within "
                                           f"{then['within_minutes']:g} min (needed {then['changes_by']:g})"})
            else:
                passed += 1
    if not edges:
        return {"criterion": c, "status": "inconclusive", "counts": {"inconclusive": 1}, "observed": [],
                "failing": [{"subject": w_targets[0]["id"], "status": "inconclusive", "check_id": "response",
                             "symptom": f"the condition ({when.get('point') or when.get('point_class')} = {when['equals']}) "
                                        "never started in the window, so the response wasn't exercised"}]}
    return {"criterion": c, "status": "fail" if failing else "pass", "counts": {"pass": passed, "fail": len(failing)},
            "failing": failing[:10], "observed": observed[:50]}


def worst(statuses) -> str:
    st = [s for s in statuses if s != "not_applicable"]
    if not st:
        return "not_applicable"
    return min(st, key=lambda s: ORDER[s])


def evaluate(proc: dict, equips: list[dict], results: list[dict], series: dict[str, list],
             truncated: set[str] | frozenset = frozenset(), window: tuple[datetime, datetime] | None = None) -> list[dict]:
    """[{equip_uri, dis, status, steps: [{id, title, status, criteria: [...]}]}]
    for every equipment the procedure applies to. `truncated`: points whose
    window read stopped at the per-point limit (History.window)."""
    classes = {p["id"]: p.get("brickClass") for e in equips for p in e["points"]}
    points = {p["id"]: p for e in equips for p in e["points"]}
    out = []
    for e in equips:
        if not applies(proc, e):
            continue
        steps = []
        for st in proc["steps"]:
            crit = [_eval(c, e, results, classes, series, points, truncated, window) for c in st["criteria"]]
            steps.append({"id": st["id"], "title": st["title"], "status": worst(c["status"] for c in crit), "criteria": crit})
        out.append({"equip_uri": e["id"], "dis": e.get("dis"), "status": worst(s["status"] for s in steps), "steps": steps})
    return out


def _eval(c, e, results, classes, series, points, truncated, window) -> dict:
    kind = criterion_kind(c)
    if kind == "range":
        return eval_range(c, e, series, points, truncated)
    if kind == "availability":
        return eval_availability(c, e, series, points, window, truncated)
    if kind == "response":
        return eval_response(c, e, series, points)
    return eval_check(c, e, results, classes)


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
    """History as of `end`: `latest` is the last sample at or before end,
    so a past test window isn't judged on data that arrived later. It is
    one backward probe per point - reading a 31-day window and taking the
    last row judged "latest" on the 5000th-oldest sample once a point had
    more than the window's per-point limit (Baler#88)."""

    def __init__(self, history: History, end: datetime):
        self.history = history
        self.end = end

    def latest(self, point_uris: list[str], before: datetime | None = None):
        return self.history.latest(point_uris, before=min(before or self.end, self.end))

    def latest_raw(self, point_uris: list[str], before: datetime | None = None):
        return self.history.latest_raw(point_uris, before=min(before or self.end, self.end))

    def window(self, point_uris: list[str], start: datetime, end: datetime, truncated: set[str] | None = None):
        return self.history.window(point_uris, start, min(end, self.end + timedelta(seconds=1)), truncated=truncated)

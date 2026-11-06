"""
The verification ladder, rungs 2-3 and the per-equipment climb (Cx 2,
Baler#38). Rung 1 (existence) is checks.existence.

  1. existence      - has each point reported in the window
  2. liveness       - do sensors move at all (a sensor frozen at 72 forever
                      is present, responding, and useless)
  3. responsiveness - do related points agree: status follows command,
                      a sensor sits near its setpoint
  (4. command response - Cx 6 passive / Cx 9 active)

Ported from PR #5's ladder, re-keyed on Brick classes instead of #5's own
function/role vocabulary: a point's role is its Brick class family
(Sensor / Setpoint / Command / Status), with Haystack markers as the
fallback for a point that has no Brick class. Pairs are found on the
same equipment by class.

A failure goes *down*, never sideways: a rung-3 failure whose sensor has
gone flat at the end of the window is reported as the rung-2 fault it
really is (`fell_to_rung: 2`). Symptoms are certain, causes candidates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean

from baler import brick_vocab, his_checks
from baler.commissioning.history import Sample

DEFAULTS = {
    "min_samples": 3,
    "flat_epsilon": 0.01,
    "pair_tolerance_minutes": 5.0,
    "status_agreement_min": 0.9,
    "tail_fraction": 0.25,  # "recently froze" = flat for the trailing quarter of the window
}

# Per-project overrides (settings.ladder): any DEFAULTS key, plus
# `tolerances` {pair key: number} for a pair's own tolerance.
_BOUNDS = {"min_samples": (1, 10000), "flat_epsilon": (0, 1e6), "pair_tolerance_minutes": (0.1, 1440),
           "status_agreement_min": (0, 1), "tail_fraction": (0.01, 1)}


def validate_settings(cfg) -> dict:
    """settings.ladder: raises ValueError/TypeError on anything unknown or
    out of range, so a typo can't silently fall back to the default."""
    if not isinstance(cfg, dict):
        raise TypeError("ladder must be an object")
    pairs = {p.key: p for p in PAIRS}
    for k, v in cfg.items():
        if k == "tolerances":
            if not isinstance(v, dict):
                raise TypeError("ladder.tolerances must be an object of {pair: number}")
            for pk, tol in v.items():
                if pk not in pairs:
                    raise ValueError(f"unknown pair {pk!r} (known: {', '.join(sorted(pairs))})")
                if isinstance(tol, bool) or not isinstance(tol, (int, float)) or tol < 0:
                    raise ValueError(f"ladder.tolerances.{pk} must be a number >= 0")
            continue
        if k not in _BOUNDS:
            raise ValueError(f"unknown ladder setting {k!r} (known: {', '.join(sorted([*_BOUNDS, 'tolerances']))})")
        lo, hi = _BOUNDS[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(f"ladder.{k} must be a number in [{lo:g}, {hi:g}]")
    return cfg


def effective(cfg: dict | None = None) -> dict:
    """DEFAULTS with a project's overrides, and every pair's tolerance."""
    out = {**DEFAULTS, **{k: v for k, v in (cfg or {}).items() if k != "tolerances"}}
    out["tolerances"] = {p.key: (cfg or {}).get("tolerances", {}).get(p.key, p.tolerance) for p in PAIRS if p.kind != "cmd_status"}
    return out


ROLES = ("sensor", "setpoint", "command", "status")
_ROLE_MARKERS = {"sensor": "sensor", "setpoint": "sp", "command": "cmd"}


@dataclass(frozen=True)
class Pair:
    key: str
    label: str
    kind: str  # cmd_status | track_abs | track_rel
    a: tuple[str, ...]  # command or sensor classes
    b: tuple[str, ...]  # status or setpoint classes
    tolerance: float = 0.0
    temperature: bool = False


PAIRS: tuple[Pair, ...] = (
    Pair("fan_cmd_status", "fan status follows command", "cmd_status", ("Fan_Command",), ("Fan_Status",)),
    Pair("pump_cmd_status", "pump status follows command", "cmd_status", ("Pump_Command",), ("Pump_Status",)),
    Pair("run_cmd_status", "run status follows enable/start command", "cmd_status",
         ("Start_Stop_Command", "On_Off_Command", "Enable_Command"), ("Run_Status",)),
    Pair("zone_air_temperature", "zone air temperature tracks setpoint", "track_abs",
         ("Zone_Air_Temperature_Sensor",), ("Zone_Air_Temperature_Setpoint",), 3.0, True),
    Pair("supply_air_temperature", "supply air temperature tracks setpoint", "track_abs",
         ("Supply_Air_Temperature_Sensor",), ("Supply_Air_Temperature_Setpoint",), 3.0, True),
    Pair("supply_air_static_pressure", "duct static pressure tracks setpoint", "track_abs",
         ("Supply_Air_Static_Pressure_Sensor",), ("Supply_Air_Static_Pressure_Setpoint",), 0.3),
    Pair("air_flow", "airflow tracks setpoint", "track_rel", ("Air_Flow_Sensor",), ("Air_Flow_Setpoint",), 0.2),
)


def _family(classes) -> frozenset[str]:
    vocab = brick_vocab.load()
    out: set[str] = set()
    for c in classes:
        out |= vocab.descendants(c) or {c}
    return frozenset(out)


def role_of(point: dict) -> str | None:
    """sensor | setpoint | command | status | None - Brick class first,
    Haystack markers when there's no class."""
    cls = point.get("brickClass")
    if cls:
        for role, root in (("setpoint", "Setpoint"), ("command", "Command"), ("status", "Status"), ("sensor", "Sensor")):
            if cls in _family([root]):
                return role
        return None
    tags = set(point.get("tags") or ())
    for role in ("setpoint", "command", "sensor"):
        if _ROLE_MARKERS[role] in tags:
            return role
    return None


# ---------------------------------------------------------------- series


def numeric(samples: list[Sample]) -> list[tuple[datetime, float]]:
    out = []
    for ts, v in samples:
        if isinstance(v, bool):
            out.append((ts, 1.0 if v else 0.0))
        elif isinstance(v, (int, float)):
            out.append((ts, float(v)))
        elif isinstance(v, str) and v.lower() in ("active", "on", "true", "running", "1"):
            out.append((ts, 1.0))
        elif isinstance(v, str) and v.lower() in ("inactive", "off", "false", "stopped", "0"):
            out.append((ts, 0.0))
    return out


def is_flat(series: list[tuple[datetime, float]], eps: float) -> bool:
    """Exactly stuck (his_checks, fault_detector's definition) or moving
    by no more than eps - an analog sensor dithering in its last digit
    is still frozen."""
    values = [v for _, v in series]
    return bool(values) and (his_checks.is_stuck(values) or max(values) - min(values) <= eps)


def _to_f(value: float, unit) -> float:
    u = str(unit or "").lower().replace("°", "deg")
    return value * 9.0 / 5.0 + 32.0 if u in ("degc", "celsius", "c") else value


def pair_samples(a, b, tolerance: timedelta) -> list[tuple[datetime, float, float]]:
    """(ts, a, b) for every a-sample with a b-sample within tolerance:
    nearest in time, no interpolation, so a gap stays a gap."""
    out = []
    j = 0
    for ts, va in a:
        while j + 1 < len(b) and abs(b[j + 1][0] - ts) <= abs(b[j][0] - ts):
            j += 1
        if b and abs(b[j][0] - ts) <= tolerance:
            out.append((ts, va, b[j][1]))
    return out


def _res(equip, check_id, subject, status, evidence, symptom=None, candidates=None, rung=None, fell=None) -> dict:
    return {"equip_uri": equip, "check_id": check_id, "subject": subject, "status": status, "severity": None,
            "symptom": symptom, "candidates": candidates or [], "evidence": {**evidence, "rung": rung, "fell_to_rung": fell}}


FLAT_CANDIDATES = [
    {"cause": "failed or disconnected sensor (the controller reports a default or last value)",
     "distinguish": "reliability / out-of-service flag on the object; a handheld reading at the sensor"},
    {"cause": "point overridden or out of service in the controller",
     "distinguish": "priority array / override indicator in the vendor tool"},
    {"cause": "the connector is polling a stale cache",
     "distinguish": "do other points on the same device change?"},
    {"cause": "the process really was steady (an unoccupied zone, equipment off)",
     "distinguish": "observe across an occupancy or schedule change"},
]

STATUS_CANDIDATES = [
    {"cause": "motor/starter or VFD not responding to the controller output",
     "distinguish": "starter/VFD run indication against the controller's output terminal"},
    {"cause": "status input wired to the wrong contact, or a failed current switch",
     "distinguish": "meter the status input while the motor is known to be running"},
    {"cause": "hand/off/auto switch not in auto, or a safety (freezestat, smoke) holding it off",
     "distinguish": "look at the switch and the safety circuit"},
    {"cause": "broken belt or coupling (motor runs, status from a current switch set too low)",
     "distinguish": "look at the drive; check the current switch setpoint"},
]

TRACK_CANDIDATES = [
    {"cause": "final control element not moving (stuck damper/valve, failed actuator)",
     "distinguish": "command it open/closed and watch feedback or the process"},
    {"cause": "capacity: plant or coil can't meet the load",
     "distinguish": "is the output at 100% while the error persists? check upstream supply conditions"},
    {"cause": "loop tuning (hunting or very slow)",
     "distinguish": "plot sensor, setpoint and output together - oscillation vs a steady offset"},
    {"cause": "sensor measuring the wrong place, or the wrong setpoint mapped",
     "distinguish": "handheld reading at the sensor; check which setpoint the loop actually uses"},
]


# ----------------------------------------------------------------- rungs


def liveness(equip: dict, series: dict, cfg: dict, failed_existence: set[str]) -> list[dict]:
    """Rung 2, per sensor point with data in the window."""
    out = []
    for p in equip["points"]:
        if role_of(p) != "sensor" or p["id"] in failed_existence:
            continue
        s = numeric(series.get(p["id"], []))
        ev = {"samples": len(s)}
        if len(s) < cfg["min_samples"]:
            out.append(_res(equip["id"], "live", p["id"], "inconclusive", ev | {"reason": f"fewer than {cfg['min_samples']} numeric samples in the window"}, rung=2))
        elif is_flat(s, cfg["flat_epsilon"]):
            label = p.get("brickClass") or p.get("dis")
            unit = f" {p['unit']}" if p.get("unit") else ""
            out.append(_res(equip["id"], "live", p["id"], "fail", ev | {"value": s[0][1]},
                            symptom=f"{label} read a constant {s[0][1]:g}{unit} for the whole window ({len(s)} samples)",
                            candidates=FLAT_CANDIDATES, rung=2))
        else:
            out.append(_res(equip["id"], "live", p["id"], "pass", ev | {"min": min(v for _, v in s), "max": max(v for _, v in s)}, rung=2))
    return out


def _pick(points: list[dict], classes, prefer_exact: bool) -> tuple[dict | None, str | None]:
    fam = _family(classes)
    hits = [p for p in points if p.get("brickClass") in fam]
    if len(hits) == 1:
        return hits[0], None
    exact = [p for p in hits if p.get("brickClass") in classes]
    if prefer_exact and len(exact) == 1:
        return exact[0], None
    return None, (f"{len(hits)} candidate points ({', '.join(sorted(str(p.get('brickClass')) for p in hits))}) - ambiguous" if hits else None)


def responsiveness(equip: dict, series: dict, cfg: dict, now: datetime, window: timedelta,
                   not_live: set[str]) -> list[dict]:
    """Rung 3, per related pair found on the equipment."""
    out = []
    tol = timedelta(minutes=cfg["pair_tolerance_minutes"])
    for pair in PAIRS:
        a, why_a = _pick(equip["points"], pair.a, True)
        b, why_b = _pick(equip["points"], pair.b, True)
        if a is None or b is None:
            if why_a or why_b:
                out.append(_res(equip["id"], f"pair:{pair.key}", equip["id"], "inconclusive",
                                {"pair": pair.label, "reason": why_a or why_b}, rung=3))
            continue
        subject = f"{a['id']}|{b['id']}"
        check = f"pair:{pair.key}"
        blocked = [x["id"] for x in (a, b) if x["id"] in not_live]
        if blocked:
            out.append(_res(equip["id"], check, subject, "inconclusive",
                            {"pair": pair.label, "reason": "not attempted: a lower rung failed for " + ", ".join(blocked)}, rung=3))
            continue
        sa, sb = numeric(series.get(a["id"], [])), numeric(series.get(b["id"], []))
        if pair.temperature:
            sa = [(t, _to_f(v, a.get("unit"))) for t, v in sa]
            sb = [(t, _to_f(v, b.get("unit"))) for t, v in sb]
        pairs = pair_samples(sa, sb, tol)
        if pair.kind == "track_rel":
            pairs = [x for x in pairs if x[2] > 0]
        if len(pairs) < cfg["min_samples"]:
            out.append(_res(equip["id"], check, subject, "inconclusive",
                            {"pair": pair.label, "paired_samples": len(pairs), "reason": f"fewer than {cfg['min_samples']} paired samples"}, rung=3))
            continue
        if pair.kind == "cmd_status":
            agree = sum(1 for _, x, y in pairs if (x > 0.5) == (y > 0.5)) / len(pairs)
            ok = agree >= cfg["status_agreement_min"]
            ev = {"pair": pair.label, "paired_samples": len(pairs), "agreement": round(agree, 3)}
            symptom = None if ok else f"{pair.label.split(' follows')[0]} disagreed with its command in {1 - agree:.0%} of {len(pairs)} paired samples"
            cands, sensor = STATUS_CANDIDATES, b
        else:
            tolerance = (cfg.get("tolerances") or {}).get(pair.key, pair.tolerance)
            err = (mean(abs(x - y) / y for _, x, y in pairs) if pair.kind == "track_rel"
                   else mean(abs(x - y) for _, x, y in pairs))
            ok = err <= tolerance
            unit = "of setpoint" if pair.kind == "track_rel" else ("degF" if pair.temperature else (a.get("unit") or ""))
            shown = f"{err:.0%}" if pair.kind == "track_rel" else f"{err:.2f} {unit}".strip()
            ev = {"pair": pair.label, "paired_samples": len(pairs), "mean_error": round(err, 4), "tolerance": tolerance}
            symptom = None if ok else f"{pair.label.split(' tracks')[0]} averaged {shown} from its setpoint over {len(pairs)} paired samples (tolerance {tolerance:g})"
            cands, sensor = TRACK_CANDIDATES, a
        if ok:
            out.append(_res(equip["id"], check, subject, "pass", ev, rung=3))
            continue
        fell = None
        # Only an analog sensor can be "frozen": a binary status that sits
        # at off while commanded on *is* the disagreement, not a rung-2 fault.
        s = numeric(series.get(sensor["id"], [])) if pair.kind != "cmd_status" else []
        tail = [(t, v) for t, v in s if t >= now - window * cfg["tail_fraction"]]
        if len(tail) >= cfg["min_samples"] and is_flat(tail, cfg["flat_epsilon"]):
            fell = 2
            ev["recheck"] = (f"{sensor.get('brickClass') or sensor.get('dis')} has been flat at {tail[0][1]:g} since "
                             f"{tail[0][0].isoformat()} after moving earlier - a sensor that froze, not a {pair.kind.replace('_', ' ')} problem")
            cands = FLAT_CANDIDATES
        out.append(_res(equip["id"], check, subject, "fail", ev, symptom=symptom, candidates=cands, rung=3, fell=fell))
    return out


def _rung_status(results: list[dict]) -> str:
    st = {r["status"] for r in results}
    if "fail" in st:
        return "fail"
    if "pass" in st:
        return "pass"
    return "inconclusive" if results else "not_attempted"


def climb(equip: dict, existence: list[dict], series: dict, now: datetime, window: timedelta, config: dict | None = None) -> list[dict]:
    """Rungs 2-3 for one equipment plus its `ladder` summary row. Climbs
    in order: nothing above a failed rung is judged."""
    cfg = {**DEFAULTS, **(config or {})}
    failed_existence = {r["subject"] for r in existence if r["status"] != "pass"}
    r1 = "pass" if any(r["status"] == "pass" for r in existence) else _rung_status(existence)
    rungs = {1: r1, 2: "not_attempted", 3: "not_attempted"}
    out: list[dict] = []
    if r1 == "pass":
        live = liveness(equip, series, cfg, failed_existence)
        out.extend(live)
        rungs[2] = _rung_status(live)
        if rungs[2] != "fail":
            not_live = failed_existence | {r["subject"] for r in live if r["status"] == "fail"}
            resp = responsiveness(equip, series, cfg, now, window, not_live)
            out.extend(resp)
            rungs[3] = _rung_status(resp)
    fell = next((r["evidence"]["fell_to_rung"] for r in out if r["evidence"].get("fell_to_rung")), None)
    highest = 0
    for n in (1, 2, 3):
        if rungs[n] != "pass":
            break
        highest = n
    if fell == 2:
        highest = min(highest, 1)
    # rung 2 with no sensors to judge is not a blocker for rung 3's verdict
    if rungs[2] == "not_attempted" and rungs[1] == "pass" and rungs[3] == "pass":
        highest = 3
    status = "fail" if "fail" in rungs.values() else ("pass" if highest == 3 else "inconclusive")
    out.append({"equip_uri": equip["id"], "check_id": "ladder", "subject": equip["id"], "status": status, "severity": None,
                "symptom": None, "candidates": [],
                "evidence": {"highest_rung_passed": highest, "rungs": {str(k): v for k, v in rungs.items()}, "fell_to_rung": fell}})
    return out

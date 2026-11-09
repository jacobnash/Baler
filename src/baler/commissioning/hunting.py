"""
Per-loop hunting report against the start-up baseline (Baler#214, epic
#221 §8.1): "AHU-1 CHW valve: 38 reversals/h, 4-minute period, ±22%,
since 2036-02-11. At start-up: 2/h."

A loop is a controlled variable and its setpoint, the pairs the Cx ladder
already finds (ladder.PAIRS, track kinds), with the analog commands and
position feedbacks on the same equipment (valve, damper, speed). For each
loop this measures (baler.oscillation, the same metrics fault_detector's
`hunting` rule uses):

- now: the error's zero crossings and each command's reversals per hour,
  period, amplitude and travel per hour, over the last `hours`;
- baseline: the same over the equipment's first completed start-up (or
  functional) test window, the Cx 6 test the equipment passed through.
  Until assets land (#197) this is the position's first start-up test;
  once they do, the baseline belongs to the asset installed then, so a
  replaced motor gets its own (ASSET_EVENTS);
- onset: the first day of the current run of days above `ratio` times the
  baseline (or above `min_reversals_per_hour` with no baseline);
- what changed around the onset: setpoint edits found in the setpoint's
  own history, and install/remove events from ASSET_EVENTS.

Nothing is concluded from sparse samples: a series sampled slower than
once a minute is reported `judgeable: false` with the reason.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from itertools import pairwise

from baler import oscillation
from baler.commissioning import ladder

COMMAND_CLASSES = ("Valve_Command", "Damper_Command", "Speed_Command", "Frequency_Command", "Position_Sensor")
# command_deadband: % (commands and positions are 0-100); error_deadband:
# the controlled variable's own units. Moves within them are noise.
DEFAULTS = {"ratio": 3.0, "min_reversals_per_hour": 12.0, "command_deadband": 2.0, "error_deadband": 0.5}

# fn(equip_uri) -> [{at: datetime, kind: "install" | "remove", detail}]; the
# asset registry (#197) answers here. Empty until it lands.
ASSET_EVENTS: list[Callable[[str], list[dict]]] = []


def loops(equip: dict) -> list[dict]:
    """The loops on one equipment: each track pair found, with the
    equipment's analog commands and position feedbacks."""
    cmd_fam = ladder._family(COMMAND_CLASSES)
    commands = [p for p in equip["points"] if p.get("brickClass") in cmd_fam]
    out = []
    for pair in ladder.PAIRS:
        if pair.kind == "cmd_status":
            continue
        sensor, _ = ladder._pick(equip["points"], pair.a, True)
        setpoint, _ = ladder._pick(equip["points"], pair.b, True)
        if sensor and setpoint:
            out.append({"equip_uri": equip["id"], "dis": equip.get("dis"), "loop": pair.key, "label": pair.label,
                        "sensor": sensor["id"], "setpoint": setpoint["id"], "commands": [c["id"] for c in commands]})
    if not out and commands:  # no setpoint pair, but commands can still hunt
        out.append({"equip_uri": equip["id"], "dis": equip.get("dis"), "loop": "commands", "label": "analog commands",
                    "sensor": None, "setpoint": None, "commands": [c["id"] for c in commands]})
    return out


def measure(loop: dict, series: dict[str, list], cfg: dict) -> dict:
    out: dict = {}
    if loop["sensor"]:
        s, sp = series.get(loop["sensor"], []), series.get(loop["setpoint"], [])
        out = oscillation.loop_metrics(s, sp, deadband=cfg["error_deadband"])
    out["commands"] = {c: oscillation.metrics(series.get(c, []), cfg["command_deadband"]) for c in loop["commands"]}
    return out


def headline(m: dict) -> float | None:
    """The loop's worst rate: command reversals/h, else error crossings/h."""
    rates = [x["reversals_per_hour"] for x in (m.get("commands") or {}).values() if x.get("judgeable")]
    if rates:
        return max(rates)
    err = m.get("error") or {}
    return err.get("crossings_per_hour") if err.get("judgeable") else None


def setpoint_edits(samples, start: datetime, end: datetime) -> list[dict]:
    """Where the setpoint's value changed, in [start, end)."""
    s = oscillation._clean(samples)
    return [{"at": b[0].isoformat(), "from": a[1], "to": b[1]}
            for a, b in pairwise(s) if a[1] != b[1] and start <= b[0] < end]


def analyse(equips: list[dict], history, now: datetime, hours: float = 24.0, days: int = 7,
            baselines: dict[str, tuple[datetime, datetime]] | None = None, cfg: dict | None = None) -> list[dict]:
    """One row per loop: now, baseline, ratio, verdict, onset, changes."""
    cfg = {**DEFAULTS, **(cfg or {})}
    baselines = baselines or {}
    rows = []
    for e in equips:
        for lp in loops(e):
            ids = [x for x in (lp["sensor"], lp["setpoint"], *lp["commands"]) if x]
            truncated: set[str] = set()
            series = history.window(ids, now - timedelta(hours=hours), now + timedelta(seconds=1), truncated=truncated)
            cur = measure(lp, series, cfg)
            rate = headline(cur)
            base, base_rate, base_win = None, None, baselines.get(lp["equip_uri"])
            if base_win:
                bseries = history.window(ids, base_win[0], base_win[1], truncated=truncated)
                base = measure(lp, bseries, cfg)
                base_rate = headline(base)
            threshold = base_rate * cfg["ratio"] if base_rate is not None else cfg["min_reversals_per_hour"]
            threshold = max(threshold, 1.0)
            if rate is None:
                verdict = "inconclusive"
            else:
                verdict = "hunting" if rate > threshold else "steady"
            row = {**lp, "now": cur, "rate_per_hour": rate, "baseline": base, "baseline_rate_per_hour": base_rate,
                   "baseline_window": [base_win[0].isoformat(), base_win[1].isoformat()] if base_win else None,
                   "ratio": oscillation.ratio(rate, base_rate), "threshold_per_hour": round(threshold, 2),
                   "verdict": verdict, "onset": None, "changes": [], "truncated": sorted(truncated)}
            if verdict == "hunting":
                row["onset"] = _onset(lp, history, now, days, threshold, cfg)
                row["changes"] = _changes(lp, history, now, row["onset"], days)
            rows.append(row)
    return rows


def _onset(lp: dict, history, now: datetime, days: int, threshold: float, cfg: dict) -> str | None:
    """The first day of the current run of days above threshold."""
    ids = [x for x in (lp["sensor"], lp["setpoint"], *lp["commands"]) if x]
    onset = None
    for d in range(days):
        end = now - timedelta(days=d)
        series = history.window(ids, end - timedelta(days=1), end)
        rate = headline(measure(lp, series, cfg))
        if rate is None or rate <= threshold:
            break
        onset = (end - timedelta(days=1)).isoformat()
    return onset


def _changes(lp: dict, history, now: datetime, onset: str | None, days: int) -> list[dict]:
    """Setpoint edits and asset events within a day either side of the onset."""
    if onset is None:
        return []
    at = datetime.fromisoformat(onset)
    start, end = at - timedelta(days=1), min(now, at + timedelta(days=1))
    out = []
    if lp["setpoint"]:
        sp = history.window([lp["setpoint"]], start - timedelta(days=1), end).get(lp["setpoint"], [])
        out += [{"kind": "setpoint_edit", "point": lp["setpoint"], **x} for x in setpoint_edits(sp, start, end)]
    for src in ASSET_EVENTS:
        out += [{"kind": f"asset_{ev['kind']}", "at": ev["at"].isoformat(), "detail": ev.get("detail")}
                for ev in src(lp["equip_uri"]) if start <= ev["at"] < end]
    return sorted(out, key=lambda x: x["at"])


def baseline_windows(repo, project_id: str) -> dict[str, tuple[datetime, datetime]]:
    """equip -> the window of its first completed start-up/functional test
    (Cx 6) in which it was judged."""
    out: dict[str, tuple[datetime, datetime]] = {}
    firsts: list[tuple[datetime, datetime, list]] = []
    for p in repo.list_procedures(project_id):
        if p.get("kind") not in ("startup", "functional"):
            continue
        for tr in repo.list_test_runs(p["id"], 200):
            if tr.get("status") == "completed" and tr.get("started_at") and tr.get("window_end"):
                firsts.append((_dt(tr["started_at"]), _dt(tr["window_end"]), tr.get("equipment") or []))
    for start, end, equipment in sorted(firsts, key=lambda x: x[0]):
        for e in equipment:
            out.setdefault(e["equip_uri"], (start, end))
    return out


def _dt(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(v)

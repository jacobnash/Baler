"""
Hunting / oscillation metrics over raw samples (Baler#214, epic #221 §8.1),
shared by fault_detector's His-mode `hunting` rule and the commissioning
per-loop report, so "hunting" means one thing in Baler.

Hunting is sustained or intermittent oscillation of a control loop. It
costs energy, actuator wear and setpoint tracking. NIST's BAS-data method
flags "unstable, excessively oscillatory regulation"; Chintala et al.
(ASHRAE 2015) found it common in AHU data. Per series this measures:

  reversals_per_hour   direction changes of the signal, counting only
                       moves larger than `deadband` (noise is not motion)
  crossings_per_hour   for an error series (sensor - setpoint): sign
                       changes, with hysteresis `deadband` around zero
  period_s             dominant period: twice the median time between
                       successive reversals
  amplitude            median half peak-to-peak between reversal extrema
  travel_per_hour      total absolute movement per hour (actuator wear)
  median_interval_s    the sampling it was judged on

A 4-minute oscillation is invisible in 15-minute averages, so this needs
raw samples: `judgeable` is false (and nothing is concluded) when the
median sample interval is longer than MAX_INTERVAL_S. That's why raw
history is kept compressed, never downsampled (history-compression.mdx).
"""

from __future__ import annotations

from datetime import datetime
from itertools import pairwise
from statistics import median

MAX_INTERVAL_S = 60.0
MIN_SAMPLES = 10


def _clean(samples) -> list[tuple[datetime, float]]:
    out = []
    for t, v in samples:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        out.append((t, float(v)))
    out.sort(key=lambda x: x[0])
    return out


def _turns(series: list[tuple[datetime, float]], deadband: float) -> list[tuple[datetime, float]]:
    """The extrema where the signal reverses, ignoring moves <= deadband."""
    if not series:
        return []
    turns: list[tuple[datetime, float]] = []
    direction = 0
    anchor = ext = series[0]
    for t, v in series[1:]:
        if direction == 0:
            if v - anchor[1] > deadband:
                direction, ext = 1, (t, v)
            elif anchor[1] - v > deadband:
                direction, ext = -1, (t, v)
        elif direction == 1:
            if v >= ext[1]:
                ext = (t, v)
            elif ext[1] - v > deadband:
                turns.append(ext)
                direction, ext = -1, (t, v)
        else:
            if v <= ext[1]:
                ext = (t, v)
            elif v - ext[1] > deadband:
                turns.append(ext)
                direction, ext = 1, (t, v)
    return turns


def metrics(samples, deadband: float = 0.0) -> dict:
    """The oscillation of one signal (a command, position or feedback)."""
    s = _clean(samples)
    if len(s) < 2:
        return {"samples": len(s), "judgeable": False, "reason": "fewer than 2 numeric samples"}
    hours = (s[-1][0] - s[0][0]).total_seconds() / 3600
    gaps = [(b[0] - a[0]).total_seconds() for a, b in pairwise(s)]
    med = median(gaps)
    out: dict = {"samples": len(s), "hours": round(hours, 3), "median_interval_s": round(med, 1)}
    if len(s) < MIN_SAMPLES or hours <= 0:
        return {**out, "judgeable": False, "reason": f"fewer than {MIN_SAMPLES} samples"}
    if med > MAX_INTERVAL_S:
        return {**out, "judgeable": False,
                "reason": f"samples every {med:.0f} s: hunting needs raw samples every {MAX_INTERVAL_S:.0f} s or faster (COV or a fast profile)"}
    turns = _turns(s, deadband)
    between = [(b[0] - a[0]).total_seconds() for a, b in pairwise(turns)]
    swings = [abs(b[1] - a[1]) / 2 for a, b in pairwise(turns)]
    travel = sum(abs(b[1] - a[1]) for a, b in pairwise(s))
    return {**out, "judgeable": True, "reversals": len(turns), "reversals_per_hour": round(len(turns) / hours, 2),
            "period_s": round(2 * median(between), 1) if between else None,
            "amplitude": round(median(swings), 4) if swings else None,
            "travel_per_hour": round(travel / hours, 4), "min": min(v for _, v in s), "max": max(v for _, v in s)}


def error_series(sensor, setpoint) -> list[tuple[datetime, float]]:
    """sensor - setpoint at each sensor sample, against the latest setpoint
    at or before it (setpoints change rarely and may be COV)."""
    sp = _clean(setpoint)
    out = []
    j = -1
    for t, v in _clean(sensor):
        while j + 1 < len(sp) and sp[j + 1][0] <= t:
            j += 1
        if j >= 0:
            out.append((t, v - sp[j][1]))
    return out


def crossings(err: list[tuple[datetime, float]], deadband: float = 0.0) -> int:
    """Sign changes of the error, with hysteresis: it must pass beyond
    +deadband / -deadband to count as being on a side."""
    side = 0
    n = 0
    for _, e in err:
        now = 1 if e > deadband else (-1 if e < -deadband else 0)
        if now and side and now != side:
            n += 1
        if now:
            side = now
    return n


def loop_metrics(sensor, setpoint, command=None, deadband: float = 0.0, command_deadband: float = 0.0) -> dict:
    """One loop: the error's zero crossings and the command's reversals."""
    err = error_series(sensor, setpoint)
    out: dict = {"error": metrics(err, deadband)}
    if out["error"].get("judgeable"):
        out["error"]["crossings_per_hour"] = round(crossings(err, deadband) / out["error"]["hours"], 2)
    if command is not None:
        out["command"] = metrics(command, command_deadband)
    return out


def ratio(now: float | None, baseline: float | None) -> float | None:
    """How many times the baseline's rate (None when either is missing;
    a zero baseline compares against 1/h so the ratio stays finite)."""
    if now is None or baseline is None:
        return None
    return round(now / max(baseline, 1.0), 2)

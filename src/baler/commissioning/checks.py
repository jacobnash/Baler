"""
Verification checks. Slice 1 (#37) has the bottom rung of the ladder and
the template check; rungs 2-3 (liveness, pairs) land in Cx 2 (#38).

House rules, from PR #5:
  - the symptom is certain, causes are candidates, each with what would
    tell it apart - never a diagnosis;
  - no data is `inconclusive`, never `pass` (and never silently `fail`:
    a point that never reported may simply not be trended yet).

A result is a plain dict: {equip_uri, check_id, subject, status,
severity, symptom, candidates, evidence}. status is one of pass | fail |
inconclusive | not_applicable.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from baler.commissioning import templates as tpl

STATUSES = ("pass", "fail", "inconclusive", "not_applicable")


def _result(equip, check_id, subject, status, evidence, severity=None, symptom=None, candidates=None) -> dict:
    return {"equip_uri": equip, "check_id": check_id, "subject": subject, "status": status, "severity": severity,
            "symptom": symptom, "candidates": candidates or [], "evidence": evidence}


def _plain(value):
    return value if isinstance(value, (int, float, str, bool)) or value is None else str(value)


def existence(equip: dict, latest: dict, now: datetime, window: timedelta, raw: dict | None = None) -> list[dict]:
    """Rung 1: has each point reported within the window?

    `latest` is the newest sample *with a value* (History.latest). `raw` is
    the newest row of any kind (History.latest_raw), for points `latest`
    found missing or stale: rows with no value inside the window mean the
    bridge is publishing empty readings - the device isn't answering - and
    that is a fail, never evidence of reporting (Baler#85)."""
    out = []
    since = now - window
    raw = raw or {}
    for p in equip["points"]:
        sample = latest.get(p["id"])
        last_raw = raw.get(p["id"])
        if sample is None and last_raw is not None and last_raw[1] is not None:
            sample = last_raw  # a real value older than History's value lookback
        if (sample is None or sample[0] < since) and last_raw is not None and last_raw[1] is None and last_raw[0] >= since:
            ev = {"last_ts": last_raw[0].isoformat(), "last_value": None,
                  "last_value_ts": sample[0].isoformat() if sample else None,
                  "last_real_value": _plain(sample[1]) if sample else None, "window_start": since.isoformat()}
            out.append(_result(
                equip["id"], "exists", p["id"], "fail", ev,
                symptom="receiving empty readings: the bridge is polling but gets no value back"
                        + (f" (last real value at {sample[0].isoformat()})" if sample else " (no real value ever)"),
                candidates=[
                    {"cause": "controller offline or unreachable from the bridge",
                     "distinguish": "do sibling points on the same device also read empty? can the edge node reach the device's address/port?"},
                    {"cause": "point or register no longer exists on the controller",
                     "distinguish": "only this point empty while siblings read fine: re-learn the device and compare"},
                    {"cause": "bridge lost its session to the device and isn't reconnecting",
                     "distinguish": "device answers a fresh read from another tool while the bridge still reads empty"},
                ],
            ))
            continue
        if sample is None:
            out.append(_result(
                equip["id"], "exists", p["id"], "inconclusive", {"last_ts": None, "window_start": since.isoformat()},
                symptom="no samples have ever been recorded for this point",
                candidates=[
                    {"cause": "point not trended / history collection not configured",
                     "distinguish": "check the Windrow connection's his_collect settings for this point"},
                    {"cause": "device offline since before it was first polled",
                     "distinguish": "Windrow device list: is the controller answering?"},
                    {"cause": "point exists in the model but not on the network",
                     "distinguish": "learn the device again and compare object lists"},
                ],
            ))
            continue
        ts, value = sample
        ev = {"last_ts": ts.isoformat(), "last_value": _plain(value), "window_start": since.isoformat()}
        if ts >= since:
            out.append(_result(equip["id"], "exists", p["id"], "pass", ev))
        else:
            out.append(_result(
                equip["id"], "exists", p["id"], "fail", ev,
                symptom=f"last sample {ts.isoformat()} is older than the {window.total_seconds() / 3600:g} h window",
                candidates=[
                    {"cause": "controller offline or unreachable", "distinguish": "do sibling points on the same device also stop then?"},
                    {"cause": "polling/COV subscription stopped", "distinguish": "other devices on the same Windrow connection still reporting?"},
                    {"cause": "point removed or renamed in the controller", "distinguish": "re-learn the device and compare object lists"},
                ],
            ))
    return out


def template_completeness(equip: dict, templates) -> list[dict]:
    """Expected points (ontology/cx-expected-points.ttl) for each template
    the equipment's Brick class falls under."""
    out = []
    for t in tpl.templates_for(equip["brickClasses"], templates):
        for outcome in tpl.evaluate(t, equip["points"]):
            req = outcome["requirement"]
            ev = {"template": t.id, "requirement": req.name, "classes": list(req.classes),
                  "matched": outcome["matched"], "matched_by": outcome["matched_by"]}
            if outcome["matched"]:
                out.append(_result(equip["id"], f"template:{req.id}", equip["id"], "pass", ev, severity=req.severity))
            else:
                out.append(_result(
                    equip["id"], f"template:{req.id}", equip["id"], "fail", ev, severity=req.severity,
                    symptom=req.message or f"no {req.name} point",
                    candidates=[
                        {"cause": "point exists on the controller but isn't classified",
                         "distinguish": "look for an unclassified point with this name on the equipment (record editor / autotag)"},
                        {"cause": "point isn't linked to this equipment",
                         "distinguish": "search the site for the class; check brick:isPointOf"},
                        {"cause": "point was never installed or programmed",
                         "distinguish": "field check against the points list"},
                    ],
                ))
    return out

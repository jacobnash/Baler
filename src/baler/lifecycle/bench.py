"""
Bench-test records on assets (#211, design §7): the first baseline.

Before a controller, actuator, sensor or drive is installed, someone tests
it on the bench. That result is recorded against the *asset* (not the
position), so years later its start-up and operating behaviour can be
compared with how the same piece of hardware behaved new.

Procedures are data, the same pattern as Cx 6 (commissioning.procedures):
a list of steps, each saying what to measure and its acceptance criterion,
in the same `range: {min, max}` shape Cx 6 uses. Kinds of step:

  - a measured value with a `range` (min and/or max) or an `equals` value;
  - a `manual` check the technician passes or fails ("actuator strokes
    fully open and closed").

A test stores the procedure *as it was run*, each step's measurement and
verdict, and an overall verdict (fail if any step fails, inconclusive if
any step has no measurement or no limits, else pass). Tests are
append-only. The asset's **baseline** is its earliest passing test before
its first install, or, if it was only bench-tested later (a spare re-tested
before going back in), its earliest passing test.

The seed templates (TEMPLATES) carry structure only: steps and units, no
tolerances. Tolerances come from the device's own submittal or the
project's specification, and a person enters them.

Schema: migration 0004.
"""

from __future__ import annotations

import re
from typing import Any

from baler.lifecycle import assets
from baler.lifecycle import rows as rows_mod
from baler.lifecycle.routes import HTTPError, Request, as_dt, need, route, text, when

PROC_COLS = ("id", "org_id", "name", "asset_type", "description", "steps", "template", "status", "created_by",
             "created_at", "updated_at")
TEST_COLS = ("id", "org_id", "asset_id", "procedure_id", "procedure", "results", "verdict", "tested_at", "technician",
             "instruments", "notes", "before_install", "document_id", "created_by", "created_at")
rows_mod.register("lc_bench_procedures", PROC_COLS, ("steps",))
rows_mod.register("lc_bench_tests", TEST_COLS, ("procedure", "results"), order="tested_at")

MAX_STEPS = 50
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,59}")


def _s(sid: str, title: str, unit: str | None = None, kind: str = "range", instruction: str = "") -> dict:
    step: dict[str, Any] = {"id": sid, "title": title, "instruction": instruction}
    if kind == "manual":
        step["criterion"] = {"manual": True}
    else:
        step["unit"] = unit
        step["criterion"] = {"range": {}}  # limits from the device's submittal or the spec
    return step


TEMPLATES: dict[str, dict] = {
    "actuator-stroke": {
        "name": "Actuator bench test", "asset_type": "actuator",
        "description": "Stroke and feedback check before install. Fill the limits from the actuator's submittal.",
        "steps": [
            _s("stroke-open", "Strokes fully open on command", kind="manual"),
            _s("stroke-closed", "Strokes fully closed on command", kind="manual"),
            _s("stroke-time", "Full-stroke time", "s", instruction="Time closed to open at the rated supply voltage."),
            _s("feedback-0", "Feedback at 0% command", "%"),
            _s("feedback-100", "Feedback at 100% command", "%"),
        ],
    },
    "temperature-sensor": {
        "name": "Temperature sensor bench check", "asset_type": "temperature_sensor",
        "description": "Compare against a reference instrument. Fill the limits from the sensor's accuracy class or the spec.",
        "steps": [
            _s("low-point", "Error at the low check point", "°F", instruction="Reading minus reference."),
            _s("high-point", "Error at the high check point", "°F", instruction="Reading minus reference."),
        ],
    },
    "controller-io": {
        "name": "Controller I/O bench check", "asset_type": "controller",
        "description": "Inputs and outputs before the controller is mounted.",
        "steps": [
            _s("powers-up", "Powers up and joins the bench network", kind="manual"),
            _s("ai-check", "Analog input reads the injected signal", "%", instruction="Error at mid-scale."),
            _s("ao-check", "Analog output drives the commanded signal", "%", instruction="Error at mid-scale."),
            _s("bo-check", "Binary outputs switch", kind="manual"),
        ],
    },
}


def _num(v: Any, where: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise TypeError(f"{where} must be a number")
    return float(v)


def normalize_steps(steps: Any) -> list[dict]:
    if not isinstance(steps, list) or not steps or len(steps) > MAX_STEPS:
        raise ValueError(f"steps is a list of 1-{MAX_STEPS} steps")
    out, seen = [], set()
    for i, s in enumerate(steps):
        where = f"step {i + 1}"
        if not isinstance(s, dict):
            raise TypeError(f"{where} is an object")
        sid = s.get("id")
        if not isinstance(sid, str) or not _ID.fullmatch(sid) or sid in seen:
            raise ValueError(f"{where}: id is a unique slug (a-z, 0-9, - or _)")
        seen.add(sid)
        title = s.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"{where}: title is required")
        c = s.get("criterion") or {}
        if not isinstance(c, dict) or sum(k in c for k in ("range", "equals", "manual")) != 1:
            raise ValueError(f"{where}: criterion is one of {{range: {{min, max}}}}, {{equals: v}} or {{manual: true}}")
        crit: dict[str, Any]
        if "range" in c:
            r = c["range"] or {}
            if not isinstance(r, dict):
                raise ValueError(f"{where}: range is {{min, max}}")
            lo = _num(r["min"], f"{where} range.min") if r.get("min") is not None else None
            hi = _num(r["max"], f"{where} range.max") if r.get("max") is not None else None
            if lo is not None and hi is not None and lo > hi:
                raise ValueError(f"{where}: range.min is above range.max")
            crit = {"range": {k: v for k, v in (("min", lo), ("max", hi)) if v is not None}}
        elif "equals" in c:
            crit = {"equals": c["equals"]}
        else:
            crit = {"manual": True}
        out.append({"id": sid, "title": title.strip()[:300], "instruction": str(s.get("instruction") or "")[:2000],
                    "unit": (str(s["unit"])[:20] if s.get("unit") else None), "criterion": crit})
    return out


def evaluate(steps: list[dict], measurements: dict) -> tuple[list[dict], str]:
    """Each step's verdict from the entered measurements ({step_id: value}
    or, for a manual step, {step_id: true/false}), and the overall verdict."""
    results = []
    for s in steps:
        v = measurements.get(s["id"])
        c = s["criterion"]
        why = ""
        if v is None:
            verdict, why = "inconclusive", "not measured"
        elif "manual" in c:
            if not isinstance(v, bool):
                raise ValueError(f"{s['id']}: a manual step is true (passed) or false (failed)")
            verdict = "pass" if v else "fail"
        elif "equals" in c:
            verdict = "pass" if v == c["equals"] else "fail"
        else:
            val = _num(v, s["id"])
            r = c["range"]
            if not r:
                verdict, why = "inconclusive", "no limits set for this step"
            elif ("min" in r and val < r["min"]) or ("max" in r and val > r["max"]):
                verdict, why = "fail", "outside " + "..".join(str(r.get(k, "")) for k in ("min", "max"))
            else:
                verdict = "pass"
        results.append({"step_id": s["id"], "title": s["title"], "measured": v, "unit": s.get("unit"),
                        "criterion": c, "verdict": verdict, "why": why})
    verdicts = {r["verdict"] for r in results}
    overall = "fail" if "fail" in verdicts else "inconclusive" if "inconclusive" in verdicts else "pass"
    return results, overall


def _get_proc(req: Request, pid: str) -> dict:
    p = req.rows.get("lc_bench_procedures", pid)
    if p is None or p["org_id"] != req.org_id:
        raise HTTPError(404, "no such procedure")
    return p


def baseline(tests: list[dict], first_install) -> dict | None:
    passing = sorted((t for t in tests if t["verdict"] == "pass"), key=lambda t: as_dt(t["tested_at"]))
    before = [t for t in passing if first_install is None or as_dt(t["tested_at"]) <= first_install]
    pick = before or passing
    return pick[0] if pick else None


def _first_install(req: Request, asset_id: str):
    ivs = assets.intervals(req.rows.find("lc_asset_events", org_id=req.org_id, asset_id=asset_id))
    return ivs[0]["installed_at"] if ivs else None


# --------------------------------------------------------------- routes


@route("GET", r"/bench/templates")
def _templates(req: Request):
    return [{"template": k, **v} for k, v in TEMPLATES.items()]


@route("GET", r"/bench/procedures")
def _procs(req: Request):
    return [rows_mod.public(p) for p in req.rows.find("lc_bench_procedures", org_id=req.org_id)]


@route("POST", r"/bench/procedures")
def _create_proc(req: Request):
    body = req.json()
    base: dict = {}
    if body.get("template"):
        if body["template"] not in TEMPLATES:
            raise ValueError(f"template is one of {', '.join(TEMPLATES)}")
        base = TEMPLATES[body["template"]]
    name = text(body, "name", 300) or base.get("name")
    if not name:
        raise ValueError("name is required")
    row = {"id": rows_mod.new_id(), "org_id": req.org_id, "name": name,
           "asset_type": text(body, "asset_type", 200) or base.get("asset_type"),
           "description": text(body, "description", 4000) or base.get("description"),
           "steps": normalize_steps(body.get("steps") or base.get("steps")), "template": body.get("template"),
           "status": "active", "created_by": req.actor(), "created_at": req.now, "updated_at": req.now}
    req.rows.insert("lc_bench_procedures", row)
    req.rows.audit(rows_mod.entity_uri("bench_procedure", row["id"]), "bench.procedure.create", req.who,
                   {"name": name, "steps": row["steps"]})
    return 201, rows_mod.public(row)


@route("PATCH", r"/bench/procedures/([^/]+)")
def _patch_proc(req: Request, pid: str):
    old = _get_proc(req, pid)
    body = req.json()
    unknown = set(body) - {"name", "asset_type", "description", "steps", "status"}
    if unknown:
        raise ValueError(f"not editable: {', '.join(sorted(unknown))}")
    ch: dict[str, Any] = {}
    for k, limit in (("name", 300), ("asset_type", 200), ("description", 4000)):
        if k in body:
            ch[k] = text(body, k, limit)
    if "steps" in body:
        ch["steps"] = normalize_steps(body["steps"])
    if "status" in body:
        if body["status"] not in ("active", "archived"):
            raise ValueError("status is active or archived")
        ch["status"] = body["status"]
    if not ch.get("name", old["name"]):
        raise ValueError("name can't be blank")
    diff = {k: {"from": old.get(k), "to": v} for k, v in ch.items() if old.get(k) != v}
    if diff:
        req.rows.update("lc_bench_procedures", pid, {**ch, "updated_at": req.now})
        req.rows.audit(rows_mod.entity_uri("bench_procedure", pid), "bench.procedure.edit", req.who, diff)
    return rows_mod.public(_get_proc(req, pid))


@route("GET", r"/assets/([^/]+)/bench-tests")
def _tests(req: Request, asset_id: str):
    assets.asset_detail(req, asset_id)
    tests = req.rows.find("lc_bench_tests", org_id=req.org_id, asset_id=asset_id)
    base = baseline(tests, _first_install(req, asset_id))
    return [{**(rows_mod.public(t) or {}), "baseline": base is not None and t["id"] == base["id"]} for t in tests]


@route("GET", r"/assets/([^/]+)/baseline")
def _baseline(req: Request, asset_id: str):
    assets.asset_detail(req, asset_id)
    tests = req.rows.find("lc_bench_tests", org_id=req.org_id, asset_id=asset_id)
    first = _first_install(req, asset_id)
    base = baseline(tests, first)
    return {"asset_id": asset_id, "first_install": first.isoformat() if first else None,
            "bench": rows_mod.public(base) if base else None}


@route("POST", r"/assets/([^/]+)/bench-tests")
def _record(req: Request, asset_id: str):
    assets.asset_detail(req, asset_id)
    body = req.json()
    if body.get("procedure_id"):
        proc = _get_proc(req, str(body["procedure_id"]))
        if proc["status"] != "active":
            raise HTTPError(409, "that procedure is archived")
        snapshot = {k: proc[k] for k in ("id", "name", "asset_type", "description", "steps")}
    elif isinstance(body.get("procedure"), dict):  # a one-off procedure, given inline
        p = body["procedure"]
        snapshot = {"id": None, "name": need(p, "name")[:300], "asset_type": p.get("asset_type"),
                    "description": p.get("description"), "steps": normalize_steps(p.get("steps"))}
    else:
        raise ValueError("give procedure_id, or procedure (name and steps) for a one-off")
    meas = body.get("measurements") or {}
    if not isinstance(meas, dict):
        raise TypeError("measurements is {step_id: value}")
    unknown = set(meas) - {s["id"] for s in snapshot["steps"]}
    if unknown:
        raise ValueError(f"no such step: {', '.join(sorted(unknown))}")
    results, verdict = evaluate(snapshot["steps"], meas)
    tested_at = when(body.get("tested_at"), "tested_at") or req.now
    if tested_at > req.now + assets.FUTURE_SLACK:
        raise ValueError("tested_at is in the future")
    first = _first_install(req, asset_id)
    row = {"id": rows_mod.new_id(), "org_id": req.org_id, "asset_id": asset_id, "procedure_id": snapshot["id"],
           "procedure": snapshot, "results": results, "verdict": verdict, "tested_at": tested_at,
           "technician": text(body, "technician", 200), "instruments": text(body, "instruments", 1000),
           "notes": text(body, "notes", 4000), "before_install": first is None or tested_at <= first,
           "document_id": text(body, "document_id", 100), "created_by": req.actor(), "created_at": req.now}
    req.rows.insert("lc_bench_tests", row)
    req.rows.audit(rows_mod.entity_uri("asset", asset_id), "asset.bench_test", req.who,
                   {"test_id": row["id"], "verdict": verdict, "tested_at": tested_at})
    return 201, rows_mod.public(row)

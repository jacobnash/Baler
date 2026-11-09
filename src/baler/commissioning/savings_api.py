"""
HTTP routes for replace-vs-keep savings cases (Baler#216), under /cx/.

A case starts from a performance baseline (#215): the existing chiller's
measured kW/ton (actual, at matched conditions, over the last year) and
the cooling it delivered (ton-hours over the last year) are filled in as
`measured` assumptions. Everything else is the user's. PATCH changes any
assumption and recomputes; the case keeps the history of its edits.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from baler import performance, savings
from baler.commissioning import baselines_api, db
from baler.commissioning.routes import Route, owned_row
from baler.http_handler_base import respond_json

CASES = "cx_savings_cases"
db.ROW_TABLES[CASES] = (("id", "project_id", "equip_uri", "asset_id", "baseline_id", "title", "assumptions", "result",
                         "history", "created_by", "created_at", "updated_at"), frozenset({"assumptions", "result", "history"}))


def measured(repo, baseline: dict, now: datetime) -> dict:
    """The assumptions the data can answer, with how they were measured."""
    start = now - timedelta(days=365)
    hours = baselines_api.hours_for(repo, baseline["points"], start, now)
    out: dict = {}
    if hours:
        span = (hours[-1]["ts"] - hours[0]["ts"]).total_seconds() / 86400 + 1 / 24
        ton_h = sum(h["tons"] for h in hours)
        per_year = ton_h * 365 / max(span, 1)
        out["annual_ton_hours"] = {"value": round(per_year), "note": f"{len(hours)} hours of load data over {span:.0f} days, "
                                                                    "scaled to a year (hours below 10 % of peak load left out)"}
    rep = performance.degradation(baseline["model"], hours)
    if rep["overall"]:
        out["current_kw_per_ton"] = {"value": rep["overall"]["actual_kw_per_ton"],
                                     "note": f"actual kW/ton over {rep['hours_matched']} hours inside the baseline's fitted "
                                             f"load/ECWT range; {rep['overall']['gap_pct']}% versus the baseline's prediction"}
    return out


def _create(h, span, claims, pid) -> None:
    """{baseline_id, title?, assumptions: {key: value | {value, note}}}."""
    if h._project(pid, claims) is None:
        return
    b = h.read_json_body()
    base = h.REPO.get_row(baselines_api.BASELINES, b.get("baseline_id") or "")
    if base is None or base["project_id"] != pid:
        raise ValueError("baseline_id is a performance baseline of this project")
    now = h.NOW()
    meas = measured(h.REPO, base, now)
    a = savings.normalize(b.get("assumptions") or {}, meas)
    row = {"id": db.new_id(), "project_id": pid, "equip_uri": base["equip_uri"], "asset_id": base.get("asset_id"),
           "baseline_id": base["id"], "title": str(b.get("title") or f"Replace vs keep: {base['equip_uri'].rsplit('/', 1)[-1]}")[:200],
           "assumptions": a, "result": savings.compute(a),
           "history": [{"at": now.isoformat(), "by": claims.get("sub"), "action": "create"}],
           "created_by": claims.get("sub"), "created_at": now, "updated_at": now}
    h.REPO.put_rows(CASES, [row])
    h._event(pid, "savings_case.create", claims, {"case_id": row["id"], "annual_savings": row["result"]["annual_savings"]})
    respond_json(h, 201, db.public_row(row))


def _list(h, span, claims, pid) -> None:
    if h._project(pid, claims) is not None:
        respond_json(h, 200, [db.public_row(r) for r in h.REPO.find_rows(CASES, project_id=pid)])


def _get(h, span, claims, cid) -> None:
    hit = owned_row(h, CASES, cid, claims, "savings case")
    if hit:
        respond_json(h, 200, db.public_row(hit[0]))


def _patch(h, span, claims, cid) -> None:
    """{assumptions: {...}}: change any assumption (user values win)."""
    hit = owned_row(h, CASES, cid, claims, "savings case")
    if not hit:
        return
    row, _ = hit
    changes = h.read_json_body().get("assumptions") or {}
    user = {k: {"value": v["value"], "note": v.get("note")} for k, v in row["assumptions"].items() if v["source"] == "user"}
    meas = {k: {"value": v["value"], "note": v.get("note")} for k, v in row["assumptions"].items() if v["source"] == "measured"}
    a = savings.normalize({**user, **changes}, meas)
    now = h.NOW()
    upd = {"assumptions": a, "result": savings.compute(a), "updated_at": now,
           "history": [*row["history"], {"at": now.isoformat(), "by": claims.get("sub"), "action": "edit", "changed": sorted(changes),
                                         "before": {k: row["assumptions"].get(k, {}).get("value") for k in changes}}]}
    h.REPO.update_row(CASES, cid, upd)
    h._event(row["project_id"], "savings_case.edit", claims, {"case_id": cid, "changed": sorted(changes)})
    respond_json(h, 200, db.public_row({**row, **upd}))


ROUTES: list[Route] = [
    ("POST", re.compile(r"/projects/([^/]+)/savings-cases"), "create_savings_case", _create),
    ("GET", re.compile(r"/projects/([^/]+)/savings-cases"), "list_savings_cases", _list),
    ("GET", re.compile(r"/savings-cases/([^/]+)"), "get_savings_case", _get),
    ("PATCH", re.compile(r"/savings-cases/([^/]+)"), "edit_savings_case", _patch),
]

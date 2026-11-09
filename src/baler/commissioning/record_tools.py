"""
Assistant tools across the building record (Baler#220, epic #221 §12):
documents, asset history, spaces and readiness, packs and compliance,
schedules, and analytics (history, hunting, baselines, savings).

Same rules as the Cx 10 tools (commissioning.agent):

- read-only: every tool here reads; anything that would change the record
  still goes through the agent's `propose` tool, for a person to accept;
- every call is audited (agent.call_tool writes a cx_events row per call);
- the same model choice (BALER_LLM_PROVIDER) and the same scope: each tool
  runs with the session starter's org and sites and the project's site;
- answers cite: document tools return the document, page and quote; data
  tools return the point, window and sample counts they read.

The building record grows in separate changes (spaces #209, readiness
#210, packs #205, compliance #207, hunting #214, baselines #215, savings
#216, schedules #217). A tool whose module isn't in this install is not
offered at all (GROUPS, checked with importlib), so the model is never
handed a tool that can only fail. Asset history (#197), warranties (#198)
and the document store (#201) answer through the hooks below once they
land; until then those tools say plainly that the record doesn't hold it.
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Callable
from datetime import timedelta
from typing import Any

# Hooks for record parts other changes build (no competing model here):
# ASSET_HISTORY[...](repo, project, equip_uri) -> install/remove events and warranties (#197/#198);
# DOCUMENT_SEARCH[...](repo, project, query) -> [{document, page, quote, sha256}] (#201).
ASSET_HISTORY: list[Callable[..., list[dict]]] = []
DOCUMENT_SEARCH: list[Callable[..., list[dict]]] = []

_S = {"type": "string"}
_N = {"type": "number"}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


def _mod(name: str) -> Any:
    return importlib.import_module(f"baler.commissioning.{name}")


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


# ------------------------------------------------------------ always here


def t_search_documents(ctx, a):
    """Spec documents and the items read from them (Cx 7), with page and
    verbatim quote, plus the document store's hits when it's present."""
    q = str(a.get("query") or "").strip().lower()
    if len(q) < 2:
        raise ValueError("query needs at least 2 characters")
    docs = {d["id"]: d for d in ctx.repo.find_rows("cx_spec_documents", project_id=ctx.project["id"])}
    hits = []
    for i in ctx.repo.find_rows("cx_spec_items", project_id=ctx.project["id"]):
        src = i.get("source") or {}
        text = " ".join(str(x) for x in (i.get("tag"), i.get("name"), i.get("equip_tag"), src.get("quote"),
                                         *(f"{k} {v}" for k, v in (i.get("properties") or {}).items())) if x)
        if q in text.lower():
            d = docs.get(i.get("document_id")) or {}
            hits.append({"document": d.get("filename") or src.get("document"), "sha256": d.get("sha256"),
                         "page": src.get("page"), "quote": src.get("quote"), "item": i.get("name"), "tag": i.get("tag"),
                         "status": i.get("status")})
    for fn in DOCUMENT_SEARCH:
        hits += fn(ctx.repo, ctx.project, q)
    note = None if DOCUMENT_SEARCH else ("only Cx 7 spec documents are searchable in this record; O&M manuals, "
                                         "submittals and warranties need the document store (#201)")
    return {"hits": hits[:50], "note": note, "cite_as": "document, page and quote"}


def t_asset_history(ctx, a):
    uri = a["equip_uri"]
    if not ctx.scope.allows(uri):
        raise ValueError("equip_uri isn't in your scope")
    if not ASSET_HISTORY:
        return {"equip_uri": uri, "events": [], "note": "this record doesn't hold asset install/remove history or "
                                                        "warranties yet (#197, #198): say so rather than guess"}
    return {"equip_uri": uri, "events": [e for fn in ASSET_HISTORY for e in fn(ctx.repo, ctx.project, uri)]}


def t_point_history(ctx, a):
    """Summary of one point's raw history over a window, with what was read."""
    uri, hours = a["point_uri"], float(a.get("hours") or 24)
    if not ctx.scope.allows(uri):
        raise ValueError("point_uri isn't in your scope")
    if not 0 < hours <= 24 * 31:
        raise ValueError("hours is in (0, 744]")
    end = ctx.now()
    with ctx.repo.history() as h:
        rows = h.window([uri], end - timedelta(hours=hours), end + timedelta(seconds=1)).get(uri, [])
    vals = [v for _, v in rows if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return {"point_uri": uri, "window": [_iso(end - timedelta(hours=hours)), _iso(end)], "samples": len(rows),
            "numeric": len(vals), "min": min(vals, default=None), "max": max(vals, default=None),
            "mean": round(sum(vals) / len(vals), 4) if vals else None,
            "first": [_iso(rows[0][0]), rows[0][1]] if rows else None, "last": [_iso(rows[-1][0]), rows[-1][1]] if rows else None}


# ------------------------------------------------------ when present


def t_list_spaces(ctx, a):
    rows = _mod("spaces_api").project_spaces(ctx.repo, ctx.project["id"])
    if a.get("space_type"):
        rows = [r for r in rows if r.get("space_type") == a["space_type"]]
    return [{k: r.get(k) for k in ("id", "name", "kind", "space_type", "parent_id")} for r in rows][:300]


def _space(ctx, sid: str) -> dict:
    s = ctx.repo.get_row("cx_spaces", sid)
    if s is None or s["project_id"] != ctx.project["id"]:
        raise ValueError("no such space in this project")
    return s


def t_space_readiness(ctx, a):
    sp = _mod("spaces_api")
    target = _space(ctx, a["space_id"])
    rep = _mod("readiness").build(ctx.repo, ctx.project, target, sp.project_spaces(ctx.repo, ctx.project["id"]), ctx.now(),
                                  ctx.scope.allows)
    return {k: rep[k] for k in ("space", "verdict", "counts", "limitations")} | {
        "rooms": [{"name": r["name"], "verdict": r["verdict"],
                   "criteria": {k: {"status": v["status"], "note": v.get("note"), "items": v["items"][:10]}
                                for k, v in r["criteria"].items()}} for r in rep["rooms"][:50]]}


def t_project_packs(ctx, _):
    pk, packs = _mod("packs_api"), _mod("packs")
    return [{"pack_id": p["pack_id"], "version": p["version"], "standard": p["source"]["standard"],
             "edition": p["source"]["edition"], "adopted_at": _iso(a["adopted_at"]), "summary": packs.summary(p),
             "active_rows": [{"id": r["id"], "title": r["title"], "citation": r["citation"], "criterion": r["criterion"]}
                             for r in packs.active_rows(p)][:100]} for a, p in pk.project_packs(ctx.repo, ctx.project["id"])]


def t_compliance_log(ctx, a):
    sid = a["space_id"]
    _space(ctx, sid)
    days = float(a.get("days") or 30)
    rows = _mod("compliance").space_history(ctx.repo, sid, ctx.now() - timedelta(days=days))
    return [{k: r.get(k) for k in ("window_start", "window_end", "title", "citation", "status", "note", "pack_id", "version")}
            for r in rows][-200:]


def t_schedule_activities(ctx, a):
    scheds = ctx.repo.find_rows("cx_schedules", project_id=ctx.project["id"])
    if not scheds:
        return {"note": "no schedule imported"}
    s = scheds[-1]
    rows = ctx.repo.find_rows("cx_schedule_activities", order="planned_start", schedule_id=s["id"])
    if a.get("status"):
        rows = [r for r in rows if r["status"] == a["status"]]
    if a.get("query"):
        q = str(a["query"]).lower()
        rows = [r for r in rows if q in r["name"].lower() or q in r["code"].lower()]
    return {"schedule": {"id": s["id"], "filename": s["filename"], "data_date": s.get("data_date")},
            "activities": [{k: r.get(k) for k in ("code", "name", "planned_start", "planned_finish", "actual_start",
                                                  "actual_finish", "status", "percent_complete")} for r in rows][:200],
            "see_also": "GET /cx/schedules/{id}/earned-evidence for what the data shows done"}


def t_hunting(ctx, a):
    hunting = _mod("hunting")
    equips = [e for e in ctx.equips() if not a.get("equip_uri") or e["id"] == a["equip_uri"]]
    with ctx.repo.history() as h:
        rows = hunting.analyse(equips, h, ctx.now(), float(a.get("hours") or 24), 7,
                               hunting.baseline_windows(ctx.repo, ctx.project["id"]))
    return [{k: r.get(k) for k in ("equip_uri", "loop", "verdict", "rate_per_hour", "baseline_rate_per_hour", "ratio",
                                   "onset", "changes")} for r in rows][:100]


def t_performance(ctx, a):
    bl = _mod("baselines_api")
    from baler import performance

    rows = [b for b in ctx.repo.find_rows("cx_performance_baselines", project_id=ctx.project["id"])
            if b["status"] == "active" and ctx.scope.allows(b["equip_uri"])
            and (not a.get("equip_uri") or b["equip_uri"] == a["equip_uri"])]
    out = []
    for b in rows[:10]:
        fit_end = b["fit_end"]
        rep = performance.degradation(b["model"], bl.hours_for(ctx.repo, b["points"], fit_end, ctx.now()))
        out.append({"baseline_id": b["id"], "equip_uri": b["equip_uri"], "kind": b["kind"],
                    "model": {k: b["model"].get(k) for k in ("cvrmse_pct", "nmbe_pct", "n_hours")},
                    "since_fit": rep["overall"], "hours_matched": rep["hours_matched"]})
    return out


def t_savings_cases(ctx, _):
    return [{"title": c["title"], "equip_uri": c["equip_uri"], "result": c["result"],
             "assumptions": {k: {"value": v["value"], "unit": v["unit"], "source": v["source"]} for k, v in c["assumptions"].items()}}
            for c in ctx.repo.find_rows("cx_savings_cases", project_id=ctx.project["id"]) if ctx.scope.allows(c["equip_uri"])][:20]


# (needs modules, tool definition, implementation). Offered only when every
# module is in this install.
GROUPS: list[tuple[tuple[str, ...], dict, Callable]] = [
    ((), {"name": "search_documents", "description": "Search the project's documents for text (equipment tags, a seal, "
          "a setpoint). Returns document, page and verbatim quote: cite those.",
          "input_schema": _obj({"query": _S}, ["query"])}, t_search_documents),
    ((), {"name": "asset_history", "description": "Install/remove history and warranties of the asset at a position "
          "(equipment). If the record doesn't hold it, say so; never guess dates or brands.",
          "input_schema": _obj({"equip_uri": _S}, ["equip_uri"])}, t_asset_history),
    ((), {"name": "point_history", "description": "One point's raw history over the last `hours` (default 24): samples, "
          "min, max, mean, first and last. Cite the point and window.",
          "input_schema": _obj({"point_uri": _S, "hours": _N}, ["point_uri"])}, t_point_history),
    (("spaces_api",), {"name": "list_spaces", "description": "The project's spaces (rooms, suites, floors), optionally by space_type.",
                       "input_schema": _obj({"space_type": _S})}, t_list_spaces),
    (("spaces_api", "readiness"), {"name": "space_readiness", "description": "Readiness of a room or every room under a "
                                   "suite: requirements, tests, TAB report, issues, each with evidence.",
                                   "input_schema": _obj({"space_id": _S}, ["space_id"])}, t_space_readiness),
    (("packs_api", "packs"), {"name": "project_packs", "description": "The requirement pack versions this project is judged "
                              "by, with their active (confirmed) rows and citations.", "input_schema": _obj({})}, t_project_packs),
    (("compliance", "spaces_api"), {"name": "compliance_log", "description": "A space's continuous-compliance log over the "
                                    "last `days` (default 30): which requirements passed or failed, cited by edition.",
                                    "input_schema": _obj({"space_id": _S, "days": _N}, ["space_id"])}, t_compliance_log),
    (("schedules_api",), {"name": "schedule_activities", "description": "Activities of the latest imported construction "
                          "schedule, by status or text.", "input_schema": _obj({"status": _S, "query": _S})}, t_schedule_activities),
    (("hunting",), {"name": "hunting", "description": "Hunting (oscillation) per control loop against the start-up "
                    "baseline: rate, ratio, onset, what changed.", "input_schema": _obj({"equip_uri": _S, "hours": _N})}, t_hunting),
    (("baselines_api",), {"name": "performance", "description": "Equipment performance baselines (chiller kW/ton) and the "
                          "gap since the fit, at matched conditions.", "input_schema": _obj({"equip_uri": _S})}, t_performance),
    (("savings_api",), {"name": "savings_cases", "description": "Replace-vs-keep cases with every assumption and its source "
                        "(measured, user, default).", "input_schema": _obj({})}, t_savings_cases),
]


def available() -> list[tuple[dict, Callable]]:
    """The tools whose modules this install has."""
    return [(tool, fn) for needs, tool, fn in GROUPS
            if all(importlib.util.find_spec(f"baler.commissioning.{m}") is not None for m in needs)]


PROMPT = """
Across the building record (beyond commissioning) you can also search documents, read asset history,
spaces and readiness, requirement packs and compliance logs, the construction schedule, raw point
history and analytics (hunting, performance baselines, replace-vs-keep cases), as far as this install
holds them. Cite every answer: a document by name, page and quote; data by point and window. When a
tool says the record doesn't hold something (an asset's warranty, an O&M manual), say that, never guess."""

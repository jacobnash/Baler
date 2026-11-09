"""
The building passport: the handover and sale package (Baler#219, epic #221
§11). ASHRAE Std 202 already names the handover deliverables (systems
manual, issues log, final and end-of-warranty Cx reports, training
records); the passport adds everything since, per position:

- install and remove history (brands, dates, reasons)        assets, #197
- warranties                                                  #198
- start-up baseline vs today                                  Cx 6 test runs
- efficiency trend and the replace-vs-keep case               #215, #216
- open deferred maintenance                                   #200
- compliance history per space                                #207
- documents                                                   Cx 7 / #201
- the issues log, accepted risks and open faults              Cx 4, faults

It reads whatever parts of the record this install has and says plainly
which parts it doesn't, and why: a section with no source is listed under
`not_in_this_record` with the issue that builds it, never left out
silently. Parts that live in tables this version may not have (baselines,
savings cases, compliance logs) are read when the table is registered
(db.ROW_TABLES); the asset registry, warranties and deferred maintenance
answer through SECTION_SOURCES once they land, so nothing here builds a
competing model of them.

Positions and assets stay separate: every row here is a position (the
Brick equipment record). Asset history hangs off it when #197 provides it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from html import escape

from baler.commissioning import db
from baler.commissioning.report import _CSS, _a11y_tables

# name -> fn(repo, project, equip_uri) -> list[dict]. #197 registers
# "assets" (install/remove events), #198 "warranties", #200 "deferred_maintenance".
SECTION_SOURCES: dict[str, Callable[..., list[dict]]] = {}
PENDING = {"assets": "#197 (positions vs installed assets)", "warranties": "#198 (warranties)",
           "deferred_maintenance": "#200 (deferred maintenance)"}
DELIVERABLES = (
    ("issues_log", "Issues log", "issues"),
    ("cx_report", "Final commissioning report", "GET /cx/projects/{id}/report?format=html"),
    ("systems_manual", "Systems manual", None),
    ("training_records", "Training records", None),
    ("end_of_warranty_report", "End-of-warranty Cx report", None),
)


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _rows(repo, table: str, **eq) -> list[dict] | None:
    """Rows of a table this install has (registered), else None."""
    if table not in db.ROW_TABLES:
        return None
    return repo.find_rows(table, **eq)


def startup_vs_today(repo, project_id: str) -> dict[str, dict]:
    """equip -> {first: the first completed start-up/power-on test it was
    judged in, latest: the newest of any kind}."""
    out: dict[str, dict] = {}
    runs = []
    for p in repo.list_procedures(project_id):
        for tr in repo.list_test_runs(p["id"], 200):
            if tr.get("status") == "completed":
                runs.append((p, tr))
    runs.sort(key=lambda x: str(x[1].get("finished_at") or x[1].get("requested_at")))
    for p, tr in runs:
        for e in tr.get("equipment") or []:
            entry = {"procedure": p["name"], "kind": p.get("kind"), "test_run_id": tr["id"],
                     "finished_at": _iso(tr.get("finished_at")), "status": e.get("status")}
            slot = out.setdefault(e["equip_uri"], {"first_startup": None, "latest": None})
            if slot["first_startup"] is None and p.get("kind") in ("startup", "power_on"):
                slot["first_startup"] = entry
            slot["latest"] = entry
    return out


def build(repo, project: dict, equips: list[dict], now: datetime, generated_by: str | None = None) -> dict:
    pid = project["id"]
    tests = startup_vs_today(repo, pid)
    issues = repo.list_issues(pid)
    risks = {r["issue_id"]: r for r in repo.list_risks(pid)}
    faults = repo.list_faults(open_only=True)
    baselines = _rows(repo, "cx_performance_baselines", project_id=pid)
    cases = _rows(repo, "cx_savings_cases", project_id=pid)
    not_in: dict[str, str] = {k: v for k, v in PENDING.items() if k not in SECTION_SOURCES}
    if baselines is None:
        not_in["efficiency"] = "#215 (performance baselines)"
    if cases is None:
        not_in["replace_vs_keep"] = "#216 (savings cases)"

    positions = []
    for e in sorted(equips, key=lambda x: x["id"]):
        uri = e["id"]
        pts = {p["id"] for p in e.get("points") or []}
        mine = [i for i in issues if i.get("equip_uri") == uri or i.get("subject") in pts]
        row = {"equip_uri": uri, "dis": e.get("dis"), "brick_class": e.get("brickClass"), "points": len(pts),
               "startup_vs_today": tests.get(uri),
               "issues": {"open": sum(1 for i in mine if i["status"] in ("open", "in_progress", "ready_for_retest")),
                          "risk_accepted": [{"issue_id": i["id"], "title": i.get("title"),
                                             "reason": (risks.get(i["id"]) or {}).get("reason")}
                                            for i in mine if i["status"] == "risk_accepted"],
                          "closed": sum(1 for i in mine if i["status"] == "closed")},
               "open_faults": [{"rule_id": f["rule_id"], "point_uri": f["point_uri"], "since": _iso(f.get("started_at"))}
                               for f in faults if f["point_uri"] in pts]}
        for name, src in SECTION_SOURCES.items():
            row[name] = src(repo, project, uri)
        if baselines is not None:
            row["efficiency"] = [{"baseline_id": b["id"], "kind": b["kind"], "fit": [_iso(b["fit_start"]), _iso(b["fit_end"])],
                                  "status": b["status"], "cvrmse_pct": b["model"].get("cvrmse_pct"),
                                  "nmbe_pct": b["model"].get("nmbe_pct"), "asset_id": b.get("asset_id")}
                                 for b in baselines if b["equip_uri"] == uri]
        if cases is not None:
            row["replace_vs_keep"] = [{"case_id": c["id"], "title": c["title"],
                                       "annual_savings": c["result"].get("annual_savings"),
                                       "simple_payback_years": c["result"].get("simple_payback_years"),
                                       "assumptions": {k: {"value": v["value"], "source": v["source"]}
                                                       for k, v in c["assumptions"].items()}}
                                      for c in cases if c["equip_uri"] == uri]
        positions.append(row)

    spaces = _rows(repo, "cx_spaces", project_id=pid)
    logs = _rows(repo, "cx_compliance_log", project_id=pid)
    compliance = None
    if spaces is None or logs is None:
        not_in["compliance_by_space"] = "#209 (spaces) and #207 (continuous compliance)"
    else:
        compliance = []
        for s in spaces:
            mine = [r for r in logs if r["space_id"] == s["id"]]
            if not mine and not s.get("space_type"):
                continue
            fails = [r for r in mine if r["status"] == "fail"]
            compliance.append({"space_id": s["id"], "name": s["name"], "space_type": s.get("space_type"),
                               "log_rows": len(mine), "pass": sum(1 for r in mine if r["status"] == "pass"),
                               "fail": len(fails), "inconclusive": sum(1 for r in mine if r["status"] == "inconclusive"),
                               "last_fail": _iso(max((r["window_end"] for r in fails), default=None)),
                               "packs": sorted({f'{r["pack_id"]} {r["version"]}' for r in mine})})

    docs = [{"sha256": d["sha256"], "filename": d["filename"], "kind": "specification", "content_kept": False}
            for d in repo.find_rows("cx_spec_documents", project_id=pid)]
    if "documents" in SECTION_SOURCES:
        docs += SECTION_SOURCES["documents"](repo, project, None)
    else:
        not_in["document_store"] = "#201 (document store): only Cx 7 spec documents are listed, by sha256"

    deliverables = []
    for key, label, where in DELIVERABLES:
        have = key == "cx_report" or (key == "issues_log" and bool(issues))
        deliverables.append({"key": key, "label": label, "in_baler": have,
                             "where": (where or "").replace("{id}", pid) if have else None,
                             "note": None if have else "not recorded in Baler"})
    return {"project": {"id": pid, "name": project["name"], "site_id": project.get("site_id"),
                        "phase": next((p["key"] for p in project.get("phases", []) if p.get("status") == "active"), None)},
            "generated_at": now.isoformat(), "generated_by": generated_by,
            "positions": positions, "compliance_by_space": compliance, "documents": docs,
            "std_202_deliverables": deliverables, "not_in_this_record": not_in,
            "export": "python -m baler.export: history as Parquet, the graph as Turtle, these records as JSON Lines"}


def _first(v) -> str:
    if not v:
        return "-"
    return f'{escape(str(v["procedure"]))}: {escape(str(v["status"]))} ({escape(str(v["finished_at"] or "-"))})'


def html(rep: dict) -> str:
    rows = []
    for p in rep["positions"]:
        t = p["startup_vs_today"] or {}
        eff = "; ".join(f'{b["kind"]} CV(RMSE) {b["cvrmse_pct"]}%' for b in p.get("efficiency") or []) or "-"
        case = "; ".join(f'{c["title"]}: ${c["annual_savings"]:,.0f}/yr, payback {c["simple_payback_years"]} yr'
                         for c in p.get("replace_vs_keep") or [] if c["annual_savings"] is not None) or "-"
        rows.append(f'<tr><td>{escape(str(p["dis"] or p["equip_uri"]))}</td><td>{escape(str(p["brick_class"] or "-"))}</td>'
                    f'<td>{_first(t.get("first_startup"))}</td><td>{_first(t.get("latest"))}</td>'
                    f'<td>{p["issues"]["open"]} open · {len(p["issues"]["risk_accepted"])} risk accepted · {p["issues"]["closed"]} closed</td>'
                    f'<td>{len(p["open_faults"])}</td><td>{escape(eff)}</td><td>{escape(case)}</td></tr>')
    comp = ""
    if rep["compliance_by_space"] is not None:
        comp = ("<h2>Compliance history by space</h2><table><caption>From the continuous compliance log</caption>"
                "<tr><th>Space</th><th>Type</th><th>Pass</th><th>Fail</th><th>Inconclusive</th><th>Last fail</th><th>Packs</th></tr>"
                + "".join(f'<tr><td>{escape(s["name"])}</td><td>{escape(str(s["space_type"] or "-"))}</td><td>{s["pass"]}</td>'
                          f'<td>{s["fail"]}</td><td>{s["inconclusive"]}</td><td>{escape(str(s["last_fail"] or "-"))}</td>'
                          f'<td>{escape(", ".join(s["packs"]))}</td></tr>' for s in rep["compliance_by_space"]) + "</table>")
    docs = "".join(f'<li>{escape(d["filename"])} <span class="muted">sha256 {escape(d["sha256"][:16])}…</span></li>'
                   for d in rep["documents"])
    dl = "".join(f'<li>{escape(d["label"])}: {"in Baler" if d["in_baler"] else escape(d["note"])}</li>'
                 for d in rep["std_202_deliverables"])
    missing = "".join(f"<li>{escape(k.replace('_', ' '))}: built by {escape(v)}</li>" for k, v in rep["not_in_this_record"].items())
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Building passport - {escape(rep["project"]["name"])}</title>
<style>{_CSS}</style></head><body>
<h1>Building passport: {escape(rep["project"]["name"])}</h1>
<p class="muted">Generated {escape(rep["generated_at"])} · phase {escape(str(rep["project"]["phase"] or "-"))}</p>
<h2>What this record doesn't hold yet</h2><ul>{missing or "<li>nothing missing</li>"}</ul>
<h2>Positions</h2>
<p class="muted">One row per position (the equipment record the points hang on). Assets installed at a position are listed
with it once the asset registry is in this record.</p>
<table><caption>Start-up baseline vs today, issues, faults, efficiency, replace-vs-keep</caption>
<tr><th>Position</th><th>Class</th><th>First start-up test</th><th>Latest test</th><th>Issues</th><th>Open faults</th>
<th>Efficiency baseline</th><th>Replace vs keep</th></tr>{"".join(rows)}</table>
{comp}
<h2>Handover deliverables (ASHRAE Std 202)</h2><ul>{dl}</ul>
<h2>Documents</h2><ul>{docs or "<li>none recorded</li>"}</ul>
<p class="muted">The full record exports with {escape(rep["export"])}.</p>
</body></html>
"""
    return _a11y_tables(page)

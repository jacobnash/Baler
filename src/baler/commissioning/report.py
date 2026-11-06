"""
The commissioning run report (Cx 5, Baler#41): one run's results shaped
for a reader - per equipment, failures first - as JSON or as a single
self-contained, printable HTML page (GET /cx/runs/{id}/report).

Pure: it's handed the project, the run and the (already scope-filtered)
results, and the project's issues/risks when Cx 4 has them. No I/O.
Every string from the graph or a result is escaped; the page carries no
script and loads nothing, so it prints the same offline.
"""

from __future__ import annotations

from html import escape

ORDER = {"fail": 0, "inconclusive": 1, "pass": 2, "not_applicable": 3}


def _equip_status(results: list[dict]) -> str:
    st = {r["status"] for r in results}
    if "fail" in st:
        return "fail"
    if "pass" in st:
        return "pass"
    return "inconclusive" if "inconclusive" in st else "not_applicable"


def equipment_summary(results: list[dict]) -> dict[str, dict]:
    """equip_uri -> {status, counts}: the one verdict per equipment the UI
    dot and the report heading use (any fail fails it)."""
    by: dict[str, list[dict]] = {}
    for r in results:
        by.setdefault(r["equip_uri"], []).append(r)
    out = {}
    for equip, rs in by.items():
        counts = {s: 0 for s in ORDER}
        for r in rs:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        ladder = next((r for r in rs if r["check_id"] == "ladder"), None)
        out[equip] = {"status": _equip_status(rs), "counts": counts,
                      "highest_rung_passed": ladder["evidence"].get("highest_rung_passed") if ladder else None}
    return out


def build(project: dict, run: dict, results: list[dict], issues: list[dict] | None = None,
          active_runs: list[dict] | None = None) -> dict:
    summary = equipment_summary(results)
    equipment = []
    for equip in sorted(summary, key=lambda e: (ORDER[summary[e]["status"]], e)):
        rs = sorted((r for r in results if r["equip_uri"] == equip),
                    key=lambda r: (ORDER.get(r["status"], 9), r["check_id"], r["subject"]))
        equipment.append({"equip_uri": equip, **summary[equip], "results": rs})
    return {
        "project": {"id": project["id"], "name": project["name"], "site_id": project.get("site_id"),
                    "phase": project.get("phase")},
        "run": run,
        "equipment": equipment,
        "issues": issues or [],
        "active_runs": active_runs or [],
    }


_CSS = """
body { font: 11pt/1.4 -apple-system, "Segoe UI", sans-serif; margin: 1.5cm; color: #111; }
h1 { font-size: 16pt; margin: 0 0 .2em; } h2 { font-size: 13pt; margin: 1.2em 0 .3em; border-bottom: 1px solid #999; }
h3 { font-size: 11pt; margin: .8em 0 .2em; } .muted { color: #555; } table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; vertical-align: top; padding: 2px 6px; border-bottom: 1px solid #ddd; font-size: 9.5pt; }
.s { font-weight: 600; text-transform: uppercase; font-size: 8.5pt; }
.fail { color: #b00; } .pass { color: #186b2f; } .inconclusive { color: #8a5a00; } .not_applicable { color: #666; }
.equip { break-inside: avoid; } ul { margin: .2em 0 .2em 1.2em; padding: 0; } li { font-size: 9pt; }
@media print { body { margin: 0; } a { color: inherit; text-decoration: none; } }
"""


def _status(s: str) -> str:
    return f'<span class="s {escape(s)}">{escape(s.replace("_", " "))}</span>'


def html(report: dict, title: str = "Commissioning report") -> str:
    p, run = report["project"], report["run"]
    summ = run.get("summary") or {}
    by = summ.get("by_status") or {}
    rows = []
    for e in report["equipment"]:
        c = e["counts"]
        rung = "" if e["highest_rung_passed"] is None else f" · highest rung passed {e['highest_rung_passed']}"
        rows.append(f'<tr><td>{escape(e["equip_uri"])}</td><td>{_status(e["status"])}</td>'
                    f'<td>{c.get("fail", 0)} fail · {c.get("inconclusive", 0)} inconclusive · {c.get("pass", 0)} pass{rung}</td></tr>')
    details = []
    for e in report["equipment"]:
        items = []
        for r in e["results"]:
            if r["status"] == "pass":
                continue
            cands = "".join(f"<li>{escape(c.get('cause', ''))} - <span class=muted>{escape(c.get('distinguish', ''))}</span></li>"
                            for c in r.get("candidates") or [])
            sev = f' <span class="muted">({escape(r["severity"])})</span>' if r.get("severity") else ""
            items.append(f'<tr><td>{_status(r["status"])}</td><td>{escape(r["check_id"])}{sev}</td>'
                         f'<td>{escape(r["subject"])}</td><td>{escape(r.get("symptom") or "")}'
                         f'{f"<ul>{cands}</ul>" if cands else ""}</td></tr>')
        passed = e["counts"].get("pass", 0)
        body = ("<table><tr><th>Status</th><th>Check</th><th>Subject</th><th>Symptom / candidate causes</th></tr>"
                + "".join(items) + "</table>") if items else ""
        details.append(f'<div class="equip"><h3>{escape(e["equip_uri"])} {_status(e["status"])}</h3>{body}'
                       f'<p class="muted">{passed} checks passed.</p></div>')
    issues = ""
    if report.get("issues"):
        irows = "".join(f'<tr><td>{escape(i.get("status", ""))}</td><td>{escape(i.get("title", ""))}</td>'
                        f'<td>{escape(i.get("equip_uri") or "")}</td><td>{escape(i.get("check_id") or "")}</td></tr>'
                        for i in report["issues"])
        issues = ("<h2>Issues log</h2><table><tr><th>Status</th><th>Issue</th><th>Equipment</th><th>Check</th></tr>"
                  f"{irows}</table>")
    active = ""
    if report.get("active_runs"):
        blocks = []
        for a in report["active_runs"]:
            cmds = "".join(f'<tr><td>{escape(str(w.get("at", "")))}</td><td>{escape(str(w.get("action", "")))}</td>'
                           f'<td>{escape(str(w.get("point", "")))}</td><td>{escape(str(w.get("value", "")))}</td>'
                           f'<td>{escape(str(w.get("level", "")))}</td></tr>' for w in a.get("writes") or [])
            res = "; ".join(f'{escape(str(x.get("title")))}: {escape(str(x.get("status")))}' for x in a.get("results") or [])
            blocks.append(f'<h3>{escape(a["equip_uri"])} - {escape(a["kind"])} {_status(a["status"]) if a["status"] in ORDER else escape(a["status"])}</h3>'
                          f'<p class="muted">confirmed by {escape(str(a.get("confirmed_by") or "-"))} · {res}</p>'
                          '<table><tr><th>When</th><th>Action</th><th>Point</th><th>Value</th><th>Priority</th></tr>'
                          f'{cmds}</table>')
        active = "<h2>Active tests - everything commanded</h2>" + "".join(blocks)
    window = f'{escape(str(run.get("window_start")))} to {escape(str(run.get("window_end")))}'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{escape(title)} - {escape(p["name"])}</title>
<style>{_CSS}</style></head><body>
<h1>{escape(title)}: {escape(p["name"])}</h1>
<p class="muted">Run {escape(run["id"])} · phase {escape(str(run.get("phase") or "-"))} · status {escape(run["status"])}<br>
Window {window} · finished {escape(str(run.get("finished_at") or "-"))}</p>
<p>{escape(summ.get("freshness") or "")}</p>
<p>{summ.get("equipment", 0)} equipment, {summ.get("points", 0)} points ·
{by.get("fail", 0)} fail · {by.get("inconclusive", 0)} inconclusive · {by.get("pass", 0)} pass ·
{summ.get("required_missing", 0)} required points missing</p>
<p class="muted">Inconclusive means there was no evidence either way - it is never counted as a pass.</p>
<h2>Equipment</h2>
<table><tr><th>Equipment</th><th>Verdict</th><th>Results</th></tr>{"".join(rows)}</table>
{issues}
{active}
<h2>Findings</h2>
{"".join(details)}
</body></html>
"""

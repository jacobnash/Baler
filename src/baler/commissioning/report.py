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

from datetime import datetime
from html import escape
from urllib.parse import quote

ORDER = {"fail": 0, "inconclusive": 1, "pass": 2, "not_applicable": 3}
# An equipment (or procedure) verdict that rests on any inconclusive
# result is "incomplete" in print - never green (Baler#87).
VERDICT_WORD = {"fail": "fail", "inconclusive": "incomplete", "pass": "pass", "not_applicable": "not applicable"}
LADDER_HELP = ("The verification ladder climbs per equipment: rung 1, every point reports; rung 2, sensors move "
               "(not frozen); rung 3, status follows command and sensors track setpoints; rung 4, the sequences "
               "(GL36, economizer). 'Highest rung passed' is how far up the equipment got before a fail or a "
               "rung that couldn't be judged.")


def _equip_status(results: list[dict]) -> str:
    """Any fail fails it; else any inconclusive leaves it incomplete
    (inconclusive is never a pass); else pass."""
    st = {r["status"] for r in results}
    if "fail" in st:
        return "fail"
    if "inconclusive" in st:
        return "inconclusive"
    return "pass" if "pass" in st else "not_applicable"


def human_duration(seconds: float | None) -> str:
    """72 -> "1 min 12 s", 0.02 h -> "1 min 12 s", 24 h -> "24 h", 3 days -> "3 d"."""
    if seconds is None:
        return "-"
    s = round(seconds)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        m, sec = divmod(s, 60)
        return f"{m} min" + (f" {sec} s" if sec else "")
    if s < 2 * 86400:
        h, rem = divmod(s, 3600)
        return f"{h} h" + (f" {rem // 60} min" if rem // 60 else "")
    d, rem = divmod(s, 86400)
    return f"{d} d" + (f" {rem // 3600} h" if rem // 3600 else "")


def reason(result: dict) -> str:
    """What a reader needs next to a non-pass result: its symptom, or for
    an inconclusive with none, why it couldn't be judged."""
    if result.get("symptom"):
        return result["symptom"]
    ev = result.get("evidence") or {}
    if ev.get("reason"):
        return str(ev["reason"])
    rungs = ev.get("rungs")
    if isinstance(rungs, dict):
        stuck = [f"rung {k} {v.replace('_', ' ')}" for k, v in sorted(rungs.items()) if v not in ("pass",)]
        if stuck:
            return "; ".join(stuck)
    if result.get("status") == "inconclusive":
        return "no evidence either way in the window"
    return ""


def _dt(v) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def evidence_link(point_uri: str | None, start, end) -> str | None:
    """The raw trend behind a result, in the Query page."""
    if not point_uri or not str(point_uri).startswith("urn:point:"):
        return None
    a, b = _dt(start), _dt(end)
    span = f"{a.date().isoformat()}..{b.date().isoformat()}" if a and b else "today"
    return "/ui/his.html?expr=" + quote(f"readAll(id==@{point_uri}).hisRead({span})", safe="")


def short_point(uri: str | None) -> str:
    return str(uri or "").replace("urn:point:", "", 1).replace("urn:equip:", "", 1)


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
        rs = sorted(({**r, "reason": reason(r), "evidence_link": evidence_link(r["subject"], run.get("window_start"),
                                                                               run.get("window_end"))}
                     for r in results if r["equip_uri"] == equip),
                    key=lambda r: (ORDER.get(r["status"], 9), r["check_id"], r["subject"]))
        equipment.append({"equip_uri": equip, **summary[equip], "verdict": VERDICT_WORD[summary[equip]["status"]],
                          "results": rs})
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


def _verdict(s: str) -> str:
    return f'<span class="s {escape(s)}">{escape(VERDICT_WORD.get(s, s))}</span>'


def _link(href: str | None, text: str = "trend") -> str:
    return f'<a href="{escape(href)}">{escape(text)}</a>' if href else ""


def _when(v) -> str:
    d = _dt(v)
    return d.strftime("%Y-%m-%d %H:%M") if d else escape(str(v or "-"))


def _actor(e: dict) -> str:
    return e.get("user_email") or e.get("user_sub") or "system"


def _history(events: list[dict]) -> str:
    """Oldest first: create -> resolve.evidence (system) -> close (jo@x)."""
    return " → ".join(f"{escape(e['action'])} ({escape(_actor(e))}, {_when(e['ts'])})" for e in reversed(events or []))


def issues_html(issues: list[dict]) -> str:
    """The issues log: one row per issue with its point, severity,
    opened/closed (run, time, who) and status history; waivers listed as
    open items with owner, reason and review-by (Baler#87)."""
    if not issues:
        return "<h2>Issues log</h2><p class=muted>No issues recorded.</p>"
    rows = []
    for i in issues:
        ev = i.get("events") or []
        closer = next((e for e in ev if e["action"] == "close" or (e["action"] == "update" and (e.get("detail") or {}).get("status") == "closed")), None)
        closed = (f'{_when(i.get("closed_at"))}<br><span class=muted>{escape(_actor(closer)) if closer else ""}'
                  f'{" · retest " + escape(str(i["closed_run"])) if i.get("closed_run") else ""}</span>') if i.get("closed_at") else ""
        rows.append(f'<tr><td>{escape(str(i["id"])[:8])}</td><td>{escape(i.get("status", "").replace("_", " "))}</td>'
                    f'<td>{escape(i.get("title", ""))}<br><span class=muted>{escape(short_point(i.get("subject")))}'
                    f'{" · " + escape(i["check_id"]) if i.get("check_id") else ""}</span></td>'
                    f'<td>{escape(i.get("severity") or "-")}</td>'
                    f'<td>{_when(i.get("created_at"))}<br><span class=muted>{escape(str(i.get("opened_run") or "manual"))}</span></td>'
                    f'<td>{closed}</td><td class=muted>{_history(ev)}</td></tr>')
    waivers = [i for i in issues if i.get("status") == "risk_accepted" and i.get("risk")]
    wtab = ""
    if waivers:
        wrows = "".join(f'<tr><td>{escape(i.get("title", ""))}</td><td>{escape(str(i["risk"].get("accepted_email") or i["risk"].get("accepted_by") or "-"))}</td>'
                        f'<td>{escape(i["risk"].get("reason") or "")}</td><td>{escape(str(i["risk"].get("review_by")))}</td></tr>'
                        for i in waivers)
        wtab = ("<h3>Waivers (accepted risks) - still open</h3><table><tr><th>Item</th><th>Owner</th><th>Reason / retest condition</th>"
                f"<th>Review by</th></tr>{wrows}</table>")
    return ("<h2>Issues log</h2><table><tr><th>ID</th><th>Status</th><th>Issue / point</th><th>Severity</th><th>Opened</th>"
            f"<th>Closed</th><th>History</th></tr>{''.join(rows)}</table>{wtab}")


def html(report: dict, title: str = "Commissioning report") -> str:
    p, run = report["project"], report["run"]
    summ = run.get("summary") or {}
    by = summ.get("by_status") or {}
    rows = []
    for e in report["equipment"]:
        c = e["counts"]
        rung = "" if e["highest_rung_passed"] is None else f" · highest rung passed {e['highest_rung_passed']}"
        rows.append(f'<tr><td>{escape(e["equip_uri"])}</td><td>{_verdict(e["status"])}</td>'
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
            link = _link(r.get("evidence_link") or evidence_link(r["subject"], run.get("window_start"), run.get("window_end")))
            items.append(f'<tr><td>{_status(r["status"])}</td><td>{escape(r["check_id"])}{sev}</td>'
                         f'<td>{escape(short_point(r["subject"]))} {link}</td><td>{escape(r.get("reason") or reason(r))}'
                         f'{f"<ul>{cands}</ul>" if cands else ""}</td></tr>')
        passed = e["counts"].get("pass", 0)
        body = ("<table><tr><th>Status</th><th>Check</th><th>Point</th><th>Symptom or reason / candidate causes</th></tr>"
                + "".join(items) + "</table>") if items else ""
        details.append(f'<div class="equip"><h3>{escape(e["equip_uri"])} {_verdict(e["status"])}</h3>{body}'
                       f'<p class="muted">{passed} checks passed.</p></div>')
    issues = issues_html(report["issues"]) if report.get("issues") else ""
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
    ws, we = _dt(run.get("window_start")), _dt(run.get("window_end"))
    length = human_duration((we - ws).total_seconds()) if ws and we else "-"
    window = f'{_when(run.get("window_start"))} to {_when(run.get("window_end"))} ({length})'
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
<p class="muted">Verdict: any fail fails the equipment; otherwise any inconclusive leaves it incomplete; only then pass.</p>
<table><tr><th>Equipment</th><th>Verdict</th><th>Results</th></tr>{"".join(rows)}</table>
<p class="muted">{escape(LADDER_HELP)}</p>
{issues}
{active}
<h2>Findings</h2>
{"".join(details)}
</body></html>
"""


# ------------------------------------------------------------------ the Cx report
#
# A run report covers one run. The Cx report (Baler#87) covers the
# project: what was in scope and what couldn't be judged, the phase history,
# spec against installed, the latest verdicts, procedure (FPT) results, and
# the issues log with who did what - so the failure and recovery stay in the
# story instead of only the last clean run. Verdicts come from rules only.


def limitations(run: dict | None, results: list[dict], procedures: list[dict], latest_test_runs: dict,
                active_runs: list[dict], writes_enabled: bool) -> list[str]:
    """Scope and limitations, generated from what the records show the
    project couldn't do."""
    out = []
    done_active = [a for a in active_runs if a.get("status") in ("completed", "passed", "failed", "aborted")]
    if not done_active:
        out.append("Passive only: no active (commanded) tests were run"
                   + ("." if writes_enabled else " - writes to equipment are disabled on this server."))
    if run is None:
        out.append("No completed verification run: nothing below is judged against live data.")
    inconc = [r for r in results if r["status"] == "inconclusive"]
    if inconc:
        out.append(f"{len(inconc)} result(s) in the latest run are inconclusive - no evidence either way, never counted as a pass.")
    no_pairs = [r for r in results if r["check_id"] == "ladder"
                and str(((r.get("evidence") or {}).get("rungs") or {}).get("3", "")) in ("not_attempted", "inconclusive")]
    if no_pairs:
        out.append(f"{len(no_pairs)} equipment could not reach rung 3 (status follows command / sensor tracks setpoint): "
                   "no command-status or sensor-setpoint pair was judged.")
    active_procs = [p for p in procedures if p.get("status") == "active"]
    never = [p["name"] for p in active_procs if latest_test_runs.get(p["id"]) is None]
    if never:
        out.append(f"Procedures never run: {', '.join(never)}.")
    if not any(p.get("kind") == "functional" for p in active_procs):
        out.append("Sequences of operation were not evaluated: the project has no functional test procedure.")
    return out


def project_build(project: dict, generated_at: datetime, generated_by: str | None, run: dict | None,
                  results: list[dict], procedures: list[dict], latest_test_runs: dict, issues: list[dict],
                  events: list[dict], alignment: dict | None, spec_documents: list[dict], active_runs: list[dict],
                  writes_enabled: bool) -> dict:
    latest = build(project, run, results, issues, active_runs) if run else None
    verdicts = {s: 0 for s in ORDER}
    for e in (latest or {}).get("equipment", []):
        verdicts[e["status"]] += 1
    fpts = []
    for proc in procedures:
        tr = latest_test_runs.get(proc["id"])
        fpts.append({"procedure_id": proc["id"], "name": proc.get("name"), "kind": proc.get("kind"),
                     "status": proc.get("status"), "test_run": tr})
    mappings = (alignment or {}).get("mappings") or []
    return {
        "project": {"id": project["id"], "name": project["name"], "site_id": project.get("site_id"),
                    "phase": project.get("phase"), "phases": project.get("phases") or []},
        "generated_at": generated_at.isoformat(),
        "generated_by": generated_by,
        "cxa": (project.get("settings") or {}).get("cxa_name"),
        "spec_documents": [{"filename": d.get("filename"), "sha256": d.get("sha256"), "created_at": d.get("created_at")}
                           for d in spec_documents],
        "verdicts": verdicts,
        "limitations": limitations(run, results, procedures, latest_test_runs, active_runs, writes_enabled),
        "phase_history": [e for e in events if e["action"] == "phase.set"],
        "spec": {"items": len(mappings), "matched": sum(1 for m in mappings if m.get("equip_uri")),
                 "unmatched": sum(1 for m in mappings if not m.get("equip_uri")),
                 "deviations": (alignment or {}).get("deviations") or []},
        "latest_run": latest,
        "procedures": fpts,
        "issues": issues,
        "active_runs": active_runs,
    }


def _obs_text(o: dict) -> str:
    """One observed-evidence entry, compact: what the rule actually saw."""
    name = o.get("dis") or short_point(o.get("subject"))
    if "moved" in o:
        moved = "nothing measurable" if o["moved"] is None else f'{o["moved"]:g}'
        return f'{name} moved {moved} after {short_point(o.get("trigger"))} started {_when(o.get("at"))}'
    unit = f' {o["unit"]}' if o.get("unit") else ""
    bits = [f'{o["min"]:g}..{o["max"]:g}{unit}' if "min" in o and isinstance(o["min"], (int, float)) else None,
            f'last {o["last"]:g}' if isinstance(o.get("last"), (int, float)) else None,
            f'{o["availability_pct"]:g}% available' if "availability_pct" in o else None,
            f'{o.get("samples", 0)} samples']
    return f'{name}: ' + ", ".join(b for b in bits if b)


def _fpt_html(fpts: list[dict]) -> str:
    if not fpts:
        return "<p class=muted>No test procedures on this project.</p>"
    blocks = []
    for f in fpts:
        tr = f["test_run"]
        if tr is None:
            blocks.append(f'<h3>{escape(f["name"] or "")} <span class=muted>({escape(f["kind"] or "")})</span></h3>'
                          '<p class="s inconclusive">never run</p>')
            continue
        rows = []
        for e in tr.get("equipment") or []:
            for st in e.get("steps") or []:
                failing = "; ".join(f'{escape(short_point(x.get("subject")))}: {escape(x.get("symptom") or x.get("status") or "")}'
                                    for c in st.get("criteria") or [] for x in c.get("failing") or [])
                seen = "<br>".join(escape(_obs_text(o)) for c in st.get("criteria") or [] for o in (c.get("observed") or [])[:8])
                rows.append(f'<tr><td>{escape(short_point(e["equip_uri"]))}</td><td>{escape(st.get("title") or st["id"])}</td>'
                            f'<td>{_verdict(st["status"])}</td><td>{failing}</td><td class=muted>{seen}</td></tr>')
        by = (tr.get("summary") or {}).get("by_status") or {}
        blocks.append(f'<h3>{escape(f["name"] or "")} <span class=muted>({escape(f["kind"] or "")}) · test run '
                      f'{escape(str(tr["id"])[:8])} · {_when(tr.get("started_at"))} to '
                      f'{_when(tr.get("window_end"))}</span></h3>'
                      f'<p class=muted>{by.get("fail", 0)} fail · {by.get("inconclusive", 0)} incomplete · {by.get("pass", 0)} pass</p>'
                      '<table><tr><th>Equipment</th><th>Step</th><th>Result</th><th>What failed</th><th>Observed</th></tr>'
                      f'{"".join(rows)}</table>')
    return "".join(blocks)


def project_html(rep: dict) -> str:
    p = rep["project"]
    v = rep["verdicts"]
    docs = "".join(f'<li>{escape(d["filename"] or "")} <span class=muted>sha256 {escape(str(d["sha256"] or "")[:12])} · '
                   f'{_when(d["created_at"])}</span></li>' for d in rep["spec_documents"]) or "<li class=muted>none uploaded</li>"
    lim = "".join(f"<li>{escape(x)}</li>" for x in rep["limitations"]) or "<li>None found in the records.</li>"
    phase_rows = "".join(
        f'<tr><td>{escape(ph["name"])}</td><td>{escape(ph["status"])}</td><td>{_when(ph.get("started_at"))}</td>'
        f'<td>{_when(ph.get("completed_at"))}</td></tr>' for ph in p["phases"])
    moves = "".join(
        f'<tr><td>{_when(e["ts"])}</td><td>{escape(_actor(e))}</td><td>{escape(str((e.get("detail") or {}).get("from")))} → '
        f'{escape(str((e.get("detail") or {}).get("to")))}</td><td>{escape((e.get("detail") or {}).get("note") or "")}'
        + (" <b>(override)</b>" if (e.get("detail") or {}).get("override") else "")
        + "".join(f'<br><span class=muted>gate: {escape(u["message"])}</span>' for u in ((e.get("detail") or {}).get("gate") or {}).get("unmet", []))
        + "</td></tr>" for e in reversed(rep["phase_history"]))
    sp = rep["spec"]
    devs = "".join(f'<tr><td>{escape(d["kind"].replace("_", " "))}</td><td>{escape(d.get("spec_tag") or ", ".join(d.get("spec_tags") or []) or "")}</td>'
                   f'<td>{escape(short_point(d.get("equip_uri")))}</td><td>{escape(d.get("interpretation") or "")}</td></tr>'
                   for d in sp["deviations"])
    latest = rep["latest_run"]
    latest_html = "<p class=muted>No completed verification run.</p>"
    if latest:
        run = latest["run"]
        eq = "".join(f'<tr><td>{escape(e["equip_uri"])}</td><td>{_verdict(e["status"])}</td>'
                     f'<td>{e["counts"].get("fail", 0)} fail · {e["counts"].get("inconclusive", 0)} inconclusive · '
                     f'{e["counts"].get("pass", 0)} pass</td></tr>' for e in latest["equipment"])
        latest_html = (f'<p class=muted>Run {escape(run["id"])} · {_when(run.get("finished_at"))} · phase '
                       f'{escape(str(run.get("phase") or "-"))}</p><table><tr><th>Equipment</th><th>Verdict</th><th>Results</th></tr>'
                       f'{eq}</table><p class=muted>{escape(LADDER_HELP)}</p>')
    findings = ""
    if latest:
        findings = html(latest, title="Latest verification run").split("<h2>Findings</h2>", 1)[-1].split("</body>", 1)[0]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Commissioning report - {escape(p["name"])}</title>
<style>{_CSS} .sign td {{ height: 2.2em; }}</style></head><body>
<h1>Commissioning report: {escape(p["name"])}</h1>
<p class="muted">Site {escape(str(p.get("site_id") or "-"))} · current phase {escape(str(p.get("phase") or "-"))} ·
generated {_when(rep["generated_at"])} by {escape(rep["generated_by"] or "-")}<br>
Commissioning authority: {escape(rep["cxa"] or "________________")}</p>
<h3>Specification documents</h3><ul>{docs}</ul>
<h2>Summary</h2>
<p>{v.get("fail", 0)} equipment fail · {v.get("inconclusive", 0)} incomplete · {v.get("pass", 0)} pass ·
{sum(1 for i in rep["issues"] if i.get("status") not in ("closed",))} issue(s) not closed.</p>
<p class="muted">Verdicts are computed from rules over the recorded data. Any fail fails; any inconclusive leaves it
incomplete - never a pass.</p>
<h2>Scope and limitations</h2><ul>{lim}</ul>
<h2>Phase history</h2>
<table><tr><th>Phase</th><th>Status</th><th>Started</th><th>Completed</th></tr>{phase_rows}</table>
<table><tr><th>When</th><th>Who</th><th>Move</th><th>Note / gate</th></tr>{moves or "<tr><td colspan=4 class=muted>no phase moves recorded</td></tr>"}</table>
<h2>Specification vs installed</h2>
<p>{sp["items"]} accepted spec equipment item(s): {sp["matched"]} matched in the field, {sp["unmatched"]} not found.</p>
{"<table><tr><th>Deviation</th><th>Spec</th><th>Field</th><th>Interpretation</th></tr>" + devs + "</table>" if devs else "<p class=muted>No deviations.</p>"}
<h2>Equipment verdicts (latest verification run)</h2>
{latest_html}
<h2>Procedure / functional test results</h2>
{_fpt_html(rep["procedures"])}
{issues_html(rep["issues"])}
<h2>Findings (latest verification run)</h2>
{findings}
<h2>Sign-off</h2>
<table class="sign"><tr><th>Role</th><th>Name</th><th>Signature</th><th>Date</th></tr>
<tr><td>Commissioning authority</td><td>{escape(rep["cxa"] or "")}</td><td></td><td></td></tr>
<tr><td>Owner's representative</td><td></td><td></td><td></td></tr>
<tr><td>Contractor</td><td></td><td></td><td></td></tr></table>
</body></html>
"""

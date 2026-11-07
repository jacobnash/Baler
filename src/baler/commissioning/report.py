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

import re
from datetime import datetime, timezone
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


def waiver_index(issues: list[dict] | None) -> dict[tuple, str]:
    """(check_id, subject) -> the id of the issue whose accepted risk waives it."""
    return {(i.get("check_id"), i.get("subject")): i["id"] for i in issues or []
            if i.get("status") == "risk_accepted" and i.get("check_id")}


def build(project: dict, run: dict, results: list[dict], issues: list[dict] | None = None,
          active_runs: list[dict] | None = None) -> dict:
    summary = equipment_summary(results)
    waivers = waiver_index(issues)
    equipment = []
    for equip in sorted(summary, key=lambda e: (ORDER[summary[e]["status"]], e)):
        rs = sorted(({**r, "reason": reason(r), "evidence_link": evidence_link(r["subject"], run.get("window_start"),
                                                                               run.get("window_end")),
                      "waived_by": waivers.get((r["check_id"], r["subject"])) if r["status"] != "pass" else None}
                     for r in results if r["equip_uri"] == equip),
                    key=lambda r: (ORDER.get(r["status"], 9), r["check_id"], r["subject"]))
        equipment.append({"equip_uri": equip, **summary[equip], "verdict": VERDICT_WORD[summary[equip]["status"]],
                          "waived": sum(1 for r in rs if r["waived_by"]), "results": rs})
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
.equip h3 { break-after: avoid; } .waived { color: #5a3d8a; border: 1px solid currentColor; border-radius: 3px; padding: 0 3px; }
tr, .signoff { break-inside: avoid; } thead { display: table-header-group; } ul { margin: .2em 0 .2em 1.2em; padding: 0; } li { font-size: 9pt; }
@media print { body { margin: 0; } a { color: inherit; text-decoration: none; } }
"""


def _status(s: str) -> str:
    return f'<span class="s {escape(s)}">{escape(s.replace("_", " "))}</span>'


def _verdict(s: str) -> str:
    return f'<span class="s {escape(s)}">{escape(VERDICT_WORD.get(s, s))}</span>'


def _waived(issue_id: str | None) -> str:
    """A text badge (not colour alone) linking to the waiver's issue row."""
    return f' <a class="s waived" href="#issue-{escape(str(issue_id))}">waived</a>' if issue_id else ""


def _waived_count(e: dict) -> str:
    return f' · <span class="s waived">{e["waived"]} waived</span>' if e.get("waived") else ""


def _link(href: str | None, text: str = "trend") -> str:
    return f'<a href="{escape(href)}">{escape(text)}</a>' if href else ""


def _when(v) -> str:
    """Times print in UTC, and say so (Baler#99)."""
    d = _dt(v)
    if not d:
        return escape(str(v or "-"))
    # A naive timestamp is UTC in this codebase (every writer stores UTC);
    # say so rather than guess a local zone.
    d = d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
    return d.strftime("%Y-%m-%d %H:%M UTC")


def _actor(e: dict) -> str:
    return e.get("user_email") or e.get("user_sub") or "system"


def _history(events: list[dict]) -> str:
    """Oldest first: create -> resolve.evidence (system) -> close (jo@x)."""
    return " → ".join(f"{escape(e['action'])} ({escape(_actor(e))}, {_when(e['ts'])})" for e in reversed(events or []))


def _accepted_by(risk: dict, cxa: str | None) -> str:
    """The signer's name, labelled CxA when it is the project's CxA, with
    the account it came through as secondary (Baler#100 review)."""
    name = risk.get("accepted_name")
    email = risk.get("accepted_email") or risk.get("accepted_by") or ""
    if not name:
        return escape(email or "-")
    want = (cxa or "").strip().lower()
    is_cxa = bool(want) and (name.strip().lower() == want or want.startswith(name.strip().lower()))
    return f'{escape(name)}{", CxA" if is_cxa else ""}<br><span class=muted>via {escape(email)}</span>'


def issues_html(issues: list[dict], cxa: str | None = None) -> str:
    """The issues log: one row per issue with its point, severity,
    opened/closed (run, time, who) and status history; waivers listed as
    open items with owner, reason and review-by (Baler#87)."""
    if not issues:
        return "<h2>Issues log</h2><p class=muted>No issues recorded.</p>"
    rows = []
    for i in issues:
        ev = i.get("events") or []
        closer = next((e for e in ev if e["action"] == "close" or (e["action"] == "update" and (e.get("detail") or {}).get("status") == "closed")), None)
        rt = i.get("retest") or {}
        retest = (_tr_link(rt["test_run_id"]) if rt.get("test_run_id") else _run_link(i.get("closed_run"))) if i.get("closed_run") else ""
        closed = (f'{_when(i.get("closed_at"))}<br><span class=muted>{escape(_actor(closer)) if closer else ""}'
                  f'{" · retest " + retest if retest else ""}</span>') if i.get("closed_at") else ""
        rows.append(f'<tr id="issue-{escape(str(i["id"]))}"><td><a href="#issue-{escape(str(i["id"]))}">{escape(str(i["id"])[:8])}</a></td>'
                    f'<td>{escape(i.get("status", "").replace("_", " "))}</td>'
                    f'<td>{escape(i.get("title", ""))}<br><span class=muted>{escape(short_point(i.get("subject")))}'
                    f'{" · " + escape(i["check_id"]) if i.get("check_id") else ""}</span></td>'
                    f'<td>{escape(i.get("severity") or "-")}</td>'
                    f'<td>{_when(i.get("created_at"))}<br><span class=muted>{_run_link(i["opened_run"]) if i.get("opened_run") else "manual"}</span></td>'
                    f'<td>{closed}</td><td class=muted>{_history(ev)}</td></tr>')
    waivers = [i for i in issues if i.get("status") == "risk_accepted" and i.get("risk")]
    wtab = ""
    if waivers:
        wrows = "".join(
            f'<tr><td><a href="#issue-{escape(str(i["id"]))}">{escape(str(i["id"])[:8])}</a> {escape(i.get("title", ""))}</td>'
            f'<td>{_accepted_by(i["risk"], cxa)}</td>'
            f'<td>{escape(i["risk"].get("owner") or "-")}</td>'
            f'<td>{escape(i["risk"].get("reason") or "")}</td>'
            f'<td>{escape(i["risk"].get("retest_trigger") or "-")}</td>'
            f'<td>{escape(str(i["risk"].get("review_by")))}</td></tr>' for i in waivers)
        wtab = ('<h3 id="waivers">Waivers (accepted risks) - still open</h3><table><caption>Waivers: inconclusive or failing '
                'results accepted as risk; each stays open until its retest</caption><tr><th>Item</th><th>Accepted by</th>'
                "<th>Owner / responsible</th><th>Reason</th><th>Retest trigger</th><th>Review by</th></tr>"
                f"{wrows}</table>")
    return ("<h2>Issues log</h2><table><caption>Every issue, with who opened, resolved and closed it</caption><tr><th>ID</th><th>Status</th><th>Issue / point</th><th>Severity</th><th>Opened</th>"
            f"<th>Closed</th><th>History</th></tr>{''.join(rows)}</table>{wtab}")


def _a11y_tables(page: str) -> str:
    """Every table's header row becomes a <thead> (repeats across printed
    pages) and its cells get scope=col (screen readers)."""
    page = re.sub(r"(<table[^>]*>(?:<caption>.*?</caption>)?)(<tr>(?:<th>.*?</th>)+</tr>)", r"\1<thead>\2</thead>", page, flags=re.DOTALL)
    return page.replace("<th>", '<th scope="col">')


def html(report: dict, title: str = "Commissioning report") -> str:
    return _a11y_tables(_html(report, title))


def _html(report: dict, title: str = "Commissioning report") -> str:
    p, run = report["project"], report["run"]
    summ = run.get("summary") or {}
    by = summ.get("by_status") or {}
    rows = []
    for e in report["equipment"]:
        c = e["counts"]
        rung = "" if e["highest_rung_passed"] is None else f" · highest rung passed {e['highest_rung_passed']}"
        rows.append(f'<tr><td>{escape(e["equip_uri"])}</td><td>{_verdict(e["status"])}{_waived_count(e)}</td>'
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
            items.append(f'<tr><td>{_status(r["status"])}{_waived(r.get("waived_by"))}</td><td>{escape(r["check_id"])}{sev}</td>'
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




def waived_keys(issues: list[dict]) -> set[tuple]:
    """(check_id, subject) of every result a person waived - an issue
    accepted as a risk. Waived is first-class in the report (Baler#99):
    still incomplete, never a pass, but not unaccounted for either."""
    return {(i.get("check_id"), i.get("subject")) for i in issues if i.get("status") == "risk_accepted" and i.get("check_id")}


_SEQ_KINDS = ("availability", "response")


def _is_sequence(c: dict) -> bool:
    """A criterion that tests a sequence of operation: tagged explicitly
    (`"sequence": "S2"`), or - as a fallback - shaped like one (availability,
    response, a range limited to occupied hours)."""
    if c.get("sequence"):
        return True
    return any(k in c for k in _SEQ_KINDS) or ("range" in c and bool(c["range"].get("hours")))


def _rule_text(c: dict) -> str:
    """A criterion as a person reads it."""
    tag = f'{c["sequence"]}: ' if c.get("sequence") else ""
    if "availability" in c:
        a = c["availability"]
        return f'{tag}available at least {a["min_pct"]:g}% (a sample at least every {a["max_gap_minutes"]:g} min)'
    if "response" in c:
        w, t = c["response"]["when"], c["response"]["then"]
        return (f'{tag}when {w.get("point_class") or short_point(w.get("point"))} = {w["equals"]}, '
                f'{t.get("point_class") or short_point(t.get("point"))} moves at least {t["changes_by"]:g} within {t["within_minutes"]:g} min')
    if "range" in c:
        r = c["range"]
        lo = "-inf" if r.get("min") is None else f'{r["min"]:g}'
        hi = "inf" if r.get("max") is None else f'{r["max"]:g}'
        txt = f'{tag}{c.get("point_class") or short_point(c.get("point"))} within {lo}..{hi} {r.get("unit") or ""}'.rstrip()
        h = r.get("hours")
        if h:
            days = "weekdays" if h.get("days") == [0, 1, 2, 3, 4] else ("days " + ",".join(map(str, h["days"])) if h.get("days") else "every day")
            txt += f', {h.get("from", 0):02d}:00-{h.get("to", 24):02d}:00 {h.get("tz") or "UTC"} {days}'
        return txt
    return f'{tag}check {c.get("check")}' + (" (strict)" if c.get("strict") else "")


def recorded_criteria(proc: dict, tr: dict | None) -> tuple[list[dict], bool]:
    """The criteria as the run judged them - each evaluated criterion carries
    the rule it was judged on - so a procedure edited after the run can't
    put new rule text next to an old verdict (Baler#100 review). Returns
    ([{step_id, title, index, criterion, judged: [...]}], edited)."""
    rows: dict[tuple, dict] = {}
    titles = {st["id"]: st.get("title") or st["id"] for st in proc.get("steps") or []}
    for e in (tr or {}).get("equipment") or []:
        for st in e.get("steps") or []:
            for ci, j in enumerate(st.get("criteria") or []):
                key = (st["id"], ci)
                row = rows.setdefault(key, {"step_id": st["id"], "title": st.get("title") or titles.get(st["id"], st["id"]),
                                            "index": ci, "criterion": j.get("criterion") or {}, "judged": []})
                row["judged"].append(j)
    current = {(st["id"], ci): c for st in proc.get("steps") or [] for ci, c in enumerate(st.get("criteria") or [])}
    if not rows:  # never run, or nothing applied: the procedure as it stands, unjudged
        return ([{"step_id": k[0], "title": titles.get(k[0], k[0]), "index": k[1], "criterion": c, "judged": []}
                 for k, c in current.items()], False)
    edited = any(current.get(k) != r["criterion"] for k, r in rows.items()) or set(current) - set(rows) != set()
    return sorted(rows.values(), key=lambda r: (list(titles).index(r["step_id"]) if r["step_id"] in titles else 99, r["index"])), edited


def _worst(judged: list[dict]) -> str | None:
    statuses = [j["status"] for j in judged if j["status"] != "not_applicable"]
    return min(statuses, key=lambda x: ORDER[x]) if statuses else None


def sequences(procedures: list[dict], latest_test_runs: dict) -> list[dict]:
    """Every sequence criterion and where it ran - by criterion, not by the
    procedure's kind (Baler#99: S2 ran inside a start-up procedure)."""
    out = []
    for proc in procedures:
        if proc.get("status") != "active":
            continue
        tr = latest_test_runs.get(proc["id"])
        crit, edited = recorded_criteria(proc, tr)
        for r in crit:
            c = r["criterion"]
            if not _is_sequence(c):
                continue
            row = {"procedure_id": proc["id"], "procedure": proc.get("name"), "step": r["title"],
                   "sequence": c.get("sequence"), "rule": _rule_text(c), "test_run_id": tr["id"] if tr else None,
                   "status": "not_evaluated", "reason": "the procedure has never been run" if tr is None else None,
                   "observed": [], "procedure_edited": edited}
            if tr is not None:
                worst = _worst(r["judged"])
                if worst:
                    row["status"] = worst
                    row["reason"] = next((f.get("symptom") for j in r["judged"] for f in j.get("failing") or [] if f.get("symptom")), None)
                    row["observed"] = [o for j in r["judged"] for o in j.get("observed") or []][:20]
                else:
                    row["reason"] = "no equipment had the points it needs"
            out.append(row)
    return out


def limitations(run: dict | None, results: list[dict], procedures: list[dict], latest_test_runs: dict,
                active_runs: list[dict], writes_enabled: bool, issues: list[dict] | None = None,
                equipment_names: bool = True) -> list[dict]:
    """Scope and limitations, generated from what the records show the
    project couldn't do. Structured: {kind, text}."""
    out: list[dict] = []
    waived = waived_keys(issues or [])

    def add(kind: str, text: str) -> None:
        out.append({"kind": kind, "text": text})

    done_active = [a for a in active_runs if a.get("status") in ("completed", "passed", "failed", "aborted")]
    if not done_active:
        add("passive_only", "Passive only: no active (commanded) tests were run"
            + ("." if writes_enabled else " - writes to equipment are disabled on this server."))
    if run is None:
        add("no_run", "No completed verification run: nothing below is judged against live data.")
    inconc = [r for r in results if r["status"] == "inconclusive"]
    if inconc:
        w = sum(1 for r in inconc if (r["check_id"], r["subject"]) in waived)
        add("inconclusive", f"{len(inconc)} result(s) in the latest run are inconclusive - no evidence either way, never "
            f"counted as a pass: {w} waived (risk accepted), {len(inconc) - w} not waived.")
    no_pairs = [r for r in results if r["check_id"] == "ladder"
                and str(((r.get("evidence") or {}).get("rungs") or {}).get("3", "")) in ("not_attempted", "inconclusive")]
    if no_pairs:
        add("rung3", f"{len(no_pairs)} equipment could not reach rung 3 (status follows command / sensor tracks setpoint): "
            "no command-status or sensor-setpoint pair was judged.")
    active_procs = [p for p in procedures if p.get("status") == "active"]
    never = [p["name"] for p in active_procs if latest_test_runs.get(p["id"]) is None]
    if never:
        add("procedure_never_run", f"Procedures never run: {', '.join(never)}.")
    seqs = sequences(procedures, latest_test_runs)
    if not seqs:
        add("no_sequences", "No sequence-of-operation criteria are defined (availability, response, or occupied-hours bands), "
            "so sequences were not evaluated.")
    if not equipment_names:
        add("equipment_names_unavailable", "Equipment names and types were unavailable when this report was built; "
            "raw equipment ids are shown.")
    if any(sq.get("procedure_edited") for sq in seqs):
        add("procedure_edited", "At least one procedure was edited after its latest test run; the rules shown are the "
            "ones that run judged.")
    for sq in seqs:
        if sq["status"] == "not_evaluated":
            add("sequence_not_evaluated", f"{sq['step']} ({sq['procedure']}) not evaluated: {sq['reason']}.")
        elif sq["status"] == "inconclusive":
            add("sequence_not_demonstrable", f"{sq['step']} ({sq['procedure']}) not demonstrable: {sq['reason'] or 'no evidence either way'}.")
    return out


def project_build(project: dict, generated_at: datetime, generated_by: str | None, run: dict | None,
                  results: list[dict], procedures: list[dict], latest_test_runs: dict, issues: list[dict],
                  events: list[dict], alignment: dict | None, spec_documents: list[dict], active_runs: list[dict],
                  writes_enabled: bool, site: dict | None = None, equipment: dict[str, dict] | None = None,
                  acceptance: dict | None = None) -> dict:
    latest = build(project, run, results, issues, active_runs) if run else None
    verdicts = {s: 0 for s in ORDER}
    for e in (latest or {}).get("equipment", []):
        verdicts[e["status"]] += 1
    fpts = []
    for proc in procedures:
        tr = latest_test_runs.get(proc["id"])
        fpts.append({"procedure_id": proc["id"], "name": proc.get("name"), "kind": proc.get("kind"),
                     "status": proc.get("status"), "steps": proc.get("steps") or [], "test_run": tr})
    mappings = (alignment or {}).get("mappings") or []
    waived = waived_keys(issues)
    inconc = [r for r in results if r["status"] == "inconclusive"]
    counts = {"open": sum(1 for i in issues if i.get("status") in ("open", "in_progress", "ready_for_retest")),
              "waived": sum(1 for i in issues if i.get("status") == "risk_accepted"),
              "closed": sum(1 for i in issues if i.get("status") == "closed")}
    counts["other"] = len(issues) - sum(counts.values())  # the parts always sum to the whole
    counts["total"] = len(issues)
    return {
        "project": {"id": project["id"], "name": project["name"], "site_id": project.get("site_id"),
                    "site_name": (site or {}).get("name"), "phase": project.get("phase"), "phases": project.get("phases") or []},
        "generated_at": generated_at.isoformat(),
        "generated_by": generated_by,
        "cxa": (project.get("settings") or {}).get("cxa_name"),
        "spec_documents": [{"filename": d.get("filename"), "sha256": d.get("sha256"), "created_at": d.get("created_at")}
                           for d in spec_documents],
        "verdicts": verdicts,
        "issue_counts": counts,
        "inconclusive": {"total": len(inconc), "waived": sum(1 for r in inconc if (r["check_id"], r["subject"]) in waived)},
        "limitations": limitations(run, results, procedures, latest_test_runs, active_runs, writes_enabled, issues,
                                   equipment_names=equipment is not None),
        "acceptance": acceptance,
        "sequences": sequences(procedures, latest_test_runs),
        "phase_history": [e for e in events if e["action"] == "phase.set"],
        "spec": {"items": len(mappings), "matched": sum(1 for m in mappings if m.get("equip_uri")),
                 "unmatched": sum(1 for m in mappings if not m.get("equip_uri")),
                 "deviations": (alignment or {}).get("deviations") or []},
        "equipment": equipment or {},
        "equipment_names": equipment is not None,
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


def _short(i) -> str:
    return escape(str(i or "")[:8])


def _run_link(run_id) -> str:
    """A run id as text, full id on hover. Not a link: the API's run report
    needs the login's Authorization header, so following a plain link from
    a saved or printed report answers 401."""
    return f'<span title="{escape(str(run_id))}">run {_short(run_id)}</span>' if run_id else "-"


def _tr_link(tr_id) -> str:
    return f'<a href="#tr-{escape(str(tr_id))}">test run {_short(tr_id)}</a>' if tr_id else "-"


def _duration(a, b) -> str:
    da, db = _dt(a), _dt(b)
    return human_duration((db - da).total_seconds()) if da and db else "-"


def _equip_label(uri: str, equipment: dict[str, dict]) -> str:
    e = equipment.get(uri) or {}
    name = e.get("dis") or short_point(uri)
    cls = (e.get("brickClass") or "").replace("_", " ")
    n = len(e.get("points") or [])
    meta = ", ".join(x for x in (cls, f"{n} points" if e else "") if x)
    return f'{escape(name)}{f" <span class=muted>({escape(meta)})</span>" if meta else ""}'


def _fpt_html(fpts: list[dict], equipment: dict[str, dict]) -> str:
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
                rows.append(f'<tr><td>{_equip_label(e["equip_uri"], equipment)}</td><td>{escape(st.get("title") or st["id"])}</td>'
                            f'<td>{_verdict(st["status"])}</td><td>{failing}</td><td class=muted>{seen}</td></tr>')
        by = (tr.get("summary") or {}).get("by_status") or {}
        blocks.append(f'<h3 id="tr-{escape(str(tr["id"]))}">{escape(f["name"] or "")} <span class=muted>({escape(f["kind"] or "")}) · '
                      f'test run {_short(tr["id"])} · {_when(tr.get("started_at"))} to {_when(tr.get("window_end"))} '
                      f'({_duration(tr.get("started_at"), tr.get("window_end"))})</span></h3>'
                      f'<p class=muted>{by.get("fail", 0)} fail · {by.get("inconclusive", 0)} incomplete · {by.get("pass", 0)} pass</p>'
                      '<table><tr><th>Equipment</th><th>Step</th><th>Result</th><th>What failed</th><th>Observed</th></tr>'
                      f'{"".join(rows)}</table>')
    return "".join(blocks)


def _criteria_html(fpts: list[dict]) -> str:
    """Acceptance criteria and results: each rule as the run judged it, the
    window it was judged over, and what every point showed - passing
    points included (Baler#99, #100 review)."""
    rows = []
    for f in fpts:
        tr = f["test_run"]
        crit, edited = recorded_criteria({"steps": f["steps"]}, tr)
        note = ('<br><span class="s inconclusive">procedure edited after this run: the rules shown are the ones this run '
                'judged</span>') if edited else ""
        for r in crit:
            status = _worst(r["judged"]) or ("not_applicable" if r["judged"] else None)
            obs = "<br>".join(escape(_obs_text(o)) for j in r["judged"] for o in (j.get("observed") or [])[:10])
            window = (f'{_when(tr.get("started_at"))} to {_when(tr.get("window_end"))} ({_duration(tr.get("started_at"), tr.get("window_end"))})'
                      if tr else "never run")
            rows.append(f'<tr><td>{escape(f["name"] or "")}<br><span class=muted>{escape(r["title"])}</span>{note}</td>'
                        f'<td>{escape(_rule_text(r["criterion"]))}</td><td class=muted>{window}<br>{_tr_link(tr["id"]) if tr else ""}</td>'
                        f'<td>{_verdict(status) if status else "<span class=muted>-</span>"}</td><td class=muted>{obs}</td></tr>')
            note = ""
    if not rows:
        return "<p class=muted>No procedures, so no acceptance criteria were defined.</p>"
    return ("<table><caption>Each procedure criterion as judged by its latest test run</caption>"
            "<tr><th>Procedure / step</th><th>Criterion</th><th>Judged over</th><th>Result</th><th>Observed</th></tr>"
            + "".join(rows) + "</table>")


def acceptance_availability(measured: dict[str, tuple[int, float]], points: dict[str, dict], start: datetime,
                            end: datetime, settle_minutes: float, min_pct: float, max_gap_minutes: float) -> dict:
    """Per-point availability over the acceptance verification run's window,
    after a settle time - as data, not a phase note (Baler#100 review).
    `measured`: (samples, availability %) per point from judged_from to
    end (repo.availability). expected = the samples a point reporting every
    max_gap would send."""
    from datetime import timedelta

    judged_from = start + timedelta(minutes=settle_minutes)
    span = max((end - judged_from).total_seconds(), 0.0)
    expected = int(span // (max_gap_minutes * 60)) if max_gap_minutes > 0 else 0
    rows = []
    for uri, pt in sorted(points.items()):
        samples, pct = measured.get(uri, (0, 0.0))
        pct = pct if span else 0.0
        rows.append({"subject": uri, "dis": pt.get("dis"), "samples": samples, "expected": expected,
                     "availability_pct": pct, "pass": pct + 1e-9 >= min_pct})
    return {"window_start": start.isoformat(), "window_end": end.isoformat(), "settle_minutes": settle_minutes,
            "judged_from": judged_from.isoformat(), "min_pct": min_pct, "max_gap_minutes": max_gap_minutes, "points": rows}


def _acceptance_html(acc: dict | None, run_id: str | None) -> str:
    if not acc:
        return "<p class=muted>No completed verification run to judge availability over.</p>"
    rows = "".join(f'<tr><td>{escape(r["dis"] or short_point(r["subject"]))}</td><td>{r["samples"]} / {r["expected"]}</td>'
                   f'<td>{r["availability_pct"]:g}%</td><td>{_verdict("pass" if r["pass"] else "fail")}</td></tr>'
                   for r in acc["points"])
    passed = sum(1 for r in acc["points"] if r["pass"])
    return (f'<p>Verification run <a href="#run-latest">{_short(run_id)}</a>: {_when(acc["window_start"])} to {_when(acc["window_end"])} '
            f'({_duration(acc["window_start"], acc["window_end"])}); settle {acc["settle_minutes"]:g} min, so judged from '
            f'{_when(acc["judged_from"])}. Threshold: available at least {acc["min_pct"]:g}% (a sample at least every '
            f'{acc["max_gap_minutes"]:g} min). {passed} of {len(acc["points"])} point(s) meet it.</p>'
            '<table><caption>Per-point availability over the acceptance window</caption>'
            "<tr><th>Point</th><th>Samples / expected</th><th>Availability</th><th>Result</th></tr>" + rows + "</table>")


_NOT_EVALUATED = '<span class="s inconclusive">not evaluated</span>'


def project_html(rep: dict) -> str:
    return _a11y_tables(_project_html(rep))


def _project_html(rep: dict) -> str:
    p = rep["project"]
    v = rep["verdicts"]
    ic = rep["issue_counts"]
    inc = rep["inconclusive"]
    equipment = rep.get("equipment") or {}
    docs = "".join(f'<li>{escape(d["filename"] or "")} <span class=muted>· sha256 (first 12) '
                   f'<code title="{escape(str(d["sha256"] or ""))}">{escape(str(d["sha256"] or "")[:12])}</code> · '
                   f'{_when(d["created_at"])}</span><br><code class="hash">{escape(str(d["sha256"] or ""))}</code></li>'
                   for d in rep["spec_documents"]) or "<li class=muted>none uploaded</li>"
    lim = "".join(f'<li data-kind="{escape(x["kind"])}">{escape(x["text"])}</li>' for x in rep["limitations"]) \
        or "<li>None found in the records.</li>"
    seq = "".join(f'<tr><td>{escape(sq["sequence"] or "-")}</td><td>{escape(sq["step"])}<br><span class=muted>{escape(sq["procedure"] or "")}</span></td>'
                  f'<td>{escape(sq["rule"])}</td><td>{_verdict(sq["status"]) if sq["status"] in ORDER else _NOT_EVALUATED}</td>'
                  f'<td>{_tr_link(sq["test_run_id"])}</td><td class=muted>{escape(sq["reason"] or "")}</td></tr>' for sq in rep["sequences"])
    phase_rows = "".join(
        f'<tr><td>{escape(ph["name"])}</td><td>{escape(ph["status"])}</td><td>{_when(ph.get("started_at"))}</td>'
        f'<td>{_when(ph.get("completed_at"))}</td></tr>' for ph in p["phases"])
    moves = "".join(
        f'<tr><td>{_when(e["ts"])}</td><td>{escape(_actor(e))}</td><td>{escape(str((e.get("detail") or {}).get("from")))} → '
        f'{escape(str((e.get("detail") or {}).get("to")))}</td><td>{escape((e.get("detail") or {}).get("note") or "")}'
        + (" <b>(override)</b>" if (e.get("detail") or {}).get("override") else "")
        + "".join(f'<br><span class=muted>gate: {escape(u["message"])}</span>' for u in ((e.get("detail") or {}).get("gate") or {}).get("unmet", []))
        + ('<br><span class=muted>gate met</span>' if ((e.get("detail") or {}).get("gate") or {}).get("met") else "")
        + "</td></tr>" for e in reversed(rep["phase_history"]))
    sp = rep["spec"]
    devs = "".join(f'<tr><td>{escape(d["kind"].replace("_", " "))}</td><td>{escape(d.get("spec_tag") or ", ".join(d.get("spec_tags") or []) or "")}</td>'
                   f'<td>{_equip_label(d["equip_uri"], equipment) if d.get("equip_uri") else ""}</td><td>{escape(d.get("interpretation") or "")}</td></tr>'
                   for d in sp["deviations"])
    latest = rep["latest_run"]
    run_id = latest["run"]["id"] if latest else None
    latest_html = "<p class=muted>No completed verification run.</p>"
    if latest:
        run = latest["run"]
        eq = "".join(f'<tr><td>{_equip_label(e["equip_uri"], equipment)}<br><span class=muted>{escape(e["equip_uri"])}</span></td>'
                     f'<td>{_verdict(e["status"])}{_waived_count(e)}</td>'
                     f'<td>{e["counts"].get("fail", 0)} fail · {e["counts"].get("inconclusive", 0)} inconclusive · '
                     f'{e["counts"].get("pass", 0)} pass</td></tr>' for e in latest["equipment"])
        latest_html = (f'<p class=muted id="run-latest">Verification run {_short(run["id"])}: window {_when(run.get("window_start"))} to '
                       f'{_when(run.get("window_end"))} ({_duration(run.get("window_start"), run.get("window_end"))}) · finished '
                       f'{_when(run.get("finished_at"))} · phase '
                       f'{escape(str(run.get("phase") or "-"))}</p><table><tr><th>Equipment</th><th>Verdict</th><th>Results</th></tr>'
                       f'{eq}</table><p class=muted>{escape(LADDER_HELP)}</p>')
    findings = ""
    if latest:
        findings = _html(latest, title="Latest verification run").split("<h2>Findings</h2>", 1)[-1].split("</body>", 1)[0]
    # The headline stays honest (incomplete is never green) but a waived
    # inconclusive is accounted for, not a contradiction of the sign-off.
    verdict_line = f'{v.get("fail", 0)} equipment fail · {v.get("inconclusive", 0)} incomplete · {v.get("pass", 0)} pass.'
    if inc["total"]:
        verdict_line += (f' Incomplete because {inc["total"]} check(s) were inconclusive: {inc["waived"]} accepted as risk '
                         f'(see <a href="#waivers">Waivers</a>), {inc["total"] - inc["waived"]} not waived.')
    issue_line = (f'Issues: {ic["open"]} open · {ic["waived"]} waived (risk accepted) · {ic["closed"]} closed'
                  + (f' · {ic["other"]} other' if ic.get("other") else "") + f' (of {ic.get("total", 0)}).')
    site = escape(p.get("site_name") or "-")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Commissioning report - {escape(p["name"])}</title>
<style>{_CSS}
.sign td {{ height: 2.2em; }} code.hash {{ font-size: 8pt; color: #555; word-break: break-all; user-select: all; }}
footer {{ margin-top: 2em; font-size: 8.5pt; color: #555; border-top: 1px solid #999; padding-top: .4em; }}
@media print {{ tr, .signoff {{ break-inside: avoid; }} thead {{ display: table-header-group; }} h2, h3 {{ break-after: avoid; }} }}
</style></head><body>
<h1>Commissioning report: {escape(p["name"])}</h1>
<p>Site <b>{site}</b> <span class=muted>({escape(str(p.get("site_id") or "-"))})</span> · current phase {escape(str(p.get("phase") or "-"))}<br>
<span class=muted>Generated {_when(rep["generated_at"])} by {escape(rep["generated_by"] or "-")}{f" from verification run {_short(run_id)}" if run_id else ""}.
All times UTC. Trend links open Baler's Query page and need a Baler login.</span><br>
Commissioning authority: {escape(rep["cxa"] or "________________")}</p>
<h3>Specification documents</h3><ul>{docs}</ul>
<h2>Summary</h2>
<p>{verdict_line}</p>
<p>{issue_line}</p>
<p class="muted">Verdicts are computed from rules over the recorded data. Any fail fails; any inconclusive leaves it
incomplete - never a pass - whether or not it was waived.</p>
<h2>Scope and limitations</h2><ul>{lim}</ul>
<h2>Sequences of operation</h2>
{"<table><caption>Each sequence-of-operation criterion and where it ran</caption><tr><th>Tag</th><th>Criterion</th><th>Rule</th><th>Result</th><th>Where it ran</th><th>Reason</th></tr>" + seq + "</table>" if seq else "<p class=muted>No sequence criteria defined.</p>"}
<h2>Acceptance criteria and results</h2>
<h3>Availability over the acceptance verification run</h3>
{_acceptance_html(rep.get("acceptance"), run_id)}
<h3>Procedure criteria</h3>
{_criteria_html(rep["procedures"])}
<h2>Phase history</h2>
<table><tr><th>Phase</th><th>Status</th><th>Started</th><th>Completed</th></tr>{phase_rows}</table>
<table><tr><th>When</th><th>Who</th><th>Move</th><th>Note / gate</th></tr>{moves or "<tr><td colspan=4 class=muted>no phase moves recorded</td></tr>"}</table>
<h2>Specification vs installed</h2>
<p>{sp["items"]} accepted spec equipment item(s): {sp["matched"]} matched in the field, {sp["unmatched"]} not found.</p>
{"<table><tr><th>Deviation</th><th>Spec</th><th>Field</th><th>Interpretation</th></tr>" + devs + "</table>" if devs else "<p class=muted>No deviations.</p>"}
<h2>Equipment verdicts (latest verification run)</h2>
{latest_html}
<h2>Procedure / functional test results</h2>
{_fpt_html(rep["procedures"], equipment)}
{issues_html(rep["issues"], rep.get("cxa"))}
<h2>Findings (latest verification run)</h2>
{findings}
<div class="signoff">
<h2>Sign-off</h2>
<p class=muted>Report generated {_when(rep["generated_at"])}{f" from verification run {_short(run_id)}" if run_id else ""}. Verdicts above are computed, not typed.</p>
<table class="sign"><tr><th>Role</th><th>Name</th><th>Signature</th><th>Date</th></tr>
<tr><td>Commissioning authority</td><td>{escape(rep["cxa"] or "")}</td><td></td><td></td></tr>
<tr><td>Owner's representative</td><td></td><td></td><td></td></tr>
<tr><td>Contractor</td><td></td><td></td><td></td></tr></table>
</div>
<footer>Baler commissioning report · {escape(p["name"])} · {site} · generated {_when(rep["generated_at"])}</footer>
</body></html>
"""

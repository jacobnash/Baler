"""
Readiness per space, zone or suite (Baler#210, epic #221 §4).

"Is the surgery suite ready for patients?" is this report for the suite's
rooms. A room is ready when, and only when:

1. requirements: every applicable requirement passes over its window;
2. tests: every piece of equipment serving it (its terminals and, by
   brick:feeds, what feeds them) has passed its latest start-up or
   functional test (Cx 6);
3. tab_report: the TAB report is linked to the room;
4. issues: every open issue on that equipment or the room's own points is
   resolved, or explicitly accepted as a risk (Cx 4).

Each criterion is pass, fail or inconclusive, with its evidence. No
evidence is never a pass (house rule since PR #5): a room with no
requirement evaluated, or equipment no test has covered, is
`inconclusive`, and the room's verdict is `incomplete`, not `ready`.
Verdicts: any fail -> not_ready; else any inconclusive -> incomplete;
else ready. A suite is ready when all its rooms are.

Requirement results come from REQUIREMENT_SOURCES, functions
(repo, project, space, now) -> [{id, title, citation, status, evidence}].
Requirement packs (#205) and continuous compliance (#207) register one.
None is registered in this slice, so requirements are inconclusive.

The TAB report link is an interim field, `attributes.tab_report`
({title, document_id?, date?}) set on the space by a person, until the
document store (#201) links documents to spaces. DOCUMENT_SOURCES lets
that store answer instead.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from html import escape

from baler.commissioning import procedures, spaces
from baler.commissioning.report import _CSS, _a11y_tables

CRITERIA = ("requirements", "tests", "tab_report", "issues")
BLOCKING_ISSUES = ("open", "in_progress", "ready_for_retest")
TEST_KINDS = ("startup", "functional", "power_on")

REQUIREMENT_SOURCES: list[Callable[..., list[dict]]] = []
DOCUMENT_SOURCES: list[Callable[..., list[dict]]] = []


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def worst(statuses) -> str:
    s = list(statuses)
    if "fail" in s:
        return "fail"
    if "inconclusive" in s or not s:
        return "inconclusive"
    return "pass"


def verdict(status: str) -> str:
    return {"pass": "ready", "fail": "not_ready"}.get(status, "incomplete")


def requirements_criterion(results: list[dict]) -> dict:
    if not results:
        return {"status": "inconclusive", "items": [],
                "note": "no requirement has been evaluated for this room (no requirement pack applies yet)"}
    return {"status": worst(r["status"] for r in results), "items": results, "note": None}


def tests_criterion(chain: list[dict], procs: list[dict], latest: dict[str, dict | None]) -> dict:
    """The latest completed test of every active start-up / functional
    procedure, for each piece of serving equipment."""
    if not chain:
        return {"status": "inconclusive", "items": [], "note": "no serving equipment is recorded for this room"}
    items = []
    for link in chain:
        uri = link["equip_uri"]
        judged = []
        for p in procs:
            if p.get("status") != "active" or p.get("kind") not in TEST_KINDS:
                continue
            tr = latest.get(p["id"])
            row = next((e for e in (tr or {}).get("equipment") or [] if e.get("equip_uri") == uri), None)
            if row is not None and tr is not None:
                judged.append({"procedure_id": p["id"], "procedure": p["name"], "kind": p["kind"], "test_run_id": tr["id"],
                               "finished_at": _iso(tr.get("finished_at")), "status": row["status"]})
        status = procedures.worst(j["status"] for j in judged) if judged else "inconclusive"
        if status == "not_applicable":
            status = "inconclusive"
        items.append({"equip_uri": uri, "level": link["level"], "status": status, "tests": judged,
                      "note": None if judged else "no completed start-up or functional test covers this equipment"})
    return {"status": worst(i["status"] for i in items), "items": items, "note": None}


def tab_criterion(links: list[dict]) -> dict:
    if not links:
        return {"status": "inconclusive", "items": [], "note": "no TAB report is linked to this room"}
    return {"status": "pass", "items": links, "note": None}


def issues_criterion(issues: list[dict], equip_uris: set[str], point_uris: set[str]) -> dict:
    mine = [i for i in issues if i.get("equip_uri") in equip_uris or i.get("subject") in point_uris]
    blocking = [i for i in mine if i["status"] in BLOCKING_ISSUES]
    accepted = [i for i in mine if i["status"] == "risk_accepted"]
    items = [{"issue_id": i["id"], "title": i.get("title"), "status": i["status"], "equip_uri": i.get("equip_uri"),
              "subject": i.get("subject")} for i in blocking + accepted]
    return {"status": "fail" if blocking else "pass", "items": items,
            "note": f"{len(blocking)} open, {len(accepted)} accepted as risks" if mine else None}


def tab_links(space: dict) -> list[dict]:
    tab = (space.get("attributes") or {}).get("tab_report")
    if isinstance(tab, dict) and tab.get("title"):
        return [{"source": "space.attributes.tab_report", **tab}]
    return []


def room_report(repo, project: dict, space: dict, now: datetime, allows, procs: list[dict],
                latest: dict[str, dict | None], issues: list[dict]) -> dict:
    serving = [r["equip_uri"] for r in repo.find_rows("cx_space_serving", space_id=space["id"])]
    chain = spaces.serving_chain(repo.store, serving, allows) if serving and repo.store is not None else []
    reqs = [r for src in REQUIREMENT_SOURCES for r in src(repo, project, space, now)]
    docs = tab_links(space) + [d for src in DOCUMENT_SOURCES for d in src(repo, project, space, "tab_report")]
    crit = {
        "requirements": requirements_criterion(reqs),
        "tests": tests_criterion(chain, procs, latest),
        "tab_report": tab_criterion(docs),
        "issues": issues_criterion(issues, {c["equip_uri"] for c in chain}, set(space.get("point_uris") or [])),
    }
    status = worst(c["status"] for c in crit.values())
    return {"space_id": space["id"], "name": space["name"], "space_type": space.get("space_type"), "status": status,
            "verdict": verdict(status), "serving_chain": chain, "criteria": crit}


def build(repo, project: dict, target: dict, all_spaces: list[dict], now: datetime, allows=lambda uri: True) -> dict:
    """The readiness report for `target` and every room under it."""
    under = spaces.descendants(target["id"], all_spaces)
    rooms = [s for s in [target, *under] if s.get("kind") == "room"]
    procs = repo.list_procedures(project["id"])
    latest = repo.latest_completed_test_runs([p["id"] for p in procs]) if procs else {}
    issues = repo.list_issues(project["id"])
    per = [room_report(repo, project, r, now, allows, procs, latest, issues) for r in rooms]
    status = worst(r["status"] for r in per) if per else "inconclusive"
    counts = {v: sum(1 for r in per if r["verdict"] == v) for v in ("ready", "incomplete", "not_ready")}
    limitations = []
    if not per:
        limitations.append("no rooms under this space: nothing could be judged")
    if not REQUIREMENT_SOURCES:
        limitations.append("no requirement packs are applied yet, so requirements are inconclusive for every room")
    return {"project": {"id": project["id"], "name": project["name"]},
            "space": {"id": target["id"], "name": target["name"], "kind": target.get("kind")},
            "generated_at": now.isoformat(), "status": status, "verdict": verdict(status), "counts": counts,
            "rooms": per, "limitations": limitations}


_VERDICT = {"ready": "READY", "incomplete": "INCOMPLETE - no evidence either way", "not_ready": "NOT READY"}
_TITLES = {"requirements": "Requirements", "tests": "Required tests", "tab_report": "TAB report", "issues": "Issues"}


def _item(kind: str, i: dict) -> str:
    if kind == "requirements":
        return f'{escape(str(i.get("title") or i.get("id")))} - {escape(str(i.get("citation") or ""))}: {escape(i["status"])}'
    if kind == "tests":
        tests = "; ".join(f'{escape(t["procedure"])} ({escape(t["status"])})' for t in i["tests"]) or escape(i.get("note") or "")
        return f'{escape(i["equip_uri"])}: {escape(i["status"])} - {tests}'
    if kind == "tab_report":
        return escape(str(i.get("title")))
    return f'{escape(str(i.get("title") or i["issue_id"]))} ({escape(i["status"])})'


def html(rep: dict) -> str:
    rows = []
    blocks = []
    for r in rep["rooms"]:
        cells = "".join(f'<td>{escape(r["criteria"][k]["status"])}</td>' for k in CRITERIA)
        rows.append(f'<tr><td>{escape(r["name"])}</td><td>{escape(str(r.get("space_type") or "-"))}</td>'
                    f'<td>{escape(_VERDICT[r["verdict"]])}</td>{cells}</tr>')
        parts = []
        for k in CRITERIA:
            c = r["criteria"][k]
            lis = "".join(f"<li>{_item(k, i)}</li>" for i in c["items"])
            note = f' <span class="muted">{escape(c["note"])}</span>' if c.get("note") else ""
            parts.append(f"<h4>{_TITLES[k]}: {escape(c['status'])}{note}</h4><ul>{lis}</ul>")
        blocks.append(f'<h3>{escape(r["name"])} - {escape(_VERDICT[r["verdict"]])}</h3>{"".join(parts)}')
    lim = "".join(f"<li>{escape(x)}</li>" for x in rep["limitations"])
    heads = "".join(f"<th>{_TITLES[k]}</th>" for k in CRITERIA)
    c = rep["counts"]
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Readiness - {escape(rep["space"]["name"])}</title>
<style>{_CSS}</style></head><body>
<h1>Readiness: {escape(rep["space"]["name"])}</h1>
<p class="muted">Project {escape(rep["project"]["name"])} · generated {escape(rep["generated_at"])}</p>
<p><strong>{escape(_VERDICT[rep["verdict"]])}</strong> · {c["ready"]} ready · {c["incomplete"]} incomplete · {c["not_ready"]} not ready</p>
<p class="muted">Ready means every applicable requirement passes over its window, the required tests passed, the TAB report
is linked, and every open issue is resolved or accepted as a risk. No evidence is never counted as a pass.</p>
{f"<h2>Limitations</h2><ul>{lim}</ul>" if lim else ""}
<h2>Rooms</h2>
<table><caption>One verdict per room</caption><tr><th>Room</th><th>Type</th><th>Verdict</th>{heads}</tr>{"".join(rows)}</table>
<h2>Evidence</h2>
{"".join(blocks)}
</body></html>
"""
    return _a11y_tables(page)

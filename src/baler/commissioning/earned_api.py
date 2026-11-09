"""
HTTP routes for linking schedule activities to evidence and the
earned-evidence report (Baler#218), under /cx/.
"""

from __future__ import annotations

import re
from datetime import datetime

from baler.commissioning import db, earned_evidence, inventory, runner, schedules_api
from baler.commissioning.routes import Route, owned_row, query
from baler.http_handler_base import respond_json

LINKS = "cx_schedule_links"
SIGNOFFS = "cx_evidence_signoffs"
db.ROW_TABLES[LINKS] = (("id", "project_id", "activity_code", "kind", "params", "note", "created_by", "created_at"),
                        frozenset({"params"}))
db.ROW_TABLES[SIGNOFFS] = (("id", "project_id", "link_id", "signed_by", "signed_by_email", "signed_at", "document", "note"),
                           frozenset({"document"}))


def _equips(h, project: dict, claims: dict) -> list[dict]:
    scopes, _ = runner.project_scopes(h.REPO, project, claims)
    return inventory.build(h.REPO.store, scopes) if h.REPO.store is not None else []


def _add_link(h, span, claims, pid) -> None:
    """{activity_code, kind, params, note?}: a person links an activity to evidence."""
    if h._project(pid, claims) is None:
        return
    b = h.read_json_body()
    code = b.get("activity_code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("activity_code is the schedule's activity code")
    kind = b.get("kind")
    params = earned_evidence.check_params(kind, b.get("params") or {})
    if kind == "procedure_passed":
        proc = h.REPO.get_procedure(params["procedure_id"])
        if proc is None or proc["project_id"] != pid:
            raise ValueError("procedure_id is a procedure of this project")
    row = {"id": db.new_id(), "project_id": pid, "activity_code": code.strip(), "kind": kind, "params": params,
           "note": str(b.get("note") or "")[:300] or None, "created_by": claims.get("sub"), "created_at": h.NOW()}
    h.REPO.put_rows(LINKS, [row])
    h._event(pid, "schedule.link", claims, {"link_id": row["id"], "activity_code": row["activity_code"], "kind": kind})
    respond_json(h, 201, db.public_row(row))


def _list_links(h, span, claims, pid) -> None:
    if h._project(pid, claims) is not None:
        respond_json(h, 200, [db.public_row(r) for r in h.REPO.find_rows(LINKS, project_id=pid)])


def _delete_link(h, span, claims, lid) -> None:
    hit = owned_row(h, LINKS, lid, claims, "link")
    if not hit:
        return
    h.REPO.delete_rows(SIGNOFFS, link_id=lid)
    h.REPO.delete_rows(LINKS, id=lid)
    h._event(hit[0]["project_id"], "schedule.unlink", claims, {"link_id": lid, "activity_code": hit[0]["activity_code"]})
    h.send_response(204)
    h.end_headers()


def _sign(h, span, claims, lid) -> None:
    """{document?: {title, ref?}, note?}: a person signs off a form_signed link."""
    hit = owned_row(h, LINKS, lid, claims, "link")
    if not hit:
        return
    link = hit[0]
    if link["kind"] != "form_signed":
        raise ValueError("only a form_signed link is signed off by a person; the others are judged from data")
    b = h.read_json_body()
    doc = b.get("document")
    if doc is not None and (not isinstance(doc, dict) or not isinstance(doc.get("title"), str)):
        raise ValueError("document is {title, ref?}")
    sub = claims.get("sub")
    row = {"id": db.new_id(), "project_id": link["project_id"], "link_id": lid, "signed_by": sub,
           "signed_by_email": h.REPO.user_email(sub), "signed_at": h.NOW(), "document": doc,
           "note": str(b.get("note") or "")[:300] or None}
    h.REPO.put_rows(SIGNOFFS, [row])
    h._event(link["project_id"], "schedule.signoff", claims, {"link_id": lid, "activity_code": link["activity_code"]})
    respond_json(h, 201, db.public_row(row))


def _report(h, span, claims, sid) -> None:
    """?as_of= (default now): the earned-evidence report for a schedule version."""
    hit = owned_row(h, schedules_api.SCHEDULES, sid, claims, "schedule")
    if not hit:
        return
    sched, project = hit
    q = query(h)
    as_of = datetime.fromisoformat(q["as_of"][0]) if q.get("as_of") else h.NOW()
    acts = h.REPO.find_rows(schedules_api.ACTIVITIES, order="planned_start", schedule_id=sid)
    links = h.REPO.find_rows(LINKS, project_id=project["id"])
    equips = {e["id"]: e for e in _equips(h, project, claims)} if any(ln["kind"] == "points_online" for ln in links) else {}
    point_ids = sorted({p["id"] for e in equips.values() for p in e["points"]})
    with h.REPO.history() as hist:
        latest = hist.latest(point_ids, before=as_of) if point_ids else {}
    proc_ids = sorted({ln["params"]["procedure_id"] for ln in links if ln["kind"] == "procedure_passed"})
    runs = h.REPO.latest_completed_test_runs(proc_ids) if proc_ids else {}
    judged = {}
    for ln in links:
        if ln["kind"] == "points_online":
            judged[ln["id"]] = earned_evidence.judge_points(ln["params"], equips, latest, as_of)
        elif ln["kind"] == "procedure_passed":
            pid = ln["params"]["procedure_id"]
            judged[ln["id"]] = earned_evidence.judge_procedure(ln["params"], h.REPO.get_procedure(pid), runs.get(pid))
        else:
            judged[ln["id"]] = earned_evidence.judge_form(h.REPO.find_rows(SIGNOFFS, order="signed_at", link_id=ln["id"]))
    rep = earned_evidence.report(acts, links, judged, as_of)
    respond_json(h, 200, {"schedule": db.public_row(sched), **rep})


def _suggest(h, span, claims, sid) -> None:
    """Proposed links (never applied): activity names that name equipment."""
    hit = owned_row(h, schedules_api.SCHEDULES, sid, claims, "schedule")
    if not hit:
        return
    _sched, project = hit
    acts = h.REPO.find_rows(schedules_api.ACTIVITIES, order="planned_start", schedule_id=sid)
    have = {(ln["activity_code"], ln["kind"]) for ln in h.REPO.find_rows(LINKS, project_id=project["id"])}
    out = [s for s in earned_evidence.suggest(acts, _equips(h, project, claims), h.REPO.list_procedures(project["id"]))
           if (s["activity_code"], s["kind"]) not in have]
    respond_json(h, 200, out)


ROUTES: list[Route] = [
    ("POST", re.compile(r"/projects/([^/]+)/schedule-links"), "add_schedule_link", _add_link),
    ("GET", re.compile(r"/projects/([^/]+)/schedule-links"), "list_schedule_links", _list_links),
    ("DELETE", re.compile(r"/schedule-links/([^/]+)"), "delete_schedule_link", _delete_link),
    ("POST", re.compile(r"/schedule-links/([^/]+)/signoff"), "sign_schedule_link", _sign),
    ("GET", re.compile(r"/schedules/([^/]+)/earned-evidence"), "earned_evidence", _report),
    ("GET", re.compile(r"/schedules/([^/]+)/link-suggestions"), "schedule_link_suggestions", _suggest),
]

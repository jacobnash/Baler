"""
HTTP routes for construction schedules (Baler#217), under /cx/. An upload
(P6 XER or MS Project XML) becomes a schedule version with its activities;
a re-import is a new version, the old one is kept (the earned-evidence
report, #218, compares against whichever version you pick).
"""

from __future__ import annotations

import base64
import hashlib
import re

from baler import schedule_import
from baler.commissioning import db
from baler.commissioning.routes import Route, owned_row, query
from baler.http_handler_base import respond_json

SCHEDULES = "cx_schedules"
ACTIVITIES = "cx_schedule_activities"

db.ROW_TABLES[SCHEDULES] = (("id", "project_id", "filename", "sha256", "format", "source_project", "data_date", "activities",
                             "info", "created_by", "created_at"), frozenset({"info"}))
db.ROW_TABLES[ACTIVITIES] = (("id", "schedule_id", "project_id", "code", "name", "wbs", "planned_start", "planned_finish",
                              "actual_start", "actual_finish", "percent_complete", "status", "milestone", "summary",
                              "predecessors"), frozenset({"predecessors"}))


def _import(h, span, claims, pid) -> None:
    """{filename, data: base64 of the .xer or .xml}."""
    if h._project(pid, claims) is None:
        return
    body = h.read_json_body()
    try:
        raw = base64.b64decode(str(body.get("data") or ""), validate=True)
    except ValueError as exc:
        raise ValueError("data must be the schedule file as base64") from exc
    acts, info = schedule_import.parse(raw)
    now = h.NOW()
    sched = {"id": db.new_id(), "project_id": pid, "filename": str(body.get("filename") or "schedule")[:200],
             "sha256": hashlib.sha256(raw).hexdigest(), "format": info["format"], "source_project": info.get("project"),
             "data_date": info.get("data_date"), "activities": len(acts), "info": {"tables": info.get("tables")},
             "created_by": claims.get("sub"), "created_at": now}
    h.REPO.put_rows(SCHEDULES, [sched])
    h.REPO.put_rows(ACTIVITIES, [{"id": db.new_id(), "schedule_id": sched["id"], "project_id": pid, **a} for a in acts])
    h._event(pid, "schedule.import", claims, {"schedule_id": sched["id"], "filename": sched["filename"],
                                             "format": sched["format"], "activities": len(acts)})
    span.set_attribute("activities", len(acts))
    respond_json(h, 201, db.public_row(sched))


def _list(h, span, claims, pid) -> None:
    if h._project(pid, claims) is not None:
        rows = h.REPO.find_rows(SCHEDULES, project_id=pid)
        respond_json(h, 200, [db.public_row(r) for r in reversed(rows)])


def _activities(h, span, claims, sid) -> None:
    """?q= (code or name contains), status=, milestones=true."""
    if not owned_row(h, SCHEDULES, sid, claims, "schedule"):
        return
    q = query(h)
    rows = h.REPO.find_rows(ACTIVITIES, order="planned_start", schedule_id=sid)
    if q.get("q"):
        needle = q["q"][0].lower()
        rows = [r for r in rows if needle in (r["code"] or "").lower() or needle in (r["name"] or "").lower()]
    if q.get("status"):
        rows = [r for r in rows if r["status"] == q["status"][0]]
    if q.get("milestones", ["false"])[0] == "true":
        rows = [r for r in rows if r["milestone"]]
    respond_json(h, 200, [db.public_row(r) for r in rows])


ROUTES: list[Route] = [
    ("POST", re.compile(r"/projects/([^/]+)/construction-schedules"), "import_schedule", _import),
    ("GET", re.compile(r"/projects/([^/]+)/construction-schedules"), "list_schedules", _list),
    ("GET", re.compile(r"/construction-schedules/([^/]+)/activities"), "schedule_activities", _activities),
]

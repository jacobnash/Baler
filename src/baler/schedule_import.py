"""
Construction schedules: Primavera P6 XER and MS Project XML (MSPDI) to a
list of activities with planned and actual dates (Baler#217, epic #221 §9).

Both parsers are clean-room and stdlib only:

- XER is P6's tab-separated export: `%T <table>` starts a table, `%F`
  names its fields, each `%R` is a row, `%E` ends the file. We read TASK
  (activities), PROJWBS (WBS names) and TASKPRED (logic links). PyP6Xer
  (LGPL-2.1) is not used, so no extra dependency or licence question.
- MSPDI is Project's XML (namespace http://schemas.microsoft.com/project):
  Project/Tasks/Task with UID, ID, Name, WBS, Start, Finish, ActualStart,
  ActualFinish, PercentComplete, Summary, Milestone, OutlineLevel and
  PredecessorLink. A document with a DOCTYPE is refused (no entity tricks).

An activity is:

    {code, name, wbs, planned_start, planned_finish, actual_start,
     actual_finish, percent_complete, status, milestone, summary,
     predecessors: [code, ...]}

Dates are ISO strings as the file states them (schedules are local wall
time; neither format reliably carries a zone, so none is invented).
`status` is not_started | in_progress | complete, from the actual dates
(P6's status_code agrees when present).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime

MAX_BYTES = 50 * 1024 * 1024
MAX_ACTIVITIES = 50000
MSPDI_NS = "{http://schemas.microsoft.com/project}"
P6_STATUS = {"TK_NotStart": "not_started", "TK_Active": "in_progress", "TK_Complete": "complete"}


class ScheduleError(ValueError):
    pass


def _date(v: str | None) -> str | None:
    """'2026-03-02 08:00' / '2026-03-02T08:00:00' -> ISO, or None. Wall
    time as the schedule states it: no zone is invented."""
    v = (v or "").strip()
    if not v:
        return None
    try:
        return datetime.fromisoformat(v[:19]).isoformat()
    except ValueError as exc:
        raise ScheduleError(f"unreadable date {v!r}") from exc


def _status(actual_start, actual_finish) -> str:
    return "complete" if actual_finish else ("in_progress" if actual_start else "not_started")


def detect(data: bytes) -> str:
    head = data[:4096].lstrip(b"\xef\xbb\xbf").lstrip()
    if head.startswith((b"ERMHDR", b"%T")) or b"\n%T\t" in data[:65536]:
        return "xer"
    if head.startswith((b"<?xml", b"<Project")):
        return "mspdi"
    raise ScheduleError("not a P6 XER or MS Project XML (MSPDI) file")


def parse(data: bytes) -> tuple[list[dict], dict]:
    """(activities, info{format, project, data_date, counts})."""
    if len(data) > MAX_BYTES:
        raise ScheduleError(f"schedule is over {MAX_BYTES // (1024 * 1024)} MB")
    fmt = detect(data)
    acts, info = parse_xer(data) if fmt == "xer" else parse_mspdi(data)
    if len(acts) > MAX_ACTIVITIES:
        raise ScheduleError(f"more than {MAX_ACTIVITIES} activities")
    return acts, {"format": fmt, **info}


# ------------------------------------------------------------------- XER


def xer_tables(text: str) -> dict[str, list[dict]]:
    tables: dict[str, list[dict]] = {}
    name: str | None = None
    fields: list[str] = []
    for line in text.splitlines():
        parts = line.split("\t")
        tag = parts[0]
        if tag == "%T":
            name, fields = parts[1].strip(), []
            tables.setdefault(name, [])
        elif tag == "%F":
            fields = [f.strip() for f in parts[1:]]
        elif tag == "%R" and name is not None:
            vals = parts[1:] + [""] * (len(fields) - len(parts) + 1)
            tables[name].append(dict(zip(fields, vals, strict=False)))
        elif tag == "%E":
            break
    return tables


def parse_xer(data: bytes) -> tuple[list[dict], dict]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:  # P6 writes the Windows code page by default
        text = data.decode("cp1252", errors="replace")
    t = xer_tables(text)
    if "TASK" not in t:
        raise ScheduleError("the XER has no TASK table")
    wbs = {r.get("wbs_id"): r.get("wbs_name") or r.get("wbs_short_name") for r in t.get("PROJWBS", [])}
    code_by_id = {r.get("task_id"): r.get("task_code") for r in t["TASK"]}
    preds: dict[str, list[str]] = {}
    for r in t.get("TASKPRED", []):
        if r.get("pred_task_id") in code_by_id:
            preds.setdefault(r.get("task_id") or "", []).append(code_by_id[r["pred_task_id"]] or "")
    acts = []
    for r in t["TASK"]:
        a_s, a_f = _date(r.get("act_start_date")), _date(r.get("act_end_date"))
        acts.append({
            "code": (r.get("task_code") or r.get("task_id") or "").strip(), "name": (r.get("task_name") or "").strip(),
            "wbs": wbs.get(r.get("wbs_id")),
            "planned_start": _date(r.get("target_start_date") or r.get("early_start_date")),
            "planned_finish": _date(r.get("target_end_date") or r.get("early_end_date")),
            "actual_start": a_s, "actual_finish": a_f,
            "percent_complete": _pct(r.get("phys_complete_pct")),
            "status": P6_STATUS.get(r.get("status_code") or "", _status(a_s, a_f)),
            "milestone": (r.get("task_type") or "") in ("TT_Mile", "TT_FinMile"), "summary": False,
            "predecessors": preds.get(r.get("task_id") or "", []),
        })
    proj = (t.get("PROJECT") or [{}])[0]
    return acts, {"project": proj.get("proj_short_name"), "data_date": _date(proj.get("last_recalc_date")),
                  "tables": {k: len(v) for k, v in t.items()}}


def _pct(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


# ----------------------------------------------------------------- MSPDI


def parse_mspdi(data: bytes) -> tuple[list[dict], dict]:
    if b"<!DOCTYPE" in data[:65536] or b"<!ENTITY" in data[:65536]:
        raise ScheduleError("XML with a DOCTYPE/ENTITY isn't accepted")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ScheduleError(f"not well-formed XML: {exc}") from exc
    ns = MSPDI_NS if root.tag.startswith(MSPDI_NS) else ""
    if root.tag != f"{ns}Project":
        raise ScheduleError("the XML root isn't an MS Project <Project>")

    def txt(el, tag):
        x = el.find(f"{ns}{tag}")
        return x.text if x is not None and x.text is not None else None

    tasks = root.find(f"{ns}Tasks")
    rows = list(tasks.findall(f"{ns}Task")) if tasks is not None else []
    code_by_uid = {txt(r, "UID"): (txt(r, "WBS") or txt(r, "ID") or txt(r, "UID")) for r in rows}
    acts = []
    for r in rows:
        if txt(r, "IsNull") == "1" or txt(r, "UID") == "0":  # the project summary task
            continue
        a_s, a_f = _date(txt(r, "ActualStart")), _date(txt(r, "ActualFinish"))
        preds = [code_by_uid.get(txt(p, "PredecessorUID")) or "" for p in r.findall(f"{ns}PredecessorLink")]
        acts.append({
            "code": code_by_uid[txt(r, "UID")] or "", "name": (txt(r, "Name") or "").strip(), "wbs": txt(r, "WBS"),
            "planned_start": _date(txt(r, "Start")), "planned_finish": _date(txt(r, "Finish")),
            "actual_start": a_s, "actual_finish": a_f, "percent_complete": _pct(txt(r, "PercentComplete")),
            "status": _status(a_s, a_f), "milestone": txt(r, "Milestone") == "1", "summary": txt(r, "Summary") == "1",
            "predecessors": [p for p in preds if p],
        })
    return acts, {"project": txt(root, "Name") or txt(root, "Title"), "data_date": _date(txt(root, "StatusDate")),
                  "tables": {"Task": len(rows)}}

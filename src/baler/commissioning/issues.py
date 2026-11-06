"""
The commissioning issues log and accepted risks (Cx 4, Baler#40).

Rules (todo/commissioning-plan.md §6.1):

  - An issue tied to a check (check_id + subject) closes only on
    evidence: a later completed run where that same check passes for that
    same subject. An inconclusive result never closes one, and no one can
    close it by hand. Issues with no check (manual, imported) close by
    hand, with an event.
  - When the check fails again on an issue marked ready_for_retest, it
    goes back to open: the retest failed.
  - An accepted risk is not a fault and not a closed issue: it has an
    owner, a reason and a review_by date. While it stands, the check
    failing again is recorded against the risk ("covered"), not reopened.
    Once review_by passes, the issue reopens.

Pure functions over dicts; commissioning_api and the runner persist what
they return. External items (CSV/JSON from CxAlloy, Facility Grid, a
spreadsheet) keep their source system and id, and re-importing the same
file updates rather than duplicates.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime

STATUSES = ("open", "in_progress", "ready_for_retest", "closed", "risk_accepted")
ACTIVE = ("open", "in_progress", "ready_for_retest", "risk_accepted")
MANUAL_TARGETS = ("open", "in_progress", "ready_for_retest", "closed")
SEVERITIES = ("critical", "major", "minor", "info")
MAX_TITLE = 300
MAX_TEXT = 5000
MAX_IMPORT = 5000


class Conflict(Exception):
    """A state change the rules forbid (answered 409)."""


def _text(v, limit: int = MAX_TEXT) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s[:limit] if s else None


def check_severity(v) -> str | None:
    if v is None or v == "":
        return None
    if v not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
    return v


def transition(issue: dict, target: str) -> None:
    """Raises Conflict/ValueError if a person may not move `issue` to
    `target` through PATCH."""
    if target not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(MANUAL_TARGETS)}")
    if target == "risk_accepted":
        raise Conflict("accept a risk with POST /issues/{id}/risk (admin)")
    if issue["status"] == "risk_accepted":
        raise Conflict("this issue is under an accepted risk - revoke the risk (DELETE /risks/{id}) first")
    if target == "closed" and issue.get("check_id"):
        raise Conflict("an issue tied to a check closes on evidence: a later run where "
                       f"{issue['check_id']} passes for {issue['subject']}")


def title_for(result: dict) -> str:
    what = result.get("symptom") or f"{result['check_id']} {result['status']}"
    equip = str(result.get("equip_uri") or "").replace("urn:equip:", "", 1)
    return f"{equip}: {what}"[:MAX_TITLE]


def from_result(result: dict, run_id: str, project_id: str, new_id, now: datetime, user_sub: str | None) -> dict:
    sev = "major" if result.get("severity") == "required" else ("minor" if result.get("severity") else None)
    return {
        "id": new_id(), "project_id": project_id, "equip_uri": result["equip_uri"], "check_id": result["check_id"],
        "subject": result["subject"], "status": "open", "title": title_for(result), "severity": sev,
        "assignee": None, "source": "baler", "source_id": None,
        "detail": {"symptom": result.get("symptom"), "candidates": result.get("candidates") or [],
                   "evidence": result.get("evidence") or {}},
        "opened_run": run_id, "last_seen_run": run_id, "closed_run": None, "created_by": user_sub,
        "created_at": now, "updated_at": now, "closed_at": None,
    }


def reconcile(issues: list[dict], risks: dict[str, dict], results: list[dict], run_id: str, now: datetime,
              today: date) -> list[tuple[str, dict, str, dict]]:
    """What a completed run changes in the log: [(issue_id, changes,
    event_action, event_detail)]. `issues` are the project's active
    issues, `risks` maps issue id -> its standing risk. Only issues whose
    (check_id, subject) the run judged are touched."""
    by_key = {(r["check_id"], r["subject"]): r for r in results}
    out: list[tuple[str, dict, str, dict]] = []
    for issue in issues:
        risk = risks.get(issue["id"])
        if issue["status"] == "risk_accepted" and risk and _due(risk["review_by"]) < today:
            out.append((issue["id"], {"status": "open", "updated_at": now}, "risk.expired",
                        {"risk_id": risk["id"], "review_by": str(risk["review_by"])}))
            issue = {**issue, "status": "open"}
            risk = None
        if not issue.get("check_id"):
            continue
        r = by_key.get((issue["check_id"], issue["subject"]))
        if r is None:
            continue
        if r["status"] == "pass":
            out.append((issue["id"], {"status": "closed", "closed_run": run_id, "closed_at": now, "last_seen_run": run_id,
                                      "updated_at": now}, "close.evidence", {"run_id": run_id}))
        elif r["status"] == "fail":
            changes: dict = {"last_seen_run": run_id, "updated_at": now}
            if issue["status"] == "risk_accepted":
                out.append((issue["id"], changes, "risk.covered", {"run_id": run_id, "risk_id": risk["id"] if risk else None}))
            elif issue["status"] == "ready_for_retest":
                out.append((issue["id"], {**changes, "status": "open"}, "retest.failed",
                            {"run_id": run_id, "symptom": r.get("symptom")}))
            else:
                out.append((issue["id"], changes, "still.failing", {"run_id": run_id}))
    return out


def _due(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def check_risk(body: dict, today: date) -> tuple[str, date]:
    reason = _text(body.get("reason"))
    if not reason:
        raise ValueError("reason is required")
    try:
        review_by = date.fromisoformat(str(body.get("review_by")))
    except ValueError as exc:
        raise ValueError("review_by must be a date, YYYY-MM-DD") from exc
    if review_by <= today:
        raise ValueError("review_by must be in the future")
    return reason, review_by


# ------------------------------------------------------------ import/export

# Header aliases, lower-cased with punctuation squeezed out, as CxAlloy,
# Facility Grid and hand-made spreadsheets name them.
_ALIASES = {
    "source_id": ("id", "issueid", "issue", "issuenumber", "number", "no", "ref", "reference", "key"),
    "title": ("title", "name", "summary", "issuetitle", "subject"),
    "description": ("description", "details", "detail", "notes", "comment", "comments", "issuedescription"),
    "status": ("status", "state", "issuestatus"),
    "assignee": ("assignee", "assignedto", "responsibleparty", "responsible", "owner", "assigned"),
    "equipment": ("equipment", "asset", "equip", "tag", "equipmenttag", "assettag", "system"),
    "severity": ("severity", "priority"),
    "location": ("location", "space", "room", "area"),
    "created": ("created", "datecreated", "createdon", "opened", "dateopened"),
}
_STATUS_MAP = {
    "open": "open", "new": "open", "notstarted": "open", "active": "open",
    "inprogress": "in_progress", "working": "in_progress", "assigned": "in_progress",
    "readyforretest": "ready_for_retest", "readyforreview": "ready_for_retest", "resolved": "ready_for_retest",
    "pendingverification": "ready_for_retest", "readyforbackcheck": "ready_for_retest",
    "closed": "closed", "complete": "closed", "completed": "closed", "done": "closed", "verified": "closed",
}
_SEVERITY_MAP = {"critical": "critical", "urgent": "critical", "high": "major", "major": "major", "medium": "minor",
                 "normal": "minor", "minor": "minor", "low": "info", "info": "info"}


def _squeeze(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _field_map(headers) -> dict[str, str]:
    out = {}
    for h in headers:
        k = _squeeze(h)
        for field, names in _ALIASES.items():
            if k in names and field not in out:
                out[field] = h
                break
    return out


def parse_import(body: dict) -> tuple[str, list[dict]]:
    """(source, rows) from {"source": "cxalloy", "csv": "..."} or
    {"source": ..., "items": [{...}, ...]}. Each row:
    {source_id, title, description, status, assignee, equipment, severity,
    location, created, extra}. Raises ValueError."""
    source = _squeeze(body.get("source")) or "import"
    if source == "baler":
        raise ValueError("source names the external system the items came from, not baler")
    if isinstance(body.get("csv"), str):
        reader = csv.DictReader(io.StringIO(body["csv"].lstrip("﻿")))
        raw = list(reader)
    elif isinstance(body.get("items"), list):
        raw = body["items"]
    else:
        raise TypeError("send csv (text) or items (a list of objects)")
    if len(raw) > MAX_IMPORT:
        raise ValueError(f"at most {MAX_IMPORT} items per import")
    rows = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise TypeError(f"item {i + 1} is not an object")
        fmap = _field_map(item.keys())

        def get(f, fm=fmap, it=item):
            return it.get(fm[f]) if f in fm else None

        title = _text(get("title"), MAX_TITLE) or _text(get("description"), MAX_TITLE)
        if not title:
            continue  # blank spreadsheet rows
        sid = _text(get("source_id"), 200)
        status_raw = _squeeze(get("status"))
        sev_raw = _squeeze(get("severity"))
        used = set(fmap.values())
        rows.append({
            "source_id": sid or f"row-{i + 1}", "title": title, "description": _text(get("description")),
            "status": _STATUS_MAP.get(status_raw, "open") if status_raw else "open",
            "status_raw": _text(get("status"), 100), "assignee": _text(get("assignee"), 200),
            "equipment": _text(get("equipment"), 500), "severity": _SEVERITY_MAP.get(sev_raw),
            "location": _text(get("location"), 500), "created": _text(get("created"), 100),
            "extra": {str(k): _text(v, 1000) for k, v in item.items() if k not in used and k is not None and _text(v)},
        })
    return source, rows


def match_equipment(ref: str | None, equips: list[dict]) -> str | None:
    """An imported item's equipment text to an inventory URI: the URI
    itself, or exactly one equipment whose dis or id tail matches."""
    if not ref:
        return None
    if ref.startswith("urn:equip:"):
        return ref if any(e["id"] == ref for e in equips) else None
    want = _squeeze(ref)
    hits = [e["id"] for e in equips
            if _squeeze(e.get("dis")) == want or _squeeze(e["id"].rsplit("/", 1)[-1]) == want]
    return hits[0] if len(hits) == 1 else None


EXPORT_SHAPES = {
    # Column names as the tools' spreadsheet issue imports use them. These
    # are best effort: map columns in the target tool's import dialog if
    # its template differs.
    "baler": ("id", "status", "severity", "title", "equip_uri", "check_id", "subject", "assignee", "source",
              "source_id", "opened_run", "closed_run", "created_at", "updated_at", "closed_at", "description"),
    "cxalloy": ("Name", "Description", "Status", "Priority", "Asset", "Assigned To", "Date Created", "Date Closed",
                "External ID"),
    "facilitygrid": ("Issue #", "Title", "Description", "Status", "Priority", "Equipment", "Responsible Party",
                     "Created Date", "Closed Date"),
}
_OUT_STATUS = {
    "cxalloy": {"open": "Open", "in_progress": "In Progress", "ready_for_retest": "Ready for Review",
                "closed": "Closed", "risk_accepted": "Accepted"},
    "facilitygrid": {"open": "Open", "in_progress": "In Progress", "ready_for_retest": "Ready for Backcheck",
                     "closed": "Closed", "risk_accepted": "Deferred"},
}
_OUT_PRIORITY = {"critical": "Critical", "major": "High", "minor": "Medium", "info": "Low"}


def _iso(v):
    return v.isoformat() if isinstance(v, (datetime, date)) else (v or "")


def describe(issue: dict) -> str:
    d = issue.get("detail") or {}
    parts = [d.get("description") or d.get("symptom") or ""]
    if issue.get("check_id"):
        parts.append(f"Check {issue['check_id']} on {issue['subject']}")
    if d.get("candidates"):
        parts.append("Candidate causes: " + "; ".join(c.get("cause", "") for c in d["candidates"]))
    return "\n".join(p for p in parts if p)


def export_rows(issues: list[dict], shape: str) -> list[dict]:
    if shape not in EXPORT_SHAPES:
        raise ValueError(f"shape must be one of {', '.join(EXPORT_SHAPES)}")
    out = []
    for i in issues:
        equip = i.get("equip_uri") or (i.get("detail") or {}).get("equipment") or ""
        if shape == "baler":
            row = {k: _iso(i.get(k)) for k in EXPORT_SHAPES["baler"] if k != "description"}
            row["description"] = describe(i)
        elif shape == "cxalloy":
            row = {"Name": i["title"], "Description": describe(i), "Status": _OUT_STATUS["cxalloy"][i["status"]],
                   "Priority": _OUT_PRIORITY.get(i.get("severity") or "", ""), "Asset": equip,
                   "Assigned To": i.get("assignee") or "", "Date Created": _iso(i.get("created_at")),
                   "Date Closed": _iso(i.get("closed_at")), "External ID": i.get("source_id") or i["id"]}
        else:
            row = {"Issue #": i.get("source_id") or i["id"], "Title": i["title"], "Description": describe(i),
                   "Status": _OUT_STATUS["facilitygrid"][i["status"]],
                   "Priority": _OUT_PRIORITY.get(i.get("severity") or "", ""), "Equipment": equip,
                   "Responsible Party": i.get("assignee") or "", "Created Date": _iso(i.get("created_at")),
                   "Closed Date": _iso(i.get("closed_at"))}
        out.append(row)
    return out


def to_csv(rows: list[dict], shape: str) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(EXPORT_SHAPES[shape]), extrasaction="ignore")
    w.writeheader()
    for r in rows:
        # A cell starting with = + - @ is a formula to a spreadsheet: quote it out.
        w.writerow({k: ("'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v) for k, v in r.items()})
    return buf.getvalue()

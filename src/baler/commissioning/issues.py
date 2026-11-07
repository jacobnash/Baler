"""
The commissioning issues log and accepted risks (Cx 4, Baler#40).

Rules (todo/commissioning-plan.md §6.1):

  - Evidence resolves, only a person closes (the CxA's independence rule,
    Baler#86). When a later completed run passes the issue's check for its
    subject, the system moves it to ready_for_retest ("resolved, pending
    verification") - never to closed. An inconclusive result moves nothing.
  - A retest is a later run or procedure test run whose judged window
    starts at or after the moment the issue was resolved (or a person
    claimed the fix by moving it to ready_for_retest). A passing retest is
    recorded on the issue (`retest`, event retest.passed). Only then can a
    named person close a check-tied issue, and the close cites that retest.
  - When the check fails again on an issue marked ready_for_retest, it
    goes back to open: the retest failed. Issues with no check (manual,
    imported) close by hand, with an event.
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
        if issue["status"] != "ready_for_retest":
            raise Conflict("an issue tied to a check is closed by a person after it is resolved: it must be "
                           "ready_for_retest first (a passing run moves it there, or claim the fix)")
        if not issue.get("retest"):
            raise Conflict(f"no passing retest yet: run the procedure or a verification run that judges "
                           f"{issue['check_id']} for {issue['subject']} over a window starting after it was resolved")


def actor(user_sub) -> str:
    """Who made a change: a named person, or the system (runs, reconcile)."""
    return "user" if user_sub else "system"


def title_for(result: dict) -> str:
    """Names the point, not just the equipment: two exists failures on one
    device read "home/bacnet/zone_temp: offline - ..." and
    "home/bacnet/fan_status: offline - ..." rather than the same line twice."""
    what = result.get("symptom") or f"{result['check_id']} {result['status']}"
    subject = str(result.get("subject") or "")
    where = subject.replace("urn:point:", "", 1) if subject.startswith("urn:point:") else \
        str(result.get("equip_uri") or "").replace("urn:equip:", "", 1)
    return f"{where}: {what}"[:MAX_TITLE]


def default_severity(result: dict) -> str:
    """Auto-opened issues always carry a severity (CxAlloy / Facility Grid
    exports want one): required template gaps and points not reporting are
    major, the rest minor."""
    if result.get("severity") == "required" or result.get("check_id") == "exists":
        return "major"
    return "minor"


def from_result(result: dict, run_id: str, project_id: str, new_id, now: datetime, user_sub: str | None) -> dict:
    sev = default_severity(result)
    return {
        "id": new_id(), "project_id": project_id, "equip_uri": result["equip_uri"], "check_id": result["check_id"],
        "subject": result["subject"], "status": "open", "title": title_for(result), "severity": sev,
        "assignee": None, "source": "baler", "source_id": None,
        "detail": {"symptom": result.get("symptom"), "candidates": result.get("candidates") or [],
                   "evidence": result.get("evidence") or {}},
        "opened_run": run_id, "last_seen_run": run_id, "closed_run": None, "created_by": user_sub,
        "resolved_at": None, "resolved_by": None, "retest": None,
        "created_at": now, "updated_at": now, "closed_at": None,
    }


def reconcile(issues: list[dict], risks: dict[str, dict], results: list[dict], run_id: str, now: datetime,
              today: date, judged_from: datetime | None = None, kind: str = "run") -> list[tuple[str, dict, str, dict]]:
    """What a completed run changes in the log: [(issue_id, changes,
    event_action, event_detail)]. `issues` are the project's active
    issues, `risks` maps issue id -> its standing risk. Only issues whose
    (check_id, subject) the run judged are touched. `kind` is "run" or
    "test_run"; `judged_from` is the start of the window the run judged -
    a pass only counts as a retest if that window starts after the issue
    was resolved."""
    ref = {"test_run_id": run_id} if kind == "test_run" else {"run_id": run_id}
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
        seen = {"last_seen_run": run_id} if kind == "run" else {}
        if r["status"] == "pass":
            if issue["status"] in ("open", "in_progress"):
                out.append((issue["id"], {"status": "ready_for_retest", "resolved_at": now, "resolved_by": ref,
                                          "retest": None, **seen, "updated_at": now},
                            "resolve.evidence", {**ref, "evidence": r.get("evidence") or {}}))
            elif issue["status"] == "ready_for_retest" and _is_retest(issue, judged_from, r.get("evidence")):
                retest = {**ref, "kind": kind, "at": now.isoformat(),
                          "judged_from": judged_from.isoformat() if judged_from else None}
                out.append((issue["id"], {"retest": retest, **seen, "updated_at": now}, "retest.passed",
                            {**ref, "judged_from": retest["judged_from"], "evidence": r.get("evidence") or {}}))
        elif r["status"] == "fail":
            changes: dict = {**seen, "updated_at": now}
            if issue["status"] == "risk_accepted":
                out.append((issue["id"], changes, "risk.covered", {**ref, "risk_id": risk["id"] if risk else None}))
            elif issue["status"] == "ready_for_retest":
                out.append((issue["id"], {**changes, "status": "open", "resolved_at": None, "resolved_by": None,
                                          "retest": None}, "retest.failed",
                            {**ref, "symptom": r.get("symptom")}))
            else:
                out.append((issue["id"], changes, "still.failing", ref))
    return out


def _when(v) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def _is_retest(issue: dict, judged_from: datetime | None, evidence: dict | None = None) -> bool:
    """A pass is a retest only if it rests on data from after the fix: the
    judged window starts after resolved_at (a procedure test run started
    after the corrective action), or the evidence's own newest sample is
    after it (a verification run's exists check seeing the point report
    again since)."""
    resolved = _when(issue.get("resolved_at"))
    if resolved is None:
        return False
    if judged_from is not None and judged_from >= resolved:
        return True
    last = _when((evidence or {}).get("last_ts"))
    return last is not None and last >= resolved


def _due(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def check_risk(body: dict, today: date) -> tuple[str, date]:
    """(reason, review_by). The other required fields - owner (who is
    responsible for the open item), retest_trigger (what retest lifts it,
    e.g. "first seasonal changeover") and who accepted it - are read by
    risk_extras."""
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


def risk_extras(body: dict, account_name: str | None = None) -> dict:
    """A waiver must say who carries the open item and what retest lifts it
    - a blank Owner / Retest trigger printed "-" in the report (Baler#100
    review, #121) - and who accepted it by name.

    The accepter's name is the display name on their own user record
    (account_name) when it has one, snapshotted onto the waiver so a later
    rename doesn't rewrite a signed record; a body accepted_name is then
    ignored, so nobody can sign as someone else. Without one, accepted_name
    is required and kept as typed (accepted_name_source "typed"; the report
    never labels a typed name as the CxA). The account email stays as
    accepted_email."""
    owner = _text(body.get("owner"), 200)
    trigger = _text(body.get("retest_trigger"), 500)
    if not owner:
        raise ValueError("owner is required: who is responsible for the open item")
    if not trigger:
        raise ValueError("retest_trigger is required: what retest lifts the waiver (e.g. first seasonal changeover)")
    account = _text(account_name, 200)
    if account:
        return {"owner": owner, "retest_trigger": trigger, "accepted_name": account, "accepted_name_source": "account"}
    typed = _text(body.get("accepted_name"), 200)
    if not typed:
        raise ValueError("accepted_name is required: your account has no display name (an admin sets it in "
                         "Ops -> Users), so say who is accepting this risk")
    return {"owner": owner, "retest_trigger": trigger, "accepted_name": typed, "accepted_name_source": "typed"}


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

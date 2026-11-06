"""
Fault mirroring and fault ingest (Cx 8, Baler#44).

Mirroring is per-project opt-in (settings.fault_mirroring). After a run,
each failing check on *confirmed* equipment opens a fault in the shared
`faults` table, the same table fault_detector writes, so the Alarms page
and webhooks see it like any other fault:

  - rule_id `cx:<check_id>` (contains ':', so fault_detector's
    orphaned-fault cleanup leaves it alone);
  - point_uri the result's real urn:point: URI (or the urn:equip: URI
    for an equipment-level check) - never a `cx:` pseudo-URI, which was
    PR #5's cross-org leak: a real URI is filtered by every reader's site
    scope like every other fault;
  - a later run where the check passes resolves it; inconclusive leaves
    it as it is; an accepted risk on the matching issue suppresses it
    (and resolves one already open).

"Confirmed" equipment: by default, equipment a person confirmed against
the spec (Cx 7 alignment decisions). `"equipment": "all"` mirrors every
equipment in the run.

The other direction: open faults from fault_detector, or items from an
outside FDD tool, become Cx issues (source `fdd:<tool>`), upserted on
the fault's id, and closed when fault_detector resolves the fault.
"""

from __future__ import annotations

from datetime import datetime

RULE_PREFIX = "cx:"
SEVERITIES = ("critical", "warning", "info")
DEFAULT_CHECKS = ("exists", "live", "pair:", "gl36:", "cx:", "ladder")


def validate_settings(cfg) -> dict:
    if not isinstance(cfg, dict):
        raise TypeError("fault_mirroring must be an object")
    extra = set(cfg) - {"enabled", "equipment", "checks", "severity"}
    if extra:
        raise ValueError(f"unknown fault_mirroring settings {sorted(extra)}")
    if not isinstance(cfg.get("enabled", False), bool):
        raise TypeError("fault_mirroring.enabled is true or false")
    if cfg.get("equipment", "confirmed") not in ("confirmed", "all"):
        raise ValueError("fault_mirroring.equipment is confirmed or all")
    checks = cfg.get("checks", list(DEFAULT_CHECKS))
    if not isinstance(checks, list) or not checks or any(not isinstance(c, str) or not c for c in checks):
        raise ValueError("fault_mirroring.checks is a non-empty list of check ids or prefixes")
    if cfg.get("severity", "warning") not in SEVERITIES:
        raise ValueError(f"fault_mirroring.severity is one of {', '.join(SEVERITIES)}")
    return cfg


def rule_id(check_id: str) -> str:
    return RULE_PREFIX + check_id


def fault_uri(result: dict) -> str:
    """The real URI a mirrored fault is on: the result's first point, else
    its equipment. Never anything but urn:point:/urn:equip:."""
    for part in str(result["subject"]).split("|"):
        if part.startswith("urn:point:"):
            return part
    return result["equip_uri"]


def _wanted(check_id: str, checks) -> bool:
    return any(check_id == c or (c.endswith((":", "*")) and check_id.startswith(c.rstrip("*"))) or check_id.startswith(c + ":")
               for c in checks)


def plan(results: list[dict], cfg: dict, confirmed: set[str] | None, risk_keys: set[tuple[str, str]],
         open_faults: set[tuple[str, str]]) -> tuple[list[tuple[str, str, dict]], list[tuple[str, str, str]]]:
    """(to_open [(rule_id, uri, result)], to_close [(rule_id, uri, why)])
    for one completed run. `confirmed` None means every equipment."""
    checks = cfg.get("checks") or list(DEFAULT_CHECKS)
    to_open, to_close = [], []
    for r in results:
        if not _wanted(r["check_id"], checks):
            continue
        rid, uri = rule_id(r["check_id"]), fault_uri(r)
        if not uri.startswith(("urn:point:", "urn:equip:")):
            continue
        key = (rid, uri)
        if r["status"] == "pass":
            if key in open_faults:
                to_close.append((rid, uri, "check passed"))
        elif r["status"] == "fail":
            if (r["check_id"], r["subject"]) in risk_keys:
                if key in open_faults:
                    to_close.append((rid, uri, "accepted risk"))
                continue
            if confirmed is not None and r["equip_uri"] not in confirmed:
                continue
            if key not in open_faults:
                to_open.append((rid, uri, r))
    return to_open, to_close


def detail_for(result: dict, project_id: str, run_id: str) -> dict:
    return {"source": "commissioning", "project_id": project_id, "run_id": run_id, "check_id": result["check_id"],
            "subject": result["subject"], "equip_uri": result["equip_uri"], "symptom": result.get("symptom")}


def apply(repo, project: dict, run_id: str, results: list[dict], now: datetime, notify=None) -> dict:
    """Mirror one run's results for a project that opted in. Returns
    {opened, closed}. `notify(event, rule_id, uri, status, detail)` fires
    webhooks (fault_detector's delivery) for each opening/resolution."""
    cfg = (project.get("settings") or {}).get("fault_mirroring") or {}
    if not cfg.get("enabled"):
        return {"opened": 0, "closed": 0}
    confirmed = None
    if cfg.get("equipment", "confirmed") == "confirmed":
        confirmed = {m["equip_uri"] for m in repo.find_rows("cx_mappings", project_id=project["id"], status="confirmed")}
    risk_keys = {(i["check_id"], i["subject"]) for i in repo.list_issues(project["id"], ("risk_accepted",)) if i.get("check_id")}
    open_faults = repo.open_mirrored_faults(project["id"])
    to_open, to_close = plan(results, cfg, confirmed, risk_keys, set(open_faults))
    sev = cfg.get("severity", "warning")
    opened = closed = 0
    for rid, uri, r in to_open:
        detail = detail_for(r, project["id"], run_id)
        if repo.open_fault(rid, uri, now, detail, sev) is not None:
            opened += 1
            if notify:
                notify("fault.opened", rid, uri, "open", detail)
    for rid, uri, why in to_close:
        if repo.close_fault(rid, uri, now) is not None:
            closed += 1
            if notify:
                notify("fault.resolved", rid, uri, "resolved", {"source": "commissioning", "project_id": project["id"],
                                                                "run_id": run_id, "why": why})
    return {"opened": opened, "closed": closed}


# ------------------------------------------------------------------ ingest


def issue_from_fault(fault: dict, equip_uri: str | None, source: str, project_id: str, new_id, now: datetime,
                     user_sub: str | None) -> dict:
    d = fault.get("detail") or {}
    title = fault.get("title") or d.get("message") or f"{fault.get('rule_id') or 'fault'} on {fault['point_uri']}"
    sev = {"critical": "critical", "warning": "major", "info": "minor"}.get(fault.get("severity") or "", None)
    return {
        "id": new_id(), "project_id": project_id, "equip_uri": equip_uri, "check_id": None, "subject": None, "status": "open",
        "title": str(title)[:300], "severity": sev, "assignee": None, "source": source, "source_id": str(fault["id"]),
        "detail": {"description": d.get("message") or d.get("symptom"), "point_uri": fault["point_uri"],
                   "rule_id": fault.get("rule_id"), "fault_started_at": str(fault.get("started_at") or ""), "fault": d},
        "opened_run": None, "last_seen_run": None, "closed_run": None, "created_by": user_sub,
        "created_at": now, "updated_at": now, "closed_at": None,
    }

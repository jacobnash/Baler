"""
Warranties on assets (#198, design §6): start, end, terms, and the
document page they came from. Two alerts:

  - **expiring**: a warranty that ends within `within_days` (default 90),
    so someone can claim, inspect or extend before it lapses;
  - **failure under warranty**: a fault or a Cx issue that opened on a
    position while the asset installed there (lifecycle.assets.asset_at)
    was under warranty, so the repair is claimed rather than paid for.

A warranty belongs to the asset, not the position: when the asset is
replaced, the new one's warranties apply, and a failure is matched against
whichever asset was in the position when it opened.

Failures come from `faults` (fault_detector's rules) and `cx_issues`. A
fault is on a point; its position is the equipment whose URI key the
point's key starts with (urn:equip:fbf/s/ahu-1 owns urn:point:fbf/s/ahu-1/sat),
the same key scheme scope.py uses.

This computes alerts on request (GET /ops/warranties/alerts). Pushing them
(webhooks, email) is not built yet. Schema: migration 0013.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from baler.lifecycle import assets
from baler.lifecycle import rows as rows_mod
from baler.lifecycle.routes import HTTPError, Request, as_dt, need, route, text, when

COLS = ("id", "org_id", "asset_id", "kind", "provider", "starts_on", "ends_on", "terms", "source", "created_by",
        "created_at", "updated_at", "voided_at", "voided_reason")
rows_mod.register("lc_warranties", COLS, ("source",), order="ends_on")

KINDS = ("parts", "labor", "parts_and_labor", "extended", "other")
DEFAULT_WITHIN_DAYS = 90
FAILURE_LOOKBACK_DAYS = 365


def _uri(warranty_id: str) -> str:
    return rows_mod.entity_uri("warranty", warranty_id)


def _date(v: Any, key: str) -> date | None:
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    d = when(v, key)
    return d.date() if d else None


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(d.day, last))


def _source(v: Any) -> dict | None:
    if v is None:
        return None
    if not isinstance(v, dict):
        raise TypeError("source is {document_id, page, quote}")
    out = {k: v.get(k) for k in ("document_id", "sha256", "page", "quote") if v.get(k) is not None}
    if "page" in out and (not isinstance(out["page"], int) or isinstance(out["page"], bool) or out["page"] < 1):
        raise ValueError("source.page is a 1-based page number")
    if "quote" in out:
        out["quote"] = str(out["quote"])[:1000]
    return out or None


def _dates(req: Request, asset_id: str, body: dict) -> tuple[date, date]:
    """starts_on (default: the asset's first install, i.e. "from start-up")
    and ends_on, or duration_months from the start."""
    starts = _date(body.get("starts_on"), "starts_on") or assets.install_date(req, asset_id)
    if starts is None:
        raise ValueError("starts_on is required (the asset hasn't been installed, so there's no start-up date to default to)")
    ends = _date(body.get("ends_on"), "ends_on")
    months = body.get("duration_months")
    if ends is None and months is not None:
        if not isinstance(months, int) or isinstance(months, bool) or not 0 < months <= 600:
            raise ValueError("duration_months is a whole number of months, 1-600")
        ends = _add_months(starts, months) - timedelta(days=1)
    if ends is None:
        raise ValueError("give ends_on or duration_months")
    if ends < starts:
        raise ValueError("ends_on is before starts_on")
    return starts, ends


def status(w: dict, today: date) -> str:
    if w.get("voided_at"):
        return "void"
    if today < w["starts_on"]:
        return "not_started"
    return "active" if today <= w["ends_on"] else "expired"


def view(w: dict, today: date) -> dict:
    return {**(rows_mod.public(w) or {}), "status": status(w, today), "days_left": (w["ends_on"] - today).days}


def _norm(w: dict) -> dict:
    """Dates as dates, whichever backend the row came from."""
    return {**w, "starts_on": _date(w["starts_on"], "starts_on"), "ends_on": _date(w["ends_on"], "ends_on")}


def for_asset(req: Request, asset_id: str) -> list[dict]:
    return [_norm(w) for w in req.rows.find("lc_warranties", org_id=req.org_id, asset_id=asset_id)]


def covering(req: Request, asset_id: str, day: date) -> list[dict]:
    return [w for w in for_asset(req, asset_id) if not w.get("voided_at") and w["starts_on"] <= day <= w["ends_on"]]


def create(req: Request, asset_id: str, body: dict) -> dict:
    assets.asset_detail(req, asset_id)  # 404 if not the caller's
    kind = body.get("kind") or "parts_and_labor"
    if kind not in KINDS:
        raise ValueError(f"kind is one of {', '.join(KINDS)}")
    starts, ends = _dates(req, asset_id, body)
    w = {"id": rows_mod.new_id(), "org_id": req.org_id, "asset_id": asset_id, "kind": kind,
         "provider": text(body, "provider", 200), "starts_on": starts, "ends_on": ends, "terms": text(body, "terms", 4000),
         "source": _source(body.get("source")), "created_by": req.actor(), "created_at": req.now, "updated_at": req.now}
    req.rows.insert("lc_warranties", w)
    req.rows.audit(_uri(w["id"]), "warranty.create", req.who, {"asset_id": asset_id, "kind": kind,
                                                               "starts_on": starts, "ends_on": ends})
    return w


def _get(req: Request, wid: str) -> dict:
    w = req.rows.get("lc_warranties", wid)
    if w is None or w["org_id"] != req.org_id:
        raise HTTPError(404, "no such warranty")
    assets.asset_detail(req, w["asset_id"])
    return _norm(w)


# --------------------------------------------------------------- alerts


def _position_of(point_uri: str, positions: list[str]) -> str | None:
    """The longest position URI whose key owns the point's key."""
    if not point_uri.startswith("urn:point:"):
        return point_uri if point_uri in positions else None
    key = point_uri[len("urn:point:"):]
    best = None
    for p in positions:
        if p.startswith("urn:equip:"):
            pk = p[len("urn:equip:"):]
            if (key == pk or key.startswith(pk + "/")) and (best is None or len(p) > len(best)):
                best = p
    return best


def recent_failures(req: Request, since: datetime) -> list[dict]:
    """Faults and Cx issues opened since `since`: [{kind, id, subject_uri,
    opened_at, title, open}]. Tests pass them as services["failures"]."""
    if "failures" in req.services:
        return [f for f in req.services["failures"] if as_dt(f["opened_at"]) >= since]
    if not req.rows.pg:
        return []
    out = []
    with req.rows.conn() as conn:
        for fid, point, rule, started, ended in conn.execute(
                "SELECT id, point_uri, rule_id, started_at, ended_at FROM faults WHERE started_at >= %s", (since,)):
            out.append({"kind": "fault", "id": str(fid), "subject_uri": point, "opened_at": started,
                        "title": rule, "open": ended is None})
        for iid, equip, title, created, st in conn.execute(
                "SELECT i.id, i.equip_uri, i.title, i.created_at, i.status FROM cx_issues i "
                "JOIN cx_projects p ON p.id = i.project_id WHERE p.org_id = %s AND i.equip_uri IS NOT NULL "
                "AND i.created_at >= %s", (req.org_id, since)):
            out.append({"kind": "cx_issue", "id": iid, "subject_uri": equip, "opened_at": created, "title": title,
                        "open": st not in ("closed", "risk_accepted")})
    return out


def alerts(req: Request, within_days: int = DEFAULT_WITHIN_DAYS) -> dict:
    today = req.now.date()
    org_assets = {a["id"]: a for a in req.rows.find("lc_assets", org_id=req.org_id)}
    ws = [_norm(w) for w in req.rows.find("lc_warranties", org_id=req.org_id) if not w.get("voided_at")]
    events = req.rows.find("lc_asset_events", org_id=req.org_id)
    ivs = assets.intervals(events)
    hidden = {iv["asset_id"] for iv in ivs if not req.allows(iv["position_uri"])}

    def brief(a: dict) -> dict:
        return {k: a.get(k) for k in ("id", "tag", "asset_type", "manufacturer", "model", "serial")}

    expiring = [{**view(w, today), "asset": brief(org_assets.get(w["asset_id"], {}))} for w in ws
                if w["asset_id"] not in hidden and today <= w["ends_on"] <= today + timedelta(days=within_days)
                and w["starts_on"] <= today]
    positions = sorted({iv["position_uri"] for iv in ivs})
    by_pos: dict[str, list[dict]] = {}
    for iv in ivs:
        by_pos.setdefault(iv["position_uri"], []).append(iv)
    failures: list[dict[str, Any]] = []
    since = req.now - timedelta(days=FAILURE_LOOKBACK_DAYS)
    for f in recent_failures(req, since):
        pos = _position_of(f["subject_uri"], positions)
        if pos is None or not req.allows(pos):
            continue
        at = as_dt(f["opened_at"])
        iv = next((x for x in by_pos[pos] if x["installed_at"] <= at and (x["removed_at"] is None or at < x["removed_at"])),
                  None)
        if iv is None:
            continue
        cover = [w for w in ws if w["asset_id"] == iv["asset_id"] and w["starts_on"] <= at.date() <= w["ends_on"]]
        if cover:
            failures.append({"failure": {**f, "opened_at": at.isoformat()}, "position_uri": pos,
                             "asset": brief(org_assets.get(iv["asset_id"], {})),
                             "warranties": [view(w, today) for w in cover]})
    failures.sort(key=lambda x: x["failure"]["opened_at"], reverse=True)
    return {"today": today.isoformat(), "within_days": within_days, "expiring": expiring,
            "failures_under_warranty": failures}


# --------------------------------------------------------------- routes


@route("GET", r"/assets/([^/]+)/warranties")
def _list_for_asset(req: Request, asset_id: str):
    assets.asset_detail(req, asset_id)
    today = req.now.date()
    return [view(w, today) for w in for_asset(req, asset_id)]


@route("POST", r"/assets/([^/]+)/warranties")
def _create(req: Request, asset_id: str):
    return 201, view(create(req, asset_id, req.json()), req.now.date())


@route("GET", r"/warranties")
def _list(req: Request):
    today = req.now.date()
    want = req.query.get("status")
    out = []
    for w in req.rows.find("lc_warranties", org_id=req.org_id):
        try:
            assets.asset_detail(req, w["asset_id"])
        except HTTPError:
            continue
        v = view(_norm(w), today)
        if not want or v["status"] == want:
            out.append(v)
    return out


@route("GET", r"/warranties/alerts")
def _alerts(req: Request):
    days = int(req.query.get("within_days") or DEFAULT_WITHIN_DAYS)
    if not 0 <= days <= 3650:
        raise ValueError("within_days is 0-3650")
    return alerts(req, days)


@route("PATCH", r"/warranties/([^/]+)")
def _patch(req: Request, wid: str):
    w = _get(req, wid)
    if w.get("voided_at"):
        raise HTTPError(409, "the warranty is void")
    body = req.json()
    unknown = set(body) - {"kind", "provider", "starts_on", "ends_on", "terms", "source"}
    if unknown:
        raise ValueError(f"not editable: {', '.join(sorted(unknown))}")
    changes: dict[str, Any] = {}
    if "kind" in body:
        if body["kind"] not in KINDS:
            raise ValueError(f"kind is one of {', '.join(KINDS)}")
        changes["kind"] = body["kind"]
    for k, limit in (("provider", 200), ("terms", 4000)):
        if k in body:
            changes[k] = text(body, k, limit)
    for k in ("starts_on", "ends_on"):
        if k in body:
            changes[k] = _date(body[k], k)
            if changes[k] is None:
                raise ValueError(f"{k} can't be blank")
    if "source" in body:
        changes["source"] = _source(body["source"])
    if changes.get("ends_on", w["ends_on"]) < changes.get("starts_on", w["starts_on"]):
        raise ValueError("ends_on is before starts_on")
    diff = {k: {"from": w.get(k), "to": v} for k, v in changes.items() if w.get(k) != v}
    if diff:
        req.rows.update("lc_warranties", wid, {**changes, "updated_at": req.now})
        req.rows.audit(_uri(wid), "warranty.edit", req.who, diff)
    return view(_get(req, wid), req.now.date())


@route("POST", r"/warranties/([^/]+)/void")
def _void(req: Request, wid: str):
    w = _get(req, wid)
    if w.get("voided_at"):
        raise HTTPError(409, "already void")
    reason = need(req.json(), "reason")
    req.rows.update("lc_warranties", wid, {"voided_at": req.now, "voided_reason": reason[:1000], "updated_at": req.now})
    req.rows.audit(_uri(wid), "warranty.void", req.who, {"reason": reason})
    return view(_get(req, wid), req.now.date())

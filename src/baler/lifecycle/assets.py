"""
Positions vs installed assets (#197, design §6).

A *position* is the Brick equipment the points hang on ("AHU-1 supply fan
motor"), referred to by its graph URI. It is permanent. An *asset* is the
physical thing installed there (make, model, serial, cost). Assets come
and go, and the link between them is an append-only list of install and
remove events, each with a time and a reason:

    position AHU-1/SF-motor --installed-- asset A102 (Baldor, 2026-03-01 -> 2031-07-14, bearing failure)
                             +-installed-- asset A517 (WEG, 2031-07-14 -> now)

Rules the events keep (checked before anything is written):

  - an asset is in at most one position at a time, and a position holds at
    most one asset at a time;
  - an asset's events run forward in time: no install or remove earlier
    than its last event, and none in the future;
  - a replacement is a remove and an install at the same instant, never an
    edit of the old row.

`asset_at(position, t)` answers which asset was in a position at a moment,
which is how a start-up baseline, a fault or a trend attaches to the
asset rather than to the position.

Field corrections (a mistyped serial) are allowed and audited in
record_edits with the old and new values. Events are never edited.
Schema: migration 0003.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from itertools import pairwise
from typing import Any

from baler.lifecycle import rows as rows_mod
from baler.lifecycle.routes import (
    HTTPError,
    Request,
    as_dt,
    need,
    number,
    route,
    text,
    when,
)

ASSET_COLS = ("id", "org_id", "site_id", "tag", "asset_type", "brick_class", "manufacturer", "model", "serial", "cost",
              "currency", "acquired_on", "attributes", "created_by", "created_at", "updated_at", "retired_at",
              "retired_reason")
EVENT_COLS = ("id", "org_id", "asset_id", "position_uri", "kind", "at", "reason", "cost", "work_ref", "created_by",
              "created_at")
rows_mod.register("lc_assets", ASSET_COLS, ("attributes",))
rows_mod.register("lc_asset_events", EVENT_COLS, (), order="at")

# Fields a person may set or correct on an asset.
EDITABLE = ("site_id", "tag", "asset_type", "brick_class", "manufacturer", "model", "serial", "cost", "currency",
            "acquired_on", "attributes")
# A little clock skew between a browser and the server is fine; a date
# next month is a typo.
FUTURE_SLACK = timedelta(minutes=10)


def _uri(asset_id: str) -> str:
    return rows_mod.entity_uri("asset", asset_id)


def _fields(body: dict) -> dict:
    out: dict[str, Any] = {}
    for k in ("site_id", "tag", "asset_type", "brick_class", "manufacturer", "model", "serial", "currency"):
        if k in body:
            out[k] = text(body, k, 200)
    if "cost" in body:
        cost = number(body, "cost")
        if cost is not None and cost < 0:
            raise ValueError("cost can't be negative")
        out["cost"] = cost
    if "acquired_on" in body:
        d = when(body.get("acquired_on"), "acquired_on")
        out["acquired_on"] = d.date() if d else None
    if "attributes" in body:
        attrs = body.get("attributes") or {}
        if not isinstance(attrs, dict) or len(attrs) > 100:
            raise ValueError("attributes is an object of at most 100 fields")
        out["attributes"] = {str(k)[:100]: v for k, v in attrs.items()}
    return out


def get_asset(req: Request, asset_id: str) -> dict:
    a = req.rows.get("lc_assets", asset_id)
    if a is None or a["org_id"] != req.org_id:
        raise HTTPError(404, "no such asset")
    return a


def create_asset(req: Request, body: dict) -> dict:
    f = _fields(body)
    if not any(f.get(k) for k in ("tag", "serial", "model", "manufacturer", "asset_type")):
        raise ValueError("give the asset at least a tag, type, manufacturer, model or serial")
    now = req.now
    row = {"id": rows_mod.new_id(), "org_id": req.org_id, "attributes": {}, **f, "created_by": req.actor(),
           "created_at": now, "updated_at": now}
    req.rows.insert("lc_assets", row)
    req.rows.audit(_uri(row["id"]), "asset.create", req.who, {k: v for k, v in f.items()})
    return row


# ------------------------------------------------------------- events


def intervals(events: list[dict]) -> list[dict]:
    """Install/remove events (any order) -> one interval per installation:
    {asset_id, position_uri, installed_at, removed_at (None: still there),
    install_reason, remove_reason, install_event, remove_event, cost, work_ref}."""
    out: list[dict] = []
    open_by_asset: dict[str, dict] = {}
    for e in sorted(events, key=lambda e: (as_dt(e["at"]), e["kind"] != "remove", e["id"])):
        if e["kind"] == "install":
            iv = {"asset_id": e["asset_id"], "position_uri": e["position_uri"], "installed_at": as_dt(e["at"]),
                  "removed_at": None, "install_reason": e.get("reason"), "remove_reason": None,
                  "install_event": e["id"], "remove_event": None, "cost": e.get("cost"), "work_ref": e.get("work_ref")}
            open_by_asset[e["asset_id"]] = iv
            out.append(iv)
        else:
            if e["asset_id"] in open_by_asset:
                closed = open_by_asset.pop(e["asset_id"])
                closed["removed_at"] = as_dt(e["at"])
                closed["remove_reason"] = e.get("reason")
                closed["remove_event"] = e["id"]
    return out


def _org_events(req: Request, **eq) -> list[dict]:
    return req.rows.find("lc_asset_events", org_id=req.org_id, **eq)


def current_position(req: Request, asset_id: str) -> dict | None:
    ivs = intervals(_org_events(req, asset_id=asset_id))
    return next((iv for iv in reversed(ivs) if iv["removed_at"] is None), None)


def occupant(req: Request, position_uri: str) -> dict | None:
    ivs = intervals(_org_events(req, position_uri=position_uri))
    return next((iv for iv in reversed(ivs) if iv["removed_at"] is None), None)


def asset_at(req: Request, position_uri: str, at: datetime) -> dict | None:
    """The interval of the asset that was in `position_uri` at `at`, or None."""
    for iv in intervals(_org_events(req, position_uri=position_uri)):
        if iv["installed_at"] <= at and (iv["removed_at"] is None or at < iv["removed_at"]):
            return iv
    return None


def _check_time(req: Request, asset_id: str, at: datetime) -> None:
    if at > req.now + FUTURE_SLACK:
        raise ValueError("that time is in the future; record an install or removal once it has happened")
    last = _org_events(req, asset_id=asset_id)
    if last and as_dt(last[-1]["at"]) > at:
        raise ValueError(f"the asset's last event is at {as_dt(last[-1]['at']).isoformat()}; events can't go back in time")


def _position(req: Request, uri: Any) -> str:
    if not isinstance(uri, str) or not uri.strip() or len(uri) > 1000 or any(c.isspace() for c in uri.strip()):
        raise ValueError("position_uri is the equipment's URI, e.g. urn:equip:fbf/site/ahu-1/sf-motor")
    uri = uri.strip()
    if not req.allows(uri):
        raise HTTPError(404, "no such position in your sites")
    return uri


def _event(req: Request, asset_id: str, position_uri: str, kind: str, at: datetime, body: dict) -> dict:
    cost = number(body, "cost") if kind == "install" else None
    ev = {"id": rows_mod.new_id(), "org_id": req.org_id, "asset_id": asset_id, "position_uri": position_uri,
          "kind": kind, "at": at, "reason": text(body, "reason", 1000),
          "cost": cost, "work_ref": text(body, "work_ref", 200), "created_by": req.actor(), "created_at": req.now}
    return ev


def install(req: Request, asset_id: str, body: dict) -> dict:
    asset = get_asset(req, asset_id)
    if asset.get("retired_at"):
        raise HTTPError(409, "the asset is retired")
    position = _position(req, body.get("position_uri"))
    at = when(body.get("at"), "at") or req.now
    where = current_position(req, asset_id)
    if where is not None:
        raise HTTPError(409, f"the asset is already installed in {where['position_uri']}; remove it first")
    holder = occupant(req, position)
    if holder is not None:
        raise HTTPError(409, f"{position} already holds asset {holder['asset_id']}; replace it instead")
    _check_time(req, asset_id, at)
    ev = _event(req, asset_id, position, "install", at, body)
    req.rows.insert("lc_asset_events", ev)
    req.rows.audit(_uri(asset_id), "asset.install", req.who, {"position_uri": position, "at": at, "reason": ev["reason"]})
    return ev


def remove(req: Request, asset_id: str, body: dict) -> dict:
    get_asset(req, asset_id)
    where = current_position(req, asset_id)
    if where is None:
        raise HTTPError(409, "the asset isn't installed anywhere")
    _position(req, where["position_uri"])
    at = when(body.get("at"), "at") or req.now
    _check_time(req, asset_id, at)
    if not text(body, "reason", 1000):
        raise ValueError("reason is required: why was it removed?")
    ev = _event(req, asset_id, where["position_uri"], "remove", at, body)
    req.rows.insert("lc_asset_events", ev)
    req.rows.audit(_uri(asset_id), "asset.remove", req.who,
                   {"position_uri": where["position_uri"], "at": at, "reason": ev["reason"]})
    return ev


def replace(req: Request, body: dict) -> dict:
    """Remove whatever is in the position and install another asset there,
    at the same instant. The new asset is `asset_id` (in stock) or `asset`
    (created here)."""
    position = _position(req, body.get("position_uri"))
    at = when(body.get("at"), "at") or req.now
    if not text(body, "remove_reason", 1000):
        raise ValueError("remove_reason is required: why was the old asset removed?")
    holder = occupant(req, position)
    if body.get("asset_id"):
        new = get_asset(req, str(body["asset_id"]))
        if new.get("retired_at"):
            raise HTTPError(409, "the new asset is retired")
        if current_position(req, new["id"]) is not None:
            raise HTTPError(409, "the new asset is installed somewhere else; remove it first")
        _check_time(req, new["id"], at)
    elif isinstance(body.get("asset"), dict):
        new = None
    else:
        raise ValueError("give asset_id (an asset in stock) or asset (a new one's fields)")
    if holder is not None:
        _check_time(req, holder["asset_id"], at)
        if new is not None and holder["asset_id"] == new["id"]:
            raise HTTPError(409, "that asset is already in this position")
    if at > req.now + FUTURE_SLACK:
        raise ValueError("that time is in the future")
    if new is None:
        new = create_asset(req, body["asset"])
    removed = None
    if holder is not None:
        removed = _event(req, holder["asset_id"], position, "remove", at, {"reason": body.get("remove_reason"),
                                                                             "work_ref": body.get("work_ref")})
        req.rows.insert("lc_asset_events", removed)
        req.rows.audit(_uri(holder["asset_id"]), "asset.remove", req.who,
                       {"position_uri": position, "at": at, "reason": removed["reason"], "replaced_by": new["id"]})
    installed = _event(req, new["id"], position, "install", at, {"reason": body.get("reason") or "replacement",
                                                                  "cost": body.get("cost"), "work_ref": body.get("work_ref")})
    req.rows.insert("lc_asset_events", installed)
    req.rows.audit(_uri(new["id"]), "asset.install", req.who,
                   {"position_uri": position, "at": at, "reason": installed["reason"],
                    "replaces": holder["asset_id"] if holder else None})
    return {"removed": removed, "installed": installed, "asset": new}


# --------------------------------------------------------------- views


def _asset_view(a: dict, iv: dict | None) -> dict:
    out = rows_mod.public(a) or {}
    out["status"] = "retired" if a.get("retired_at") else "installed" if iv else "in_stock"
    out["position_uri"] = iv["position_uri"] if iv else None
    out["installed_at"] = iv["installed_at"].isoformat() if iv else None
    return out


def _iv_view(iv: dict, assets: dict[str, dict]) -> dict:
    a = assets.get(iv["asset_id"]) or {}
    return {**rows_mod.public(iv),  # type: ignore[dict-item]
            "asset": {k: a.get(k) for k in ("id", "tag", "asset_type", "manufacturer", "model", "serial")}}


def list_assets(req: Request) -> list[dict]:
    q = req.query
    assets = req.rows.find("lc_assets", org_id=req.org_id)
    open_iv = {iv["asset_id"]: iv for iv in intervals(_org_events(req)) if iv["removed_at"] is None}
    out = []
    needle = (q.get("q") or "").lower()
    for a in assets:
        iv = open_iv.get(a["id"])
        if iv is not None and not req.allows(iv["position_uri"]):
            continue
        v = _asset_view(a, iv)
        if q.get("status") and v["status"] != q["status"]:
            continue
        if q.get("position") and v["position_uri"] != q["position"]:
            continue
        if q.get("manufacturer") and (a.get("manufacturer") or "").lower() != q["manufacturer"].lower():
            continue
        if q.get("type") and q["type"].lower() not in ((a.get("asset_type") or "") + " " + (a.get("brick_class") or "")).lower():
            continue
        if needle and needle not in " ".join(str(a.get(k) or "") for k in ("tag", "manufacturer", "model", "serial",
                                                                            "asset_type")).lower():
            continue
        out.append(v)
    return out


def asset_detail(req: Request, asset_id: str) -> dict:
    a = get_asset(req, asset_id)
    ivs = intervals(_org_events(req, asset_id=asset_id))
    if any(not req.allows(iv["position_uri"]) for iv in ivs):
        raise HTTPError(404, "no such asset")
    cur = next((iv for iv in ivs if iv["removed_at"] is None), None)
    return {**_asset_view(a, cur), "history": [rows_mod.public(iv) for iv in ivs]}


def position_history(req: Request, position_uri: str) -> dict:
    position = _position(req, position_uri)
    ivs = intervals(_org_events(req, position_uri=position))
    assets = {a["id"]: a for a in req.rows.find("lc_assets", org_id=req.org_id, id=[iv["asset_id"] for iv in ivs])} if ivs else {}
    return {"position_uri": position, "history": [_iv_view(iv, assets) for iv in ivs],
            "current": next((_iv_view(iv, assets) for iv in ivs if iv["removed_at"] is None), None)}


def changes(req: Request) -> list[dict]:
    """Replacements: an install into a position that held a different asset
    before, in [since, until). Filter by type/brick class or by the new
    asset's manufacturer: "pumps changed in 2031, to brand Y"."""
    since = when(req.query.get("since"), "since")
    until = when(req.query.get("until"), "until")
    want_type = (req.query.get("type") or "").lower()
    want_make = (req.query.get("manufacturer") or "").lower()
    ivs = intervals(_org_events(req))
    by_pos: dict[str, list[dict]] = {}
    for iv in ivs:
        by_pos.setdefault(iv["position_uri"], []).append(iv)
    assets = {a["id"]: a for a in req.rows.find("lc_assets", org_id=req.org_id)}
    out = []
    for pos, seq in by_pos.items():
        if not req.allows(pos):
            continue
        for prev, cur in pairwise(seq):
            t = cur["installed_at"]
            if (since and t < since) or (until and t >= until):
                continue
            new, old = assets.get(cur["asset_id"], {}), assets.get(prev["asset_id"], {})
            kinds = " ".join(str(x.get(k) or "") for x in (new, old) for k in ("asset_type", "brick_class")).lower()
            if want_type and want_type not in kinds:
                continue
            if want_make and (new.get("manufacturer") or "").lower() != want_make:
                continue
            out.append({"position_uri": pos, "at": t.isoformat(), "removed_reason": prev["remove_reason"],
                        "from": {k: old.get(k) for k in ("id", "manufacturer", "model", "serial")},
                        "to": {k: new.get(k) for k in ("id", "manufacturer", "model", "serial")},
                        "brand_changed": (old.get("manufacturer") or "").lower() != (new.get("manufacturer") or "").lower()})
    return sorted(out, key=lambda c: c["at"])


# --------------------------------------------------------------- routes


@route("GET", r"/assets")
def _list(req: Request):
    return list_assets(req)


@route("POST", r"/assets")
def _create(req: Request):
    return 201, rows_mod.public(create_asset(req, req.json()))


@route("GET", r"/assets/changes")
def _changes(req: Request):
    return changes(req)


@route("GET", r"/assets/([^/]+)")
def _get(req: Request, asset_id: str):
    return asset_detail(req, asset_id)


@route("PATCH", r"/assets/([^/]+)")
def _patch(req: Request, asset_id: str):
    asset_detail(req, asset_id)  # 404 outside the caller's sites
    a = get_asset(req, asset_id)
    body = req.json()
    unknown = set(body) - set(EDITABLE)
    if unknown:
        raise ValueError(f"not editable: {', '.join(sorted(unknown))} (install and remove history is append-only)")
    f = _fields(body)
    changed = {k: {"from": a.get(k), "to": v} for k, v in f.items() if a.get(k) != v}
    if not changed:
        return rows_mod.public(a)
    out = req.rows.update("lc_assets", asset_id, {**f, "updated_at": req.now})
    req.rows.audit(_uri(asset_id), "asset.edit", req.who, changed)
    return rows_mod.public(out)


@route("POST", r"/assets/([^/]+)/retire")
def _retire(req: Request, asset_id: str):
    a = get_asset(req, asset_id)
    if a.get("retired_at"):
        raise HTTPError(409, "already retired")
    if current_position(req, asset_id) is not None:
        raise HTTPError(409, "remove it from its position first")
    reason = need(req.json(), "reason")
    out = req.rows.update("lc_assets", asset_id, {"retired_at": req.now, "retired_reason": reason[:1000],
                                                  "updated_at": req.now})
    req.rows.audit(_uri(asset_id), "asset.retire", req.who, {"reason": reason})
    return rows_mod.public(out)


@route("POST", r"/assets/([^/]+)/install")
def _install(req: Request, asset_id: str):
    return 201, rows_mod.public(install(req, asset_id, req.json()))


@route("POST", r"/assets/([^/]+)/remove")
def _remove(req: Request, asset_id: str):
    return 201, rows_mod.public(remove(req, asset_id, req.json()))


@route("GET", r"/assets/([^/]+)/audit")
def _audit(req: Request, asset_id: str):
    asset_detail(req, asset_id)
    return req.rows.audit_log(_uri(asset_id))


@route("GET", r"/positions/history")
def _position_history(req: Request):
    return position_history(req, req.query.get("uri"))


@route("GET", r"/positions/asset-at")
def _asset_at(req: Request):
    position = _position(req, req.query.get("uri"))
    at = when(req.query.get("at"), "at") or req.now
    iv = asset_at(req, position, at)
    if iv is None:
        return {"position_uri": position, "at": at.isoformat(), "asset": None}
    return {"position_uri": position, "at": at.isoformat(), "interval": rows_mod.public(iv),
            "asset": rows_mod.public(get_asset(req, iv["asset_id"]))}


@route("POST", r"/positions/replace")
def _replace(req: Request):
    out = replace(req, req.json())
    return 201, {k: rows_mod.public(v) for k, v in out.items()}


def install_date(req: Request, asset_id: str) -> date | None:
    """The asset's first install date (warranty and service-life math)."""
    ivs = intervals(_org_events(req, asset_id=asset_id))
    return ivs[0]["installed_at"].date() if ivs else None

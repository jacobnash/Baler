"""
Deferred maintenance, service-life references and FCI (#200, design §6, §8.3).

- **Deferred-maintenance register**: work that was put off, with its cost,
  its risk and the asset (or position) it affects. Items are closed
  (done, cancelled), never deleted, so the register's history stays.
- **Service-life reference**: expected life per asset type. Baler ships
  *no* values. A person enters each one with the reference it came from
  (for example ASHRAE's public service-life database), its URL and the
  date it was read. Published tables disagree with each other and change,
  so the citation and its date travel with the number.
- **Replacement value**: dated estimates per asset, append-only; the
  latest is current.
- **FCI** (facility condition index) = open deferred cost / current
  replacement value of the installed assets in scope. The roll-up states
  how many assets have no replacement value, because a ratio over a
  partial denominator overstates the condition problem.
- **Capital plan**: per year, assets reaching the end of their expected
  life (by first install date + the type's median years) and deferred items
  targeted at that year. It feeds the building passport (#219).

Schema: migration 0004.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from baler.lifecycle import assets
from baler.lifecycle import rows as rows_mod
from baler.lifecycle.routes import HTTPError, Request, need, number, route, text, when

DEFERRED_COLS = ("id", "org_id", "asset_id", "position_uri", "title", "description", "category", "cost", "currency",
                 "risk", "identified_on", "target_year", "status", "resolved_at", "resolution", "source", "created_by",
                 "created_at", "updated_at")
LIFE_COLS = ("id", "org_id", "asset_type", "median_years", "source_title", "source_url", "retrieved_on", "note",
             "created_by", "created_at", "updated_at")
VALUE_COLS = ("id", "org_id", "asset_id", "replacement_value", "currency", "as_of", "basis", "created_by", "created_at")
rows_mod.register("lc_deferred_items", DEFERRED_COLS, ("source",))
rows_mod.register("lc_service_life", LIFE_COLS, order="asset_type")
rows_mod.register("lc_asset_values", VALUE_COLS, order="as_of")

RISKS = ("low", "medium", "high", "critical")
OPEN = ("open", "planned")
STATUSES = ("open", "planned", "done", "cancelled")


def _date(v: Any, key: str) -> date | None:
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    d = when(v, key)
    return d.date() if d else None


def _f(v: Any) -> float:
    return float(v) if isinstance(v, (Decimal, int, float)) else 0.0


# ---------------------------------------------------------- deferred items


def _item_fields(req: Request, body: dict, old: dict | None = None) -> dict:
    out: dict[str, Any] = {}
    for k, limit in (("title", 300), ("description", 4000), ("category", 100), ("currency", 10), ("resolution", 2000)):
        if k in body:
            out[k] = text(body, k, limit)
    if "cost" in body:
        cost = number(body, "cost")
        if cost is None or cost < 0:
            raise ValueError("cost is a number >= 0")
        out["cost"] = cost
    if "risk" in body:
        if body["risk"] not in RISKS:
            raise ValueError(f"risk is one of {', '.join(RISKS)}")
        out["risk"] = body["risk"]
    if "identified_on" in body:
        out["identified_on"] = _date(body["identified_on"], "identified_on")
    if "target_year" in body:
        y = body["target_year"]
        if y is not None and (not isinstance(y, int) or isinstance(y, bool) or not 1900 < y < 2200):
            raise ValueError("target_year is a year")
        out["target_year"] = y
    if "source" in body:
        if body["source"] is not None and not isinstance(body["source"], dict):
            raise ValueError("source is {document_id, page, quote}")
        out["source"] = body["source"]
    if body.get("asset_id"):
        assets.asset_detail(req, str(body["asset_id"]))
        out["asset_id"] = str(body["asset_id"])
    if body.get("position_uri"):
        out["position_uri"] = assets._position(req, body["position_uri"])
    merged = {**(old or {}), **out}
    if not merged.get("title"):
        raise ValueError("title is required")
    if merged.get("cost") is None:
        raise ValueError("cost is required (an estimate is fine; say so in description)")
    if not merged.get("asset_id") and not merged.get("position_uri"):
        raise ValueError("give the asset_id or position_uri it affects")
    return out


def _visible(req: Request, item: dict) -> bool:
    if item.get("position_uri") and not req.allows(item["position_uri"]):
        return False
    if item.get("asset_id"):
        try:
            assets.asset_detail(req, item["asset_id"])
        except HTTPError:
            return False
    return True


def _get_item(req: Request, item_id: str) -> dict:
    it = req.rows.get("lc_deferred_items", item_id)
    if it is None or it["org_id"] != req.org_id or not _visible(req, it):
        raise HTTPError(404, "no such item")
    return it


def items(req: Request) -> list[dict]:
    return [i for i in req.rows.find("lc_deferred_items", org_id=req.org_id) if _visible(req, i)]


# ------------------------------------------------------------ service life


def life_for(refs: list[dict], asset: dict) -> dict | None:
    keys = {(asset.get("asset_type") or "").lower(), (asset.get("brick_class") or "").lower()} - {""}
    return next((r for r in refs if r["asset_type"].lower() in keys), None)


def current_values(req: Request) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for v in req.rows.find("lc_asset_values", org_id=req.org_id):  # oldest first: the newest wins
        prev = out.get(v["asset_id"])
        if prev is None or (_date(v["as_of"], "as_of"), str(v["created_at"])) >= (_date(prev["as_of"], "as_of"),
                                                                                   str(prev["created_at"])):
            out[v["asset_id"]] = v
    return out


def asset_life(req: Request) -> list[dict]:
    """Installed assets with their age, expected life and what's left."""
    today = req.now.date()
    refs = req.rows.find("lc_service_life", org_id=req.org_id)
    values = current_values(req)
    ivs = assets.intervals(req.rows.find("lc_asset_events", org_id=req.org_id))
    first: dict[str, date] = {}
    current: dict[str, dict] = {}
    for iv in ivs:
        first.setdefault(iv["asset_id"], iv["installed_at"].date())
        if iv["removed_at"] is None:
            current[iv["asset_id"]] = iv
    out = []
    for a in req.rows.find("lc_assets", org_id=req.org_id):
        iv = current.get(a["id"])
        if iv is None or not req.allows(iv["position_uri"]):
            continue
        ref = life_for(refs, a)
        age = (today - first[a["id"]]).days / 365.25
        row: dict[str, Any] = {
            "asset_id": a["id"], "asset": {k: a.get(k) for k in ("tag", "asset_type", "manufacturer", "model", "serial")},
            "position_uri": iv["position_uri"], "installed_on": first[a["id"]].isoformat(), "age_years": round(age, 1),
            "replacement_value": _f(values[a["id"]]["replacement_value"]) if a["id"] in values else None,
            "expected_life_years": None, "remaining_years": None, "end_of_life_year": None, "reference": None}
        if ref:
            years = _f(ref["median_years"])
            row.update(expected_life_years=years, remaining_years=round(years - age, 1),
                       end_of_life_year=first[a["id"]].year + round(years),
                       reference={k: rows_mod.public(ref)[k] for k in ("source_title", "source_url", "retrieved_on")})
        out.append(row)
    return out


def fci(req: Request) -> dict:
    open_items = [i for i in items(req) if i["status"] in OPEN]
    deferred = sum(_f(i["cost"]) for i in open_items)
    life = asset_life(req)
    valued = [r for r in life if r["replacement_value"] is not None]
    crv = sum(r["replacement_value"] for r in valued)
    return {"deferred_cost": round(deferred, 2), "replacement_value": round(crv, 2),
            "fci": round(deferred / crv, 4) if crv > 0 else None, "open_items": len(open_items),
            "installed_assets": len(life), "assets_without_value": len(life) - len(valued),
            "by_risk": {r: round(sum(_f(i["cost"]) for i in open_items if i["risk"] == r), 2) for r in RISKS},
            "note": ("FCI = open deferred cost / replacement value of installed assets with a value. "
                     + (f"{len(life) - len(valued)} installed asset(s) have no replacement value, so the ratio is "
                        "overstated." if len(life) > len(valued) else ""))}


def capital_plan(req: Request, years: int = 20) -> list[dict]:
    today = req.now.date()
    plan: dict[int, dict] = {}

    def year(y: int) -> dict:
        return plan.setdefault(y, {"year": y, "end_of_life": [], "deferred": [], "replacement_value": 0.0,
                                   "deferred_cost": 0.0})

    for r in asset_life(req):
        if r["end_of_life_year"] is not None and r["end_of_life_year"] <= today.year + years:
            y = year(max(r["end_of_life_year"], today.year))
            y["end_of_life"].append(r)
            y["replacement_value"] += r["replacement_value"] or 0.0
    for i in items(req):
        if i["status"] in OPEN:
            y = year(i.get("target_year") or today.year)
            y["deferred"].append(rows_mod.public(i))
            y["deferred_cost"] += _f(i["cost"])
    return [plan[k] for k in sorted(plan)]


# ----------------------------------------------------------------- routes


@route("GET", r"/deferred")
def _list(req: Request):
    st = req.query.get("status")
    return [rows_mod.public(i) for i in items(req) if not st or i["status"] in st.split(",")]


@route("POST", r"/deferred")
def _create(req: Request):
    body = req.json()
    f = _item_fields(req, body)
    row = {"risk": "medium", "identified_on": req.now.date(), **f, "id": rows_mod.new_id(), "org_id": req.org_id,
           "status": "open", "created_by": req.actor(), "created_at": req.now, "updated_at": req.now}
    req.rows.insert("lc_deferred_items", row)
    req.rows.audit(rows_mod.entity_uri("deferred", row["id"]), "deferred.create", req.who, f)
    return 201, rows_mod.public(row)


@route("GET", r"/deferred/fci")
def _fci(req: Request):
    return fci(req)


@route("GET", r"/deferred/capital-plan")
def _plan(req: Request):
    years = int(req.query.get("years") or 20)
    if not 1 <= years <= 100:
        raise ValueError("years is 1-100")
    return capital_plan(req, years)


@route("PATCH", r"/deferred/([^/]+)")
def _patch(req: Request, item_id: str):
    old = _get_item(req, item_id)
    body = req.json()
    allowed = {"title", "description", "category", "cost", "currency", "risk", "identified_on", "target_year", "source",
               "status", "resolution"}
    unknown = set(body) - allowed
    if unknown:
        raise ValueError(f"not editable: {', '.join(sorted(unknown))}")
    f = _item_fields(req, body, old)
    if "status" in body:
        if body["status"] not in STATUSES:
            raise ValueError(f"status is one of {', '.join(STATUSES)}")
        f["status"] = body["status"]
        if body["status"] in ("done", "cancelled") and old["status"] in OPEN:
            if not f.get("resolution") and not old.get("resolution"):
                raise ValueError("resolution is required to close an item")
            f["resolved_at"] = req.now
        elif body["status"] in OPEN:
            f["resolved_at"] = None
    diff = {k: {"from": old.get(k), "to": v} for k, v in f.items() if old.get(k) != v}
    if diff:
        req.rows.update("lc_deferred_items", item_id, {**f, "updated_at": req.now})
        req.rows.audit(rows_mod.entity_uri("deferred", item_id), "deferred.edit", req.who, diff)
    return rows_mod.public(_get_item(req, item_id))


@route("GET", r"/service-life")
def _life_refs(req: Request):
    return [rows_mod.public(r) for r in req.rows.find("lc_service_life", org_id=req.org_id)]


@route("POST", r"/service-life")
def _life_set(req: Request):
    """Add or replace the reference for an asset type (audited)."""
    body = req.json()
    asset_type = need(body, "asset_type")[:200]
    years = number(body, "median_years")
    if years is None or not 0 < years < 200:
        raise ValueError("median_years is the expected life in years")
    title = need(body, "source_title")[:300]
    retrieved = _date(body.get("retrieved_on"), "retrieved_on")
    if retrieved is None:
        raise ValueError("retrieved_on is required: the date the reference was read (references change)")
    fields = {"asset_type": asset_type, "median_years": years, "source_title": title,
              "source_url": text(body, "source_url", 1000), "retrieved_on": retrieved, "note": text(body, "note", 2000)}
    existing = next((r for r in req.rows.find("lc_service_life", org_id=req.org_id)
                     if r["asset_type"].lower() == asset_type.lower()), None)
    if existing:
        req.rows.update("lc_service_life", existing["id"], {**fields, "updated_at": req.now})
        rid = existing["id"]
        detail = {k: {"from": existing.get(k), "to": v} for k, v in fields.items() if existing.get(k) != v}
    else:
        rid = rows_mod.new_id()
        req.rows.insert("lc_service_life", {**fields, "id": rid, "org_id": req.org_id, "created_by": req.actor(),
                                            "created_at": req.now, "updated_at": req.now})
        detail = fields
    req.rows.audit(rows_mod.entity_uri("service_life", rid), "service_life.set", req.who, detail)
    return (200 if existing else 201), rows_mod.public(req.rows.get("lc_service_life", rid))


@route("GET", r"/service-life/assets")
def _life_assets(req: Request):
    return asset_life(req)


@route("GET", r"/assets/([^/]+)/values")
def _values(req: Request, asset_id: str):
    assets.asset_detail(req, asset_id)
    return [rows_mod.public(v) for v in req.rows.find("lc_asset_values", org_id=req.org_id, asset_id=asset_id)]


@route("POST", r"/assets/([^/]+)/values")
def _add_value(req: Request, asset_id: str):
    assets.asset_detail(req, asset_id)
    body = req.json()
    v = number(body, "replacement_value")
    if v is None or v < 0:
        raise ValueError("replacement_value is a number >= 0")
    row = {"id": rows_mod.new_id(), "org_id": req.org_id, "asset_id": asset_id, "replacement_value": v,
           "currency": text(body, "currency", 10), "as_of": _date(body.get("as_of"), "as_of") or req.now.date(),
           "basis": text(body, "basis", 1000), "created_by": req.actor(), "created_at": req.now}
    req.rows.insert("lc_asset_values", row)
    req.rows.audit(rows_mod.entity_uri("asset", asset_id), "asset.value", req.who,
                   {"replacement_value": v, "as_of": row["as_of"], "basis": row["basis"]})
    return 201, rows_mod.public(row)

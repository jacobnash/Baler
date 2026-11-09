"""
COBie import and export: Type, Component, Warranty, Spare, Job (#204,
design §5).

COBie is the US owner-handover interchange: a spreadsheet whose sheets and
columns are defined by NIBS, *National BIM Standard-United States, Version
3*, section 4.2, COBie, Annex A. Only column names (schema field names) are
used here; no text of the standard is reproduced. Per Annex A, warranty
terms sit on the **Type** sheet (WarrantyGuarantorParts/Labor,
WarrantyDurationParts/Labor, WarrantyDurationUnit) and each
**Component** carries its WarrantyStartDate.

Import (handover in) seeds the lifecycle record:

  - each Component becomes an asset, with its Type's manufacturer and
    model, its serial, tag, bar code and space kept;
  - optionally installed into a position: COBie knows spaces, not Brick
    equipment, so the caller maps component names to position URIs;
  - its Type's parts and labour warranties become warranties on it,
    starting at its WarrantyStartDate (else its InstallationDate);
  - each Job becomes a PM task on its Type (day/week/month/year
    frequencies; others are reported, not guessed);
  - each Spare becomes a row in the spares list.

It is a dry run unless `commit` is true: the plan lists every row it would
write, and every row it skipped and why. A component already imported (same
COBie name) is skipped, so importing the same workbook twice adds nothing.

Export (handover or sale out) writes the same sheets from the record:
assets grouped into Types, warranties onto their Type, PM tasks as Jobs,
spares as Spares.

Needs openpyxl (the cx-spec extra).
"""

from __future__ import annotations

import base64
import io
from datetime import date, datetime
from typing import Any

from baler.lifecycle import assets, pm, warranties
from baler.lifecycle import rows as rows_mod
from baler.lifecycle.routes import HTTPError, Raw, Request, need, route, text

rows_mod.register("lc_spares", ("id", "org_id", "type_name", "name", "category", "suppliers", "set_number",
                                "part_number", "description", "source", "created_by", "created_at"), ("source",))

# Column names of the COBie worksheets (Annex A). Only the ones Baler reads or writes.
TYPE_COLUMNS = ("Name", "CreatedBy", "CreatedOn", "Category", "Description", "AssetType", "Manufacturer", "ModelNumber",
                "WarrantyGuarantorParts", "WarrantyDurationParts", "WarrantyGuarantorLabor", "WarrantyDurationLabor",
                "WarrantyDurationUnit", "ExtSystem", "ExtObject", "ExtIdentifier", "ReplacementCost", "ExpectedLife",
                "DurationUnit", "WarrantyDescription")
COMPONENT_COLUMNS = ("Name", "CreatedBy", "CreatedOn", "TypeName", "Space", "Description", "ExtSystem", "ExtObject",
                     "ExtIdentifier", "SerialNumber", "InstallationDate", "WarrantyStartDate", "TagNumber", "BarCode",
                     "AssetIdentifier")
SPARE_COLUMNS = ("Name", "CreatedBy", "CreatedOn", "Category", "TypeName", "Suppliers", "ExtSystem", "ExtObject",
                 "ExtIdentifier", "Description", "SetNumber", "PartNumber")
JOB_COLUMNS = pm.COBIE_JOB_COLUMNS
SHEETS = ("Type", "Component", "Spare", "Job")
MAX_BYTES = 30 * 1024 * 1024
_FREQ_DAYS = {"day": 1, "daily": 1, "week": 7, "weekly": 7, "month": 30, "monthly": 30, "quarter": 91, "quarterly": 91,
              "year": 365, "yearly": 365, "annual": 365, "annually": 365}
_N_A = {"", "n/a", "na", "-", "none"}


def _s(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in _N_A else s


def _num(v: Any) -> float | None:
    s = _s(v)
    if s is None:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _day(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = _s(v)
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")[:19]).date()
    except ValueError:
        return None


def _months(duration: Any, unit: Any) -> int | None:
    n = _num(duration)
    if not n or n <= 0:
        return None
    u = (_s(unit) or "year").lower()
    if u.startswith("year"):
        return round(n * 12)
    if u.startswith("month"):
        return round(n)
    if u.startswith("week"):
        return max(1, round(n / 4.345))
    if u.startswith("day"):
        return max(1, round(n / 30.44))
    return None


# ---------------------------------------------------------------- read


def read_workbook(data: bytes) -> dict[str, list[dict]]:
    """{sheet: [row dicts by header]} for the sheets Baler reads."""
    try:
        import openpyxl
    except ImportError:
        raise HTTPError(501, "COBie needs openpyxl (the cx-spec extra) on the server") from None
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - openpyxl raises many types on a bad file
        raise ValueError(f"not a readable .xlsx workbook ({type(exc).__name__})") from None
    out: dict[str, list[dict]] = {}
    names = {n.lower(): n for n in wb.sheetnames}
    for sheet in SHEETS:
        if sheet.lower() not in names:
            out[sheet] = []
            continue
        it = wb[names[sheet.lower()]].iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else "" for h in next(it, ())]
        rows = []
        for n, r in enumerate(it, start=2):
            row = {header[i]: v for i, v in enumerate(r) if i < len(header) and header[i]}
            if _s(row.get("Name")):
                rows.append({**row, "_row": n})
        out[sheet] = rows
    wb.close()
    return out


# ------------------------------------------------------------------ plan


def plan(req: Request, book: dict[str, list[dict]], positions: dict[str, str]) -> dict:
    """What an import would write, and what it would skip and why."""
    types = {_s(t["Name"]): t for t in book["Type"]}
    existing = {(a.get("attributes") or {}).get("cobie_component") for a in req.rows.find("lc_assets", org_id=req.org_id)}
    existing.discard(None)
    out: dict[str, list] = {"assets": [], "installs": [], "warranties": [], "pm_tasks": [], "spares": [], "skipped": []}
    for c in book["Component"]:
        name = _s(c["Name"])
        if name in existing:
            out["skipped"].append({"sheet": "Component", "row": c["_row"], "name": name, "why": "already imported"})
            continue
        tname = _s(c.get("TypeName"))
        t = types.get(tname, {})
        if tname and not t:
            out["skipped"].append({"sheet": "Component", "row": c["_row"], "name": name,
                                   "why": f"TypeName {tname!r} isn't on the Type sheet (imported without type data)"})
        asset = {"tag": _s(c.get("TagNumber")) or name, "asset_type": tname,
                 "manufacturer": _s(t.get("Manufacturer")), "model": _s(t.get("ModelNumber")),
                 "serial": _s(c.get("SerialNumber")), "cost": _num(t.get("ReplacementCost")),
                 "attributes": {k: v for k, v in {
                     "cobie_component": name, "cobie_type": tname, "space": _s(c.get("Space")),
                     "bar_code": _s(c.get("BarCode")), "asset_identifier": _s(c.get("AssetIdentifier")),
                     "description": _s(c.get("Description")), "category": _s(t.get("Category")),
                     "expected_life": _s(t.get("ExpectedLife")), "expected_life_unit": _s(t.get("DurationUnit")),
                 }.items() if v is not None}}
        out["assets"].append({"row": c["_row"], "component": name, "asset": asset})
        installed = _day(c.get("InstallationDate"))
        if name in positions:
            out["installs"].append({"component": name, "position_uri": positions[name],
                                    "at": (installed or req.now.date()).isoformat(), "reason": "COBie handover"})
        start = _day(c.get("WarrantyStartDate")) or installed
        for part, label in (("Parts", "parts"), ("Labor", "labor")):
            months = _months(t.get(f"WarrantyDuration{part}"), t.get("WarrantyDurationUnit"))
            if not months:
                continue
            if start is None:
                out["skipped"].append({"sheet": "Type", "row": t.get("_row"), "name": tname,
                                       "why": f"{label} warranty for {name}: no WarrantyStartDate or InstallationDate"})
                continue
            out["warranties"].append({"component": name, "kind": label, "starts_on": start.isoformat(),
                                      "duration_months": months, "provider": _s(t.get(f"WarrantyGuarantor{part}")),
                                      "terms": _s(t.get("WarrantyDescription")),
                                      "source": {"quote": f"COBie Type {tname}: WarrantyDuration{part} "
                                                          f"{t.get(f'WarrantyDuration{part}')} {t.get('WarrantyDurationUnit') or ''}".strip()}})
    for j in book["Job"]:
        unit = (_s(j.get("FrequencyUnit")) or "").lower()
        freq = _num(j.get("Frequency"))
        tname = _s(j.get("TypeName"))
        if not tname or not freq or unit not in _FREQ_DAYS:
            out["skipped"].append({"sheet": "Job", "row": j["_row"], "name": _s(j["Name"]),
                                   "why": f"needs TypeName and a day/week/month/year frequency (got {freq} {unit or '-'})"})
            continue
        out["pm_tasks"].append({"title": _s(j["Name"]), "description": _s(j.get("Description")), "asset_type": tname,
                                "trigger": "calendar", "interval_days": max(1, round(freq * _FREQ_DAYS[unit])),
                                "duration_hours": _num(j.get("Duration")) if (_s(j.get("DurationUnit")) or "").lower().startswith("hour") else None,
                                "source": {"cobie_job": _s(j["Name"]), "row": j["_row"]}})
    for sp in book["Spare"]:
        if not _s(sp.get("TypeName")):
            out["skipped"].append({"sheet": "Spare", "row": sp["_row"], "name": _s(sp["Name"]), "why": "no TypeName"})
            continue
        out["spares"].append({"type_name": _s(sp["TypeName"]), "name": _s(sp["Name"]), "category": _s(sp.get("Category")),
                              "suppliers": _s(sp.get("Suppliers")), "set_number": _s(sp.get("SetNumber")),
                              "part_number": _s(sp.get("PartNumber")), "description": _s(sp.get("Description")),
                              "source": {"row": sp["_row"]}})
    return out


def commit(req: Request, p: dict) -> dict:
    """Write a plan through the same functions (and checks, and audit) the
    API uses for each kind of row."""
    ids: dict[str, str] = {}
    errors: list[dict] = []
    for a in p["assets"]:
        ids[a["component"]] = assets.create_asset(req, a["asset"])["id"]
    for i in p["installs"]:
        try:
            assets.install(req, ids[i["component"]], i)
        except (HTTPError, ValueError) as exc:
            errors.append({"component": i["component"], "step": "install", "why": getattr(exc, "message", str(exc))})
    for w in p["warranties"]:
        try:
            warranties.create(req, ids[w["component"]], {k: v for k, v in w.items() if k != "component"})
        except (HTTPError, ValueError) as exc:
            errors.append({"component": w["component"], "step": "warranty", "why": getattr(exc, "message", str(exc))})
    for t in p["pm_tasks"]:
        fields = pm._task_fields(t, {"status": "active"})
        row = {**fields, "id": rows_mod.new_id(), "org_id": req.org_id, "created_by": req.actor(), "created_at": req.now,
               "updated_at": req.now}
        req.rows.insert("lc_pm_tasks", row)
        req.rows.audit(rows_mod.entity_uri("pm_task", row["id"]), "pm.create", req.who, {"via": "cobie", **t})
    for sp in p["spares"]:
        req.rows.insert("lc_spares", {**sp, "id": rows_mod.new_id(), "org_id": req.org_id, "created_by": req.actor(),
                                      "created_at": req.now})
    return {"created": {"assets": len(p["assets"]), "installs": len(p["installs"]) - sum(e["step"] == "install" for e in errors),
                        "warranties": len(p["warranties"]) - sum(e["step"] == "warranty" for e in errors),
                        "pm_tasks": len(p["pm_tasks"]), "spares": len(p["spares"])}, "errors": errors}


# ---------------------------------------------------------------- export


def _type_name(a: dict) -> str:
    return ((a.get("attributes") or {}).get("cobie_type")
            or " ".join(x for x in (a.get("asset_type") or "Asset", a.get("manufacturer"), a.get("model")) if x))


def export(req: Request) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    info = wb.active
    info.title = "Instruction"
    info.append(["Exported from Baler"])
    info.append(["Sheets: Type, Component, Spare, Job. Column names per NIBS NBIMS-US V3 4.2 COBie, Annex A."])
    info.append([f"Created {req.now.date().isoformat()}"])
    org_assets = [a for a in req.rows.find("lc_assets", org_id=req.org_id) if not a.get("retired_at")]
    ivs = assets.intervals(req.rows.find("lc_asset_events", org_id=req.org_id))
    first_install: dict[str, date] = {}
    for iv in ivs:
        if req.allows(iv["position_uri"]):
            first_install.setdefault(iv["asset_id"], iv["installed_at"].date())
    hidden = {iv["asset_id"] for iv in ivs if not req.allows(iv["position_uri"])}
    org_assets = [a for a in org_assets if a["id"] not in hidden]
    ws_by_asset: dict[str, list[dict]] = {}
    for w in req.rows.find("lc_warranties", org_id=req.org_id):
        if not w.get("voided_at"):
            ws_by_asset.setdefault(w["asset_id"], []).append(warranties._norm(w))
    who = req.actor() or "baler"
    created = req.now.replace(microsecond=0).isoformat()

    types: dict[str, dict] = {}
    for a in org_assets:
        name = _type_name(a)
        t = types.setdefault(name, {"Name": name, "CreatedBy": who, "CreatedOn": created,
                                    "Category": (a.get("attributes") or {}).get("category") or a.get("brick_class") or "",
                                    "Description": a.get("asset_type") or "", "AssetType": "Fixed",
                                    "Manufacturer": a.get("manufacturer") or "", "ModelNumber": a.get("model") or "",
                                    "ExtSystem": "Baler", "ExtObject": "lc_assets", "ExtIdentifier": "",
                                    "ReplacementCost": a.get("cost") or "", "WarrantyDurationUnit": "month"})
        for w in ws_by_asset.get(a["id"], []):
            months = round((w["ends_on"] - w["starts_on"]).days / 30.44)
            for part, kinds in (("Parts", ("parts", "parts_and_labor")), ("Labor", ("labor", "parts_and_labor"))):
                if w["kind"] in kinds and not t.get(f"WarrantyDuration{part}"):
                    t[f"WarrantyDuration{part}"] = months
                    t[f"WarrantyGuarantor{part}"] = w.get("provider") or ""
                    t.setdefault("WarrantyDescription", w.get("terms") or "")
    sheet = wb.create_sheet("Type")
    sheet.append(list(TYPE_COLUMNS))
    for t in types.values():
        sheet.append([t.get(c, "") for c in TYPE_COLUMNS])

    sheet = wb.create_sheet("Component")
    sheet.append(list(COMPONENT_COLUMNS))
    for a in org_assets:
        attrs = a.get("attributes") or {}
        starts = [w["starts_on"] for w in ws_by_asset.get(a["id"], [])]
        inst = first_install.get(a["id"])
        sheet.append([{"Name": attrs.get("cobie_component") or a.get("tag") or a["id"], "CreatedBy": who,
                       "CreatedOn": created, "TypeName": _type_name(a), "Space": attrs.get("space") or "",
                       "Description": attrs.get("description") or a.get("asset_type") or "", "ExtSystem": "Baler",
                       "ExtObject": "lc_assets", "ExtIdentifier": a["id"], "SerialNumber": a.get("serial") or "",
                       "InstallationDate": inst.isoformat() if inst else "",
                       "WarrantyStartDate": min(starts).isoformat() if starts else "", "TagNumber": a.get("tag") or "",
                       "BarCode": attrs.get("bar_code") or "", "AssetIdentifier": attrs.get("asset_identifier") or ""}[c]
                      for c in COMPONENT_COLUMNS])

    sheet = wb.create_sheet("Spare")
    sheet.append(list(SPARE_COLUMNS))
    for sp in req.rows.find("lc_spares", org_id=req.org_id):
        sheet.append([{"Name": sp["name"], "CreatedBy": who, "CreatedOn": created, "Category": sp.get("category") or "",
                       "TypeName": sp["type_name"], "Suppliers": sp.get("suppliers") or "", "ExtSystem": "Baler",
                       "ExtObject": "lc_spares", "ExtIdentifier": sp["id"], "Description": sp.get("description") or "",
                       "SetNumber": sp.get("set_number") or "", "PartNumber": sp.get("part_number") or ""}[c]
                      for c in SPARE_COLUMNS])

    sheet = wb.create_sheet("Job")
    sheet.append(list(JOB_COLUMNS))
    for j in pm.cobie_jobs(req):
        sheet.append([j.get(c, "") for c in JOB_COLUMNS])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- routes


@route("POST", r"/cobie/import")
def _import(req: Request):
    body = req.json()
    try:
        data = base64.b64decode(need(body, "workbook"), validate=True)
    except (ValueError, TypeError):
        raise ValueError("workbook is the .xlsx file, base64-encoded") from None
    if len(data) > MAX_BYTES:
        raise HTTPError(413, "the workbook is over 30 MB")
    positions = body.get("positions") or {}
    if not isinstance(positions, dict) or not all(isinstance(v, str) for v in positions.values()):
        raise ValueError("positions maps COBie component names to position URIs")
    for uri in positions.values():
        assets._position(req, uri)
    p = plan(req, read_workbook(data), positions)
    summary = {k: len(v) for k, v in p.items()}
    if not body.get("commit"):
        return {"dry_run": True, "summary": summary, "plan": p}
    result = commit(req, p)
    req.rows.audit(rows_mod.entity_uri("cobie", req.org_id), "cobie.import", req.who,
                   {"filename": text(body, "filename", 300), "summary": summary, "errors": len(result["errors"])})
    return 201, {"dry_run": False, "summary": summary, **result, "skipped": p["skipped"]}


@route("GET", r"/cobie/export")
def _export(req: Request):
    try:
        data = export(req)
    except ImportError:
        raise HTTPError(501, "COBie needs openpyxl (the cx-spec extra) on the server") from None
    return Raw(data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
               f"cobie-{req.now.date().isoformat()}.xlsx")


@route("GET", r"/spares")
def _spares(req: Request):
    t = req.query.get("type")
    return [rows_mod.public(s) for s in req.rows.find("lc_spares", org_id=req.org_id, order="type_name")
            if not t or s["type_name"].lower() == t.lower()]


@route("POST", r"/spares")
def _add_spare(req: Request):
    body = req.json()
    row = {"id": rows_mod.new_id(), "org_id": req.org_id, "type_name": need(body, "type_name")[:200],
           "name": need(body, "name")[:300], "category": text(body, "category", 100), "suppliers": text(body, "suppliers", 500),
           "set_number": text(body, "set_number", 100), "part_number": text(body, "part_number", 100),
           "description": text(body, "description", 2000), "source": None, "created_by": req.actor(), "created_at": req.now}
    req.rows.insert("lc_spares", row)
    req.rows.audit(rows_mod.entity_uri("spare", row["id"]), "spare.create", req.who,
                   {k: row[k] for k in ("type_name", "name", "part_number")})
    return 201, rows_mod.public(row)


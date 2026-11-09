"""
Requirement packs: standards as data (Baler#205, epic #221 §3.2).

Hard-coding "an OR needs N air changes" is wrong three ways: the number depends on
the standard's edition, on the jurisdiction, and owners add their own. So a
requirement is a row of data, in a versioned pack, and is judged by an
engine Baler already runs. A pack adds the citation, the edition, the
jurisdiction and the applicability around that engine; it never brings a
new one.

A pack:

    {"pack_id": "ashrae-170-or", "version": "2021-structure.1",
     "title": "...", "source": {"standard": "ASHRAE 170", "edition": "2021"},
     "jurisdiction": ["US"], "notice": "...",
     "requirements": [ROW, ...]}

A row:

    {"id": "or-pressure",
     "title": "Pressure relationship to adjacent spaces",
     "citation": {"ref": "Table N, Operating room", "page": null},
     "applies_to": {"space_type": ["operating_room"], "equip_class": [],
                    "phase": ["acceptance", "operations"]},
     "criterion": {"quantity": "pressure_to_adjacent", "op": ">=",
                   "value": null, "unit": "inH2O"},
     "evidence": {"point_class": "Differential_Pressure_Sensor",
                  "window_hours": 24, "coverage": 0.95},
     "check": {"engine": "procedure", "criterion": "range"},
     "verified_by": null}

Rules:

- **Citations only.** A row cites where its number comes from (`ref`,
  optional `page`). It carries no text from the standard: the only free
  text is the row's own short title and an optional `note`, both capped.
  A verbatim quote belongs in an extraction proposal (#206), which stays
  with the user's own uploaded document and never in a shipped pack.
- **Engines are reused.** `check.engine` is one of:
    cx         a Cx check id or family (exists, live, pair:, template:,
               gl36:, cx:econ_)
    procedure  a procedure criterion kind (range, availability, response)
    shacl      a template in ontology/cx-expected-points.ttl
    gl36       a ConStrain item Baler runs (commissioning.gl36.ITEMS)
- **Nothing is active until a person confirms its numbers.** A value may
  be blank (structure-only packs ship that way). Entering or changing a
  value clears the row's `verified_by`; confirming records who and when.
  `active_rows` returns only confirmed rows with every value filled.
- **Versions are immutable once published.** A draft is edited; publishing
  freezes it. A new edition is a new pack version, and a project adopts a
  version by choice (cx_project_packs), so a building designed to FGI 2018
  stays judged by FGI 2018.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime

from baler.commissioning import procedures
from baler.commissioning import templates as tpl

STATUSES = ("draft", "published", "retired")
ENGINES = ("cx", "procedure", "shacl", "gl36")
PHASES = ("design", "construction", "startup", "functional", "acceptance", "warranty", "operations")
OPS = (">=", "<=", ">", "<", "between", "==")
CX_FAMILIES = ("exists", "live", "pair:", "template:", "gl36:", "cx:econ_")
MAX_ROWS = 500
MAX_TITLE = 160
MAX_NOTE = 300
_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,79}")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,39}")
_CLASS = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,99}")
_SLUG = re.compile(r"[a-z][a-z0-9_]{0,59}")

# The quantities rows are written in, with the unit a value is entered in
# and the point classes that usually evidence them. Units are the ones the
# row must name; Baler does not convert between them.
QUANTITIES: dict[str, dict] = {
    "pressure_to_adjacent": {"units": ["inH2O", "Pa"], "point_class": "Differential_Pressure_Sensor",
                             "label": "Pressure relationship to adjacent spaces"},
    "total_ach": {"units": ["1/h"], "point_class": None, "label": "Total air changes per hour"},
    "outdoor_ach": {"units": ["1/h"], "point_class": None, "label": "Outdoor air changes per hour"},
    "temperature": {"units": ["degF", "degC"], "point_class": "Zone_Air_Temperature_Sensor", "label": "Space temperature"},
    "relative_humidity": {"units": ["%RH"], "point_class": "Zone_Air_Humidity_Sensor", "label": "Space relative humidity"},
    "filtration": {"units": ["MERV", "HEPA"], "point_class": None, "label": "Filtration (final filter)"},
    "airflow": {"units": ["cfm", "L/s"], "point_class": "Supply_Air_Flow_Sensor", "label": "Airflow"},
    "exhaust_direct": {"units": ["bool"], "point_class": None, "label": "All room air exhausted directly outdoors"},
    "recirculation_by_room_units": {"units": ["bool"], "point_class": None, "label": "Recirculation by room units allowed"},
}


def _text(v, what: str, limit: int, required: bool = False) -> str | None:
    if v is None or v == "":
        if required:
            raise ValueError(f"{what} is required")
        return None
    if not isinstance(v, str) or len(v) > limit:
        raise ValueError(f"{what} must be a string of at most {limit} characters")
    return v.strip()


def _list(v, what: str, rx: re.Pattern | None = None, allowed: tuple | None = None) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or len(v) > 50 or not all(isinstance(x, str) for x in v):
        raise ValueError(f"{what} is a list of up to 50 strings")
    for x in v:
        if (rx and not rx.fullmatch(x)) or (allowed and x not in allowed):
            raise ValueError(f"{what}: {x!r} isn't valid" + (f" (one of {', '.join(allowed)})" if allowed else ""))
    return list(dict.fromkeys(v))


def _number(v, what: str) -> float | int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise TypeError(f"{what} must be a number or null (blank until a person enters it)")
    return v


def check_ref(check, where: str, shacl_ids: set[str] | None = None) -> dict:
    """The engine a row names, checked to exist in Baler today."""
    if not isinstance(check, dict) or check.get("engine") not in ENGINES:
        raise ValueError(f"{where}: check.engine must be one of {', '.join(ENGINES)}")
    eng = check["engine"]
    if eng == "cx":
        cid = check.get("check_id")
        if not isinstance(cid, str) or not (cid in ("exists", "live") or cid.startswith(CX_FAMILIES[2:])):
            raise ValueError(f"{where}: check.check_id must be a Cx check (exists, live, or a pair:/template:/gl36:/cx:econ_ id)")
        return {"engine": eng, "check_id": cid}
    if eng == "procedure":
        kind = check.get("criterion")
        if kind not in ("range", "availability", "response"):
            raise ValueError(f"{where}: check.criterion must be range, availability or response (commissioning.procedures)")
        return {"engine": eng, "criterion": kind}
    if eng == "shacl":
        ids = shacl_ids if shacl_ids is not None else {t.id for t in tpl.load()}
        if check.get("template") not in ids:
            raise ValueError(f"{where}: check.template must be one of the templates in ontology/cx-expected-points.ttl")
        return {"engine": eng, "template": check["template"]}
    from baler.commissioning import gl36

    if check.get("item") not in gl36.ITEMS:
        raise ValueError(f"{where}: check.item must be a GL36 item Baler runs ({', '.join(sorted(gl36.ITEMS))})")
    return {"engine": eng, "item": check["item"]}


def _criterion(c, where: str) -> dict:
    if not isinstance(c, dict):
        raise TypeError(f"{where}: criterion is an object")
    q = c.get("quantity")
    if q is not None and (not isinstance(q, str) or not _SLUG.fullmatch(q)):
        raise ValueError(f"{where}: criterion.quantity is a slug like temperature")
    op = c.get("op")
    if op is not None and op not in OPS:
        raise ValueError(f"{where}: criterion.op must be one of {' '.join(OPS)}")
    unit = _text(c.get("unit"), f"{where}: criterion.unit", 20)
    if q in QUANTITIES and unit is not None and unit not in QUANTITIES[q]["units"]:
        raise ValueError(f"{where}: {q} is entered in {' or '.join(QUANTITIES[q]['units'])}, not {unit}")
    out: dict = {"quantity": q, "op": op, "unit": unit}
    if op == "between":
        out["min"] = _number(c.get("min"), f"{where}: criterion.min")
        out["max"] = _number(c.get("max"), f"{where}: criterion.max")
        if out["min"] is not None and out["max"] is not None and out["min"] > out["max"]:
            raise ValueError(f"{where}: criterion.min is above criterion.max")
    else:
        v = c.get("value")
        out["value"] = v if isinstance(v, str) and q == "filtration" and len(v) <= 20 else _number(v, f"{where}: criterion.value")
    return out


def blanks(row: dict) -> list[str]:
    """The criterion values still to be entered."""
    c = row["criterion"]
    if c.get("op") is None:
        return []
    keys = ("min", "max") if c["op"] == "between" else ("value",)
    return [k for k in keys if c.get(k) is None]


def normalize_row(r, where: str, shacl_ids: set[str] | None = None) -> dict:
    if not isinstance(r, dict):
        raise TypeError(f"{where} is an object")
    known = {"id", "title", "citation", "applies_to", "criterion", "evidence", "check", "verified_by", "note"}
    if set(r) - known:
        raise ValueError(f"{where}: unknown fields {', '.join(sorted(set(r) - known))} (rows carry citations, never quoted text)")
    rid = r.get("id")
    if not isinstance(rid, str) or not _ID.fullmatch(rid):
        raise ValueError(f"{where}: id is a slug like or-pressure")
    cit = r.get("citation")
    if not isinstance(cit, dict) or not _text(cit.get("ref"), f"{where}: citation.ref", 120, required=True):
        raise ValueError(f"{where}: citation.ref (table, section or clause) is required")
    if set(cit) - {"ref", "page"}:
        raise ValueError(f"{where}: citation is {{ref, page}}: no quoted text in a pack")
    page = cit.get("page")
    if page is not None and (isinstance(page, bool) or not isinstance(page, (int, str))):
        raise ValueError(f"{where}: citation.page is a number or label")
    app = r.get("applies_to") or {}
    if not isinstance(app, dict) or set(app) - {"space_type", "equip_class", "phase"}:
        raise ValueError(f"{where}: applies_to is {{space_type, equip_class, phase}}")
    applies = {"space_type": _list(app.get("space_type"), f"{where}: applies_to.space_type", _SLUG),
               "equip_class": _list(app.get("equip_class"), f"{where}: applies_to.equip_class", _CLASS),
               "phase": _list(app.get("phase"), f"{where}: applies_to.phase", allowed=PHASES)}
    if not applies["space_type"] and not applies["equip_class"]:
        raise ValueError(f"{where}: applies_to needs a space_type or an equip_class")
    ev = r.get("evidence") or {}
    if not isinstance(ev, dict) or set(ev) - {"point_class", "window_hours", "coverage"}:
        raise ValueError(f"{where}: evidence is {{point_class, window_hours, coverage}}")
    pc = ev.get("point_class")
    if pc is not None and (not isinstance(pc, str) or not _CLASS.fullmatch(pc)):
        raise ValueError(f"{where}: evidence.point_class is a Brick class name")
    wh = ev.get("window_hours", 24)
    cov = ev.get("coverage", 0.95)
    if isinstance(wh, bool) or not isinstance(wh, (int, float)) or not 0 < wh <= 24 * 31:
        raise ValueError(f"{where}: evidence.window_hours must be in (0, 744]")
    if isinstance(cov, bool) or not isinstance(cov, (int, float)) or not 0 < cov <= 1:
        raise ValueError(f"{where}: evidence.coverage is a fraction in (0, 1]")
    vb = r.get("verified_by")
    if vb is not None and (not isinstance(vb, dict) or not vb.get("user") or not vb.get("at")):
        raise ValueError(f"{where}: verified_by is {{user, at}} or null")
    return {"id": rid, "title": _text(r.get("title"), f"{where}: title", MAX_TITLE, required=True),
            "citation": {"ref": cit["ref"].strip(), "page": page}, "applies_to": applies,
            "criterion": _criterion(r.get("criterion") or {}, where),
            "evidence": {"point_class": pc, "window_hours": wh, "coverage": cov},
            "check": check_ref(r.get("check"), where, shacl_ids), "verified_by": vb,
            "note": _text(r.get("note"), f"{where}: note", MAX_NOTE)}


def normalize(body) -> dict:
    """A pack document, checked. Raises ValueError (TypeError for a wrong
    type) naming the first problem."""
    if not isinstance(body, dict):
        raise TypeError("a pack is an object")
    known = {"pack_id", "version", "title", "source", "jurisdiction", "notice", "requirements"}
    if set(body) - known:
        raise ValueError(f"unknown pack fields: {', '.join(sorted(set(body) - known))}")
    pid = body.get("pack_id")
    if not isinstance(pid, str) or not _ID.fullmatch(pid):
        raise ValueError("pack_id is a slug like ashrae-170-or")
    ver = body.get("version")
    if not isinstance(ver, str) or not _VERSION.fullmatch(ver):
        raise ValueError("version is a short label like 2021.1")
    src = body.get("source")
    if not isinstance(src, dict) or set(src) - {"standard", "edition", "publisher", "kind"}:
        raise ValueError("source is {standard, edition, publisher?, kind?}")
    standard = _text(src.get("standard"), "source.standard", 120, required=True)
    edition = _text(src.get("edition"), "source.edition", 40, required=True)  # a requirement without an edition is a guess
    rows = body.get("requirements")
    if not isinstance(rows, list) or not 0 < len(rows) <= MAX_ROWS:
        raise ValueError(f"requirements is a list of 1 to {MAX_ROWS} rows")
    shacl_ids = {t.id for t in tpl.load()}
    out_rows = [normalize_row(r, f"requirements[{i}]", shacl_ids) for i, r in enumerate(rows)]
    ids = [r["id"] for r in out_rows]
    if len(set(ids)) != len(ids):
        raise ValueError("requirement ids must be unique within a pack")
    return {"pack_id": pid, "version": ver, "title": _text(body.get("title"), "title", MAX_TITLE, required=True),
            "source": {"standard": standard, "edition": edition, "publisher": _text(src.get("publisher"), "source.publisher", 120),
                       "kind": _text(src.get("kind"), "source.kind", 40)},
            "jurisdiction": _list(body.get("jurisdiction"), "jurisdiction"),
            "notice": _text(body.get("notice"), "notice", 600), "requirements": out_rows}


def set_values(pack: dict, rid: str, values: dict, who: str | None, now: datetime) -> dict:
    """Enter or change a draft row's criterion values. Any change clears
    the row's confirmation: a person confirms the numbers as they now are."""
    if pack["status"] != "draft":
        raise ValueError("only a draft pack's values can change: published versions are frozen (make a new version)")
    if not isinstance(values, dict) or set(values) - {"value", "min", "max", "op", "unit"}:
        raise ValueError("values is {value | min, max, op?, unit?}")
    rows = copy.deepcopy(pack["requirements"])
    row = next((r for r in rows if r["id"] == rid), None)
    if row is None:
        raise KeyError(rid)
    crit = _criterion({**row["criterion"], **values}, f"requirement {rid}")
    if crit != row["criterion"]:
        row["criterion"] = crit
        row["verified_by"] = None
    return {"requirements": rows, "history": [*pack.get("history", []),
                                              {"at": now.isoformat(), "by": who, "action": "values", "row": rid, "values": values}]}


def confirm(pack: dict, rid: str, who: str | None, email: str | None, now: datetime) -> dict:
    """A person confirms a row's numbers for the pack's edition. Refused
    while any value is blank."""
    if pack["status"] != "draft":
        raise ValueError("only a draft pack's rows can be confirmed")
    rows = copy.deepcopy(pack["requirements"])
    row = next((r for r in rows if r["id"] == rid), None)
    if row is None:
        raise KeyError(rid)
    missing = blanks(row)
    if missing:
        raise ValueError(f"enter {', '.join(missing)} before confirming: blank values are never active")
    row["verified_by"] = {"user": who, "email": email, "at": now.isoformat(), "edition": pack["source"]["edition"]}
    return {"requirements": rows, "history": [*pack.get("history", []),
                                              {"at": now.isoformat(), "by": who, "action": "confirm", "row": rid}]}


def active_rows(pack: dict) -> list[dict]:
    """The rows an engine may judge: confirmed, with every value filled."""
    return [r for r in pack["requirements"] if r.get("verified_by") and not blanks(r)]


def applies(row: dict, *, space_type: str | None = None, equip_classes=(), phase: str | None = None) -> bool:
    """Does the row apply to this space or equipment in this phase?
    Equipment classes are matched subclass-aware (Brick)."""
    app = row["applies_to"]
    if app["phase"] and phase is not None and phase not in app["phase"]:
        return False
    if space_type is not None and space_type in app["space_type"]:
        return True
    if app["equip_class"] and equip_classes:
        want = set().union(*(procedures._family(c) for c in app["equip_class"]))
        return any(c in want for c in equip_classes)
    return False


def summary(pack: dict) -> dict:
    rows = pack["requirements"]
    return {"rows": len(rows), "confirmed": sum(1 for r in rows if r.get("verified_by")),
            "blank": sum(1 for r in rows if blanks(r)), "active": len(active_rows(pack))}

"""
Spec ingestion and spec<->device alignment (Cx 7, Baler#43).

What the design documents say (equipment schedules, point lists,
sequences) becomes spec items. Two ways in:

  - CSV/XLSX: a schedule or point list a person exported. Columns are
    matched by common names. Rows a person supplied are `accepted` unless
    the import asks for `proposed`.
  - PDF via the Claude API (`extract_pdf`, the `llm` extra, the
    autotag/llm_classifier pattern): every extracted item is a *proposal*
    (`proposed`) a human accepts, corrects or rejects, and every one cites
    its document, page and the text it was read from.

Alignment matches accepted spec equipment to the inventory, keyed on
Brick classes and real urn:equip: URIs (PR #5's alignment, re-keyed). The
evidence is weighed as an integrator would: the tag first (allowing for
delimiters, case, leading zeros and known abbreviations), then whether the
Brick class agrees with the type the spec states. Confidence is a word,
decided by rules on the evidence:

  confirmed  a person confirmed the mapping
  high       the tag matches and the class agrees
  medium     the tag matches but the class is unknown on one side, or the
             letters differ by a known abbreviation only
  low        the number matches but the letters don't, or the class
             contradicts the spec - named alternatives say what else it
             could be
  unmatched  nothing in the inventory fits

Disagreements are deviations, never resolved silently: a spec item with
nothing in the field (read per phase - absent during construction is
progress, absent in operations is an outage), a type mismatch, two spec
items claiming one device, and field equipment no spec item names.
"""

from __future__ import annotations

import base64
import csv
import io
import re
from dataclasses import dataclass

from baler import brick_vocab

KINDS = ("equipment", "point", "sequence")
STATUSES = ("proposed", "accepted", "rejected")
CONFIDENCE = ("confirmed", "high", "medium", "low", "unmatched")
MAX_ROWS = 20000
MAX_PDF_BYTES = 30 * 1024 * 1024
# A field equipment is a candidate at all from this score; the confidence
# word comes from rules on the evidence, not from this number (#5).
MIN_CANDIDATE_SCORE = 0.25


@dataclass(frozen=True)
class EquipType:
    name: str
    brick: str
    letters: tuple[str, ...]  # tag prefixes, preferred first
    words: tuple[str, ...]  # how a schedule's "type" column says it


TYPES: tuple[EquipType, ...] = (
    EquipType("air handling unit", "Air_Handling_Unit", ("AHU", "AH"), ("air handling", "air handler", "ahu")),
    EquipType("rooftop unit", "Rooftop_Unit", ("RTU",), ("rooftop", "roof top", "rtu", "packaged unit")),
    EquipType("makeup air unit", "Makeup_Air_Unit", ("MAU",), ("makeup air", "make-up air", "make up air", "mau")),
    EquipType("dedicated outdoor air system", "Dedicated_Outdoor_Air_System_Unit", ("DOAS",), ("dedicated outdoor", "doas")),
    EquipType("variable air volume box", "Variable_Air_Volume_Box", ("VAV", "VB", "TU"), ("variable air volume", "vav", "terminal unit")),
    EquipType("fan coil unit", "Fan_Coil_Unit", ("FCU", "FC"), ("fan coil", "fcu")),
    EquipType("chiller", "Chiller", ("CH", "CHLR", "CHL"), ("chiller",)),
    EquipType("boiler", "Boiler", ("B", "BLR"), ("boiler",)),
    EquipType("pump", "Pump", ("P", "PMP", "CHWP", "HWP", "CWP", "PCHWP", "SCHWP"), ("pump",)),
    EquipType("exhaust fan", "Exhaust_Fan", ("EF",), ("exhaust fan",)),
    EquipType("cooling tower", "Cooling_Tower", ("CT",), ("cooling tower",)),
    EquipType("heat exchanger", "Heat_Exchanger", ("HX", "HEX"), ("heat exchanger",)),
    EquipType("meter", "Meter", ("M", "MTR", "EM"), ("meter",)),
)
_BY_LETTERS = {letters: t for t in TYPES for letters in t.letters}


# ------------------------------------------------------------------- tags


def normalize_tag(tag) -> str:
    """AHU-1 == ahu_1 == AHU 1 == AHU01: letters upper, digits without
    leading zeros, delimiters dropped."""
    parts = re.findall(r"[A-Za-z]+|\d+", str(tag or ""))
    return "".join(p.upper() if p.isalpha() else str(int(p)) for p in parts)


def tag_letters(tag) -> str:
    m = re.match(r"\s*([A-Za-z]+)", str(tag or ""))
    return m.group(1).upper() if m else ""


def tag_number(tag) -> str:
    return "".join(str(int(n)) for n in re.findall(r"\d+", str(tag or "")))


def type_from_tag(tag) -> EquipType | None:
    return _BY_LETTERS.get(tag_letters(tag))


def type_from_text(text) -> EquipType | None:
    t = str(text or "").lower()
    if not t:
        return None
    hit = next((x for x in TYPES if any(w in t for w in x.words)), None)
    if hit:
        return hit
    return _BY_LETTERS.get(t.strip().upper())


def brick_class_for(type_text=None, tag=None) -> tuple[str | None, str]:
    """(Brick class, how it was decided) for a spec equipment row."""
    vocab = brick_vocab.load()
    if type_text:
        canon = vocab.canonical_class(str(type_text).strip().replace(" ", "_"))
        if canon:
            return canon, "type column is a Brick class"
        t = type_from_text(type_text)
        if t:
            return t.brick, f"type column says {t.name}"
    t = type_from_tag(tag)
    if t:
        return t.brick, f"inferred from the tag's letters ({tag_letters(tag)})"
    return None, "type unknown"


def point_class_for(name=None, type_text=None) -> str | None:
    vocab = brick_vocab.load()
    for text in (type_text, name):
        if not text:
            continue
        canon = vocab.canonical_class(str(text).strip().replace(" ", "_"))
        if canon:
            return canon
    for text in (type_text, name):
        if not text:
            continue
        low = str(text).lower()
        role = ("Setpoint" if re.search(r"\b(sp|stpt|setpoint|set point)\b", low) else
                "Command" if re.search(r"\b(cmd|command|start|stop|enable)\b", low) else
                "Status" if re.search(r"\b(sts|status|proof)\b", low) else
                "Alarm" if "alarm" in low else "Sensor")
        words = str(text).split()
        hits: list[str] = []
        while words and not hits:  # "Supply Fan Status" -> "Fan Status" -> Fan_Status
            hits = vocab.suggest_class(" ".join(words), 8)
            words = words[1:]
        best = next((h for h in hits if h.endswith("_" + role)), None)
        if best is None and role == "Sensor":
            best = next(iter(vocab.suggest_class(f"{text} sensor", 1)), None)
        if best is None:
            best = next((h for h in hits if not h.endswith(("_Alarm", "_Parameter"))), None)
        if best:
            return best
    return None


# ------------------------------------------------------------- CSV / XLSX

_ALIASES = {
    "tag": ("tag", "equipmenttag", "equiptag", "mark", "unittag", "id", "designation", "unit"),
    "equipment": ("equipment", "equip", "equipmentid", "parent", "unitid", "device", "controller", "system"),
    "name": ("name", "description", "desc", "pointname", "point", "objectname", "label"),
    "type": ("type", "equipmenttype", "pointtype", "class", "brickclass", "category", "kind"),
    "location": ("location", "room", "area", "space", "floor", "serves", "servicearea"),
    "units": ("units", "unit", "engineeringunits", "eu", "uom"),
    "page": ("page", "sheet"),
}


def _squeeze(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _field_map(headers) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in headers:
        k = _squeeze(h)
        for field, names in _ALIASES.items():
            if k in names and field not in out and h not in out.values():
                out[field] = h
                break
    return out


def read_rows(fmt: str, data: str) -> list[dict]:
    """CSV text, or XLSX as base64 (first sheet, first row headers)."""
    if fmt == "csv":
        rows = list(csv.DictReader(io.StringIO(data.lstrip("﻿"))))
    elif fmt == "xlsx":
        try:
            import openpyxl
        except ImportError as exc:
            raise ValueError("XLSX import needs openpyxl: install baler with the cx-spec extra") from exc
        try:
            raw = base64.b64decode(data, validate=True)
        except ValueError as exc:
            raise ValueError("xlsx data must be base64") from exc
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        sheet = wb.worksheets[0]
        it = sheet.iter_rows(values_only=True)
        headers = [str(h) if h is not None else "" for h in next(it, [])]
        rows = [{headers[i]: v for i, v in enumerate(r) if i < len(headers) and headers[i]} for r in it]
    else:
        raise ValueError("format must be csv or xlsx")
    if len(rows) > MAX_ROWS:
        raise ValueError(f"at most {MAX_ROWS} rows")
    return rows


def _s(v, limit: int = 500) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s[:limit] if s else None


def items_from_rows(rows: list[dict], document: str, kind_hint: str = "auto") -> list[dict]:
    """Spec items (without ids) from schedule / point-list rows. A row
    with an equipment column and a point name is a point; a row with a
    tag is equipment."""
    out = []
    for i, row in enumerate(rows):
        fmap = _field_map(row.keys())

        def get(f, fm=fmap, r=row):
            return _s(r.get(fm[f])) if f in fm else None

        tag, equip, name, typ = get("tag"), get("equipment"), get("name"), get("type")
        is_point = kind_hint == "points" or (kind_hint == "auto" and equip and name and not tag)
        used = set(fmap.values())
        extra = {str(k): _s(v) for k, v in row.items() if k not in used and k and _s(v)}
        src = {"document": document, "row": i + 2, "page": get("page"), "method": "table"}
        if is_point:
            if not (equip and (name or typ)):
                continue
            cls = point_class_for(name, typ)
            out.append({"kind": "point", "tag": name or typ, "name": name or typ, "equip_tag": equip, "brick_class": cls,
                        "properties": {"type": typ, "units": get("units"), **extra}, "source": src,
                        "confidence": None if cls is None else 0.6})
        else:
            tag = tag or (equip if not name else None)
            if not tag:
                continue
            cls, how = brick_class_for(typ, tag)
            out.append({"kind": "equipment", "tag": tag, "name": name or typ or tag, "equip_tag": None, "brick_class": cls,
                        "properties": {"type": typ, "location": get("location"), "class_basis": how, **extra},
                        "source": src, "confidence": None})
    return out


# --------------------------------------------------------------- alignment


def _class_family(cls: str | None) -> set[str]:
    if not cls:
        return set()
    return brick_vocab.load().descendants(cls) or {cls}


def _class_agrees(spec_cls: str | None, equip_classes: list[str]) -> bool | None:
    """True/False, or None when one side has no class."""
    if not spec_cls or not equip_classes:
        return None
    fam = _class_family(spec_cls)
    return any(c in fam or spec_cls in _class_family(c) for c in equip_classes)


def _field_tags(equip: dict) -> list[str]:
    tail = equip["id"].rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return [t for t in {equip.get("dis") or "", tail} if t]


def _tag_evidence(spec_tag: str, equip: dict) -> tuple[str, float, str] | None:
    """(kind, score, detail) of the best tag evidence, or None."""
    want = normalize_tag(spec_tag)
    best = None
    for name in _field_tags(equip):
        if normalize_tag(name) == want:
            return ("exact", 1.0, f"field name {name!r} is {spec_tag} (delimiters/case/leading zeros aside)")
        # a name like "AHU-1 Supply" or "Bldg A AHU_1": the tag as a token run
        if want and want in [normalize_tag(t) for t in re.findall(r"[A-Za-z]+[\s_\-]*\d+(?:[\s_\-]+\d+)*", name)]:
            best = best or ("contains", 0.9, f"field name {name!r} carries the tag {spec_tag}")
        sl, fl = tag_letters(spec_tag), tag_letters(name)
        if tag_number(name) and tag_number(name) == tag_number(spec_tag) and sl and fl and sl != fl:
            st, ft = _BY_LETTERS.get(sl), _BY_LETTERS.get(fl)
            if st is not None and st is ft:
                cand = ("abbreviation", 0.8, f"field name {name!r}: {fl} is a known abbreviation of {sl}, same number")
            else:
                cand = ("number_only", 0.4, f"field name {name!r} has the number {tag_number(spec_tag)} but letters {fl}, not {sl}")
            if best is None or cand[1] > best[1]:
                best = cand
        elif sl and sl == fl and tag_number(name) and tag_number(spec_tag) and tag_number(name) != tag_number(spec_tag):
            cand = ("letters_only", 0.2, f"field name {name!r} is a {sl} but number {tag_number(name)}, not {tag_number(spec_tag)}")
            if best is None or cand[1] > best[1]:
                best = cand
    return best


def _word(tag_kind: str, agrees: bool | None) -> str:
    if tag_kind in ("exact", "contains"):
        return "high" if agrees else ("low" if agrees is False else "medium")
    if tag_kind == "abbreviation":
        return "medium" if agrees is not False else "low"
    return "low"


PHASE_ABSENT = {
    "design": "the design isn't built yet - nothing in the field is expected",
    "construction": "not yet installed or not yet networked - progress, not a fault",
    "startup": "should be powered and networked by start-up - check it is installed and on the network",
    "functional": "functional testing needs it online - it is missing from the network",
    "acceptance": "acceptance needs it online - it is missing from the network",
    "warranty": "missing in warranty - the contractor is still responsible",
    "operations": "expected to be running - missing in operations is an outage, or the documentation is stale",
}


def align(items: list[dict], equips: list[dict], phase: str | None, decided: dict[str, dict] | None = None) -> dict:
    """{"mappings": [...], "deviations": [...]} for accepted equipment
    items. `decided` maps spec item id -> a person's confirmed or
    rejected mapping ({status, equip_uri}); a confirmation pins it, a
    rejection takes that equipment out of the running for that item."""
    decided = decided or {}
    spec_equip = [i for i in items if i["kind"] == "equipment" and i["status"] == "accepted"]
    by_id = {e["id"]: e for e in equips}
    mappings: list[dict] = []
    for item in spec_equip:
        d = decided.get(item["id"])
        if d and d["status"] == "confirmed" and d["equip_uri"] in by_id:
            mappings.append({"spec_item_id": item["id"], "spec_tag": item["tag"], "equip_uri": d["equip_uri"],
                             "confidence": "confirmed", "basis": [{"kind": "person", "detail": "confirmed by a person"}],
                             "alternatives": []})
            continue
        rejected = d["equip_uri"] if d and d["status"] == "rejected" else None
        cands = []
        for e in equips:
            if e["id"] == rejected:
                continue
            ev = _tag_evidence(item["tag"], e)
            if ev is None:
                continue
            agrees = _class_agrees(item.get("brick_class"), e.get("brickClasses") or [])
            score = ev[1] + (0.1 if agrees else (-0.3 if agrees is False else 0))
            if score >= MIN_CANDIDATE_SCORE:
                cands.append((score, e, ev, agrees))
        cands.sort(key=lambda c: (-c[0], c[1]["id"]))
        if not cands:
            mappings.append({"spec_item_id": item["id"], "spec_tag": item["tag"], "equip_uri": None, "confidence": "unmatched",
                             "basis": [], "alternatives": []})
            continue
        score, e, ev, agrees = cands[0]
        basis = [{"kind": "tag", "evidence": ev[0], "score": ev[1], "detail": ev[2]}]
        if agrees is not None:
            basis.append({"kind": "class", "agrees": agrees,
                          "detail": f"spec says {item.get('brick_class')}, the field says {', '.join(e.get('brickClasses') or [])}"})
        word = _word(ev[0], agrees)
        alts = [{"equip_uri": c[1]["id"], "interpretation": f"{c[2][2]}" + ("" if c[3] is not False else " (class contradicts the spec)")}
                for c in cands[1:4]]
        if word == "low" and ev[0] == "number_only":
            alts.append({"equip_uri": None, "interpretation": f"not {item['tag']} at all - {item['tag']} may be named differently or not networked"})
        if agrees is False:
            alts.append({"equip_uri": e["id"], "interpretation": "the equipment was replaced or re-purposed, or the model's class is wrong - "
                         "check the nameplate"})
        mappings.append({"spec_item_id": item["id"], "spec_tag": item["tag"], "equip_uri": e["id"], "confidence": word,
                         "basis": basis, "alternatives": alts})

    deviations: list[dict] = []
    claimed: dict[str, list[dict]] = {}
    for m in mappings:
        if m["equip_uri"]:
            claimed.setdefault(m["equip_uri"], []).append(m)
        if m["confidence"] == "unmatched":
            deviations.append({"kind": "spec_not_found", "spec_tag": m["spec_tag"], "spec_item_id": m["spec_item_id"],
                               "severity": "info" if phase in ("design", "construction") else "warning",
                               "interpretation": PHASE_ABSENT.get(phase or "", "not found in the inventory this project can see"),
                               "to_settle": "find it on the network (Windrow device list) or confirm it isn't installed"})
        if any(b.get("kind") == "class" and b.get("agrees") is False for b in m["basis"]):
            deviations.append({"kind": "type_mismatch", "spec_tag": m["spec_tag"], "spec_item_id": m["spec_item_id"],
                               "equip_uri": m["equip_uri"], "severity": "warning",
                               "interpretation": "the tag matches but the class doesn't: a replacement, a re-purposed controller, "
                                                 "or a wrong class in the model",
                               "to_settle": "nameplate or controller program; correct the class with the record editor if the model is wrong"})
    for uri, ms in claimed.items():
        if len(ms) > 1:
            deviations.append({"kind": "duplicate_claim", "equip_uri": uri, "spec_tags": [m["spec_tag"] for m in ms],
                               "severity": "warning", "interpretation": "two spec items point at the same equipment",
                               "to_settle": "confirm one mapping; the other is named differently or missing"})
    templated = {c for t in TYPES for c in _class_family(t.brick)}
    for e in equips:
        if e["id"] not in claimed and any(c in templated for c in e.get("brickClasses") or []):
            deviations.append({"kind": "unspecified_in_field", "equip_uri": e["id"], "severity": "info",
                               "interpretation": f"{e.get('dis') or e['id']} is in the field but no accepted spec item names it",
                               "to_settle": "add it to the schedule, or confirm it's out of scope"})
    return {"mappings": mappings, "deviations": deviations}


def points_alignment(item: dict, equip: dict, point_items: list[dict]) -> list[dict]:
    """Spec points of one equipment against its inventory points: present
    (a point of that class exists), absent (never invented), and field
    points no spec point names."""
    out = []
    used: set[str] = set()
    mine = [p for p in point_items if normalize_tag(p.get("equip_tag")) == normalize_tag(item["tag"]) and p["status"] == "accepted"]
    for sp in mine:
        fam = _class_family(sp.get("brick_class"))
        hit = next((p for p in equip["points"] if p["id"] not in used and fam and p.get("brickClass") in fam), None)
        if hit:
            used.add(hit["id"])
        out.append({"spec_item_id": sp["id"], "spec_name": sp["name"], "brick_class": sp.get("brick_class"),
                    "status": "present" if hit else "specified_absent", "point_uri": hit["id"] if hit else None})
    if mine:
        for p in equip["points"]:
            if p["id"] not in used:
                out.append({"spec_item_id": None, "spec_name": None, "brick_class": p.get("brickClass"),
                            "status": "unspecified_present", "point_uri": p["id"]})
    return out

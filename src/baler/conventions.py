"""
Site naming conventions (Baler#186): annotate one label, map hundreds.

An integrator works out a site's naming convention once - `AHU{n}-{point}`,
`VAV-{flr}-{n}-{pt}` - and today does it in a spreadsheet. In Baler they
mark up one label in a tiny annotation language:

    fbf/{equip_type:ahu}-{equip_id:1}/{point:DIS-AIR-TEM}-{ignore:12}

Plain text is literal; each `{role:text}` is a field. Roles:

  equip_type  the equipment kind's code (`ahu`, `chw-pump`)
  equip_id    the equipment's number or name within its kind
  point       the point's code (`DIS-AIR-TEM`, `SAT`)
  ignore      anything that varies but means nothing (an instance number)

That compiles to a template (an anchored regex), which runs over every
point label (the source topic) on the site. A convention can hold several
templates, tried in order: a site with two styles needs two annotations.
Codes are normalised (lower case, letters and digits only), so
`Discharge_Air_Temp`, `discharge_air_temp` and `DischargeAirTemp` are one
code. The convention's dictionaries say what each code means:
`point_codes` {code: Brick class}, `equip_codes` {code: Brick class}. One
entry covers every point with that code, which is the leverage: a preview
lists the unknown codes by how many points each would settle, most first.

It is rung 2 of the ladder, before the rules: autotag classifies a
matching point whose code is known as `convention-classified`, and the
equipment it belongs to from its equip code. Applying a convention from
the API is a person's action, written like a hand edit (record_edit) and
logged in record_edits.

This is NREL BuildingMOTIF's label-parser idea, not its code. openhay#7's
shared tokenizer and abbreviation lexicon aren't built yet; when they are,
an unknown code can be pre-filled from the lexicon instead of left blank.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from rdflib import RDF, SKOS, Literal, URIRef

from baler import brick_vocab, ddl
from baler.store import BRICK, PROJ, TD

ROLES = ("equip_type", "equip_id", "point", "ignore")
# Non-greedy, so the literals around a field decide where it ends.
_ROLE_RE = {
    "equip_type": r"[A-Za-z][A-Za-z_-]*?",
    "equip_id": r"[A-Za-z0-9]+?",
    "point": r"[A-Za-z0-9][A-Za-z0-9 ._-]*?",
}
_FIELD = re.compile(r"\{(\w+):([^{}]*)\}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS naming_conventions (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    scope_prefix TEXT NOT NULL,          -- the topic prefix it applies under ('' = everywhere visible)
    templates    JSONB NOT NULL,         -- annotation strings, tried in order
    point_codes  JSONB NOT NULL,         -- {code: Brick class}
    equip_codes  JSONB NOT NULL,
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL
);
"""


class ConventionError(ValueError):
    pass


def code(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


@dataclass
class Template:
    annotation: str
    regex: re.Pattern
    roles: list[str]


def compile_annotation(annotation: str) -> Template:
    """`fbf/{equip_type:ahu}-{equip_id:1}/{point:DIS-AIR-TEM}-{ignore:12}`
    -> an anchored regex with one group per field."""
    pos, parts, roles = 0, [], []
    for m in _FIELD.finditer(annotation):
        role, text = m.group(1), m.group(2)
        if role not in ROLES:
            raise ConventionError(f"unknown role {role!r} in {annotation!r}; roles are {', '.join(ROLES)}")
        parts.append(re.escape(annotation[pos:m.start()]))
        if role == "ignore":
            rx = r"\d+?" if text.isdigit() else r".+?"
        else:
            rx = _ROLE_RE[role]
        parts.append(f"({rx})")
        roles.append(role)
        pos = m.end()
    parts.append(re.escape(annotation[pos:]))
    if "point" not in roles:
        raise ConventionError(f"{annotation!r} marks no {{point:...}} field")
    if roles.count("point") > 1 or roles.count("equip_type") > 1 or roles.count("equip_id") > 1:
        raise ConventionError(f"{annotation!r} marks a field twice")
    if "{" in annotation[pos:] or "}" in annotation[pos:]:
        raise ConventionError(f"unbalanced braces in {annotation!r}")
    return Template(annotation, re.compile("^" + "".join(parts) + "$"), roles)


def example_of(annotation: str) -> str:
    """The label an annotation was written from (fields' text in place)."""
    return _FIELD.sub(lambda m: m.group(2), annotation)


@dataclass
class Convention:
    templates: list[str]
    point_codes: dict[str, str] = field(default_factory=dict)
    equip_codes: dict[str, str] = field(default_factory=dict)
    scope_prefix: str = ""
    name: str = ""
    id: str = ""

    def __post_init__(self):
        self.compiled = [compile_annotation(a) for a in self.templates]
        vocab = brick_vocab.load()
        for book in (self.point_codes, self.equip_codes):
            for k, v in list(book.items()):
                canon = vocab.canonical_class(str(v))
                if not canon:
                    raise ConventionError(f"code {k!r}: no Brick class named {v!r}")
                del book[k]
                book[code(k)] = canon

    def covers(self, topic: str) -> bool:
        p = self.scope_prefix.rstrip("/")
        return not p or topic == p or topic.startswith(p + "/")

    def parse(self, topic: str) -> dict | None:
        """{template, point_code, equip_type, equip_id, point_class,
        equip_class} for the first template matching `topic`, else None."""
        if not self.covers(topic):
            return None
        for i, t in enumerate(self.compiled):
            m = t.regex.match(topic)
            if not m:
                continue
            got = dict(zip(t.roles, m.groups()))
            pc = code(got.get("point", ""))
            ec = code(got.get("equip_type", "")) if "equip_type" in got else None
            return {"template": i, "point_code": pc, "point_text": got.get("point"), "equip_type": ec,
                    "equip_id": got.get("equip_id"), "point_class": self.point_codes.get(pc),
                    "equip_class": self.equip_codes.get(ec) if ec else None}
        return None

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "scope_prefix": self.scope_prefix, "templates": self.templates,
                "point_codes": self.point_codes, "equip_codes": self.equip_codes}


def from_body(body: dict) -> Convention:
    templates = body.get("templates")
    if not isinstance(templates, list) or not templates or not all(isinstance(t, str) and t.strip() for t in templates):
        raise ConventionError("templates is a non-empty list of annotated labels")
    for key in ("point_codes", "equip_codes"):
        if not isinstance(body.get(key) or {}, dict):
            raise ConventionError(f"{key} is {{code: Brick class}}")
    return Convention([t.strip() for t in templates], dict(body.get("point_codes") or {}), dict(body.get("equip_codes") or {}),
                      str(body.get("scope_prefix") or "").strip(), str(body.get("name") or "").strip()[:200], str(body.get("id") or ""))


# --- what it would do -------------------------------------------------------


def preview(conv: Convention, labels: dict[str, str], classified: set[str] | None = None, only_unclassified: bool = False,
            examples: int = 3) -> dict:
    """Run `conv` over {point_uri: topic}. Counts of matched / classified
    points, the unknown codes ranked by how many points each would settle
    (with example labels), the equipment it finds, and the labels no
    template matched (a sample), so the person knows what to annotate
    next. `only_unclassified` counts only points without a class."""
    classified = classified or set()
    matched = known = 0
    unknown: Counter[str] = Counter()
    unknown_examples: dict[str, list[str]] = {}
    unknown_equip: Counter[str] = Counter()
    unmatched: list[str] = []
    equips: Counter[str] = Counter()
    by_template: Counter[int] = Counter()
    considered = 0
    for uri, topic in sorted(labels.items()):
        if only_unclassified and uri in classified:
            continue
        if not conv.covers(topic):
            continue
        considered += 1
        got = conv.parse(topic)
        if got is None:
            if len(unmatched) < 20:
                unmatched.append(topic)
            continue
        matched += 1
        by_template[got["template"]] += 1
        if got["equip_type"] is not None:
            equips[f"{got['equip_type']}:{got['equip_id']}"] += 1
            if got["equip_class"] is None:
                unknown_equip[got["equip_type"]] += 1
        if got["point_class"]:
            known += 1
        else:
            unknown[got["point_code"]] += 1
            unknown_examples.setdefault(got["point_code"], [])
            if len(unknown_examples[got["point_code"]]) < examples:
                unknown_examples[got["point_code"]].append(got["point_text"])
    return {
        "points": considered,
        "matched": matched,
        "classified": known,
        "by_template": [by_template[i] for i in range(len(conv.templates))],
        "unknown_point_codes": [{"code": c, "points": n, "examples": unknown_examples[c]} for c, n in unknown.most_common()],
        "unknown_equip_codes": [{"code": c, "points": n} for c, n in unknown_equip.most_common()],
        "equipment": len(equips),
        "unmatched_sample": unmatched,
    }


def apply(conv: Convention, store, labels: dict[str, str], skip: set[str] | None = None, edit=None) -> dict:
    """Classify every point `conv` parses with a known code, and the
    equipment those points belong to from a known equip code. `edit(uri,
    brick_class)` writes a class (record_edit for a person's apply; the
    default writes directly, for the ladder). `skip`: points already
    classified - a convention never overrides an existing class."""
    skip = skip or set()
    done_points: list[str] = []
    equip_votes: dict[str, Counter] = {}
    for uri, topic in sorted(labels.items()):
        got = conv.parse(topic)
        if got is None:
            continue
        if got["equip_class"]:
            for e in _equips_of(store, uri):
                equip_votes.setdefault(e, Counter())[got["equip_class"]] += 1
        if uri in skip or not got["point_class"]:
            continue
        (edit or _write)(store, uri, got["point_class"])
        _mark(store, uri, conv, got)
        done_points.append(uri)
    done_equip = []
    for e, votes in sorted(equip_votes.items()):
        if e in skip or len(votes) != 1:  # points disagree about their equipment: leave it
            continue
        cls = next(iter(votes))
        (edit or _write)(store, e, cls)
        store.add_relationship(URIRef(e), SKOS.definition, Literal(f"convention-classified: equip code ({conv.name or conv.id})"))
        done_equip.append(e)
    return {"points": len(done_points), "equipment": len(done_equip), "point_uris": done_points, "equip_uris": done_equip}


def _equips_of(store, uri: str) -> list[str]:
    return [str(r.e) for r in store.query(f"SELECT ?e WHERE {{ ?e <{BRICK.hasPoint}> <{uri}> }}")]


def _write(store, uri: str, brick_class: str) -> None:
    """The ladder's write: the Brick class, replacing a PROJ/Unmapped
    fallback (the same swap mapping.classify_on_ingest makes)."""
    u = URIRef(uri)
    for r in store.query(f"SELECT ?t WHERE {{ <{uri}> a ?t }}"):
        t = URIRef(str(r.t))
        if str(t).startswith(str(PROJ)) or t == TD.UnmappedPoint:
            store.remove_relationship(u, RDF.type, t)
    store.add_entity(u, BRICK[brick_class])


def _mark(store, uri: str, conv: Convention, got: dict) -> None:
    store.add_relationship(URIRef(uri), SKOS.definition, Literal(
        f"convention-classified: {got['point_text']!r} under {conv.templates[got['template']]!r} ({conv.name or conv.id})"))


def point_labels(store, allows=None) -> dict[str, str]:
    """{point uri: source topic} - what conventions parse."""
    out = {}
    for r in store.query(f"SELECT ?p ?t WHERE {{ ?p <{TD.sourceTopic}> ?t . FILTER(STRSTARTS(STR(?p), \"urn:point:\")) }}"):
        if allows is None or allows(str(r.p)):
            out[str(r.p)] = str(r.t)
    return out


def brick_typed(store) -> set[str]:
    return {str(r.s) for r in store.query(f"SELECT DISTINCT ?s WHERE {{ ?s a ?t . FILTER(STRSTARTS(STR(?t), \"{BRICK}\")) }}")}


# --- storage --------------------------------------------------------------


def ensure_schema(conn) -> None:
    ddl.run(conn, SCHEMA)


def save(conn, conv: Convention, by: str | None) -> Convention:
    now = datetime.now(timezone.utc)
    if not conv.id:
        conv.id = uuid.uuid4().hex
    conn.execute(
        """INSERT INTO naming_conventions (id, name, scope_prefix, templates, point_codes, equip_codes, created_by, created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, scope_prefix = EXCLUDED.scope_prefix, templates = EXCLUDED.templates,
             point_codes = EXCLUDED.point_codes, equip_codes = EXCLUDED.equip_codes, updated_at = EXCLUDED.updated_at""",
        (conv.id, conv.name or conv.id, conv.scope_prefix, json.dumps(conv.templates), json.dumps(conv.point_codes),
         json.dumps(conv.equip_codes), by, now, now),
    )
    return conv


def load_all(conn) -> list[Convention]:
    rows = conn.execute("SELECT id, name, scope_prefix, templates, point_codes, equip_codes FROM naming_conventions ORDER BY created_at").fetchall()
    return [Convention(list(t), dict(p), dict(e), s, n, i) for i, n, s, t, p, e in rows]


def get(conn, conv_id: str) -> Convention | None:
    return next((c for c in load_all(conn) if c.id == conv_id), None)


def delete(conn, conv_id: str) -> bool:
    return bool(conn.execute("DELETE FROM naming_conventions WHERE id = %s", (conv_id,)).rowcount)


def run_ladder(conventions: list[Convention], store, uris: list[str]) -> set[str]:
    """Rung 2 for autotag: classify the unclassified `uris` saved
    conventions settle. Returns the uris it classified (points and equip)."""
    if not conventions:
        return set()
    labels = {u: t for u, t in point_labels(store).items() if u in set(uris)}
    typed = brick_typed(store)
    done: set[str] = set()
    for conv in conventions:
        out = apply(conv, store, {u: t for u, t in labels.items() if u not in done}, skip=typed | done)
        done |= set(out["point_uris"]) | set(out["equip_uris"])
    return done

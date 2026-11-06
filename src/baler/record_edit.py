"""
Hand edits to equip/point records (Baler#19): display name, Brick class,
tags, delete - the operator's fix for a wrong class or a stale tag, which
used to need SPARQL against the graph. Every edit is logged in
`record_edits` (who, when, what changed).

Edits replace a tag outright (old value, unit and timezone go), unlike
ingest's additive tag writes. Structural tags (id, equipRef, siteRef,
curVal, ...) aren't hand-editable: they come from graph links and live
history, and editing a copy would only diverge from the source of truth.
Deleting a record removes it from the graph (its own triples and every
link to it); its history rows stay in point_history.
"""

from __future__ import annotations

import json
import re

from rdflib import RDF, Literal, URIRef

from baler import brick_vocab, ddl
from baler.ingest import _tag_triples
from baler.store import BRICK, HAYSTACK, HTZ, HUNIT

SCHEMA = """
CREATE TABLE IF NOT EXISTS record_edits (
    id         BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    entity_uri TEXT NOT NULL,
    action     TEXT NOT NULL,      -- edit | delete
    user_sub   TEXT,
    user_email TEXT,
    detail     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS record_edits_entity ON record_edits (entity_uri, ts DESC);
"""

# Derived from graph links or live history, or identity: not hand-editable.
RESERVED_TAGS = frozenset({
    "id", "equipRef", "siteRef", "brickClass", "brickHasPart", "brickFeeds", "navId",
    "curVal", "curStatus", "his", "hisStart", "hisEnd", "mod", "writeVal",
})
_TAG_NAME = re.compile(r"[a-z][A-Za-z0-9_]*")
_BRICK_NS = str(BRICK)


def ensure_schema(conn) -> None:
    ddl.run(conn, SCHEMA)


def exists(store, uri: str) -> bool:
    return bool(list(store.query(f"SELECT ?p WHERE {{ <{uri}> ?p ?o }} LIMIT 1")))


def _check_tag(name) -> str:
    if not isinstance(name, str) or not _TAG_NAME.fullmatch(name):
        raise ValueError(f"bad tag name {name!r}: letters, digits and _, starting lowercase")
    if name in RESERVED_TAGS:
        raise ValueError(f"{name} isn't hand-editable (it comes from the graph's links or live history)")
    return name


def _value_triples(uri: URIRef, tag: str, value) -> list:
    """true = marker; a number; {"val": n, "unit": "°F"}; a string."""
    if value is True:
        return [(uri, HAYSTACK.hasTag, Literal(tag))]
    if isinstance(value, dict) and set(value) <= {"val", "unit"} and isinstance(value.get("val"), (int, float)):
        out = [(uri, HAYSTACK[tag], Literal(float(value["val"])))]
        if value.get("unit"):
            out.append((uri, HUNIT[tag], Literal(str(value["unit"]))))
        return out
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"{tag}: a value is true (marker), a number, {{\"val\": n, \"unit\": u}} or a string")
    return [(uri, p, o) for p, o in _tag_triples(tag, float(value) if isinstance(value, int) else value)]


def _remove_tag_sparql(uri: str, tag: str) -> str:
    lit = json.dumps(tag)
    return (
        f"DELETE WHERE {{ <{uri}> <{HAYSTACK}hasTag> {lit} }} ;\n"
        f"DELETE WHERE {{ <{uri}> <{HAYSTACK[tag]}> ?v }} ;\n"
        f"DELETE WHERE {{ <{uri}> <{HUNIT[tag]}> ?u }} ;\n"
        f"DELETE WHERE {{ <{uri}> <{HTZ[tag]}> ?z }}"
    )


def apply_edit(store, uri: str, body: dict) -> dict:
    """{dis?, brickClass?, set?: {tag: value}, remove?: [tag]} -> what changed.
    Validates everything before writing anything."""
    unknown = set(body) - {"id", "dis", "brickClass", "set", "remove"}
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
    sets = dict(body.get("set") or {})
    removes = list(body.get("remove") or [])
    if not isinstance(sets, dict) or not isinstance(removes, list):
        raise TypeError("set is {tag: value}, remove is [tag, ...]")
    if "dis" in body:
        if not isinstance(body["dis"], str) or not body["dis"].strip():
            raise ValueError("dis must be a non-empty string")
        sets["dis"] = body["dis"].strip()
    for tag in list(sets) + removes:
        _check_tag(tag)
    both = set(sets) & set(removes)
    if both:
        raise ValueError(f"both set and removed: {', '.join(sorted(both))}")
    brick_iri = None
    if body.get("brickClass") is not None:
        canonical = brick_vocab.load().canonical_class(str(body["brickClass"]))
        if not canonical:
            raise ValueError(f"no Brick class named {body['brickClass']!r}")
        brick_iri = BRICK[canonical]
    if not (sets or removes or brick_iri):
        raise ValueError("nothing to change: send dis, brickClass, set and/or remove")

    node = URIRef(uri)
    new_triples = [t for tag, v in sets.items() for t in _value_triples(node, tag, v)]
    updates = [_remove_tag_sparql(uri, tag) for tag in list(sets) + removes]
    if brick_iri is not None:
        updates.append(
            f"DELETE {{ <{uri}> <{RDF.type}> ?t }} WHERE {{ <{uri}> <{RDF.type}> ?t . "
            f'FILTER(STRSTARTS(STR(?t), "{_BRICK_NS}")) }}'
        )
        new_triples.append((node, RDF.type, brick_iri))
    store.update(" ;\n".join(updates))
    if new_triples:
        store.add_many(new_triples)
    changed: dict = {}
    if sets:
        changed["set"] = sets
    if removes:
        changed["remove"] = removes
    if brick_iri is not None:
        changed["brickClass"] = str(brick_iri)[len(_BRICK_NS):]
    return changed


def delete_record(store, uri: str) -> None:
    store.update(f"DELETE WHERE {{ <{uri}> ?p ?o }} ;\nDELETE WHERE {{ ?s ?p <{uri}> }}")


def log_edit(conn, uri: str, action: str, user_sub: str | None, user_email: str | None, detail: dict) -> None:
    conn.execute(
        "INSERT INTO record_edits (entity_uri, action, user_sub, user_email, detail) VALUES (%s, %s, %s, %s, %s)",
        (uri, action, user_sub, user_email, json.dumps(detail, default=str)),
    )


def list_edits(conn, uri: str, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT ts, action, user_email, detail FROM record_edits WHERE entity_uri = %s ORDER BY ts DESC LIMIT %s",
        (uri, limit),
    ).fetchall()
    return [{"ts": r[0].isoformat(), "action": r[1], "who": r[2], "detail": r[3]} for r in rows]

"""
Haystack tags -> Brick classes. Not a general ontology mapper - a small,
first-match-wins rule engine over marker-tag SETS (see
rules/haystack_to_brick.yaml), because that's literally what the earlier
"how do we get something in Haystack but not in Brick into Brick" question
needs and nothing more.
"""

import os

import yaml
from rdflib import RDF, RDFS, SKOS, Literal, URIRef

from baler import tracing
from baler.schemas import MappingRule
from baler.store import BRICK, PROJ, TD

tracer = tracing.get_tracer(__name__)

DEFAULT_RULES_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "rules", "haystack_to_brick.yaml")
EQUIP_RULES_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "rules", "haystack_equip_to_brick.yaml")


def load_rules(path: str = DEFAULT_RULES_PATH) -> list[MappingRule]:
    with open(path) as f:
        return yaml.safe_load(f)["rules"]


def read_tags(store, point_uri) -> set[str]:
    rows = store.query(f"""
        PREFIX haystack: <urn:baler:haystack#>
        SELECT ?tag WHERE {{ <{point_uri}> haystack:hasTag ?tag }}
    """)
    return {str(r.tag) for r in rows}


def classify_point(store, point_uri, rules: list[MappingRule] | None = None) -> tuple[str, str | None]:
    """Three outcomes:

    1. **Direct**: some rule's tags are a SUBSET of this point's tags -> the
       real Brick class directly. Subset, not exact-equality: a real point
       always carries extra structural tags (`point`, `cur`, `dis`, `kind`,
       `unit`, ...) alongside the domain-specific markers a rule cares
       about, and none of that extra information should demote a clean
       match to a fallback. When multiple rules qualify, the most specific
       (largest tag set) wins.
    2. **Fallback**: no rule's tags are a full subset, but at least one rule
       shares SOME tags with this point (e.g. `zone`/`air`/`sensor` present
       but `temp` isn't - a CO2 sensor tagged in the same structural family
       as our one temp-sensor rule) -> a PROJ class, subClassOf the
       best-overlapping rule's Brick class. Lossless: the raw
       HAYSTACK:hasTag triples are never touched, so nothing not covered by
       a rule is dropped on the floor.
    3. **Miss**: no overlap with any rule at all -> TD:UnmappedPoint.
    """
    if rules is None:
        rules = load_rules()

    with tracer.start_as_current_span("mapping.classify_point") as span:
        span.set_attribute("point_uri", str(point_uri))
        tags = read_tags(store, point_uri)

        direct_matches = [r for r in rules if set(r["tags"]) <= tags]
        if direct_matches:
            rule = max(direct_matches, key=lambda r: len(r["tags"]))
            store.add_entity(point_uri, BRICK[rule["brick_class"]])
            span.set_attribute("outcome", "direct")
            span.set_attribute("matched_rule", rule["brick_class"])
            return "direct", rule["brick_class"]

        overlaps = [(len(set(r["tags"]) & tags), r) for r in rules]
        overlaps = [(score, r) for score, r in overlaps if score > 0]
        if overlaps:
            _, rule = max(overlaps, key=lambda pair: pair[0])
            extra = sorted(t.capitalize() for t in tags - set(rule["tags"]))
            proj_class = f"{rule['brick_class']}_{'_'.join(extra)}" if extra else f"{rule['brick_class']}_Variant"
            store.add_entity(point_uri, PROJ[proj_class])
            store.add_relationship(PROJ[proj_class], RDFS.subClassOf, BRICK[rule["brick_class"]])
            # Brick's own extension guidance (docs.brickschema.org/extra/extending.html)
            # asks for a skos:definition on any proprietary class - so a human
            # reading the graph later sees *why* this PROJ class exists
            # (which tags produced it) without re-deriving it from the rules file.
            store.add_relationship(PROJ[proj_class], SKOS.definition, Literal(", ".join(sorted(tags))))
            span.set_attribute("outcome", "fallback")
            span.set_attribute("matched_rule", rule["brick_class"])
            return "fallback", rule["brick_class"]

        store.add_entity(point_uri, TD.UnmappedPoint)
        span.set_attribute("outcome", "miss")
        span.set_attribute("matched_rule", "")
        return "miss", None


def classify_on_ingest(store, uri, rules: list[MappingRule]) -> str | None:
    """Classification as tags arrive over MQTT (Baler#82): Windrow/edge
    equips and points used to stay unclassified until someone ran
    `python -m baler.autotag`, unlike the pullers, which classify as they
    ingest. Deliberately narrower than classify_point:

      * a direct rule match only - no PROJ fallback, no TD:UnmappedPoint, no
        LLM. Those stay autotag's job, run by a human who reads its report.
      * only while the entity has no Brick class: a class set by hand
        (PATCH /ingest/records), by autotag or by an earlier message is
        never replaced. A PROJ/UnmappedPoint type an earlier autotag left
        is, once a direct match exists - the same swap reclassify makes.

    Returns the Brick class asserted, or None. Cheap enough per tag message
    (retained, so a burst on every listener or Windrow restart): one type
    lookup, one tag read, at most one write."""
    rows = store.query(f"SELECT ?type WHERE {{ <{uri}> a ?type }}")
    types = [URIRef(str(r.type)) for r in rows]
    if any(str(t).startswith(str(BRICK)) for t in types):
        return None
    tags = read_tags(store, uri)
    direct_matches = [r for r in rules if set(r["tags"]) <= tags]
    if not direct_matches:
        return None
    rule = max(direct_matches, key=lambda r: len(r["tags"]))
    for t in types:
        if str(t).startswith(str(PROJ)) or t == TD.UnmappedPoint:
            store.remove_relationship(uri, RDF.type, t)
    store.add_entity(uri, BRICK[rule["brick_class"]])
    return rule["brick_class"]


def _values(uris) -> str:
    return " ".join(f"<{u}>" for u in uris)


def classify_on_ingest_many(store, entities: dict, chunk: int = 500) -> dict:
    """classify_on_ingest for many entities at once - {uri: rules} - as the
    listener's tag path applies a batch of tag messages (Baler#233): two
    reads per `chunk` entities (every type, every tag) and one write for all
    the new classes, instead of two reads and a write per entity. Same rule
    as classify_on_ingest, entity by entity. Returns {uri: brick class}
    for the entities it classified."""
    out: dict = {}
    items = list(entities.items())
    for i in range(0, len(items), chunk):
        part = dict(items[i:i + chunk])
        values = _values(part)
        types: dict[str, list[URIRef]] = {}
        for r in store.query(f"SELECT ?s ?type WHERE {{ VALUES ?s {{ {values} }} ?s a ?type }}"):
            types.setdefault(str(r.s), []).append(URIRef(str(r.type)))
        tags: dict[str, set[str]] = {}
        for r in store.query(f"""
            PREFIX haystack: <urn:baler:haystack#>
            SELECT ?s ?tag WHERE {{ VALUES ?s {{ {values} }} ?s haystack:hasTag ?tag }}"""):
            tags.setdefault(str(r.s), set()).add(str(r.tag))
        adds = []
        for uri, rules in part.items():
            ts = types.get(str(uri), [])
            if any(str(t).startswith(str(BRICK)) for t in ts):
                continue
            have = tags.get(str(uri), set())
            direct = [r for r in rules if set(r["tags"]) <= have]
            if not direct:
                continue
            rule = max(direct, key=lambda r: len(r["tags"]))
            for t in ts:
                if str(t).startswith(str(PROJ)) or t == TD.UnmappedPoint:
                    store.remove_relationship(URIRef(str(uri)), RDF.type, t)
            adds.append((URIRef(str(uri)), RDF.type, BRICK[rule["brick_class"]]))
            out[uri] = rule["brick_class"]
        store.add_many(adds)
    return out


def classify_point_with_fallback(store, point_uri, rules: list[MappingRule] | None = None) -> tuple[str, str | None]:
    """classify_point, kept under this name for the pullers that call it.
    It used to take an LLM callable for what the rules couldn't place; that
    is now model_step.run, which batches by naming pattern, needs agreement
    before it writes, and leaves the rest as mapping proposals (#182)."""
    return classify_point(store, point_uri, rules)


def reclassify(store, uri, rules: list[MappingRule] | None = None) -> tuple[str, str | None]:
    """classify_point is deliberately pure-additive (existing callers rely
    on that), which means calling it twice for the same URI with a
    different result - a rules file change, a human correcting a mistake -
    leaves both the old and new rdf:type asserted rather than replacing.
    This is the explicit "I want this entity's classification refreshed"
    operation: remove whatever classify_point itself would have asserted
    last time (a BRICK class, a PROJ class, or TD:UnmappedPoint - never
    anything outside those three namespaces, so this never touches a type
    triple this function didn't put there), then classify fresh.

    Deliberately doesn't touch a PROJ class's own subClassOf/skos:definition
    triples - other entities may share that same PROJ class, so only the
    uri's own rdf:type edge into it is this function's business."""
    with tracer.start_as_current_span("mapping.reclassify") as span:
        span.set_attribute("uri", str(uri))
        existing_types = store.query(f"SELECT ?type WHERE {{ <{uri}> a ?type }}")
        for row in existing_types:
            t = URIRef(str(row.type))
            if str(t).startswith(str(BRICK)) or str(t).startswith(str(PROJ)) or t == TD.UnmappedPoint:
                store.remove_relationship(uri, RDF.type, t)
        return classify_point(store, uri, rules)

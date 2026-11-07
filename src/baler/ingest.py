"""
The one ingestion path every protocol shares. BACnet, Modbus, and native
MQTT sources all land here through the same envelope - see
docs/adding-a-new-source.md in Windrow for the wire contract this assumes.
"""

import hashlib
import json
from datetime import datetime, timezone

from rdflib import OWL, RDF, XSD, Literal, URIRef

from baler import timeseries, tracing
from baler.store import BRICK, DICT_NS, HAYSTACK, HTZ, HUNIT, TD

tracer = tracing.get_tracer(__name__)

# Points already known to have their identity/sourceTopic triples in the
# graph - a real point gets read every few seconds forever, so without
# this, every single reading pays 2 Oxigraph HTTP round trips to re-assert
# a fact that's already true after the first one. Idempotent either way
# (RDF triples are a set), this just stops paying for it redundantly.
# Process-scoped is the right lifetime: both mqtt_listener.py and
# ingest_api.py build one store per process and call ingest_reading in a
# loop for the life of that process.
_known_points: set[URIRef] = set()


def topic_to_point_uri(topic: str) -> URIRef:
    """Point identity = the topic itself - it already encodes
    source+device+point, no separate ID scheme needed."""
    return URIRef(f"urn:point:{topic}")


def ingest_reading(store, ts_conn, topic: str, value, ts: float | None = None) -> URIRef:
    """Writes to both the graph (identity + raw topic, for Phase 2 to key
    off) and the time-series store (the actual value history). Idempotent
    on (point_uri, ts) - required because Windrow publishes with retain=True,
    which replays the last message on every listener restart."""
    with tracer.start_as_current_span("ingest.reading") as span:
        point_uri = topic_to_point_uri(topic)
        span.set_attribute("point_uri", str(point_uri))
        span.set_attribute("topic", topic)

        dt = datetime.now(timezone.utc) if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc)

        if point_uri not in _known_points:
            with tracer.start_as_current_span("ingest.graph_write"):
                store.add_many([
                    (point_uri, RDF.type, TD.RawPoint),
                    (point_uri, TD.sourceTopic, Literal(topic)),
                ])
            _known_points.add(point_uri)

        with tracer.start_as_current_span("ingest.timeseries_write"):
            timeseries.write_point_value(ts_conn, str(point_uri), value, dt)

        return point_uri


def ingest_readings(store, ts_conn, readings) -> int:
    """ingest_reading for many readings at once - (topic, value, ts) each -
    as mqtt_listener's batches use it: one Oxigraph round trip for every
    point seen for the first time in the batch (none once they're all
    known), and one transaction for the rows (timeseries.write_point_values).
    Same facts and the same idempotency as calling ingest_reading per
    reading, in order. Returns the number of rows written."""
    with tracer.start_as_current_span("ingest.readings") as span:
        rows = []
        new: dict[URIRef, str] = {}
        now = None
        for topic, value, ts in readings:
            point_uri = topic_to_point_uri(topic)
            if point_uri not in _known_points:
                new[point_uri] = topic
            if ts is None:
                now = now or datetime.now(timezone.utc)
                dt = now
            else:
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            rows.append((str(point_uri), value, dt))
        span.set_attribute("readings", len(rows))
        span.set_attribute("new_points", len(new))
        if new:
            with tracer.start_as_current_span("ingest.graph_write"):
                store.add_many([t for uri, topic in new.items()
                                for t in ((uri, RDF.type, TD.RawPoint), (uri, TD.sourceTopic, Literal(topic)))])
            _known_points.update(new)
        with tracer.start_as_current_span("ingest.timeseries_write"):
            return timeseries.write_point_values(ts_conn, rows)


def _write_tags(store, uri: URIRef, tags: dict) -> None:
    """Marker tags (value True) become HAYSTACK:hasTag triples; valued tags
    become HAYSTACK:<tagName> triples. Lossless and additive - never
    deletes existing tag facts, so mapping.py's Brick classification can
    always fall back to the raw tags even after a rule changes. Shared by
    ingest_tags (points) and ingest_equip_tags (equip/site recs) - tags are
    tags regardless of which kind of entity they're describing."""
    triples: list = []
    _dict_triples(uri, tags, triples)

    store.add_many(triples)  # one round trip for every tag instead of one per tag


def _dict_triples(subject, tags: dict, out: list) -> None:
    """A record's (or nested Dict's) tags as triples on `subject`."""
    for tag, value in tags.items():
        if value is True:
            out.append((subject, HAYSTACK.hasTag, Literal(tag)))
        elif isinstance(value, dict):
            # a nested Dict: its own node, so `ref->thru` can step into it
            node = URIRef(DICT_NS + hashlib.sha1(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest())
            out.append((subject, HAYSTACK[tag], node))
            _dict_triples(node, value, out)
        else:
            out.extend((subject, p, o) for p, o in _tag_triples(tag, value))


def _hay_type(value) -> str | None:
    """'Number'/'Ref' for openhay values (haystack_import passes them through
    typed), without importing openhay here - it's an optional extra."""
    t = type(value)
    return t.__name__ if t.__module__.startswith("openhay") else None


def _tag_triples(tag: str, value):
    """(predicate, object) pairs for one valued tag, typed so filters can
    compare with Haystack semantics:
      - Number: an xsd:double, plus HUNIT:<tag> holding its unit if any
      - Ref: a HAYSTACK.Ref-typed literal of the id; a *list* of Refs is one
        triple per Ref (so `ref==@a` and `ref->dis` check every element)
      - date/time/dateTime: xsd typed; a dateTime also gets HTZ:<tag> with
        its Haystack timezone name
      - nested dicts: a node of their own (see _dict_triples)
      - other lists: JSON text (rdflib would otherwise store Python repr,
        which isn't valid JSON), as before
    Plain JSON values (MQTT tags, haystack_puller) keep their old shape."""
    kind = _hay_type(value)
    if kind == "Number":
        out = [(HAYSTACK[tag], Literal(float(value.val)))]
        if value.unit:
            out.append((HUNIT[tag], Literal(value.unit)))
        return out
    if kind == "Ref":
        return [(HAYSTACK[tag], Literal(value.id, datatype=HAYSTACK.Ref))]
    if isinstance(value, list) and value and all(_hay_type(v) == "Ref" for v in value):
        return [(HAYSTACK[tag], Literal(v.id, datatype=HAYSTACK.Ref)) for v in value]
    if kind == "Uri":  # typed, so filters can tell a Uri from a Str (`uri == `...``)
        return [(HAYSTACK[tag], Literal(str(value.val), datatype=XSD.anyURI))]
    if kind is not None:  # Symbol, XStr, Coord: their text
        return [(HAYSTACK[tag], Literal(str(getattr(value, "val", value))))]
    if isinstance(value, datetime) and getattr(value.tzinfo, "key", None):
        key = value.tzinfo.key  # type: ignore[union-attr]
        name = "UTC" if key in ("UTC", "Etc/UTC") else key.split("/")[-1]
        return [(HAYSTACK[tag], Literal(value)), (HTZ[tag], Literal(name))]
    if isinstance(value, (dict, list)):
        return [(HAYSTACK[tag], Literal(json.dumps(value, default=str)))]
    return [(HAYSTACK[tag], Literal(value))]


def ingest_tags(store, topic: str, tags: dict, ts: float | None = None) -> URIRef:
    with tracer.start_as_current_span("ingest.tags_write") as span:
        point_uri = topic_to_point_uri(topic)
        span.set_attribute("point_uri", str(point_uri))
        span.set_attribute("tag_count", len(tags))
        _write_tags(store, point_uri, tags)
        return point_uri


def topic_prefix_to_equip_uri(topic_prefix: str) -> URIRef:
    """Equipment identity = the connection's topic_prefix (e.g. "fbf/ahu-3"),
    not the BACnet device_instance - the topic_prefix is the boundary a
    human already draws at connection-creation time (which learned points
    go under this prefix), and it's right more often: one physical BACnet
    device can expose more than one logical piece of equipment (a
    chiller-plant controller owning several chillers), and topic_prefix
    naturally becomes one equip URI per connection either way."""
    return URIRef(f"urn:equip:{topic_prefix}")


def haystack_ref_to_equip_uri(ref: str) -> URIRef:
    """Equipment identity for a Haystack-sourced equip rec, keyed by its
    own Haystack id (the Ref string an equipRef tag points at)."""
    return URIRef(f"urn:equip:haystack:{ref}")


def ingest_equip_tags(store, topic_prefix: str, tags: dict) -> URIRef:
    """BACnet/Modbus-side equip ingestion, keyed by topic_prefix. Same tag
    storage as ingest_tags, keyed by equip identity instead of point
    identity - so mapping.classify_point (generic over any URI with
    hasTag triples) can classify equip recs into Brick equipment classes
    (rules/haystack_equip_to_brick.yaml) exactly like it classifies points."""
    with tracer.start_as_current_span("ingest.equip_tags_write") as span:
        equip_uri = topic_prefix_to_equip_uri(topic_prefix)
        span.set_attribute("equip_uri", str(equip_uri))
        span.set_attribute("tag_count", len(tags))
        _write_tags(store, equip_uri, tags)
        return equip_uri


def ingest_haystack_equip_tags(store, ref: str, tags: dict) -> URIRef:
    """Haystack-side equip ingestion, keyed by the equip rec's own Haystack
    id - the identity link_equip_ref resolves a point's equipRef against."""
    with tracer.start_as_current_span("ingest.haystack_equip_tags_write") as span:
        equip_uri = haystack_ref_to_equip_uri(ref)
        span.set_attribute("equip_uri", str(equip_uri))
        span.set_attribute("tag_count", len(tags))
        _write_tags(store, equip_uri, tags)
        return equip_uri


def link_point_to_equip(store, point_uri: URIRef, equip_uri: URIRef) -> None:
    """brick:hasPoint / brick:isPointOf - structural, and deliberately not
    gated on either end being Brick-classified yet: "these points belong to
    this equipment" is true and useful (scoping history, fault rules) even
    before/without a Brick class ever getting assigned to the equipment."""
    store.add_relationship(equip_uri, BRICK.hasPoint, point_uri)
    store.add_relationship(point_uri, BRICK.isPointOf, equip_uri)


def link_equip_ref(store, point_uri: URIRef) -> URIRef | None:
    """Resolves a point's HAYSTACK:equipRef literal (already ingested
    losslessly by ingest_tags, unchanged, same as any other valued tag)
    into a real brick:isPointOf edge against the matching equip URI.
    Returns the equip URI linked, or None if this point has no equipRef or
    no equip rec with that id has been ingested yet (ingest_equip_tags runs
    independently - order isn't guaranteed, so a miss here is normal, not
    an error, until the equip side catches up)."""
    with tracer.start_as_current_span("ingest.link_equip_ref") as span:
        span.set_attribute("point_uri", str(point_uri))
        rows = list(store.query(f"""
            PREFIX haystack: <urn:baler:haystack#>
            SELECT ?ref WHERE {{ <{point_uri}> haystack:equipRef ?ref }}
        """))
        if not rows:
            span.set_attribute("outcome", "no_equip_ref")
            return None

        ref = str(rows[0].ref)
        equip_uri = haystack_ref_to_equip_uri(ref)
        # Store/RemoteStore's query() only exposes SELECT-shaped row
        # iteration (no ASK support) - a cheap SELECT LIMIT 1 does the same
        # existence check identically on both.
        found = list(store.query(f"SELECT ?p WHERE {{ <{equip_uri}> ?p ?o }} LIMIT 1"))
        if not found:
            span.set_attribute("outcome", "equip_not_yet_ingested")
            return None

        link_point_to_equip(store, point_uri, equip_uri)
        span.set_attribute("outcome", "linked")
        span.set_attribute("equip_uri", str(equip_uri))
        return equip_uri


def merge_equip(store, uri_a: URIRef, uri_b: URIRef) -> None:
    """Declares two equip URIs are the same physical equipment - e.g. the
    same AHU bridged both via BACnet (urn:equip:fbf/ahu-3) and separately
    via Haystack (urn:equip:haystack:AHU-1). Deliberately not something
    this system ever guesses (matching on dis/label text is exactly the
    kind of heuristic that silently merges the wrong things) - a human (or
    whatever's calling this) already has both real URIs in hand, so this
    is a direct fact, not a ref to resolve. Both directions are asserted
    explicitly since Oxigraph doesn't do OWL reasoning - a query can't
    lean on owl:sameAs's symmetry unless both triples actually exist."""
    store.add_relationship(uri_a, OWL.sameAs, uri_b)
    store.add_relationship(uri_b, OWL.sameAs, uri_a)


def points_of_equip(store, uri: URIRef) -> list[str]:
    """Every point of this equipment, including points only reachable
    through a merge_equip'd sibling URI - the read-side half of gap 1.
    Querying brick:hasPoint on just `uri` after a merge would silently see
    only half the points; the (owl:sameAs)* property path walks the whole
    equivalence set first (0 or more hops - includes `uri` itself when no
    merge has happened, so this is a safe default for every equip URI,
    merged or not)."""
    rows = store.query(f"""
        PREFIX brick: <https://brickschema.org/schema/Brick#>
        PREFIX owl: <http://www.w3.org/2002/07/owl#>
        SELECT DISTINCT ?point WHERE {{
            <{uri}> owl:sameAs* ?equiv .
            ?equiv brick:hasPoint ?point .
        }}
    """)
    return [str(row.point) for row in rows]


def link_part_of(store, child_uri: URIRef, parent_uri: URIRef) -> None:
    """brick:hasPart / brick:isPartOf - a point or sub-component's place in
    a larger equipment's structure. Same reasoning as merge_equip: identity
    for a sub-component is always constructible by whoever's declaring the
    relationship (e.g. topic_prefix_to_equip_uri("fbf/ahu-3/economizer") for
    a human-named sub-component with no BACnet object of its own), so this
    is a direct two-URI fact, not tag-resolution. Deliberately manual only -
    no automatic label/prefix clustering: a wrong hasPart edge silently
    corrupts topology in a way that's costlier to notice than a wrong Brick
    class label (which the PROJ fallback already makes cheap and visible)."""
    store.add_relationship(parent_uri, BRICK.hasPart, child_uri)
    store.add_relationship(child_uri, BRICK.isPartOf, parent_uri)

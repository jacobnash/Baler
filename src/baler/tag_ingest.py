"""
The tag path (Baler#233): tags and equip messages, off the readings path.

Tags used to be handled inline in mqtt_listener's on_message: 3+ Oxigraph
round trips and a classification per message, so a tags storm (every
point's tags, retained, on each Windrow or listener restart) held every
reading queued behind it, and the scale ladder's graph phase ran at
150-1,400 tags/s.

Now a listener worker only queues a tag message into its current batch:
it commits with the batch's readings, inside the batch's reading-feed row
(reading_feed.append_from_stage), and is acknowledged after that commit like
a reading. One `tag_ingest` process per listener (mqtt_listener's
supervisor starts it) reads the feed as a consumer of its own, in commit
order, and applies each poll's tags to the graph with apply(): one
INSERT DATA for every tag of every message, one SPARQL update per link
direction, two reads and one write for the classification of the lot.

So a tags storm costs the readings path a few JSON values per batch, and
the graph catches up on its own clock; how far behind it is reads off the
feed like any consumer's lag (/ops/status, the ladder). It's at least once:
a redelivered batch reapplies tags, and every effect here is idempotent.
"""

from __future__ import annotations

import functools
import json
import os
import time
from functools import lru_cache

from rdflib import URIRef

from baler import heartbeat, mapping, reading_feed, tracing
from baler.ingest import (
    haystack_ref_to_equip_uri,
    tag_triples,
    topic_prefix_to_equip_uri,
    topic_to_point_uri,
)
from baler.store import BRICK, HAYSTACK

tracer = tracing.get_tracer(__name__)

CONSUMER = "tag_ingest"
# Only one tag_ingest drains the feed at a time, whatever runs it (a second
# listener container, say): the others wait on this session lock.
DRAIN_LOCK_KEY = 243_233_001


@lru_cache(maxsize=2)
def _rules(path: str):
    return mapping.load_rules(path)


def kind(topic: str) -> str | None:
    """'equip' (BACnet/Modbus connection equip), 'haystack_equip',
    'point' tags, or None for a topic that isn't a tags message."""
    if not topic.endswith("/tags"):
        return None
    if topic.endswith("/equip/tags"):
        return "equip"
    if "/equip/" in topic:
        return "haystack_equip"
    return "point"


def is_tags(topic: str) -> bool:
    return topic.endswith("/tags")


# A point's equipRef -> brick:isPointOf/hasPoint, for every point in VALUES
# whose equip rec has landed (the batched ingest.link_equip_ref).
_LINK_POINTS = """
PREFIX haystack: <urn:baler:haystack#>
PREFIX brick: <https://brickschema.org/schema/Brick#>
INSERT {{ ?p brick:isPointOf ?e . ?e brick:hasPoint ?p }}
WHERE {{
    VALUES ?p {{ {values} }}
    ?p haystack:equipRef ?ref .
    BIND(IRI(CONCAT("urn:equip:haystack:", STR(?ref))) AS ?e)
    FILTER EXISTS {{ ?e ?any ?thing }}
}}"""

# The other direction: points whose tags landed before their equip rec.
# Inline handling only ever linked from the point side, so a point that
# arrived first stayed unlinked until it was re-tagged; with tags applied
# by a separate process, in commit order across several listener workers,
# either order is normal.
_LINK_EQUIPS = """
PREFIX haystack: <urn:baler:haystack#>
PREFIX brick: <https://brickschema.org/schema/Brick#>
INSERT {{ ?p brick:isPointOf ?e . ?e brick:hasPoint ?p }}
WHERE {{
    VALUES (?e ?ref) {{ {values} }}
    ?p haystack:equipRef ?ref .
}}"""


def _lit(s: str) -> str:
    return json.dumps(s)  # a JSON string is a valid SPARQL string literal


def apply(store, msgs, log=print) -> dict:
    """Apply tag messages - (topic, value, ts) each, `value` the envelope's
    JSON text - to the graph, as handling them one by one in order would.
    Malformed ones are logged, counted and skipped. Returns counts."""
    with tracer.start_as_current_span("tag_ingest.apply") as span:
        triples: list = []
        points: set[URIRef] = set()
        equips: dict[URIRef, str] = {}  # haystack equip uri -> its ref
        classify: dict = {}
        bad = 0
        for topic, value, _ts in msgs:
            k = kind(topic)
            try:
                data = json.loads(value)
                if not isinstance(data, dict):
                    raise TypeError(f"expected a JSON object, got {type(data).__name__}")
                if k == "equip":
                    prefix = topic[: -len("/equip/tags")]
                    uri = topic_prefix_to_equip_uri(prefix)
                    tags = data.get("tags", {})
                    labels = data.get("points", [])
                    new = tag_triples(uri, tags)
                    for label in labels:
                        p = topic_to_point_uri(f"{prefix}/{label}")
                        new += [(uri, BRICK.hasPoint, p), (p, BRICK.isPointOf, uri)]
                    classify[uri] = _rules(mapping.EQUIP_RULES_PATH)
                elif k == "haystack_equip":
                    ref = topic[: -len("/tags")].rsplit("/equip/", 1)[1]
                    uri = haystack_ref_to_equip_uri(ref)
                    new = tag_triples(uri, data)
                    equips[uri] = ref
                    classify[uri] = _rules(mapping.EQUIP_RULES_PATH)
                elif k == "point":
                    uri = topic_to_point_uri(topic[: -len("/tags")])
                    new = tag_triples(uri, data)
                    points.add(uri)
                    classify[uri] = _rules(mapping.DEFAULT_RULES_PATH)
                else:
                    continue
            except (json.JSONDecodeError, TypeError, ValueError, AttributeError) as exc:
                bad += 1
                log(f"malformed tags payload on {topic}: {exc!r}")
                continue
            triples.extend(new)
        span.set_attribute("messages", len(msgs))
        span.set_attribute("triples", len(triples))
        store.add_many(triples)
        if points:
            store.update(_LINK_POINTS.format(values=" ".join(f"<{p}>" for p in points)))
        if equips:
            ref_t = HAYSTACK.Ref
            values = " ".join(f"(<{e}> {_lit(r)}) (<{e}> {_lit(r)}^^<{ref_t}>)" for e, r in equips.items())
            store.update(_LINK_EQUIPS.format(values=values))
        classified = mapping.classify_on_ingest_many(store, classify) if classify else {}
        return {"messages": len(msgs), "bad": bad, "points": len(points), "equips": len(classify) - len(points),
                "triples": len(triples), "classified": len(classified)}


def _beat(conn, consumer: reading_feed.FeedConsumer, totals: dict) -> None:
    stats = consumer.refresh_lag()
    heartbeat.beat(conn, "mqtt_listener/tags", {**totals, "feed": stats.as_dict(), "pid": os.getpid()})


def run(store, conn, stop=None, max_batches: int = 200, idle_s: float = 0.2, report_every: float = 10.0,
        log=None) -> None:
    """The tag_ingest loop: hold the drain lock, then poll the feed's tags,
    apply them, move the offset - forever, or until `stop` is set. An
    exception (Oxigraph down) propagates: the process exits and the
    supervisor restarts it; the offset hasn't moved, so nothing is lost
    unless it stays down past the feed's retention, and then the pruner
    counts what was missed (missed_readings counts the batches' tags)."""
    log = log or functools.partial(print, flush=True)
    while not conn.execute("SELECT pg_try_advisory_lock(%s)", (DRAIN_LOCK_KEY,)).fetchone()[0]:
        if stop is None:
            time.sleep(5)
        elif stop.wait(5):
            return
    consumer = reading_feed.FeedConsumer(conn, CONSUMER, max_batches=max_batches, log=log, tags_only=True)
    consumer.start()
    totals = {"messages": 0, "bad": 0, "triples": 0, "classified": 0, "polls": 0, "apply_s": 0.0}
    last_report = 0.0
    log(f"tag_ingest: draining the reading feed's tags from seq {consumer.seq}")
    while stop is None or not stop.is_set():
        batches = consumer.poll()
        msgs = [m for b in batches for m in b.tags]
        if msgs:
            t0 = time.perf_counter()
            counts = apply(store, msgs, log=log)
            for k in ("messages", "bad", "triples", "classified"):
                totals[k] += counts[k]
            totals["apply_s"] = round(totals["apply_s"] + time.perf_counter() - t0, 3)
        totals["polls"] += 1
        consumer.commit(batches)
        if time.monotonic() - last_report >= report_every:
            last_report = time.monotonic()
            _beat(conn, consumer, totals)
            log(f"tag_ingest: {totals['messages']} tag messages applied so far, "
                f"{consumer.stats.lag_batches} feed batches behind ({consumer.stats.lag_s:.1f} s)")
        if len(batches) < max_batches:
            time.sleep(idle_s)

"""
Unit tests for the core ingest_reading logic (fast - in-memory graph store,
real Postgres), plus one true end-to-end integration test proving the wire
contract round-trips over a real broker without coupling to Windrow's package
directly (per the plan's "wire contract, not a class hierarchy" principle).
"""

import json
import time
from types import SimpleNamespace

import pytest
from rdflib import OWL

from baler.ingest import (
    haystack_ref_to_equip_uri,
    ingest_equip_tags,
    ingest_haystack_equip_tags,
    ingest_reading,
    ingest_tags,
    link_equip_ref,
    link_part_of,
    link_point_to_equip,
    merge_equip,
    points_of_equip,
    topic_prefix_to_equip_uri,
    topic_to_point_uri,
)
from baler.mqtt_listener import make_on_message
from baler.mqtt_util import make_client
from baler.store import BRICK, TD, Store
from baler.timeseries import connect, read_latest


@pytest.fixture
def ts_conn():
    conn = connect()
    conn.execute("DELETE FROM point_history WHERE point_uri LIKE %s", ("urn:point:test-ingest:%",))
    return conn


@pytest.mark.integration
def test_ingest_reading_writes_graph_and_timeseries(ts_conn):
    store = Store()
    topic = "test-ingest:zone-temp"
    point_uri = ingest_reading(store, ts_conn, topic, 71.0, ts=time.time())

    assert point_uri == topic_to_point_uri(topic)
    rows = list(store.query(f"""
        PREFIX td: <urn:baler:td#>
        SELECT ?topic WHERE {{ <{point_uri}> td:sourceTopic ?topic }}
    """))
    assert str(rows[0].topic) == topic
    assert read_latest(ts_conn, str(point_uri)) == 71.0


@pytest.mark.integration
def test_ingest_reading_preserves_value_types(ts_conn):
    store = Store()
    ingest_reading(store, ts_conn, "test-ingest:fan-status", "active", ts=time.time())
    assert read_latest(ts_conn, str(topic_to_point_uri("test-ingest:fan-status"))) == "active"


@pytest.mark.integration
def test_ingest_reading_is_idempotent_on_same_topic_and_ts(ts_conn):
    """Repeating the exact same (point, ts, value) - e.g. an MQTT-retained
    message redelivered on resubscribe - is a safe no-op: the graph never
    duplicates the identity triple, and the timeseries row is unchanged.
    A *different* value at the same (point, ts) legitimately overwrites -
    matching Haxall's IHisExt.write contract (an existing timestamp plus a
    new value overwrites the current one) - see test_timeseries.py."""
    store = Store()
    topic = "test-ingest:idempotent"
    ts = time.time()

    ingest_reading(store, ts_conn, topic, 1.0, ts=ts)
    ingest_reading(store, ts_conn, topic, 1.0, ts=ts)  # exact repeat - stays 1.0

    assert read_latest(ts_conn, str(topic_to_point_uri(topic))) == 1.0
    # RDF triples are a set - re-adding the same fact twice never duplicates
    count = sum(1 for _ in store.graph.triples((topic_to_point_uri(topic), TD.RawPoint, None)))
    assert count <= 1


def test_ingest_tags_writes_marker_and_valued_triples():
    store = Store()
    topic = "test-ingest:tags-point"
    tags = {"zone": True, "air": True, "equipRef": "AHU-1"}

    point_uri = ingest_tags(store, topic, tags)

    assert point_uri == topic_to_point_uri(topic)
    marker_rows = list(store.query(f"""
        PREFIX haystack: <urn:baler:haystack#>
        SELECT ?tag WHERE {{ <{point_uri}> haystack:hasTag ?tag }}
    """))
    assert {str(r.tag) for r in marker_rows} == {"zone", "air"}

    equip_rows = list(store.query(f"""
        PREFIX haystack: <urn:baler:haystack#>
        SELECT ?ref WHERE {{ <{point_uri}> haystack:equipRef ?ref }}
    """))
    assert str(equip_rows[0].ref) == "AHU-1"


def test_ingest_tags_serializes_nested_dict_and_list_values_as_json():
    store = Store()
    topic = "test-ingest:nested-tags"
    point_uri = ingest_tags(store, topic, {"geoCoord": {"lat": 37.5, "lng": -122.3}, "enum": ["a", "b"]})

    rows = list(store.query(f"""
        PREFIX haystack: <urn:baler:haystack#>
        SELECT ?v WHERE {{ <{point_uri}> haystack:geoCoord ?v }}
    """))
    assert json.loads(str(rows[0].v)) == {"lat": 37.5, "lng": -122.3}


@pytest.mark.integration
def test_listener_routes_tags_suffix_without_touching_timeseries(ts_conn):
    store = Store()
    on_message = make_on_message(store, ts_conn)
    topic = "test-ingest:tags-routing"
    tags_msg = SimpleNamespace(
        topic=f"{topic}/tags",
        payload=json.dumps({"point": topic, "value": json.dumps({"fan": True}), "ts": time.time()}).encode(),
    )

    on_message(None, None, tags_msg)

    rows = list(store.query(f"""
        PREFIX haystack: <urn:baler:haystack#>
        SELECT ?tag WHERE {{ <{topic_to_point_uri(topic)}> haystack:hasTag ?tag }}
    """))
    assert {str(r.tag) for r in rows} == {"fan"}
    assert read_latest(ts_conn, str(topic_to_point_uri(topic))) is None


def test_listener_routes_bacnet_equip_tags_suffix_to_hasPoint_and_equip_tags():
    store = Store()
    on_message = make_on_message(store, None)
    topic_prefix = "fbf/ahu-listener-test"
    equip_msg = SimpleNamespace(
        topic=f"{topic_prefix}/equip/tags",
        payload=json.dumps({
            "point": "equip",
            "value": json.dumps({"tags": {"ahu": True}, "points": ["zone-temp", "fan-status"]}),
            "ts": time.time(),
        }).encode(),
    )

    on_message(None, None, equip_msg)

    equip_uri = topic_prefix_to_equip_uri(topic_prefix)
    tag_rows = list(store.query(f"""
        PREFIX haystack: <urn:baler:haystack#>
        SELECT ?tag WHERE {{ <{equip_uri}> haystack:hasTag ?tag }}
    """))
    assert {str(r.tag) for r in tag_rows} == {"ahu"}

    linked = {str(row.p) for row in store.query(f"""
        PREFIX brick: <https://brickschema.org/schema/Brick#>
        SELECT ?p WHERE {{ <{equip_uri}> brick:hasPoint ?p }}
    """)}
    assert linked == {
        str(topic_to_point_uri(f"{topic_prefix}/zone-temp")),
        str(topic_to_point_uri(f"{topic_prefix}/fan-status")),
    }


def test_listener_routes_haystack_equip_ref_tags_suffix():
    store = Store()
    on_message = make_on_message(store, None)
    ref = "AHU-listener-test-1"
    equip_msg = SimpleNamespace(
        topic=f"fbf/haxall-readback/equip/{ref}/tags",
        payload=json.dumps({"point": f"equip/{ref}", "value": json.dumps({"ahu": True}), "ts": time.time()}).encode(),
    )

    on_message(None, None, equip_msg)

    equip_uri = haystack_ref_to_equip_uri(ref)
    tag_rows = list(store.query(f"""
        PREFIX haystack: <urn:baler:haystack#>
        SELECT ?tag WHERE {{ <{equip_uri}> haystack:hasTag ?tag }}
    """))
    assert {str(r.tag) for r in tag_rows} == {"ahu"}


def test_listener_plain_tags_branch_resolves_equip_ref_when_equip_already_landed():
    """The wiring fix: a point's ordinary /tags message (not the
    /equip/tags or /equip/{ref}/tags ones) now also resolves equipRef
    automatically - previously ingest_tags ran and stopped, leaving
    HAYSTACK:equipRef an inert literal forever."""
    store = Store()
    on_message = make_on_message(store, None)
    ingest_haystack_equip_tags(store, "AHU-listener-test-2", {"ahu": True})  # equip side lands first

    topic = "test-ingest:equip-ref-wiring"
    tags_msg = SimpleNamespace(
        topic=f"{topic}/tags",
        payload=json.dumps({"point": topic, "value": json.dumps({"zone": True, "equipRef": "AHU-listener-test-2"}), "ts": time.time()}).encode(),
    )

    on_message(None, None, tags_msg)

    equip_uri = haystack_ref_to_equip_uri("AHU-listener-test-2")
    point_uri = topic_to_point_uri(topic)
    assert (equip_uri, BRICK.hasPoint, point_uri) in store.graph
    assert (point_uri, BRICK.isPointOf, equip_uri) in store.graph


@pytest.mark.integration
def test_listener_handles_malformed_payload_without_crashing(ts_conn):
    store = Store()
    on_message = make_on_message(store, ts_conn)
    bad_msg = SimpleNamespace(topic="test-ingest:bad", payload=b"not json")

    on_message(None, None, bad_msg)  # must not raise

    assert read_latest(ts_conn, str(topic_to_point_uri("test-ingest:bad"))) is None


def test_ingest_equip_tags_uses_equip_identity_not_point_identity():
    store = Store()
    equip_uri = ingest_equip_tags(store, "fbf/ahu-3", {"ahu": True, "dis": "AHU-3"})

    assert equip_uri == topic_prefix_to_equip_uri("fbf/ahu-3")
    assert equip_uri != topic_to_point_uri("fbf/ahu-3")
    rows = list(store.query(f"""
        PREFIX haystack: <urn:baler:haystack#>
        SELECT ?tag WHERE {{ <{equip_uri}> haystack:hasTag ?tag }}
    """))
    assert {str(r.tag) for r in rows} == {"ahu"}


def test_link_point_to_equip_writes_both_directions():
    store = Store()
    point_uri = topic_to_point_uri("fbf/ahu-3/zone-temp")
    equip_uri = topic_prefix_to_equip_uri("fbf/ahu-3")

    link_point_to_equip(store, point_uri, equip_uri)

    assert (equip_uri, BRICK.hasPoint, point_uri) in store.graph
    assert (point_uri, BRICK.isPointOf, equip_uri) in store.graph


def test_link_equip_ref_resolves_after_equip_is_ingested():
    """Order shouldn't matter for correctness, only for whether the link
    exists yet - a point tagged before its equip rec arrives is a normal
    transient state, not an error, and a second call after the equip
    arrives completes the link."""
    store = Store()
    point_uri = ingest_tags(store, "test-ingest:linked-point", {"zone": True, "equipRef": "AHU-1"})

    assert link_equip_ref(store, point_uri) is None  # equip rec not ingested yet

    ingest_haystack_equip_tags(store, "AHU-1", {"ahu": True})
    equip_uri = link_equip_ref(store, point_uri)

    assert equip_uri == haystack_ref_to_equip_uri("AHU-1")
    assert (equip_uri, BRICK.hasPoint, point_uri) in store.graph


def test_link_equip_ref_is_none_when_point_has_no_equip_ref():
    store = Store()
    point_uri = ingest_tags(store, "test-ingest:no-equip-ref", {"writable": True})
    assert link_equip_ref(store, point_uri) is None


def test_merge_equip_asserts_owl_sameas_both_directions():
    store = Store()
    bacnet_equip = topic_prefix_to_equip_uri("fbf/ahu-3")
    haystack_equip = haystack_ref_to_equip_uri("AHU-1")

    merge_equip(store, bacnet_equip, haystack_equip)

    assert (bacnet_equip, OWL.sameAs, haystack_equip) in store.graph
    assert (haystack_equip, OWL.sameAs, bacnet_equip) in store.graph


def test_points_of_equip_returns_only_its_own_points_when_unmerged():
    store = Store()
    equip_uri = topic_prefix_to_equip_uri("fbf/ahu-4")
    point_uri = topic_to_point_uri("fbf/ahu-4/zone-temp")
    link_point_to_equip(store, point_uri, equip_uri)

    assert points_of_equip(store, equip_uri) == [str(point_uri)]


def test_points_of_equip_unions_across_a_merged_pair_from_either_side():
    store = Store()
    bacnet_equip = topic_prefix_to_equip_uri("fbf/ahu-5")
    haystack_equip = haystack_ref_to_equip_uri("AHU-5")
    bacnet_point = topic_to_point_uri("fbf/ahu-5/zone-temp")
    haystack_point = topic_to_point_uri("test-ingest:haystack-fan-status")

    link_point_to_equip(store, bacnet_point, bacnet_equip)
    link_point_to_equip(store, haystack_point, haystack_equip)
    merge_equip(store, bacnet_equip, haystack_equip)

    expected = {str(bacnet_point), str(haystack_point)}
    assert set(points_of_equip(store, bacnet_equip)) == expected
    assert set(points_of_equip(store, haystack_equip)) == expected  # queryable from either URI


def test_link_part_of_writes_both_directions():
    store = Store()
    parent = topic_prefix_to_equip_uri("fbf/ahu-3")
    child = topic_prefix_to_equip_uri("fbf/ahu-3/economizer")

    link_part_of(store, child, parent)

    assert (parent, BRICK.hasPart, child) in store.graph
    assert (child, BRICK.isPartOf, parent) in store.graph


@pytest.mark.integration
def test_full_round_trip_over_real_broker(ts_conn):
    """Publishes the exact envelope Windrow's bridges use, over the real
    compose-managed broker, and confirms a short-lived listener loop
    ingests it - proves the wire contract, not just the function calls."""
    from baler.remote_store import RemoteStore

    topic = f"test-ingest:roundtrip:{int(time.time())}"
    store = RemoteStore()

    listener = make_client()
    listener.on_message = make_on_message(store, ts_conn)
    listener.connect("localhost", 1883)
    listener.subscribe(f"fbf/{topic}")

    publisher = make_client()
    publisher.connect("localhost", 1883)
    payload = json.dumps({"point": topic, "value": 42.0, "ts": time.time()})
    publisher.publish(f"fbf/{topic}", payload, retain=True)
    publisher.disconnect()

    for _ in range(20):  # up to ~2s, no arbitrary long sleep
        listener.loop(timeout=0.1)
        if read_latest(ts_conn, str(topic_to_point_uri(f"fbf/{topic}"))) is not None:
            break

    assert read_latest(ts_conn, str(topic_to_point_uri(f"fbf/{topic}"))) == 42.0
    listener.disconnect()

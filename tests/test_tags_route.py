"""
POST /tags - the HTTP equivalent of the `<topic>/tags` MQTT suffix
convention. Same live_server-against-real-spec pattern as
test_history_routes.py.
"""

import time
from http.server import ThreadingHTTPServer
from threading import Thread

import jsonschema
import pytest
import requests
import yaml
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from baler.ingest import (
    haystack_ref_to_equip_uri,
    ingest_haystack_equip_tags,
    link_point_to_equip,
    points_of_equip,
    topic_prefix_to_equip_uri,
    topic_to_point_uri,
)
from baler.ingest_api import OPENAPI_SPEC_PATH, make_handler
from baler.remote_store import RemoteStore
from baler.timeseries import connect_pool


@pytest.fixture(scope="module")
def spec() -> dict:
    with open(OPENAPI_SPEC_PATH) as f:
        return yaml.safe_load(f)


GATEWAY_SECRET = "test-gateway-secret"


@pytest.fixture
def live_server(monkeypatch):
    monkeypatch.setenv("BALER_GATEWAY_SECRET", GATEWAY_SECRET)
    store = RemoteStore()
    ts_pool = connect_pool()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, ts_pool))
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    yield base_url
    server.shutdown()
    ts_pool.close()


@pytest.fixture
def gw(live_server):
    """A requests.Session with the gateway-secret header pre-attached -
    every route except GET /openapi.yaml and GET /docs needs it (see
    gateway_auth.request_came_through_gateway)."""
    s = requests.Session()
    s.headers["X-Gateway-Secret"] = GATEWAY_SECRET
    return s


def _assert_matches_schema(instance, spec: dict, json_pointer: str) -> None:
    resource = Resource.from_contents(spec, default_specification=DRAFT202012)
    registry = Registry().with_resource(uri="spec", resource=resource)
    schema = {"$ref": f"spec#{json_pointer}"}
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls(schema, registry=registry).validate(instance)


@pytest.mark.integration
def test_post_tags_without_gateway_secret_is_401(live_server):
    resp = requests.post(f"{live_server}/tags", json={"point": "whatever", "tags": {}})
    assert resp.status_code == 401


@pytest.mark.integration
def test_tags_direct_match_classifies_into_brick(gw, live_server, spec):
    topic = f"test-tags:direct:{int(time.time())}"
    resp = gw.post(f"{live_server}/tags", json={
        "point": topic,
        "tags": {"zone": True, "air": True, "temp": True, "sensor": True, "unit": "°F"},
    })
    assert resp.status_code == 200
    body = resp.json()
    _assert_matches_schema(body, spec, "/paths/~1tags/post/responses/200/content/application~1json/schema")
    assert body == {"point": f"urn:point:{topic}", "outcome": "direct", "brickClass": "Zone_Air_Temperature_Sensor"}


@pytest.mark.integration
def test_tags_no_overlap_is_a_miss(gw, live_server):
    topic = f"test-tags:miss:{int(time.time())}"
    resp = gw.post(f"{live_server}/tags", json={"point": topic, "tags": {"weather": True}})
    assert resp.status_code == 200
    assert resp.json() == {"point": f"urn:point:{topic}", "outcome": "miss", "brickClass": None}


@pytest.mark.integration
def test_tags_missing_field_matches_documented_400(gw, live_server, spec):
    resp = gw.post(f"{live_server}/tags", json={"point": "whatever"})  # no tags
    assert resp.status_code == 400
    _assert_matches_schema(resp.json(), spec, "/paths/~1tags/post/responses/400/content/application~1json/schema")


@pytest.mark.integration
def test_tags_reposting_with_completing_tags_reclassifies_not_double_types(gw, live_server):
    """The reclassify wiring fix, honestly exercised: tags are additive
    (POSTing twice never un-asserts the first POST's tags), so the only way
    a second POST legitimately changes the outcome is when the added tags
    complete a *more specific* match than before - here, fallback (missing
    "temp") -> direct (all 4 tags present). Previously (bare classify_point)
    the stale PROJ fallback type would still be asserted alongside the new
    direct Brick type; reclassify must leave only the second."""
    topic = f"test-tags:reclassify:{int(time.time())}"
    first = gw.post(f"{live_server}/tags", json={"point": topic, "tags": {"zone": True, "air": True, "sensor": True}})
    assert first.json()["outcome"] == "fallback"
    assert first.json()["brickClass"] == "Zone_Air_Temperature_Sensor"

    second = gw.post(f"{live_server}/tags", json={"point": topic, "tags": {"temp": True}})  # completes the direct match
    assert second.status_code == 200
    assert second.json() == {"point": f"urn:point:{topic}", "outcome": "direct", "brickClass": "Zone_Air_Temperature_Sensor"}

    store = RemoteStore()
    rows = store.query(f"""
        PREFIX brick: <https://brickschema.org/schema/Brick#>
        SELECT ?type WHERE {{ <urn:point:{topic}> a ?type }}
    """)
    ontology_types = {r.type for r in rows if "brickschema.org" in r.type or "baler:proj" in r.type}
    assert ontology_types == {"https://brickschema.org/schema/Brick#Zone_Air_Temperature_Sensor"}


@pytest.mark.integration
def test_tags_wires_equip_ref_into_hasPoint_automatically(gw, live_server):
    """The other wiring fix: a point's equipRef, once the matching equip's
    own tags have landed, now resolves into a real hasPoint/isPointOf edge
    without any extra call - link_equip_ref existed but was never called
    from this path before."""
    ref = f"test-tags-equip-{int(time.time())}"
    topic = f"test-tags:equip-ref-wiring:{int(time.time())}"

    store = RemoteStore()
    ingest_haystack_equip_tags(store, ref, {"ahu": True})

    resp = gw.post(f"{live_server}/tags", json={"point": topic, "tags": {"zone": True, "equipRef": ref}})
    assert resp.status_code == 200

    equip_uri = haystack_ref_to_equip_uri(ref)
    rows = store.query(f"""
        PREFIX brick: <https://brickschema.org/schema/Brick#>
        SELECT ?p WHERE {{ <{equip_uri}> brick:hasPoint ?p }}
    """)
    assert {r.p for r in rows} == {f"urn:point:{topic}"}


@pytest.mark.integration
def test_equip_merge_unions_points_via_points_of_equip(gw, live_server, spec):
    store = RemoteStore()
    suffix = int(time.time())
    bacnet_equip = topic_prefix_to_equip_uri(f"test-equip-merge:{suffix}")
    haystack_equip = haystack_ref_to_equip_uri(f"test-equip-merge-{suffix}")
    bacnet_point = topic_to_point_uri(f"test-equip-merge:{suffix}/zone-temp")
    haystack_point = topic_to_point_uri(f"test-equip-merge-haystack:{suffix}/fan-status")

    link_point_to_equip(store, bacnet_point, bacnet_equip)
    ingest_haystack_equip_tags(store, f"test-equip-merge-{suffix}", {"ahu": True})
    link_point_to_equip(store, haystack_point, haystack_equip)

    resp = gw.post(f"{live_server}/equip/merge", json={"a": str(bacnet_equip), "b": str(haystack_equip)})
    assert resp.status_code == 204

    expected = {str(bacnet_point), str(haystack_point)}
    assert set(points_of_equip(store, bacnet_equip)) == expected
    assert set(points_of_equip(store, haystack_equip)) == expected


@pytest.mark.integration
def test_equip_merge_missing_field_matches_documented_400(gw, live_server, spec):
    resp = gw.post(f"{live_server}/equip/merge", json={"a": "urn:equip:whatever"})  # no b
    assert resp.status_code == 400
    _assert_matches_schema(resp.json(), spec, "/paths/~1equip~1merge/post/responses/400/content/application~1json/schema")


@pytest.mark.integration
def test_part_links_hasPart_and_isPartOf(gw, live_server):
    suffix = int(time.time())
    parent = f"urn:equip:test-part:{suffix}"
    child = f"urn:equip:test-part:{suffix}/economizer"

    resp = gw.post(f"{live_server}/part", json={"child": child, "parent": parent})
    assert resp.status_code == 204

    store = RemoteStore()
    rows = store.query(f"""
        PREFIX brick: <https://brickschema.org/schema/Brick#>
        SELECT ?c WHERE {{ <{parent}> brick:hasPart ?c }}
    """)
    assert {r.c for r in rows} == {child}


@pytest.mark.integration
def test_part_missing_field_matches_documented_400(gw, live_server, spec):
    resp = gw.post(f"{live_server}/part", json={"child": "urn:point:whatever"})  # no parent
    assert resp.status_code == 400
    _assert_matches_schema(resp.json(), spec, "/paths/~1part/post/responses/400/content/application~1json/schema")

"""
GET/DELETE /history - the Baler side of Haxall's IHisExt read/write
contract. Same live_server-against-real-spec pattern as test_openapi.py:
a route is only "documented" if a real request/response round trip
actually matches what the spec claims.
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
    """json_pointer e.g. '/paths/~1history/get/responses/200/content/application~1json/schema'.
    Validating via a $ref that stays rooted in the registered spec resource
    (rather than pulling out a raw dict) means nested $refs inside the
    target schema - like an array's `items: {$ref: HistoryItem}` - resolve
    correctly too, not just a $ref at the very top of the extracted schema."""
    resource = Resource.from_contents(spec, default_specification=DRAFT202012)
    registry = Registry().with_resource(uri="spec", resource=resource)
    schema = {"$ref": f"spec#{json_pointer}"}
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls(schema, registry=registry).validate(instance)


@pytest.mark.integration
def test_get_history_without_gateway_secret_is_401(live_server):
    resp = requests.get(f"{live_server}/history", params={"point": "whatever"})
    assert resp.status_code == 401


@pytest.mark.integration
def test_history_round_trip_matches_documented_schema(gw, live_server, spec):
    # Both timestamps safely in the past - omitting `end` defaults to "now",
    # so a timestamp even a second ahead of the real clock would (correctly)
    # get excluded by that bound.
    topic = f"test-history:round-trip:{int(time.time())}"
    t0 = time.time() - 10
    gw.post(f"{live_server}/ingest", json=[
        {"point": topic, "value": 1.0, "ts": t0},
        {"point": topic, "value": 2.0, "ts": t0 + 1},
    ])

    resp = gw.get(f"{live_server}/history", params={"point": topic})
    assert resp.status_code == 200
    items = resp.json()
    _assert_matches_schema(items, spec, "/paths/~1history/get/responses/200/content/application~1json/schema")
    # Postgres timestamptz is microsecond-resolution, so it can round a
    # `time.time()` float's sub-microsecond digits differently than
    # Python's own repr - exact equality here is a real, ~30%-of-the-time
    # flake, not a schema/behavior bug. abs=1e-6 matches that resolution.
    assert items[0]["value"] == 1.0 and items[0]["ts"] == pytest.approx(t0, abs=1e-6)
    assert items[1]["value"] == 2.0 and items[1]["ts"] == pytest.approx(t0 + 1, abs=1e-6)


@pytest.mark.integration
def test_history_start_end_span_filters_inclusively(gw, live_server):
    topic = f"test-history:span:{int(time.time())}"
    t0 = time.time() - 30
    gw.post(f"{live_server}/ingest", json=[
        {"point": topic, "value": 1.0, "ts": t0},
        {"point": topic, "value": 2.0, "ts": t0 + 10},
        {"point": topic, "value": 3.0, "ts": t0 + 20},
    ])

    resp = gw.get(f"{live_server}/history", params={"point": topic, "start": t0, "end": t0 + 10})
    assert [item["value"] for item in resp.json()] == [1.0, 2.0]


@pytest.mark.integration
def test_history_missing_point_matches_documented_400(gw, live_server, spec):
    resp = gw.get(f"{live_server}/history")
    assert resp.status_code == 400
    _assert_matches_schema(resp.json(), spec, "/paths/~1history/get/responses/400/content/application~1json/schema")


@pytest.mark.integration
def test_history_malformed_start_matches_documented_400(gw, live_server):
    resp = gw.get(f"{live_server}/history", params={"point": "whatever", "start": "not-a-number"})
    assert resp.status_code == 400
    assert "error" in resp.json()


@pytest.mark.integration
def test_delete_history_item_then_404_on_repeat(gw, live_server):
    topic = f"test-history:delete:{int(time.time())}"
    t0 = time.time()
    gw.post(f"{live_server}/ingest", json={"point": topic, "value": 1.0, "ts": t0})

    resp = gw.delete(f"{live_server}/history", params={"point": topic, "ts": t0})
    assert resp.status_code == 204
    assert resp.content == b""

    resp_again = gw.delete(f"{live_server}/history", params={"point": topic, "ts": t0})
    assert resp_again.status_code == 404

    assert gw.get(f"{live_server}/history", params={"point": topic}).json() == []


@pytest.mark.integration
def test_delete_history_missing_params_matches_documented_400(gw, live_server, spec):
    resp = gw.delete(f"{live_server}/history", params={"point": "whatever"})  # no ts
    assert resp.status_code == 400
    _assert_matches_schema(resp.json(), spec, "/paths/~1history/delete/responses/400/content/application~1json/schema")

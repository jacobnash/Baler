"""
Two layers, both required for "the docs are actually true": (1) the spec
itself is well-formed OpenAPI, (2) real live requests against the real
running API actually produce responses matching what the spec documents.
Writing an accurate-looking YAML file proves nothing on its own - only a
real request/response round trip does.
"""

import time
from http.server import ThreadingHTTPServer
from threading import Thread

import pytest
import requests
import yaml
from conftest import assert_matches_schema, load_spec
from openapi_spec_validator import validate

from baler.ingest_api import OPENAPI_SPEC_PATH, make_handler
from baler.remote_store import RemoteStore
from baler.timeseries import connect_pool


@pytest.fixture(scope="module")
def spec() -> dict:
    return load_spec(OPENAPI_SPEC_PATH)


def test_spec_is_well_formed_openapi(spec):
    validate(spec)  # raises on any real structural problem


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


def _schema_for(spec: dict, status: str, path: str = "/ingest", method: str = "post") -> dict:
    response = spec["paths"][path][method]["responses"][status]
    return response["content"]["application/json"]["schema"]


@pytest.mark.integration
def test_get_openapi_yaml_serves_the_real_spec_file(live_server):
    resp = requests.get(f"{live_server}/openapi.yaml")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "application/yaml"
    served = yaml.safe_load(resp.text)
    assert served["info"]["title"] == "Baler Ingest API"


@pytest.mark.integration
def test_post_ingest_without_gateway_secret_is_401(live_server):
    resp = requests.post(f"{live_server}/ingest", json={"point": "whatever", "value": 1.0})
    assert resp.status_code == 401


@pytest.mark.integration
def test_post_ingest_single_reading_matches_documented_204(gw, live_server, spec):
    topic = f"test-openapi:single:{int(time.time())}"
    resp = gw.post(f"{live_server}/ingest", json={"point": topic, "value": 71.0})

    assert resp.status_code == 204
    assert resp.content == b""  # spec documents no response body on 204


@pytest.mark.integration
def test_post_ingest_batch_matches_documented_204(gw, live_server):
    ts = int(time.time())
    resp = gw.post(
        f"{live_server}/ingest",
        json=[
            {"point": f"test-openapi:batch-a:{ts}", "value": 1.0},
            {"point": f"test-openapi:batch-b:{ts}", "value": "active"},
        ],
    )
    assert resp.status_code == 204


@pytest.mark.integration
def test_post_ingest_malformed_json_matches_documented_400_schema(gw, live_server, spec):
    resp = gw.post(
        f"{live_server}/ingest",
        data=b"not json",
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 400
    body = resp.json()
    assert_matches_schema(body, _schema_for(spec, "400"), spec)
    assert "error" in body


@pytest.mark.integration
def test_post_ingest_missing_point_field_matches_documented_400_schema(gw, live_server, spec):
    resp = gw.post(f"{live_server}/ingest", json={"value": 71.0})  # no "point"

    assert resp.status_code == 400
    body = resp.json()
    assert_matches_schema(body, _schema_for(spec, "400"), spec)


@pytest.mark.integration
def test_unknown_path_matches_documented_404(gw, live_server):
    resp = gw.get(f"{live_server}/does-not-exist")
    assert resp.status_code == 404

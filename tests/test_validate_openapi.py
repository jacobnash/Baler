"""
Same two-layer pattern as test_fault_openapi.py: (1) the spec is
well-formed OpenAPI, (2) real live requests against the real running
validate_api.py produce responses matching what the spec documents.
"""

from http.server import ThreadingHTTPServer
from threading import Thread

import pytest
import requests
import yaml
from conftest import assert_matches_schema, load_spec
from openapi_spec_validator import validate

from baler.remote_store import RemoteStore
from baler.validate_api import OPENAPI_SPEC_PATH, make_handler


@pytest.fixture(scope="module")
def spec() -> dict:
    return load_spec(OPENAPI_SPEC_PATH)


def test_spec_is_well_formed_openapi(spec):
    validate(spec)


GATEWAY_SECRET = "test-gateway-secret"


@pytest.fixture
def live_server(monkeypatch):
    monkeypatch.setenv("BALER_GATEWAY_SECRET", GATEWAY_SECRET)
    store = RemoteStore()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store))
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.mark.integration
def test_get_openapi_yaml_serves_the_real_spec_file(live_server):
    resp = requests.get(f"{live_server}/openapi.yaml")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "application/yaml"
    served = yaml.safe_load(resp.text)
    assert served["info"]["title"] == "Baler Validate API"


@pytest.mark.integration
def test_post_validate_without_gateway_secret_is_401(live_server):
    resp = requests.post(f"{live_server}/validate")
    assert resp.status_code == 401


@pytest.mark.integration
def test_post_validate_matches_documented_schema(live_server, spec):
    resp = requests.post(f"{live_server}/validate", headers={"X-Gateway-Secret": GATEWAY_SECRET})
    assert resp.status_code == 200
    body = resp.json()
    assert_matches_schema(
        body,
        spec["paths"]["/validate"]["post"]["responses"]["200"]["content"]["application/json"]["schema"],
        spec,
    )
    assert isinstance(body["conforms"], bool)
    assert isinstance(body["violations"], list)

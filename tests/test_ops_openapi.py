"""
Same two-layer pattern as test_validate_openapi.py: (1) the spec is
well-formed OpenAPI, (2) live requests against ops_api match the schema.
"""

from http.server import ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch

import pytest
import requests
import yaml
from conftest import assert_matches_schema, load_spec
from openapi_spec_validator import validate

from baler.ops_api import OPENAPI_SPEC_PATH, make_handler


@pytest.fixture(scope="module")
def spec() -> dict:
    return load_spec(OPENAPI_SPEC_PATH)


def test_spec_is_well_formed_openapi(spec):
    validate(spec)


GATEWAY_SECRET = "test-gateway-secret"


@pytest.fixture
def live_server(monkeypatch):
    monkeypatch.setenv("BALER_GATEWAY_SECRET", GATEWAY_SECRET)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler())
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_get_openapi_yaml_serves_the_real_spec_file(live_server):
    resp = requests.get(f"{live_server}/openapi.yaml")
    assert resp.status_code == 200
    served = yaml.safe_load(resp.text)
    assert served["info"]["title"] == "Baler Ops API"


def test_get_status_without_gateway_secret_is_401(live_server):
    resp = requests.get(f"{live_server}/status")
    assert resp.status_code == 401


def test_get_status_matches_documented_schema(live_server, spec):
    fake = {
        "summary": "ok",
        "generated_at": "2026-09-09T00:00:00+00:00",
        "building": {
            "last_ingest_at": None,
            "points_seen_last_hour": 0,
            "open_faults": 0,
            "disabled_derivation_targets": 0,
            "needs_first_org": True,
            "cards": [
                {"id": "open_faults", "label": "Open faults", "layer": "building", "service": "building",
                 "state": "ok", "detail": "0", "href": "alarms.html"},
            ],
        },
        "platform": [
            {
                "id": "historian",
                "label": "Historian",
                "layer": "platform",
                "service": "postgres",
                "state": "ok",
                "detail": None,
            }
        ],
        "jobs": [],
        "tools": [],
        "attention": [],
        "first_actions": [
            {
                "id": "connect_source",
                "label": "Connect a source",
                "href": "connectors.html",
                "description": "x",
                "enabled": True,
            }
        ],
    }
    with patch("baler.ops_api.collect_status", return_value=fake):
        resp = requests.get(
            f"{live_server}/status",
            headers={"X-Gateway-Secret": GATEWAY_SECRET},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert_matches_schema(
        body,
        spec["paths"]["/status"]["get"]["responses"]["200"]["content"]["application/json"]["schema"],
        spec,
    )
    assert body["summary"] == "ok"
    assert body["building"]["needs_first_org"] is True


def test_get_metrics_needs_the_gateway_and_serves_prometheus_text(live_server, spec):
    assert requests.get(f"{live_server}/metrics").status_code == 401
    assert "/metrics" in spec["paths"]



def test_get_metrics_through_the_gateway(monkeypatch):
    from contextlib import contextmanager

    class Pool:
        @contextmanager
        def connection(self):
            yield object()

    monkeypatch.setenv("BALER_GATEWAY_SECRET", GATEWAY_SECRET)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ts_pool=Pool()))
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        with patch("baler.ops_api.scope_for", return_value=None), \
             patch("baler.ops_metrics.collect", return_value={"now": __import__("datetime").datetime.now(), "details": {
                 "mqtt_listener": {"queue": 4}}}):
            resp = requests.get(f"http://127.0.0.1:{server.server_address[1]}/metrics",
                                headers={"X-Gateway-Secret": GATEWAY_SECRET})
    finally:
        server.shutdown()
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/plain; version=0.0.4")
    assert "baler_listener_queue_depth 4" in resp.text

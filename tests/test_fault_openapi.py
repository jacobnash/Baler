"""
Two layers, same reasoning as test_openapi.py: (1) the spec is well-formed
OpenAPI, (2) real live requests against the real running fault_api.py
produce responses matching what the spec documents.
"""

from http.server import ThreadingHTTPServer
from threading import Thread

import pytest
import requests
import yaml
from conftest import assert_matches_schema, load_spec
from openapi_spec_validator import validate

from baler.fault_api import OPENAPI_SPEC_PATH, make_handler
from baler.faults import ensure_schema
from baler.timeseries import connect_pool


@pytest.fixture(scope="module")
def spec() -> dict:
    return load_spec(OPENAPI_SPEC_PATH)


def test_spec_is_well_formed_openapi(spec):
    validate(spec)


def _lookup(spec: dict, ref: str) -> dict:
    node = spec
    for part in ref.lstrip("#/").split("/"):
        node = node[part]
    return node


def _response_schema(spec: dict, response: dict) -> dict:
    """Resolves a response-level $ref (e.g. '#/components/responses/BadRequest')
    before drilling into .content.application/json.schema - distinct from
    a schema-level $ref, which assert_matches_schema handles separately."""
    if "$ref" in response:
        response = _lookup(spec, response["$ref"])
    return response["content"]["application/json"]["schema"]


GATEWAY_SECRET = "test-gateway-secret"


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    monkeypatch.setenv("BALER_GATEWAY_SECRET", GATEWAY_SECRET)
    ts_pool = connect_pool()
    with ts_pool.connection() as conn:
        ensure_schema(conn)
    rules_path = str(tmp_path / "rules.json")
    webhooks_path = str(tmp_path / "webhooks.json")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(rules_path, webhooks_path, ts_pool))
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


@pytest.mark.integration
def test_get_openapi_yaml_serves_the_real_spec_file(live_server):
    resp = requests.get(f"{live_server}/openapi.yaml")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "application/yaml"
    served = yaml.safe_load(resp.text)
    assert served["info"]["title"] == "Baler Fault API"


@pytest.mark.integration
def test_post_without_gateway_secret_is_401(live_server):
    resp = requests.post(f"{live_server}/rules", json={"name": "x", "mode": "cur", "type": "range", "applies_to": {}, "min": 1, "max": 2})
    assert resp.status_code == 401


@pytest.mark.integration
def test_full_rule_webhook_fault_flow_matches_documented_schemas(gw, live_server, spec):
    rule_resp = gw.post(
        f"{live_server}/rules",
        json={"name": "temp-range", "mode": "cur", "type": "range", "applies_to": {"topic_glob": "test-openapi-fd/*"}, "min": 60.0, "max": 80.0},
    )
    assert rule_resp.status_code == 201
    rule = rule_resp.json()
    assert_matches_schema(rule, spec["paths"]["/rules"]["post"]["responses"]["201"]["content"]["application/json"]["schema"], spec)

    list_resp = gw.get(f"{live_server}/rules")
    assert list_resp.status_code == 200
    assert any(r["id"] == rule["id"] for r in list_resp.json())

    webhook_resp = gw.post(
        f"{live_server}/webhooks",
        json={"url": "https://example.com/hooks", "secret": "shh", "filter": None},
    )
    assert webhook_resp.status_code == 201
    webhook = webhook_resp.json()
    assert webhook["secret"] == "shh"  # shown once, at creation

    webhooks_list = gw.get(f"{live_server}/webhooks").json()
    matching = next(w for w in webhooks_list if w["id"] == webhook["id"])
    assert matching["secret"] is None  # redacted on every later GET
    # never had a delivery attempt - health defaults, not absent/error
    assert matching["disabled"] is False
    assert matching["consecutive_failures"] == 0
    assert matching["last_error"] is None
    assert_matches_schema(matching, {"$ref": "#/components/schemas/Webhook"}, spec)

    enable_resp = gw.post(f"{live_server}/webhooks/{webhook['id']}/enable")
    assert enable_resp.status_code == 204
    enable_missing_resp = gw.post(f"{live_server}/webhooks/does-not-exist/enable")
    assert enable_missing_resp.status_code == 404

    faults_resp = gw.get(f"{live_server}/faults")
    assert faults_resp.status_code == 200
    assert isinstance(faults_resp.json(), list)

    # ack/snooze need a real fault row - open one directly against the same
    # DB this live server's ts_pool points at (fault_detector.py's job, not
    # this API's, to open faults - so bypass it here).
    from datetime import datetime, timezone

    from baler.faults import open_fault
    from baler.timeseries import connect

    ts_conn = connect()
    fault_id = open_fault(ts_conn, rule["id"], "urn:point:test-openapi-fd/ack-snooze", datetime.now(timezone.utc), severity="critical")
    ts_conn.close()

    ack_resp = gw.post(f"{live_server}/faults/{fault_id}/ack")
    assert ack_resp.status_code == 204
    ack_missing_resp = gw.post(f"{live_server}/faults/999999999/ack")
    assert ack_missing_resp.status_code == 404

    snooze_resp = gw.post(f"{live_server}/faults/{fault_id}/snooze", json={"minutes": 30})
    assert snooze_resp.status_code == 200
    assert_matches_schema(
        snooze_resp.json(),
        spec["paths"]["/faults/{id}/snooze"]["post"]["responses"]["200"]["content"]["application/json"]["schema"],
        spec,
    )
    snooze_bad_resp = gw.post(f"{live_server}/faults/{fault_id}/snooze", json={"minutes": -5})
    assert snooze_bad_resp.status_code == 400
    snooze_missing_resp = gw.post(f"{live_server}/faults/999999999/snooze", json={"minutes": 5})
    assert snooze_missing_resp.status_code == 404

    acked_fault = next(f for f in gw.get(f"{live_server}/faults").json() if f["id"] == fault_id)
    assert acked_fault["severity"] == "critical"
    assert acked_fault["acked_by"] is not None
    assert acked_fault["snoozed_until"] is not None
    assert_matches_schema(acked_fault, {"$ref": "#/components/schemas/Fault"}, spec)

    # malformed body -> documented 400
    bad_resp = gw.post(f"{live_server}/rules", json={"mode": "cur"})  # missing name/type/applies_to
    assert bad_resp.status_code == 400
    assert_matches_schema(bad_resp.json(), _response_schema(spec, spec["paths"]["/rules"]["post"]["responses"]["400"]), spec)

    delete_resp = gw.delete(f"{live_server}/rules/{rule['id']}")
    assert delete_resp.status_code == 204
    delete_again = gw.delete(f"{live_server}/rules/{rule['id']}")
    assert delete_again.status_code == 404

    gw.delete(f"{live_server}/webhooks/{webhook['id']}")

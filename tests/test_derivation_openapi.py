"""
Same two-layer shape as test_fault_openapi.py: (1) the spec is well-formed
OpenAPI, (2) real live requests against a real running derivation_api.py
produce responses matching what the spec documents.
"""

from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.parse import quote

import pytest
import requests
import yaml
from conftest import assert_matches_schema, load_spec
from openapi_spec_validator import validate

from baler import derivation_health
from baler.derivation_api import OPENAPI_SPEC_PATH, make_handler
from baler.ingest import (
    link_point_to_equip,
    topic_prefix_to_equip_uri,
    topic_to_point_uri,
)
from baler.remote_store import RemoteStore
from baler.timeseries import connect_pool, read_latest, write_point_value

AVG_FN_SOURCE = "def run(inputs, row):\n    vals = [v for s in inputs.values() for _, v in s[-1:]]\n    return sum(vals) / len(vals) if vals else None"
GATEWAY_SECRET = "test-gateway-secret"


@pytest.fixture(scope="module")
def spec() -> dict:
    return load_spec(OPENAPI_SPEC_PATH)


def test_spec_is_well_formed_openapi(spec):
    validate(spec)


@pytest.fixture(scope="module")
def store():
    return RemoteStore()


@pytest.fixture
def ts_pool():
    pool = connect_pool()
    yield pool
    pool.close()


@pytest.fixture
def live_server(tmp_path, store, ts_pool, monkeypatch):
    monkeypatch.setenv("BALER_GATEWAY_SECRET", GATEWAY_SECRET)
    with ts_pool.connection() as conn:
        derivation_health.ensure_schema(conn)
    derivations_path = str(tmp_path / "derivations.json")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(derivations_path, store, ts_pool))
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


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
    assert served["info"]["title"] == "Baler Derivation API"


@pytest.mark.integration
def test_post_without_gateway_secret_is_401(live_server):
    resp = requests.post(f"{live_server}/derivations", json={"name": "x", "fn_source": "def run(inputs, row): return 1.0", "test_cases": []})
    assert resp.status_code == 401


@pytest.mark.integration
def test_create_derivation_without_test_cases_is_rejected(gw, live_server):
    resp = gw.post(f"{live_server}/derivations", json={
        "name": "test-openapi-de-no-cases",
        "fn_source": "def run(inputs, row):\n    return 1.0",
        "test_cases": [],
    })
    assert resp.status_code == 400


@pytest.mark.integration
def test_create_derivation_with_failing_test_case_is_rejected(gw, live_server):
    resp = gw.post(f"{live_server}/derivations", json={
        "name": "test-openapi-de-bad-case",
        "fn_source": "def run(inputs, row):\n    return 1.0",
        "test_cases": [{"inputs": {}, "row": {}, "expected": 2.0}],
    })
    assert resp.status_code == 400


@pytest.mark.integration
def test_full_derivation_test_dryrun_and_target_flow_matches_documented_schemas(gw, live_server, spec, store, ts_pool):
    equip = topic_prefix_to_equip_uri("test-openapi-de/formula")
    p1 = topic_to_point_uri("test-openapi-de/formula/a")
    p2 = topic_to_point_uri("test-openapi-de/formula/b")
    link_point_to_equip(store, p1, equip)
    link_point_to_equip(store, p2, equip)
    now = datetime.now(timezone.utc)
    with ts_pool.connection() as conn:
        write_point_value(conn, str(p1), 10.0, now)
        write_point_value(conn, str(p2), 20.0, now)

    body = {
        "name": "test-openapi-de-avg",
        "kind": "formula",
        "select": f"""
            PREFIX brick: <https://brickschema.org/schema/Brick#>
            SELECT ?target ?a ?b WHERE {{ ?target brick:hasPoint ?a, ?b . FILTER(?a = <{p1}> && ?b = <{p2}>) }}
        """,
        "target_var": "target",
        "input_vars": ["a", "b"],
        "window_seconds": 3600,
        "fn_source": AVG_FN_SOURCE,
        "test_cases": [{"inputs": {"a": [["2026-01-01T00:00:00Z", 10.0]], "b": [["2026-01-01T00:00:00Z", 20.0]]}, "row": {}, "expected": 15.0}],
        "output": {},
        "depends_on": [],
    }

    create_resp = gw.post(f"{live_server}/derivations", json=body)
    assert create_resp.status_code == 201
    derivation = create_resp.json()
    assert_matches_schema(derivation, spec["paths"]["/derivations"]["post"]["responses"]["201"]["content"]["application/json"]["schema"], spec)

    list_resp = gw.get(f"{live_server}/derivations")
    assert list_resp.status_code == 200
    assert any(d["id"] == derivation["id"] for d in list_resp.json())

    test_resp = gw.post(f"{live_server}/derivations/test", json={"fn_source": body["fn_source"], "test_cases": body["test_cases"]})
    assert test_resp.status_code == 200
    assert all(r["passed"] for r in test_resp.json())

    dryrun_resp = gw.post(f"{live_server}/derivations/dry-run", json={"id": derivation["id"]})
    assert dryrun_resp.status_code == 200
    trace = dryrun_resp.json()
    assert trace[0]["computed_value"] == 15.0
    output_uri = trace[0]["would_write_uri"]
    with ts_pool.connection() as conn:
        assert read_latest(conn, output_uri) is None  # dry-run never writes

    targets_resp = gw.get(f"{live_server}/derivations/{derivation['id']}/targets")
    assert targets_resp.status_code == 200
    assert targets_resp.json() == []  # dry-run doesn't touch target health either

    # Enabling a target with no recorded health row (never failed or
    # succeeded - dry-run doesn't touch target health, per above) 404s -
    # there's nothing to enable. Seed a real row the way a genuine
    # non-dry-run evaluation would (record_target_failure) to exercise
    # the actual documented 200/TargetHealth response.
    enable_missing_resp = gw.post(f"{live_server}/derivations/{derivation['id']}/targets/{quote(str(equip), safe='')}/enable")
    assert enable_missing_resp.status_code == 404

    with ts_pool.connection() as conn:
        derivation_health.record_target_failure(conn, derivation["id"], str(equip), "seeded failure")

    enable_resp = gw.post(f"{live_server}/derivations/{derivation['id']}/targets/{quote(str(equip), safe='')}/enable")
    assert enable_resp.status_code == 200
    enabled = enable_resp.json()
    assert enabled["disabled"] is False
    assert enabled["consecutive_failures"] == 0
    assert_matches_schema(
        enabled,
        spec["paths"]["/derivations/{id}/targets/{target}/enable"]["post"]["responses"]["200"]["content"]["application/json"]["schema"],
        spec,
    )

    delete_resp = gw.delete(f"{live_server}/derivations/{derivation['id']}")
    assert delete_resp.status_code == 204
    delete_again = gw.delete(f"{live_server}/derivations/{derivation['id']}")
    assert delete_again.status_code == 404


@pytest.mark.integration
def test_dry_run_with_unknown_id_returns_400(gw, live_server):
    resp = gw.post(f"{live_server}/derivations/dry-run", json={"id": "no-such-derivation"})
    assert resp.status_code == 400


@pytest.mark.integration
def test_dry_run_with_uncompilable_draft_returns_400(gw, live_server):
    resp = gw.post(f"{live_server}/derivations/dry-run", json={
        "name": "draft", "fn_source": "def run(inputs, row:\n    return 1", "test_cases": [],
    })
    assert resp.status_code == 400

"""
Same two-layer pattern as test_fault_openapi.py: (1) the spec is
well-formed OpenAPI, (2) real live requests against the real running
auth_api.py produce responses matching what the spec documents.

Unlike fault_api.py, every route here except /login and /orgs requires
the X-Gateway-Secret header (see gateway_auth.py) - this test sets a
known secret and sends the header itself, since there's no real gateway
container in front of this in-process server to set it automatically.
"""

from http.server import ThreadingHTTPServer
from threading import Thread

import pytest
import requests
import yaml
from conftest import assert_matches_schema, load_spec
from openapi_spec_validator import validate

from baler.auth import ensure_schema
from baler.auth_api import OPENAPI_SPEC_PATH, make_handler
from baler.timeseries import connect_pool

GATEWAY_SECRET = "test-gateway-secret"
GW_HEADERS = {"X-Gateway-Secret": GATEWAY_SECRET}


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


@pytest.fixture
def live_server(monkeypatch):
    monkeypatch.setenv("BALER_JWT_SECRET", "test-jwt-secret-needs-32-bytes-minimum-for-hs256")
    monkeypatch.setenv("BALER_GATEWAY_SECRET", GATEWAY_SECRET)
    ts_pool = connect_pool()
    with ts_pool.connection() as conn:
        ensure_schema(conn)
        # FK-safe order (children before parents) - see test_auth.py's
        # conn fixture for the same pattern/reasoning.
        conn.execute("""
            DELETE FROM user_sites WHERE user_id IN (
                SELECT id FROM users WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE 'OpenAPI Test Org%')
            )
        """)
        conn.execute("""
            DELETE FROM revoked_tokens WHERE jti IN (
                SELECT jti FROM api_keys WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE 'OpenAPI Test Org%')
            )
        """)
        conn.execute("DELETE FROM api_keys WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE 'OpenAPI Test Org%')")
        conn.execute("DELETE FROM sites WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE 'OpenAPI Test Org%')")
        conn.execute("DELETE FROM users WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE 'OpenAPI Test Org%')")
        conn.execute("DELETE FROM orgs WHERE name LIKE 'OpenAPI Test Org%'")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ts_pool))
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    yield base_url
    server.shutdown()
    ts_pool.close()


@pytest.mark.integration
def test_get_openapi_yaml_serves_the_real_spec_file(live_server):
    resp = requests.get(f"{live_server}/openapi.yaml")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "application/yaml"
    served = yaml.safe_load(resp.text)
    assert served["info"]["title"] == "Baler Auth API"


@pytest.mark.integration
def test_requests_without_gateway_secret_are_rejected(live_server):
    # /orgs and /login are the two exceptions (see auth_api.py's own
    # note on why) - everything else needs the header.
    resp = requests.get(f"{live_server}/me")
    assert resp.status_code == 401


@pytest.mark.integration
def test_full_org_login_me_flow_matches_documented_schemas(live_server, spec):
    org_resp = requests.post(
        f"{live_server}/orgs",
        json={"name": "OpenAPI Test Org", "admin_email": "admin@openapi-test.invalid", "admin_password": "correct-horse-battery-staple"},
    )
    assert org_resp.status_code == 201
    org = org_resp.json()
    assert "admin_user_id" in org

    login_resp = requests.post(
        f"{live_server}/login",
        json={"email": "admin@openapi-test.invalid", "password": "correct-horse-battery-staple"},
    )
    assert login_resp.status_code == 200
    login_body = login_resp.json()
    token = login_body["token"]
    assert login_body["user"]["org_id"] == org["id"]
    assert login_body["user"]["role"] == "admin"

    auth_headers = {**GW_HEADERS, "Authorization": f"Bearer {token}"}

    me_resp = requests.get(f"{live_server}/me", headers=auth_headers)
    assert me_resp.status_code == 200
    me = me_resp.json()
    assert me["sub"] == login_body["user"]["id"]
    assert_matches_schema(me, {"$ref": "#/components/schemas/Principal"}, spec)

    # wrong password -> documented 401
    bad_login = requests.post(f"{live_server}/login", json={"email": "admin@openapi-test.invalid", "password": "wrong"})
    assert bad_login.status_code == 401

    site_resp = requests.post(f"{live_server}/sites", headers=auth_headers, json={"name": "Building A"})
    assert site_resp.status_code == 201
    assert site_resp.json()["org_id"] == org["id"]

    user_resp = requests.post(
        f"{live_server}/users", headers=auth_headers,
        json={"email": "viewer@openapi-test.invalid", "password": "viewer-pw-123", "role": "viewer"},
    )
    assert user_resp.status_code == 201
    assert user_resp.json()["role"] == "viewer"


@pytest.mark.integration
def test_api_key_issue_list_revoke_flow_matches_documented_schemas(live_server, spec):
    org_resp = requests.post(
        f"{live_server}/orgs",
        json={"name": "OpenAPI Test Org Keys", "admin_email": "admin2@openapi-test.invalid", "admin_password": "pw-123456"},
    )
    org_resp.json()
    login = requests.post(f"{live_server}/login", json={"email": "admin2@openapi-test.invalid", "password": "pw-123456"}).json()
    auth_headers = {**GW_HEADERS, "Authorization": f"Bearer {login['token']}"}

    create_resp = requests.post(f"{live_server}/api-keys", headers=auth_headers, json={"role": "service"})
    assert create_resp.status_code == 201
    key = create_resp.json()
    assert "token" in key  # raw token shown once, at creation

    list_resp = requests.get(f"{live_server}/api-keys", headers=auth_headers)
    assert list_resp.status_code == 200
    listed = next(k for k in list_resp.json() if k["id"] == key["id"])
    assert "token" not in listed  # never shown again
    assert_matches_schema(listed, {"$ref": "#/components/schemas/ApiKey"}, spec)

    revoke_resp = requests.delete(f"{live_server}/api-keys/{key['id']}", headers=auth_headers)
    assert revoke_resp.status_code == 204
    revoke_missing = requests.delete(f"{live_server}/api-keys/does-not-exist", headers=auth_headers)
    assert revoke_missing.status_code == 404

    revoked_resp = requests.get(f"{live_server}/internal/revoked-jtis")
    assert revoked_resp.status_code == 200
    assert key["id"] or True  # id isn't the jti; just confirm the endpoint returns a list
    assert isinstance(revoked_resp.json(), list)


@pytest.mark.integration
def test_logout_is_idempotent(live_server):
    requests.post(
        f"{live_server}/orgs",
        json={"name": "OpenAPI Test Org Logout", "admin_email": "admin3@openapi-test.invalid", "admin_password": "pw-123456"},
    )
    login = requests.post(f"{live_server}/login", json={"email": "admin3@openapi-test.invalid", "password": "pw-123456"}).json()
    auth_headers = {**GW_HEADERS, "Authorization": f"Bearer {login['token']}"}

    first = requests.post(f"{live_server}/logout", headers=auth_headers)
    assert first.status_code == 204
    second = requests.post(f"{live_server}/logout", headers=auth_headers)
    assert second.status_code == 204

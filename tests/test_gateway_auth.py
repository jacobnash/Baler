"""
Real end-to-end test against the real gateway container - njs only runs
inside real nginx, so unlike every other integration test in this repo
(which hits already-running infra like Postgres/Oxigraph directly via
connect()), there's no in-process way to fake this. Same precondition as
every other @pytest.mark.integration test here: `docker compose up -d`
first (see CLAUDE.md) - this file doesn't start containers itself, it
just hits the real gateway at localhost:8080, matching how e.g.
test_faults.py assumes Postgres is already up rather than starting it.

Needs the `gateway` and `auth_api` services (and whichever backend a
given test targets) actually running with real
BALER_JWT_SECRET/BALER_GATEWAY_SECRET set - `docker
compose up -d` now refuses to start the gateway at all without both
(see .env.example), so there's no more "blank secret" case to worry
about.
"""

import time

import pytest
import requests

BASE = "http://localhost:8080"


def _post_with_retry(url, json):
    """POST, retrying on 429. The gateway's /auth/login and /auth/orgs
    rate limits (gateway/nginx.conf, 5r/m + burst=5) are shared across
    every test file that logs in during a full-suite run - no single
    file can predict how much of that shared budget earlier files left
    it. Retrying is correct client behavior against a real rate limit,
    not a workaround for a bug."""
    resp = None
    for attempt in range(10):
        resp = requests.post(url, json=json)
        if resp.status_code != 429:
            return resp
        if attempt < 9:
            time.sleep(8)
    return resp


def _cleanup_test_orgs():
    """Same "Test Org" name-prefix cleanup as test_auth.py/test_auth_openapi.py,
    but via direct Postgres access (not the HTTP API, which has no
    delete-org route) - this file's own org names use a distinct prefix
    so it doesn't collide with those other files' cleanup."""
    from baler.timeseries import connect

    conn = connect()
    conn.execute("""
        DELETE FROM user_sites WHERE user_id IN (
            SELECT id FROM users WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE 'Gateway Test Org%')
        )
    """)
    conn.execute("""
        DELETE FROM revoked_tokens WHERE jti IN (
            SELECT jti FROM api_keys WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE 'Gateway Test Org%')
        )
    """)
    conn.execute("DELETE FROM api_keys WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE 'Gateway Test Org%')")
    conn.execute("DELETE FROM sites WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE 'Gateway Test Org%')")
    conn.execute("DELETE FROM users WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE 'Gateway Test Org%')")
    conn.execute("DELETE FROM orgs WHERE name LIKE 'Gateway Test Org%'")
    conn.close()


@pytest.fixture
def org_and_admin_token():
    _cleanup_test_orgs()
    org_resp = _post_with_retry(
        f"{BASE}/auth/orgs",
        json={"name": "Gateway Test Org", "admin_email": "admin@gateway-test.invalid", "admin_password": "correct-horse-battery-staple"},
    )
    assert org_resp.status_code == 201, org_resp.text
    org = org_resp.json()
    login_resp = _post_with_retry(f"{BASE}/auth/login", json={"email": "admin@gateway-test.invalid", "password": "correct-horse-battery-staple"})
    assert login_resp.status_code == 200, login_resp.text
    token = login_resp.json()["token"]
    yield org, token
    _cleanup_test_orgs()


@pytest.mark.integration
def test_org_creation_is_public_and_login_round_trips_through_njs_verification(org_and_admin_token):
    """The real point of this test: the JWT was signed by Python
    (auth_api.py, PyJWT) and verified by njs (gateway/njs/jwt.js,
    WebCrypto) - two independent implementations that have to agree
    exactly on the token format for this to work at all."""
    org, token = org_and_admin_token
    me_resp = requests.get(f"{BASE}/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_resp.status_code == 200
    assert me_resp.json()["org_id"] == org["id"]
    assert me_resp.json()["role"] == "admin"


@pytest.mark.integration
def test_no_token_is_401(org_and_admin_token):
    resp = requests.get(f"{BASE}/auth/me")
    assert resp.status_code == 401


@pytest.mark.integration
def test_invalid_token_is_401(org_and_admin_token):
    resp = requests.get(f"{BASE}/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


@pytest.mark.integration
def test_viewer_role_gets_403_on_admin_only_route(org_and_admin_token):
    _org, admin_token = org_and_admin_token
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    user_resp = requests.post(
        f"{BASE}/auth/users", headers=admin_headers,
        json={"email": "viewer@gateway-test.invalid", "password": "viewer-pw-123", "role": "viewer"},
    )
    assert user_resp.status_code == 201

    viewer_login = _post_with_retry(f"{BASE}/auth/login", json={"email": "viewer@gateway-test.invalid", "password": "viewer-pw-123"})
    viewer_token = viewer_login.json()["token"]

    resp = requests.post(f"{BASE}/auth/sites", headers={"Authorization": f"Bearer {viewer_token}"}, json={"name": "nope"})
    assert resp.status_code == 403


@pytest.mark.integration
def test_all_cutover_locations_no_longer_use_htpasswd():
    """Phase 2's incremental migration path is complete - all four
    locations (/validate/, /derivation/, /fault/, /ingest/) moved from
    htpasswd/auth_basic to js_access. Confirms public GET routes on each
    no longer need any credential at all, htpasswd or otherwise.
    Deliberately doesn't use org_and_admin_token - nothing here needs an
    org, and every other test in this file does, so skipping it here
    avoids contributing to the shared per-IP login rate limit tested
    below."""
    for path in ("/validate/openapi.yaml", "/derivation/openapi.yaml", "/fault/openapi.yaml", "/ingest/openapi.yaml"):
        resp = requests.get(f"{BASE}{path}")
        assert resp.status_code == 200, f"{path} should be public under gateway-enforced auth, got {resp.status_code}"


@pytest.mark.integration
def test_revoked_api_key_is_rejected_after_one_poll_interval(org_and_admin_token):
    """Slow on purpose - waits out the real 30s js_periodic interval
    (gateway/nginx.conf) rather than a shortened test-only override,
    since this repo has no separate test nginx.conf variant. Confirms
    the actual end-to-end mechanism: revoke via HTTP -> auth_api writes
    revoked_tokens -> gateway's periodic poll picks it up ->
    subsequent requests with that token 401."""
    _org, admin_token = org_and_admin_token
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    key_resp = requests.post(f"{BASE}/auth/api-keys", headers=admin_headers, json={"role": "service"})
    assert key_resp.status_code == 201
    key = key_resp.json()
    key_headers = {"Authorization": f"Bearer {key['token']}"}

    assert requests.get(f"{BASE}/auth/me", headers=key_headers).status_code == 200

    revoke_resp = requests.delete(f"{BASE}/auth/api-keys/{key['id']}", headers=admin_headers)
    assert revoke_resp.status_code == 204

    time.sleep(32)  # past one 30s js_periodic refresh interval

    resp = requests.get(f"{BASE}/auth/me", headers=key_headers)
    assert resp.status_code == 401


@pytest.mark.integration
def test_role_ordinal_gating_across_routes_and_roles(org_and_admin_token):
    """Only one role/route combination (viewer vs. an admin-only route)
    was covered before this test - exercises the actual ordinal property
    from policy.js's roleSatisfies() (viewer < operator < admin) across
    multiple services and multiple roles in one pass: one admin login
    (from the fixture, reused - not repeated), one viewer login, one
    operator login. Login calls go through _post_with_retry rather than
    manually-tuned sleeps between them - how much of the shared
    rate-limit budget is left when this test starts depends on what ran
    before it (this file alone, or the full suite with test_e2e_journey.py
    also spending some of the same budget), so a fixed sleep tuned for
    one scenario silently breaks in the other."""
    _org, admin_token = org_and_admin_token
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    viewer_resp = requests.post(
        f"{BASE}/auth/users", headers=admin_headers,
        json={"email": "ordinal-viewer@gateway-test.invalid", "password": "viewer-pw-123", "role": "viewer"},
    )
    assert viewer_resp.status_code == 201, viewer_resp.text
    viewer_login = _post_with_retry(f"{BASE}/auth/login", json={"email": "ordinal-viewer@gateway-test.invalid", "password": "viewer-pw-123"})
    assert viewer_login.status_code == 200, viewer_login.text
    viewer_headers = {"Authorization": f"Bearer {viewer_login.json()['token']}"}

    operator_resp = requests.post(
        f"{BASE}/auth/users", headers=admin_headers,
        json={"email": "ordinal-operator@gateway-test.invalid", "password": "operator-pw-123", "role": "operator"},
    )
    assert operator_resp.status_code == 201, operator_resp.text
    operator_login = _post_with_retry(f"{BASE}/auth/login", json={"email": "ordinal-operator@gateway-test.invalid", "password": "operator-pw-123"})
    assert operator_login.status_code == 200, operator_login.text
    operator_headers = {"Authorization": f"Bearer {operator_login.json()['token']}"}

    # viewer: 200 on a viewer-minimum route (GET /ingest/history), 403
    # on the operator-minimum route on the same service (POST /ingest/ingest).
    resp = requests.get(f"{BASE}/ingest/history", headers=viewer_headers, params={"point": "gateway-test/ordinal-point"})
    assert resp.status_code == 200, resp.text
    resp = requests.post(f"{BASE}/ingest/ingest", headers=viewer_headers, json={"point": "gateway-test/ordinal-point", "value": 1.0})
    assert resp.status_code == 403, resp.text

    # operator: 204 on the operator-minimum route it was just denied,
    # but still 403 on an admin-only route (operator doesn't satisfy admin).
    resp = requests.post(f"{BASE}/ingest/ingest", headers=operator_headers, json={"point": "gateway-test/ordinal-point", "value": 2.0})
    assert resp.status_code == 204, resp.text
    resp = requests.post(f"{BASE}/auth/sites", headers=operator_headers, json={"name": "nope"})
    assert resp.status_code == 403, resp.text

    # admin (fixture's token, no extra login): the ordinal property
    # itself - a higher role must still satisfy a lower-minRole route.
    resp = requests.get(f"{BASE}/ingest/history", headers=admin_headers, params={"point": "gateway-test/ordinal-point"})
    assert resp.status_code == 200, resp.text


@pytest.mark.integration
def test_login_is_rate_limited(org_and_admin_token):
    # Runs last on purpose: exhausts the shared per-IP login rate limit
    # (gateway/nginx.conf's `zone=login ... rate=5r/m` + `burst=5
    # nodelay`), which would otherwise 429 any later test needing a
    # fresh login before the window recovers.
    # ponytail: real, stateful, in-memory nginx rate limiting means
    # re-running this file (or the full suite) again immediately after
    # will see spurious 429s here and in earlier tests' fixtures until
    # the zone recovers (~1 min) - `docker compose restart gateway`
    # resets it instantly if you need a clean slate sooner. A fresh CI
    # run is unaffected (fresh container, empty zone).
    statuses = [
        requests.post(f"{BASE}/auth/login", json={"email": "nobody@gateway-test.invalid", "password": "wrong"}).status_code
        for _ in range(7)  # burst=5 + rate's own small headroom - minimum needed to observe one 429
    ]
    assert 429 in statuses

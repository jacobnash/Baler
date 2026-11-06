"""
Live against the real running Postgres, same shape as test_faults.py.
Each test creates its own org/user/key with random ids (json_store.new_id()),
so there's no cross-test collision to guard against with prefix cleanup -
unlike faults.py's meaningful test-chosen rule_id/webhook_id strings.
"""

import time

import pytest

from baler.auth import (
    create_api_key,
    create_org,
    create_site,
    create_user,
    decode_own_token,
    ensure_schema,
    get_user_by_email,
    hash_password,
    issue_api_key_token,
    issue_session_token,
    list_api_keys,
    list_revoked_jtis,
    revoke_api_key,
    user_site_ids,
    verify_password,
)
from baler.timeseries import connect


@pytest.fixture
def conn():
    c = connect()
    ensure_schema(c)
    # Every test in this file uses an org name starting with "Test Org" -
    # clean up in FK-safe order (children before parents) so re-running
    # the suite doesn't collide with the unique email/jti constraints
    # left over from a previous run.
    c.execute("""
        DELETE FROM user_sites WHERE user_id IN (
            SELECT id FROM users WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE 'Test Org%')
        )
    """)
    c.execute("""
        DELETE FROM revoked_tokens WHERE jti IN (
            SELECT jti FROM api_keys WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE 'Test Org%')
        )
    """)
    c.execute("DELETE FROM api_keys WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE 'Test Org%')")
    c.execute("DELETE FROM sites WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE 'Test Org%')")
    c.execute("DELETE FROM users WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE 'Test Org%')")
    c.execute("DELETE FROM orgs WHERE name LIKE 'Test Org%'")
    return c


@pytest.fixture(autouse=True)
def jwt_secret(monkeypatch):
    # Real HMAC secrets have no sane default (unlike e.g. a localhost DB
    # DSN) - every test needs one set explicitly.
    monkeypatch.setenv("BALER_JWT_SECRET", "test-secret-do-not-use-in-prod-need-32-bytes-min")


def test_hash_password_roundtrip():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed) is True
    assert verify_password("wrong password", hashed) is False


def test_hash_password_uses_a_fresh_salt_each_time():
    a = hash_password("same password")
    b = hash_password("same password")
    assert a != b  # different random salts -> different stored hashes
    assert verify_password("same password", a) is True
    assert verify_password("same password", b) is True


def test_verify_password_rejects_malformed_stored_hash():
    assert verify_password("anything", "not-a-valid-hash-format") is False


@pytest.mark.integration
def test_create_org_creates_org_and_first_admin_user(conn):
    org = create_org(conn, "Test Org", "admin@test-auth.invalid", "admin-password-123")
    assert org["name"] == "Test Org"

    user = get_user_by_email(conn, "admin@test-auth.invalid")
    assert user is not None
    assert user["org_id"] == org["id"]
    assert user["role"] == "admin"
    assert verify_password("admin-password-123", user["password_hash"]) is True


@pytest.mark.integration
def test_create_user_adds_a_user_to_an_existing_org(conn):
    org = create_org(conn, "Test Org 2", "admin2@test-auth.invalid", "pw")
    viewer = create_user(conn, org["id"], "viewer@test-auth.invalid", "viewer-pw", "viewer")
    assert viewer["org_id"] == org["id"]
    assert viewer["role"] == "viewer"

    fetched = get_user_by_email(conn, "viewer@test-auth.invalid")
    assert fetched["id"] == viewer["id"]


@pytest.mark.integration
def test_user_site_ids_empty_by_default(conn):
    create_org(conn, "Test Org 3", "admin3@test-auth.invalid", "pw")
    user = get_user_by_email(conn, "admin3@test-auth.invalid")
    assert user_site_ids(conn, user["id"]) == []


@pytest.mark.integration
def test_create_site_under_an_org(conn):
    org = create_org(conn, "Test Org 4", "admin4@test-auth.invalid", "pw")
    site = create_site(conn, org["id"], "Building A")
    assert site["org_id"] == org["id"]
    assert site["name"] == "Building A"


@pytest.mark.integration
def test_issue_session_token_roundtrips_through_decode_own_token(conn):
    org = create_org(conn, "Test Org 5", "admin5@test-auth.invalid", "pw")
    user = get_user_by_email(conn, "admin5@test-auth.invalid")

    token = issue_session_token(user, site_ids=["site-1"])
    claims = decode_own_token(f"Bearer {token}")

    assert claims["sub"] == user["id"]
    assert claims["org_id"] == org["id"]
    assert claims["role"] == "admin"
    assert claims["site_ids"] == ["site-1"]
    assert claims["kind"] == "session"
    assert "exp" in claims  # session tokens are short-TTL, unlike api_key tokens


def test_decode_own_token_rejects_missing_or_malformed_header():
    assert decode_own_token(None) is None
    assert decode_own_token("not-a-bearer-header") is None
    assert decode_own_token("Bearer not.a.validjwt") is None


@pytest.mark.integration
def test_create_api_key_and_issue_token_roundtrip(conn):
    org = create_org(conn, "Test Org 6", "admin6@test-auth.invalid", "pw")
    key = create_api_key(conn, org["id"], "service", site_ids=["site-a", "site-b"])
    assert key["role"] == "service"
    assert key["expires_at"] is None  # no expires_in_seconds passed -> no expiry

    token = issue_api_key_token(key)
    claims = decode_own_token(f"Bearer {token}")
    assert claims["sub"] == key["id"]
    assert claims["kind"] == "api_key"
    assert claims["jti"] == key["jti"]
    assert "exp" not in claims  # no expiry requested


@pytest.mark.integration
def test_create_api_key_with_expiry_sets_exp_claim(conn):
    org = create_org(conn, "Test Org 7", "admin7@test-auth.invalid", "pw")
    key = create_api_key(conn, org["id"], "service", expires_in_seconds=3600)
    assert key["expires_at"] is not None

    token = issue_api_key_token(key)
    claims = decode_own_token(f"Bearer {token}")
    assert claims["exp"] > time.time()


@pytest.mark.integration
def test_list_api_keys_scoped_to_org(conn):
    org_a = create_org(conn, "Test Org 8a", "admin8a@test-auth.invalid", "pw")
    org_b = create_org(conn, "Test Org 8b", "admin8b@test-auth.invalid", "pw")
    create_api_key(conn, org_a["id"], "service")
    create_api_key(conn, org_b["id"], "service")

    keys_a = list_api_keys(conn, org_a["id"])
    assert len(keys_a) == 1
    assert keys_a[0]["org_id"] == org_a["id"]


@pytest.mark.integration
def test_revoke_api_key_marks_revoked_and_denylists_jti(conn):
    org = create_org(conn, "Test Org 9", "admin9@test-auth.invalid", "pw")
    key = create_api_key(conn, org["id"], "service")

    assert revoke_api_key(conn, key["id"]) is True
    assert key["jti"] in list_revoked_jtis(conn)

    keys = list_api_keys(conn, org["id"])
    assert keys[0]["revoked_at"] is not None


@pytest.mark.integration
def test_revoke_api_key_returns_false_for_unknown_id(conn):
    assert revoke_api_key(conn, "does-not-exist") is False


@pytest.mark.integration
def test_list_users_is_org_scoped_and_has_no_hashes(conn):
    from baler.auth import list_users

    org = create_org(conn, "Test Org Users", "admin-u@test-auth.invalid", "pw")
    create_user(conn, org["id"], "op-u@test-auth.invalid", "pw", "operator")
    other = create_org(conn, "Test Org Users B", "admin-ub@test-auth.invalid", "pw")
    users = list_users(conn, org["id"])
    assert [u["email"] for u in users] == ["admin-u@test-auth.invalid", "op-u@test-auth.invalid"]
    assert all("password_hash" not in u for u in users)
    assert [u["email"] for u in list_users(conn, other["id"])] == ["admin-ub@test-auth.invalid"]


@pytest.mark.integration
def test_delete_user_only_in_own_org_and_ends_sessions(conn):
    from baler.auth import delete_user

    org = create_org(conn, "Test Org Del", "admin-d@test-auth.invalid", "pw")
    other = create_org(conn, "Test Org Del B", "admin-db@test-auth.invalid", "pw")
    victim = create_user(conn, org["id"], "gone@test-auth.invalid", "pw", "viewer")
    assert delete_user(conn, other["id"], victim["id"]) is False  # not their org
    assert get_user_by_email(conn, "gone@test-auth.invalid") is not None
    assert delete_user(conn, org["id"], victim["id"]) is True
    assert get_user_by_email(conn, "gone@test-auth.invalid") is None
    cutoffs = conn.execute("SELECT jti FROM revoked_tokens WHERE jti LIKE %s", (f"user:{victim['id']}:%",)).fetchall()
    assert len(cutoffs) == 1
    conn.execute("DELETE FROM revoked_tokens WHERE jti LIKE 'user:%'")


@pytest.mark.integration
def test_set_password_changes_login_and_ends_sessions(conn):
    from baler.auth import set_password

    org = create_org(conn, "Test Org Pw", "admin-p@test-auth.invalid", "old-pw")
    user = get_user_by_email(conn, "admin-p@test-auth.invalid")
    assert set_password(conn, org["id"], "no-such-user", "x") is False
    assert set_password(conn, org["id"], user["id"], "new-pw") is True
    stored = get_user_by_email(conn, "admin-p@test-auth.invalid")["password_hash"]
    assert verify_password("new-pw", stored) and not verify_password("old-pw", stored)
    assert conn.execute("SELECT 1 FROM revoked_tokens WHERE jti LIKE %s", (f"user:{user['id']}:%",)).fetchone()
    conn.execute("DELETE FROM revoked_tokens WHERE jti LIKE 'user:%'")


@pytest.mark.integration
def test_create_user_rules(conn):
    from baler.auth import EmailTaken

    org = create_org(conn, "Test Org Rules", "Admin-R@Test-Auth.invalid ", "pw")
    assert get_user_by_email(conn, "admin-r@test-auth.invalid")["id"] == org["admin_user_id"]  # stored normalised
    assert get_user_by_email(conn, " ADMIN-R@test-auth.INVALID")["id"] == org["admin_user_id"]  # login normalises too
    with pytest.raises(ValueError):
        create_user(conn, org["id"], "svc@test-auth.invalid", "pw", "service")  # API-key-only role
    with pytest.raises(EmailTaken) as exc:
        create_user(conn, org["id"], "ADMIN-R@test-auth.invalid", "pw", "viewer")
    assert "org" not in str(exc.value)  # says nothing about where it's taken


@pytest.mark.integration
def test_update_user_role_sites_and_last_admin_guard(conn):
    from baler.auth import LastAdmin, delete_user, update_user

    org = create_org(conn, "Test Org Upd", "admin-up@test-auth.invalid", "pw")
    site = create_site(conn, org["id"], "Building U")
    other = create_org(conn, "Test Org Upd B", "admin-upb@test-auth.invalid", "pw")
    other_site = create_site(conn, other["id"], "Elsewhere")
    user = create_user(conn, org["id"], "op-up@test-auth.invalid", "pw", "operator")

    assert update_user(conn, org["id"], user["id"], role="viewer", site_ids=[site["id"]]) == {
        "id": user["id"], "role": "viewer", "site_ids": [site["id"]]}
    assert user_site_ids(conn, user["id"]) == [site["id"]]
    with pytest.raises(ValueError):
        update_user(conn, org["id"], user["id"], site_ids=[other_site["id"]])  # not this org's site
    assert update_user(conn, other["id"], user["id"], role="admin") is None  # not this org's user
    with pytest.raises(LastAdmin):
        update_user(conn, org["id"], org["admin_user_id"], role="viewer")
    with pytest.raises(LastAdmin):
        delete_user(conn, org["id"], org["admin_user_id"])
    update_user(conn, org["id"], user["id"], role="admin")
    assert delete_user(conn, org["id"], org["admin_user_id"]) is True  # another admin remains
    conn.execute("DELETE FROM user_sites WHERE user_id = %s", (user["id"],))
    conn.execute("DELETE FROM revoked_tokens WHERE jti LIKE 'user:%'")


@pytest.mark.integration
def test_disable_blocks_login_and_guards_last_admin(conn):
    from baler.auth import LastAdmin, list_users, update_user

    org = create_org(conn, "Test Org Dis", "admin-dis@test-auth.invalid", "pw")
    user = create_user(conn, org["id"], "off@test-auth.invalid", "pw", "viewer")
    assert update_user(conn, org["id"], user["id"], disabled=True)["disabled"] is True
    assert get_user_by_email(conn, "off@test-auth.invalid") is None  # login sees no such user
    assert {u["email"]: u["disabled"] for u in list_users(conn, org["id"])}["off@test-auth.invalid"] is True
    assert update_user(conn, org["id"], user["id"], disabled=False)["disabled"] is False
    assert get_user_by_email(conn, "off@test-auth.invalid") is not None
    with pytest.raises(LastAdmin):
        update_user(conn, org["id"], org["admin_user_id"], disabled=True)
    conn.execute("DELETE FROM revoked_tokens WHERE jti LIKE 'user:%'")

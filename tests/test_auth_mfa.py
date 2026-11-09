"""Password reset and MFA against Postgres (#278). Needs docker compose's
postgres (migration 0003 applied by the migrate service)."""

from datetime import datetime, timedelta, timezone

import pytest

from baler import auth, mfa, password_reset, totp
from baler.timeseries import connect

pytestmark = pytest.mark.integration

ORG = "Test Org MFA"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("BALER_JWT_SECRET", "test-secret-do-not-use-in-prod-need-32-bytes-min")
    monkeypatch.setattr(auth, "_PBKDF2_ITERATIONS", 1000)  # speed; same code path


@pytest.fixture
def conn():
    c = connect()
    auth.ensure_schema(c)

    def clean():
        c.execute(f"DELETE FROM auth_events WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE '{ORG}%')")
        c.execute(f"DELETE FROM user_sites WHERE user_id IN (SELECT u.id FROM users u JOIN orgs o ON o.id = u.org_id "
                  f"WHERE o.name LIKE '{ORG}%')")
        c.execute(f"DELETE FROM users WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE '{ORG}%')")
        c.execute(f"DELETE FROM orgs WHERE name LIKE '{ORG}%'")

    clean()
    yield c
    clean()
    c.close()


@pytest.fixture
def user(conn):
    org = auth.create_org(conn, f"{ORG} {datetime.now(timezone.utc).timestamp()}", f"mfa-{datetime.now(timezone.utc).timestamp()}@example.org",
                          "correct horse battery")
    return auth.get_user(conn, org["admin_user_id"])


def events(conn, user_id):
    return [r[0] for r in conn.execute("SELECT event FROM auth_events WHERE user_id = %s ORDER BY id", (user_id,))]


def enrol(conn, user):
    secret = mfa.start_enrolment(conn, user)["secret"]
    codes = mfa.confirm_enrolment(conn, user, totp.code_at(secret))
    # the enrolment code's step is used; later codes come from the next steps
    conn.execute("UPDATE users SET totp_last_step = totp_last_step - 5 WHERE id = %s", (user["id"],))
    return secret, codes


# --- password reset -------------------------------------------------------------


@pytest.fixture
def mail(monkeypatch):
    monkeypatch.setenv("BALER_SMTP_HOST", "smtp.example.org")
    monkeypatch.setenv("BALER_SMTP_FROM", "baler@example.org")
    monkeypatch.setenv("BALER_PUBLIC_URL", "https://baler.example.org")
    sent = []
    return sent, (lambda to, link, ttl: sent.append((to, link.split("#token=")[1])))


def test_reset_works_once_and_answers_alike(conn, user, mail):
    sent, send = mail
    known = password_reset.request(conn, user["email"], send=send, background=False)
    unknown = password_reset.request(conn, "nobody-here@example.org", send=send, background=False)
    assert known == unknown == password_reset.ACCEPTED
    assert len(sent) == 1 and sent[0][0] == user["email"]
    token = sent[0][1]
    stored = conn.execute("SELECT token_hash FROM password_resets WHERE user_id = %s", (user["id"],)).fetchone()[0]
    assert stored != token and stored == password_reset.hash_token(token)  # only the hash is stored

    before = datetime.now(timezone.utc).timestamp()
    password_reset.confirm(conn, token, "a brand new password")
    assert auth.verify_password("a brand new password", auth.get_user(conn, user["id"])["password_hash"])
    # sessions ended: a cutoff row for this user at or after the reset
    cut = conn.execute("SELECT jti FROM revoked_tokens WHERE jti LIKE %s", (f"user:{user['id']}:%",)).fetchall()
    assert any(float(j.rsplit(":", 1)[1]) >= before - 1 for (j,) in cut)
    with pytest.raises(password_reset.ResetError):
        password_reset.confirm(conn, token, "another new password")  # reused
    assert events(conn, user["id"]) == ["password_reset_requested", "password_reset_completed"]


def test_reset_expired_and_replaced_tokens_fail(conn, user, mail):
    sent, send = mail
    password_reset.request(conn, user["email"], send=send, background=False)
    password_reset.request(conn, user["email"], send=send, background=False)
    first, second = sent[0][1], sent[1][1]
    with pytest.raises(password_reset.ResetError):
        password_reset.confirm(conn, first, "a brand new password")  # replaced by the second request
    conn.execute("UPDATE password_resets SET expires_at = now() - interval '1 second' WHERE token_hash = %s",
                 (password_reset.hash_token(second),))
    with pytest.raises(password_reset.ResetError):
        password_reset.confirm(conn, second, "a brand new password")  # expired


def test_reset_is_rate_limited_per_account(conn, user, mail):
    sent, send = mail
    for _ in range(password_reset.MAX_PER_HOUR + 2):
        assert password_reset.request(conn, user["email"], send=send, background=False) == password_reset.ACCEPTED
    assert len(sent) == password_reset.MAX_PER_HOUR
    assert "password_reset_rate_limited" in events(conn, user["id"])


# --- MFA ------------------------------------------------------------------------


def test_mfa_login_needs_a_code_and_codes_work_once(conn, user):
    assert mfa.login_step(conn, user["id"]) == "session"
    secret, codes = enrol(conn, user)
    assert len(codes) == mfa.RECOVERY_CODES and mfa.login_step(conn, user["id"]) == "mfa"
    code = totp.code_at(secret)
    assert mfa.verify(conn, user, code) == "totp"
    with pytest.raises(mfa.MFAError):
        mfa.verify(conn, user, code)  # replay
    assert mfa.verify(conn, user, codes[0]) == "recovery"
    with pytest.raises(mfa.MFAError):
        mfa.verify(conn, user, codes[0])  # each recovery code once
    assert mfa.state(conn, user["id"])["recovery_codes_left"] == mfa.RECOVERY_CODES - 1
    stored = [r[0] for r in conn.execute("SELECT code_hash FROM user_recovery_codes WHERE user_id = %s", (user["id"],))]
    assert codes[1] not in stored  # hashed
    assert events(conn, user["id"]) == ["mfa_enabled", "mfa_recovery_code_used"]


def test_mfa_locks_after_repeated_wrong_codes(conn, user):
    secret, _ = enrol(conn, user)
    for _ in range(mfa.MAX_FAILURES - 1):
        with pytest.raises(mfa.MFAError):
            mfa.verify(conn, user, "000000")
    with pytest.raises(mfa.Locked):
        mfa.verify(conn, user, "000000")
    with pytest.raises(mfa.Locked):
        mfa.verify(conn, user, totp.code_at(secret))  # even a right code, until the lock ends
    conn.execute("UPDATE users SET mfa_locked_until = %s WHERE id = %s",
                 (datetime.now(timezone.utc) - timedelta(seconds=1), user["id"]))
    assert mfa.verify(conn, user, totp.code_at(secret)) == "totp"


def test_disable_and_admin_reset(conn, user):
    secret, _ = enrol(conn, user)
    mfa.disable(conn, user, totp.code_at(secret))
    assert mfa.login_step(conn, user["id"]) == "session"
    enrol(conn, user)
    assert mfa.admin_reset(conn, user["org_id"], user["id"], actor_id=user["id"])
    assert not mfa.state(conn, user["id"])["enabled"]
    assert events(conn, user["id"])[-1] == "mfa_reset_by_admin"


def test_org_requirement(conn, user):
    other = auth.create_user(conn, user["org_id"], f"op-{datetime.now(timezone.utc).timestamp()}@example.org",
                             "correct horse battery", "operator")
    with pytest.raises(mfa.MFAError, match="your own account"):
        mfa.set_org_required(conn, user["org_id"], True, {"id": user["id"]})
    secret, _ = enrol(conn, user)
    org = mfa.set_org_required(conn, user["org_id"], True, {"id": user["id"]})
    assert org["mfa_required"] is True and org["users_with_mfa"] == 1
    assert mfa.login_step(conn, other["id"]) == "enroll"  # sent through enrolment
    assert mfa.login_step(conn, user["id"]) == "mfa"
    with pytest.raises(mfa.MFAError, match="requires"):
        mfa.disable(conn, user, totp.code_at(secret))
    mfa.set_org_required(conn, user["org_id"], False, {"id": user["id"]})
    assert mfa.login_step(conn, other["id"]) == "session"


def test_api_keys_unaffected_by_org_requirement(conn, user):
    enrol(conn, user)
    mfa.set_org_required(conn, user["org_id"], True, {"id": user["id"]})
    key = auth.create_api_key(conn, user["org_id"], "service")
    claims = auth.decode_own_token("Bearer " + auth.issue_api_key_token(key))
    assert claims["kind"] == "api_key" and "mfa_enroll" not in claims

"""MFA and password reset without a database (#278): the pending-login
token, secret sealing, session claims, reset configuration, and auth_api's
login branching over HTTP with the database calls faked. The database
flows themselves are in test_auth_mfa.py (integration)."""

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import jwt as pyjwt
import pytest

from baler import auth, auth_api, mfa, password_reset

SECRET = "test-secret-do-not-use-in-prod-need-32-bytes-min"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("BALER_JWT_SECRET", SECRET)
    monkeypatch.setenv("BALER_GATEWAY_SECRET", "gw")
    monkeypatch.delenv("BALER_MFA_KEY", raising=False)


# --- tokens and claims ---------------------------------------------------------


def test_mfa_token_is_never_a_valid_bearer_token():
    """Signed with a key derived from BALER_JWT_SECRET, not with it: the
    gateway (and decode_own_token) refuse it outright."""
    token = mfa.issue_mfa_token("u1")
    assert mfa.read_mfa_token(token) == "u1"
    assert auth.decode_own_token(f"Bearer {token}") is None
    with pytest.raises(pyjwt.InvalidSignatureError):
        pyjwt.decode(token, SECRET, algorithms=["HS256"])


def test_mfa_token_rejects_session_tokens_and_garbage():
    session = auth.issue_session_token({"id": "u1", "org_id": "o1", "role": "admin"}, [])
    for bad in (session, "x.y.z", ""):
        with pytest.raises(mfa.MFAError):
            mfa.read_mfa_token(bad)


def test_session_claims_for_each_login_outcome():
    user = {"id": "u1", "org_id": "o1", "role": "operator"}
    plain = auth.decode_own_token("Bearer " + auth.issue_session_token(user, ["s1"]))
    assert "amr" not in plain and "mfa_enroll" not in plain  # unchanged for everyone without MFA
    otp = auth.decode_own_token("Bearer " + auth.issue_session_token(user, [], mfa="otp"))
    assert otp["amr"] == ["pwd", "otp"]
    enroll = auth.decode_own_token("Bearer " + auth.issue_session_token(user, [], mfa="enroll"))
    assert enroll["mfa_enroll"] is True and enroll["exp"] - enroll["iat"] <= auth.ENROLL_TTL_SECONDS + 1
    with pytest.raises(ValueError):
        auth.issue_session_token(user, [], mfa="sms")


def test_seal_unseal(monkeypatch):
    assert mfa.unseal(mfa.seal("JBSWY3DPEHPK3PXP")) == "JBSWY3DPEHPK3PXP"  # no key: as is
    monkeypatch.setenv("BALER_MFA_KEY", "k1" * 20)
    sealed = mfa.seal("JBSWY3DPEHPK3PXP")
    assert sealed.startswith("fernet:") and "JBSWY3DP" not in sealed
    assert mfa.unseal(sealed) == "JBSWY3DPEHPK3PXP"
    monkeypatch.setenv("BALER_MFA_KEY", "k2" * 20)
    with pytest.raises(mfa.MFAError, match="changed"):
        mfa.unseal(sealed)
    monkeypatch.delenv("BALER_MFA_KEY")
    with pytest.raises(mfa.MFAError, match="not set"):
        mfa.unseal(sealed)


def test_short_mfa_key_is_refused(monkeypatch):
    """HKDF doesn't stretch a password: a short BALER_MFA_KEY is refused, not used."""
    monkeypatch.delenv("BALER_MFA_KEY", raising=False)
    assert mfa.key_problem() is None
    monkeypatch.setenv("BALER_MFA_KEY", "x" * (mfa.MFA_KEY_MIN_CHARS - 1))
    assert "at least 32" in mfa.key_problem()
    with pytest.raises(mfa.MFAError, match="at least 32"):
        mfa.seal("JBSWY3DPEHPK3PXP")
    with pytest.raises(mfa.MFAError, match="at least 32"):
        mfa.unseal("fernet:anything")
    monkeypatch.setenv("BALER_MFA_KEY", "x" * mfa.MFA_KEY_MIN_CHARS)
    assert mfa.key_problem() is None


# --- password reset configuration -------------------------------------------------


def test_reset_off_without_email_answers_everyone_alike(monkeypatch):
    monkeypatch.delenv("BALER_SMTP_HOST", raising=False)
    monkeypatch.setenv("BALER_PUBLIC_URL", "https://baler.example.org")
    assert not password_reset.enabled()
    for email in ("known@example.org", "unknown@example.org"):
        with pytest.raises(password_reset.ResetError, match="ask an admin"):
            password_reset.request(None, email)


def test_reset_ttl_is_clamped_to_an_hour(monkeypatch):
    monkeypatch.setenv("BALER_PASSWORD_RESET_TTL_SECONDS", "86400")
    assert password_reset.ttl_seconds() == 3600
    monkeypatch.setenv("BALER_PASSWORD_RESET_TTL_SECONDS", "")
    assert password_reset.ttl_seconds() == 1800


def test_reset_link_keeps_the_token_in_the_fragment(monkeypatch):
    monkeypatch.setenv("BALER_PUBLIC_URL", "https://baler.example.org/")
    assert password_reset.reset_link("abc") == "https://baler.example.org/ui/reset.html#token=abc"
    assert password_reset.hash_token("abc") != "abc" and len(password_reset.hash_token("abc")) == 64


# --- auth_api over HTTP, database faked ----------------------------------------------


class FakePool:
    @contextmanager
    def connection(self):
        yield object()


@pytest.fixture
def api(monkeypatch):
    user = {"id": "u1", "org_id": "o1", "email": "ana@example.org", "role": "operator",
            "password_hash": "x"}
    steps = {"u1": "session"}
    monkeypatch.setattr(auth, "get_user_by_email", lambda conn, e: dict(user) if e == user["email"] else None)
    monkeypatch.setattr(auth, "get_user", lambda conn, uid: dict(user) if uid == "u1" else None)
    monkeypatch.setattr(auth, "verify_password", lambda pw, h: pw == "right-password")
    monkeypatch.setattr(auth, "user_site_ids", lambda conn, uid: [])
    monkeypatch.setattr(mfa, "login_step", lambda conn, uid: steps[uid])
    monkeypatch.setattr(mfa, "state", lambda conn, uid: {"enabled": steps[uid] == "mfa", "required": False,
                                                         "pending": False, "recovery_codes_left": 9})
    verified = []

    def verify(conn, u, code, ip=None, allow_recovery=True):
        if code == "000000":
            raise mfa.MFAError("that code isn't right")
        if code == "locked":
            raise mfa.Locked("too many wrong codes")
        verified.append(code)
        return "totp"

    monkeypatch.setattr(mfa, "verify", verify)
    monkeypatch.setattr(auth, "get_display_name", lambda conn, uid: None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), auth_api.make_handler(FakePool(), broker=object()))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(method, path, body=None, token=None):
        req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "X-Gateway-Secret": "gw",
                                              **({"Authorization": f"Bearer {token}"} if token else {})})
        try:
            with urllib.request.urlopen(req) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, json.loads(raw) if raw else None

    yield call, steps, verified
    server.shutdown()


def test_login_without_mfa_is_unchanged(api):
    call, _steps, _ = api
    status, body = call("POST", "/login", {"email": "ana@example.org", "password": "right-password"})
    assert status == 200 and set(body) == {"token", "user"}
    assert call("POST", "/login", {"email": "ana@example.org", "password": "wrong"})[0] == 401
    assert call("POST", "/login", {"email": "nobody@example.org", "password": "right-password"})[0] == 401


def test_login_with_mfa_needs_the_second_step(api):
    call, steps, verified = api
    steps["u1"] = "mfa"
    status, body = call("POST", "/login", {"email": "ana@example.org", "password": "right-password"})
    assert status == 200 and body["mfa_required"] is True and "token" not in body
    # the pending token is not a bearer token
    assert call("GET", "/me", token=body["mfa_token"])[0] == 401
    assert call("POST", "/login/mfa", {"mfa_token": body["mfa_token"], "code": "000000"})[0] == 401
    assert call("POST", "/login/mfa", {"mfa_token": body["mfa_token"], "code": "locked"})[0] == 429
    assert call("POST", "/login/mfa", {"mfa_token": "forged", "code": "123456"})[0] == 401
    status, done = call("POST", "/login/mfa", {"mfa_token": body["mfa_token"], "code": "123456"})
    assert status == 200 and verified == ["123456"]
    assert auth.decode_own_token("Bearer " + done["token"])["amr"] == ["pwd", "otp"]


def test_org_requirement_sends_users_through_enrolment(api):
    call, steps, _ = api
    steps["u1"] = "enroll"
    status, body = call("POST", "/login", {"email": "ana@example.org", "password": "right-password"})
    assert status == 200 and body["mfa_enrollment_required"] is True
    token = body["token"]
    status, me = call("GET", "/me", token=token)
    assert status == 200 and me["mfa_enrollment_required"] is True
    # anything but enrolment is refused by auth_api too (and by the gateway)
    assert call("GET", "/users", token=token)[0] == 403
    assert call("POST", "/me/password", {"current_password": "a", "new_password": "b" * 12}, token=token)[0] == 403


def test_api_keys_are_untouched(api):
    call, steps, _ = api
    steps["u1"] = "enroll"  # irrelevant to a key
    key = {"id": "k1", "org_id": "o1", "role": "service", "site_ids": [], "jti": "j1", "expires_at": None}
    status, me = call("GET", "/me", token=auth.issue_api_key_token(key))
    assert status == 200 and me["kind"] == "api_key" and me["mfa_enrollment_required"] is False


def test_reset_config_is_public(api, monkeypatch):
    call, _, _ = api
    monkeypatch.delenv("BALER_SMTP_HOST", raising=False)
    assert call("GET", "/password-reset/config") == (200, {"self_service": False})
    status, body = call("POST", "/password-reset", {"email": "ana@example.org"})
    assert status == 503 and "ask an admin" in body["error"]

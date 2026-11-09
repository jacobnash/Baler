"""
Multi-factor login (#278): TOTP enrolment, verification with a replay guard
and a lockout, one-time recovery codes, the org-wide requirement, and the
audit trail. auth_api.py is the HTTP layer; the gateway (gateway/njs/)
keeps an enrolment-only session away from everything but enrolment.

How login works with MFA:

1. POST /auth/login with the password. A user without MFA, in an org that
   doesn't require it, gets a session token exactly as before.
2. A user with MFA gets no session, only an `mfa_token`: a 5-minute JWT
   signed with a key derived from BALER_JWT_SECRET but different from it,
   so the gateway can never accept it as a bearer token. POST
   /auth/login/mfa with it and a TOTP or recovery code returns the session.
3. A user without MFA in an org that requires it gets a session token
   carrying `mfa_enroll: true`. The gateway lets it reach only the
   enrolment routes (policy.MFA_ENROLL_ROUTES); confirming enrolment
   returns a normal session.

API keys and edge (broker) credentials never go through any of this.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import psycopg

from baler import totp

MFA_TOKEN_TTL_SECONDS = 300
# Wrong codes (TOTP or recovery) in a row before the account's second
# factor locks for MFA_LOCK_SECONDS. The password step has its own nginx
# rate limit; this one also holds across IPs.
MAX_FAILURES = 5
MFA_LOCK_SECONDS = 15 * 60
RECOVERY_CODES = 10


class MFAError(ValueError):
    """A code was wrong, the factor is locked, or the request doesn't fit
    the account's MFA state. The message is safe to show the user."""


class Locked(MFAError):
    pass


# --- audit ------------------------------------------------------------------


def record_event(conn: psycopg.Connection, event: str, *, org_id: str | None = None, user_id: str | None = None,
                 actor_id: str | None = None, detail: dict | None = None, ip: str | None = None) -> None:
    conn.execute(
        "INSERT INTO auth_events (org_id, user_id, actor_id, event, detail, ip) VALUES (%s, %s, %s, %s, %s, %s)",
        (org_id, user_id, actor_id, event, json.dumps(detail) if detail else None, ip),
    )


def list_events(conn: psycopg.Connection, org_id: str, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        "SELECT e.at, e.event, e.user_id, u.email, e.actor_id, a.email, e.detail, e.ip FROM auth_events e "
        "LEFT JOIN users u ON u.id = e.user_id LEFT JOIN users a ON a.id = e.actor_id "
        "WHERE e.org_id = %s ORDER BY e.at DESC LIMIT %s", (org_id, max(1, min(limit, 1000)))).fetchall()
    return [{"at": r[0].isoformat(), "event": r[1], "user_id": r[2], "user_email": r[3], "actor_id": r[4],
             "actor_email": r[5], "detail": r[6], "ip": r[7]} for r in rows]


# --- secrets at rest ----------------------------------------------------------


# HKDF extracts a key from material that is already random; it does not
# stretch a human-chosen password. So BALER_MFA_KEY must be a long random
# string (.env.example has a command that makes one), and a shorter one is
# refused rather than used.
MFA_KEY_MIN_CHARS = 32


def key_problem() -> str | None:
    """Why BALER_MFA_KEY can't be used, or None (also None when it's unset:
    secrets are then stored unsealed, which auth_api warns about)."""
    key = os.environ.get("BALER_MFA_KEY", "")
    if key and len(key) < MFA_KEY_MIN_CHARS:
        return (f"BALER_MFA_KEY is {len(key)} characters; it must be a random string of at least "
                f"{MFA_KEY_MIN_CHARS} (e.g. python -c \"import secrets;print(secrets.token_urlsafe(48))\")")
    return None


def _fernet():
    key = os.environ.get("BALER_MFA_KEY", "")
    if not key:
        return None
    problem = key_problem()
    if problem:
        raise MFAError(problem)
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"baler-mfa").derive(key.encode())
    return Fernet(base64.urlsafe_b64encode(derived))


def unsealed_count(conn: psycopg.Connection) -> int:
    """Authenticator secrets (enrolled or pending) stored without
    BALER_MFA_KEY: readable by anyone who can read the users table."""
    row = conn.execute(
        "SELECT count(*) FROM users WHERE (totp_secret IS NOT NULL AND totp_secret NOT LIKE 'fernet:%%') "
        "OR (totp_pending_secret IS NOT NULL AND totp_pending_secret NOT LIKE 'fernet:%%')").fetchone()
    return int(row[0]) if row else 0


def seal(secret_b32: str) -> str:
    """The TOTP secret as stored: sealed with BALER_MFA_KEY when set."""
    f = _fernet()
    return "fernet:" + f.encrypt(secret_b32.encode()).decode() if f else secret_b32


def unseal(stored: str) -> str:
    if not stored.startswith("fernet:"):
        return stored
    f = _fernet()
    if f is None:
        raise MFAError("this account's authenticator can't be read (BALER_MFA_KEY is not set); ask an admin to reset it")
    try:
        return f.decrypt(stored[len("fernet:"):].encode()).decode()
    except Exception as exc:  # InvalidToken: wrong key
        raise MFAError("this account's authenticator can't be read (BALER_MFA_KEY changed); "
                       "ask an admin to reset it") from exc


# --- the pending-login token --------------------------------------------------


def _mfa_key() -> str:
    from baler import auth

    # Derived, not the JWT secret itself: the gateway verifies bearer tokens
    # with BALER_JWT_SECRET, so a token signed with this can never pass it.
    return hmac.new(auth._jwt_secret().encode(), b"baler-mfa-pending", hashlib.sha256).hexdigest()


def issue_mfa_token(user_id: str) -> str:
    now = int(time.time())
    return pyjwt.encode({"sub": user_id, "kind": "mfa_pending", "jti": secrets.token_hex(8), "iat": now,
                         "exp": now + MFA_TOKEN_TTL_SECONDS}, _mfa_key(), algorithm="HS256")


def read_mfa_token(token: str) -> str:
    try:
        claims = pyjwt.decode(token, _mfa_key(), algorithms=["HS256"])
    except pyjwt.PyJWTError as exc:
        raise MFAError("the sign-in step expired; enter your password again") from exc
    if claims.get("kind") != "mfa_pending" or not claims.get("sub"):
        raise MFAError("the sign-in step expired; enter your password again")
    return claims["sub"]


# --- state --------------------------------------------------------------------


def state(conn: psycopg.Connection, user_id: str) -> dict:
    """{enabled, required (by the org), pending (enrolment started),
    recovery_codes_left}."""
    row = conn.execute(
        "SELECT u.totp_enabled_at, u.totp_pending_secret IS NOT NULL, o.mfa_required, "
        "(SELECT count(*) FROM user_recovery_codes c WHERE c.user_id = u.id AND c.used_at IS NULL) "
        "FROM users u JOIN orgs o ON o.id = u.org_id WHERE u.id = %s", (user_id,)).fetchone()
    if not row:
        return {"enabled": False, "required": False, "pending": False, "recovery_codes_left": 0}
    return {"enabled": row[0] is not None, "enabled_at": row[0].isoformat() if row[0] else None,
            "pending": bool(row[1]), "required": bool(row[2]), "recovery_codes_left": int(row[3])}


def login_step(conn: psycopg.Connection, user_id: str) -> str:
    """What a correct password leads to: "session" (no MFA, not required),
    "mfa" (ask for a code) or "enroll" (required, not set up yet)."""
    s = state(conn, user_id)
    if s["enabled"]:
        return "mfa"
    return "enroll" if s["required"] else "session"


# --- enrolment ----------------------------------------------------------------


def start_enrolment(conn: psycopg.Connection, user: dict) -> dict:
    """A new secret, held as pending until a code from it is confirmed.
    Starting again replaces it. Refused while MFA is already on (turn it
    off first), so a stolen session can't silently swap the device."""
    if state(conn, user["id"])["enabled"]:
        raise MFAError("MFA is already on; turn it off first to set up a new authenticator")
    secret = totp.new_secret()
    conn.execute("UPDATE users SET totp_pending_secret = %s WHERE id = %s", (seal(secret), user["id"]))
    return {"secret": secret, "otpauth_uri": totp.provisioning_uri(secret, user["email"])}


def _new_recovery_codes(conn: psycopg.Connection, user_id: str) -> list[str]:
    codes = ["-".join(secrets.token_hex(3) for _ in range(2)) for _ in range(RECOVERY_CODES)]
    conn.execute("DELETE FROM user_recovery_codes WHERE user_id = %s", (user_id,))
    for c in codes:
        conn.execute("INSERT INTO user_recovery_codes (user_id, code_hash) VALUES (%s, %s)", (user_id, _hash_code(c)))
    return codes


def _hash_code(code: str) -> str:
    return hashlib.sha256("".join(code.lower().split()).replace("-", "").encode()).hexdigest()


def confirm_enrolment(conn: psycopg.Connection, user: dict, code: str, ip: str | None = None) -> list[str]:
    """Turn MFA on once a code from the pending secret checks out; returns
    the recovery codes (shown once, stored hashed)."""
    with conn.transaction():
        row = conn.execute("SELECT totp_pending_secret, totp_enabled_at FROM users WHERE id = %s FOR UPDATE",
                           (user["id"],)).fetchone()
        if not row or not row[0]:
            raise MFAError("start setting up an authenticator first")
        if row[1] is not None:
            raise MFAError("MFA is already on")
        used = totp.match(unseal(row[0]), code)
        if used is None:
            raise MFAError("that code isn't right; check the time on your phone and try the next code")
        conn.execute("UPDATE users SET totp_secret = totp_pending_secret, totp_pending_secret = NULL, "
                     "totp_enabled_at = now(), totp_last_step = %s, mfa_failures = 0, mfa_locked_until = NULL "
                     "WHERE id = %s", (used, user["id"]))
        codes = _new_recovery_codes(conn, user["id"])
        record_event(conn, "mfa_enabled", org_id=user["org_id"], user_id=user["id"], actor_id=user["id"], ip=ip)
    return codes


def regenerate_recovery_codes(conn: psycopg.Connection, user: dict, code: str, ip: str | None = None) -> list[str]:
    # verify() first, outside the transaction below, so a wrong code's
    # failure count is kept (it commits its own bookkeeping).
    verify(conn, user, code, ip=ip, allow_recovery=False)
    with conn.transaction():
        codes = _new_recovery_codes(conn, user["id"])
        record_event(conn, "mfa_recovery_codes_regenerated", org_id=user["org_id"], user_id=user["id"],
                     actor_id=user["id"], ip=ip)
    return codes


def disable(conn: psycopg.Connection, user: dict, code: str, ip: str | None = None) -> None:
    """The user turns their own MFA off (password checked by the caller,
    plus a current code here). Refused while their org requires MFA."""
    if state(conn, user["id"])["required"]:
        raise MFAError("your org requires MFA, so it can't be turned off; an admin can reset it if you lost your device")
    verify(conn, user, code, ip=ip)  # outside the transaction: see regenerate_recovery_codes
    with conn.transaction():
        _clear(conn, user["id"])
        record_event(conn, "mfa_disabled", org_id=user["org_id"], user_id=user["id"], actor_id=user["id"], ip=ip)


def admin_reset(conn: psycopg.Connection, org_id: str, user_id: str, actor_id: str, ip: str | None = None) -> bool:
    """An admin clears a user's MFA (lost device). Their sessions end; if
    the org requires MFA they enrol again at next login."""
    from baler import auth

    with conn.transaction():
        row = conn.execute("SELECT 1 FROM users WHERE id = %s AND org_id = %s FOR UPDATE", (user_id, org_id)).fetchone()
        if not row:
            return False
        _clear(conn, user_id)
        auth.end_user_sessions(conn, user_id)
        record_event(conn, "mfa_reset_by_admin", org_id=org_id, user_id=user_id, actor_id=actor_id, ip=ip)
    return True


def _clear(conn: psycopg.Connection, user_id: str) -> None:
    conn.execute("UPDATE users SET totp_secret = NULL, totp_pending_secret = NULL, totp_enabled_at = NULL, "
                 "totp_last_step = NULL, mfa_failures = 0, mfa_locked_until = NULL WHERE id = %s", (user_id,))
    conn.execute("DELETE FROM user_recovery_codes WHERE user_id = %s", (user_id,))


# --- verification ---------------------------------------------------------------


def verify(conn: psycopg.Connection, user: dict, code: str, *, ip: str | None = None,
           allow_recovery: bool = True) -> str:
    """Check a TOTP code (each works once) or, if `allow_recovery`, a
    recovery code (each works once, and its use is audited). Returns
    "totp" or "recovery". Raises Locked after MAX_FAILURES wrong codes,
    MFAError for a wrong one. Commits its own bookkeeping, so a failure
    counts even though the caller's request fails."""
    code = (code or "").strip()
    with conn.transaction():
        row = conn.execute("SELECT totp_secret, totp_last_step, mfa_failures, mfa_locked_until FROM users "
                           "WHERE id = %s FOR UPDATE", (user["id"],)).fetchone()
        if not row or not row[0]:
            raise MFAError("MFA isn't on for this account")
        now = datetime.now(timezone.utc)
        if row[3] is not None and row[3] > now:
            raise Locked("too many wrong codes; try again in a few minutes")
        used = totp.match(unseal(row[0]), code, after_step=row[1])
        if used is not None:
            conn.execute("UPDATE users SET totp_last_step = %s, mfa_failures = 0, mfa_locked_until = NULL WHERE id = %s",
                         (used, user["id"]))
            return "totp"
        if allow_recovery and code and not code.isdigit():
            hit = conn.execute(
                "UPDATE user_recovery_codes SET used_at = now() WHERE user_id = %s AND code_hash = %s AND used_at IS NULL "
                "RETURNING 1", (user["id"], _hash_code(code))).fetchone()
            if hit:
                conn.execute("UPDATE users SET mfa_failures = 0, mfa_locked_until = NULL WHERE id = %s", (user["id"],))
                left = conn.execute("SELECT count(*) FROM user_recovery_codes WHERE user_id = %s AND used_at IS NULL",
                                    (user["id"],)).fetchone()
                record_event(conn, "mfa_recovery_code_used", org_id=user["org_id"], user_id=user["id"],
                             actor_id=user["id"], detail={"left": int(left[0]) if left else 0}, ip=ip)
                return "recovery"
        failures = int(row[2]) + 1
        locked = now + timedelta(seconds=MFA_LOCK_SECONDS) if failures >= MAX_FAILURES else None
        conn.execute("UPDATE users SET mfa_failures = %s, mfa_locked_until = %s WHERE id = %s",
                     (0 if locked else failures, locked, user["id"]))
        if locked:
            record_event(conn, "mfa_locked", org_id=user["org_id"], user_id=user["id"], ip=ip)
    if locked:
        raise Locked("too many wrong codes; try again in a few minutes")
    raise MFAError("that code isn't right")


# --- org requirement --------------------------------------------------------------


def set_org_required(conn: psycopg.Connection, org_id: str, required: bool, actor: dict, ip: str | None = None) -> dict:
    """Require MFA for the org (or stop). The admin doing it must have MFA
    on themselves, so turning it on can't lock out the only person who
    could turn it off. Turning it on ends the sessions of everyone in the
    org without MFA: they enrol at their next login."""
    from baler import auth

    if not isinstance(required, bool):
        raise TypeError("mfa_required must be true or false")
    if required and not state(conn, actor["id"])["enabled"]:
        raise MFAError("turn on MFA for your own account before requiring it for the org")
    with conn.transaction():
        conn.execute("UPDATE orgs SET mfa_required = %s WHERE id = %s", (required, org_id))
        ended = 0
        if required:
            rows = conn.execute("SELECT id FROM users WHERE org_id = %s AND totp_enabled_at IS NULL "
                                "AND disabled_at IS NULL", (org_id,)).fetchall()
            for (uid,) in rows:
                auth.end_user_sessions(conn, uid)
            ended = len(rows)
        record_event(conn, "org_mfa_required" if required else "org_mfa_not_required", org_id=org_id,
                     actor_id=actor["id"], detail={"sessions_ended": ended} if required else None, ip=ip)
    return get_org(conn, org_id)


def get_org(conn: psycopg.Connection, org_id: str) -> dict:
    row = conn.execute(
        "SELECT o.id, o.name, o.mfa_required, count(u.id) FILTER (WHERE u.disabled_at IS NULL), "
        "count(u.id) FILTER (WHERE u.disabled_at IS NULL AND u.totp_enabled_at IS NOT NULL) "
        "FROM orgs o LEFT JOIN users u ON u.org_id = o.id WHERE o.id = %s GROUP BY o.id", (org_id,)).fetchone()
    if not row:
        return {}
    return {"id": row[0], "name": row[1], "mfa_required": bool(row[2]), "users": int(row[3]), "users_with_mfa": int(row[4])}


def mfa_by_user(conn: psycopg.Connection, org_id: str) -> dict[str, bool]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT id, totp_enabled_at IS NOT NULL FROM users WHERE org_id = %s", (org_id,)).fetchall()}

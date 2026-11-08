"""
Postgres-backed identity: orgs, sites, users, API keys, and the
revocation denylist the gateway's njs periodic poller consumes. Same
shape as faults.py - SQL/business logic lives here, auth_api.py is the
HTTP layer on top, matching fault_api.py's relationship to faults.py.

Enforcement (who's allowed to call what) happens in the gateway
(gateway/njs/), not here - this module only needs to answer "is this
password right" and "what does this token's owner look like", not
"is this role allowed to hit this route".
"""

import hashlib
import hmac
import os
import time
from datetime import datetime, timezone

import jwt as pyjwt
import psycopg

from baler import json_store, schema, scope

# Schema (orgs, sites, users, user_sites, api_keys, revoked_tokens):
# baler/migrations/, from 0001 (#194). Change it with a new migration there,
# not here.

SESSION_TTL_SECONDS = 12 * 3600

# 600k rounds is OWASP's 2023+ recommendation for PBKDF2-HMAC-SHA256 -
# stdlib-only (hashlib), no new dependency for something this codebase
# only needs to do at login time, not in a hot loop.
_PBKDF2_ITERATIONS = 600_000


def ensure_schema(conn: psycopg.Connection) -> None:
    schema.ensure(conn)


# --- passwords ---


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt_hex, dk_hex = stored_hash.split("$")
    except ValueError:
        return False
    salt = bytes.fromhex(salt_hex)
    expected = bytes.fromhex(dk_hex)
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return hmac.compare_digest(actual, expected)


# --- JWT issuing (verification happens in the gateway's njs, not here -
# this is the "sign" half only, mirroring gen_hs_jwt.js's role) ---


def _jwt_secret() -> str:
    secret = os.environ.get("BALER_JWT_SECRET")
    if not secret:
        raise RuntimeError("BALER_JWT_SECRET is not set - refusing to issue unsigned-equivalent tokens")
    return secret


def issue_session_token(user: dict, site_ids: list[str]) -> str:
    jti = json_store.new_id()
    claims = {
        "sub": user["id"],
        "org_id": user["org_id"],
        "role": user["role"],
        "site_ids": site_ids,
        "kind": "session",
        "jti": jti,
        # milliseconds, not whole seconds: a session cutoff (end_user_sessions)
        # in the same second as a login must still end that login (#50)
        "iat": round(time.time(), 3),
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
    }
    return pyjwt.encode(claims, _jwt_secret(), algorithm="HS256")


def issue_api_key_token(api_key: dict) -> str:
    claims = {
        "sub": api_key["id"],
        "org_id": api_key["org_id"],
        "role": api_key["role"],
        "site_ids": api_key["site_ids"] or [],
        "kind": "api_key",
        "jti": api_key["jti"],
    }
    if api_key["expires_at"] is not None:
        claims["exp"] = int(api_key["expires_at"].timestamp())
    return pyjwt.encode(claims, _jwt_secret(), algorithm="HS256")


def decode_own_token(authorization_header: str | None) -> dict | None:
    """Decodes+verifies the caller's own bearer token, for handlers that
    need to know who's calling (e.g. /auth/me, /auth/logout, org-scoping
    admin actions to the admin's own org) - independent re-verification,
    not a "trust the gateway already checked this" shortcut, since this
    is the identity service itself."""
    if not authorization_header or not authorization_header.startswith("Bearer "):
        return None
    token = authorization_header[len("Bearer "):]
    try:
        return pyjwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except pyjwt.PyJWTError:
        return None


# --- orgs / sites ---


def create_org(conn: psycopg.Connection, name: str, admin_email: str, admin_password: str) -> dict:
    """Creates the org and its first admin user in one call - the only
    way any org's first user gets created (see create_user for adding
    more users afterward). Solves the bootstrap problem: an admin-only
    "create a user" endpoint has no answer for "who creates the very
    first admin", so org creation carries its own."""
    org_id = json_store.new_id()
    now = datetime.now(timezone.utc)
    with conn.transaction():  # a taken admin email leaves no empty org behind
        conn.execute("INSERT INTO orgs (id, name, created_at) VALUES (%s, %s, %s)", (org_id, name, now))
        user = create_user(conn, org_id, admin_email, admin_password, "admin")
    return {"id": org_id, "name": name, "admin_user_id": user["id"]}


class PrefixConflict(ValueError):
    """A topic prefix overlaps one another org already owns."""


def _check_prefixes(conn: psycopg.Connection, org_id: str, prefixes: list[str]) -> list[str]:
    """Normalize, and refuse any prefix that equals, contains or is
    contained by another org's - otherwise claiming `fbf` would expose every
    other tenant's `fbf/...` data (see scope.py)."""
    normalized = sorted({scope.normalize_prefix(p) for p in prefixes})
    others = conn.execute(
        "SELECT org_id, unnest(topic_prefixes) FROM sites WHERE org_id <> %s", (org_id,)
    ).fetchall()
    for p in normalized:
        for _, theirs in others:
            t = scope.normalize_prefix(theirs)
            if p == t or p.startswith(t + "/") or t.startswith(p + "/"):
                raise PrefixConflict(f"topic prefix {p!r} overlaps {t!r}, owned by another org")
    return normalized


def create_site(conn: psycopg.Connection, org_id: str, name: str, topic_prefixes: list[str] | None = None) -> dict:
    prefixes = _check_prefixes(conn, org_id, topic_prefixes or [])
    site_id = json_store.new_id()
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO sites (id, org_id, name, created_at, topic_prefixes) VALUES (%s, %s, %s, %s, %s)",
        (site_id, org_id, name, now, prefixes),
    )
    return {"id": site_id, "org_id": org_id, "name": name, "topic_prefixes": prefixes}


def set_site_prefixes(conn: psycopg.Connection, org_id: str, site_id: str, topic_prefixes: list[str]) -> dict | None:
    """Replace a site's prefixes. None if the site isn't in `org_id`."""
    prefixes = _check_prefixes(conn, org_id, topic_prefixes)
    row = conn.execute(
        "UPDATE sites SET topic_prefixes = %s WHERE id = %s AND org_id = %s RETURNING id, org_id, name",
        (prefixes, site_id, org_id),
    ).fetchone()
    if not row:
        return None
    return {"id": row[0], "org_id": row[1], "name": row[2], "topic_prefixes": prefixes}


# --- users ---


# Roles a person can hold. "service" (rank of operator, see the gateway's
# policy.ROLE_RANK) is for API keys only, never a login.
USER_ROLES = ("viewer", "operator", "admin")
MIN_PASSWORD_LENGTH = 12


class EmailTaken(ValueError):
    """The email is in use. Emails are unique across every org, so the
    message never says where - that would reveal another org's users."""


class LastAdmin(ValueError):
    """The change would leave the org with no admin."""


def normalize_email(email: str) -> str:
    return email.strip().lower()


def check_new_password(password) -> None:
    """For passwords set through the API (not existing accounts)."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")


def _check_role(role) -> str:
    if role not in USER_ROLES:
        raise ValueError(f"role must be one of {', '.join(USER_ROLES)}")
    return role


MAX_DISPLAY_NAME = 120


def check_display_name(name) -> str | None:
    """A display name trimmed to one line, or None for "unset" (None or
    blank)."""
    if name is None:
        return None
    if not isinstance(name, str):
        raise TypeError("display_name must be a string")
    clean = " ".join(name.split())
    if len(clean) > MAX_DISPLAY_NAME:
        raise ValueError(f"display_name must be at most {MAX_DISPLAY_NAME} characters")
    return clean or None


def create_user(conn: psycopg.Connection, org_id: str, email: str, password: str, role: str,
                display_name: str | None = None) -> dict:
    _check_role(role)
    name = check_display_name(display_name)
    email = normalize_email(email)
    if not email or "@" not in email:
        raise ValueError("email must be an email address")
    user_id = json_store.new_id()
    now = datetime.now(timezone.utc)
    try:
        with conn.transaction():
            conn.execute(
                "INSERT INTO users (id, org_id, email, password_hash, role, created_at, display_name) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (user_id, org_id, email, hash_password(password), role, now, name),
            )
    except psycopg.errors.UniqueViolation as exc:
        raise EmailTaken("that email can't be used") from exc
    return {"id": user_id, "org_id": org_id, "email": email, "role": role, "display_name": name}


def list_sites(conn: psycopg.Connection, org_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, topic_prefixes FROM sites WHERE org_id = %s ORDER BY name", (org_id,)
    ).fetchall()
    return [{"id": r[0], "name": r[1], "topic_prefixes": list(r[2] or [])} for r in rows]


def _admin_count(conn: psycopg.Connection, org_id: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM users WHERE org_id = %s AND role = 'admin' AND disabled_at IS NULL", (org_id,)
    ).fetchone()
    return int(row[0]) if row else 0


_UNSET = object()


def update_user(conn: psycopg.Connection, org_id: str, user_id: str,
                role: str | None = None, site_ids: list[str] | None = None,
                disabled: bool | None = None, display_name=_UNSET) -> dict | None:
    """Change a user's role and/or the sites they're limited to (empty =
    the whole org), disable/enable them, or set their display name (None
    or "" clears it). Role and sites live in their session tokens, so their
    sessions end (as they do on disable); a display name isn't in the token,
    so changing only that leaves them signed in. None when there's no such
    user in the org."""
    if disabled is not None and not isinstance(disabled, bool):
        raise ValueError("disabled must be true or false")
    name = None if display_name is _UNSET else check_display_name(display_name)
    if role is not None:
        _check_role(role)
    if site_ids is not None:
        if not isinstance(site_ids, list) or not all(isinstance(x, str) for x in site_ids):
            raise ValueError("site_ids must be a list of site ids")
        known = {s["id"] for s in list_sites(conn, org_id)}
        unknown = sorted(set(site_ids) - known)
        if unknown:
            raise ValueError(f"no such site in your org: {', '.join(unknown)}")
    with conn.transaction():
        row = conn.execute(
            "SELECT role, disabled_at FROM users WHERE id = %s AND org_id = %s FOR UPDATE", (user_id, org_id)
        ).fetchone()
        if not row:
            return None
        active_admin = row[0] == "admin" and row[1] is None
        if role is not None and active_admin and role != "admin" and _admin_count(conn, org_id) <= 1:
            raise LastAdmin("the org's last admin can't be demoted")
        if disabled and active_admin and _admin_count(conn, org_id) <= 1:
            raise LastAdmin("the org's last admin can't be disabled")
        if disabled is not None:
            conn.execute(
                "UPDATE users SET disabled_at = CASE WHEN %s THEN COALESCE(disabled_at, now()) END WHERE id = %s",
                (disabled, user_id),
            )
        if role is not None:
            conn.execute("UPDATE users SET role = %s WHERE id = %s", (role, user_id))
        if site_ids is not None:
            conn.execute("DELETE FROM user_sites WHERE user_id = %s", (user_id,))
            for sid in sorted(set(site_ids)):
                conn.execute("INSERT INTO user_sites (user_id, site_id) VALUES (%s, %s)", (user_id, sid))
        if display_name is not _UNSET:
            conn.execute("UPDATE users SET display_name = %s WHERE id = %s", (name, user_id))
        if role is not None or site_ids is not None or disabled is not None:
            end_user_sessions(conn, user_id)
        shown = conn.execute("SELECT display_name FROM users WHERE id = %s", (user_id,)).fetchone()
    now_disabled = row[1] is not None if disabled is None else disabled
    return {"id": user_id, "role": role or row[0], "site_ids": user_site_ids(conn, user_id), "disabled": now_disabled,
            "display_name": shown[0] if shown else None}


def get_user_by_email(conn: psycopg.Connection, email: str) -> dict | None:
    row = conn.execute(
        # a disabled user is no user at login (password or SCRAM): the same
        # answer as a wrong password, so disabling reveals nothing
        "SELECT id, org_id, email, password_hash, role FROM users WHERE lower(email) = %s AND disabled_at IS NULL",
        (normalize_email(email),)
    ).fetchone()
    if not row:
        return None
    return {"id": row[0], "org_id": row[1], "email": row[2], "password_hash": row[3], "role": row[4]}


def list_users(conn: psycopg.Connection, org_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT u.id, u.email, u.role, u.created_at,
                  COALESCE(array_agg(us.site_id) FILTER (WHERE us.site_id IS NOT NULL), '{}'),
                  u.disabled_at IS NOT NULL, u.display_name
           FROM users u LEFT JOIN user_sites us ON us.user_id = u.id
           WHERE u.org_id = %s GROUP BY u.id ORDER BY u.email""",
        (org_id,),
    ).fetchall()
    return [{"id": r[0], "email": r[1], "role": r[2], "created_at": r[3].isoformat(), "site_ids": list(r[4]),
             "disabled": bool(r[5]), "display_name": r[6]} for r in rows]


# Sessions are stateless JWTs, so ending a user's sessions (deleted, or
# password reset) is a cutoff in the denylist the gateway already polls:
# "user:<id>:<epoch>" rejects that user's session tokens issued before
# <epoch> (gateway/njs/main.js). Kept until every such token would have
# expired anyway, then pruned like any session revocation.
_USER_CUTOFF = "user:{}:{}"


def end_user_sessions(conn: psycopg.Connection, user_id: str) -> None:
    now = f"{time.time():.3f}"  # ms, like session iat (#50)
    conn.execute(
        "INSERT INTO revoked_tokens (jti, revoked_at, expires_at) VALUES (%s, now(), now() + make_interval(secs => %s)) "
        "ON CONFLICT (jti) DO NOTHING",
        (_USER_CUTOFF.format(user_id, now), SESSION_TTL_SECONDS),
    )
    conn.execute("DELETE FROM revoked_tokens WHERE expires_at IS NOT NULL AND expires_at < now()")


def delete_user(conn: psycopg.Connection, org_id: str, user_id: str) -> bool:
    """Remove a user from `org_id` (only that org's users) and end their
    live sessions. False when there's no such user in the org."""
    with conn.transaction():
        row = conn.execute("SELECT role FROM users WHERE id = %s AND org_id = %s FOR UPDATE", (user_id, org_id)).fetchone()
        if not row:
            return False
        if row[0] == "admin" and _admin_count(conn, org_id) <= 1:
            raise LastAdmin("the org's last admin can't be deleted")
        conn.execute("DELETE FROM user_sites WHERE user_id = %s", (user_id,))
        conn.execute("DELETE FROM users WHERE id = %s AND org_id = %s", (user_id, org_id))
        end_user_sessions(conn, user_id)
    return True


def set_password(conn: psycopg.Connection, org_id: str, user_id: str, password: str) -> bool:
    """New password for a user in `org_id`; every session they had ends
    (they, or whoever had the old password, log in again)."""
    if not password:
        raise ValueError("password must not be empty")
    with conn.transaction():
        cur = conn.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s AND org_id = %s",
            (hash_password(password), user_id, org_id),
        )
        if cur.rowcount == 0:
            return False
        end_user_sessions(conn, user_id)
    return True


def get_user(conn: psycopg.Connection, user_id: str) -> dict | None:
    row = conn.execute(
        "SELECT id, org_id, email, password_hash, role FROM users WHERE id = %s", (user_id,)
    ).fetchone()
    if not row:
        return None
    return {"id": row[0], "org_id": row[1], "email": row[2], "password_hash": row[3], "role": row[4]}


def get_display_name(conn: psycopg.Connection, user_id: str) -> str | None:
    """The user's display name (Baler#121), or None. Apart from get_user so
    services that don't run this module's schema can keep calling that."""
    row = conn.execute("SELECT display_name FROM users WHERE id = %s", (user_id,)).fetchone()
    return row[0] if row else None


def user_site_ids(conn: psycopg.Connection, user_id: str) -> list[str]:
    rows = conn.execute("SELECT site_id FROM user_sites WHERE user_id = %s", (user_id,)).fetchall()
    return [r[0] for r in rows]


# --- API keys ---


def create_api_key(
    conn: psycopg.Connection,
    org_id: str,
    role: str,
    site_ids: list[str] | None = None,
    expires_in_seconds: int | None = None,
) -> dict:
    key_id = json_store.new_id()
    jti = json_store.new_id()
    now = datetime.now(timezone.utc)
    expires_at = datetime.fromtimestamp(now.timestamp() + expires_in_seconds, tz=timezone.utc) if expires_in_seconds else None
    conn.execute(
        """
        INSERT INTO api_keys (id, org_id, site_ids, role, jti, expires_at, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (key_id, org_id, site_ids or None, role, jti, expires_at, now),
    )
    return {"id": key_id, "org_id": org_id, "site_ids": site_ids or [], "role": role, "jti": jti, "expires_at": expires_at}


def list_api_keys(conn: psycopg.Connection, org_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, org_id, site_ids, role, expires_at, revoked_at, created_at, last_used_at
        FROM api_keys WHERE org_id = %s ORDER BY created_at DESC
        """,
        (org_id,),
    ).fetchall()
    return [
        {
            "id": r[0], "org_id": r[1], "site_ids": r[2] or [], "role": r[3],
            "expires_at": r[4].isoformat() if r[4] else None,
            "revoked_at": r[5].isoformat() if r[5] else None,
            "created_at": r[6].isoformat(),
            "last_used_at": r[7].isoformat() if r[7] else None,
        }
        for r in rows
    ]


def revoke_api_key(conn: psycopg.Connection, key_id: str) -> bool:
    """Marks the key revoked and adds its jti to the denylist the
    gateway polls. Returns False if no such key exists (caller 404s)."""
    row = conn.execute("SELECT jti FROM api_keys WHERE id = %s", (key_id,)).fetchone()
    if not row:
        return False
    jti = row[0]
    now = datetime.now(timezone.utc)
    conn.execute("UPDATE api_keys SET revoked_at = %s WHERE id = %s", (now, key_id))
    conn.execute(
        "INSERT INTO revoked_tokens (jti, revoked_at) VALUES (%s, %s) ON CONFLICT (jti) DO NOTHING",
        (jti, now),
    )
    return True


def revoke_session(conn: psycopg.Connection, claims: dict) -> bool:
    """End one session token now (Haystack's `close` op): its jti joins the
    denylist until the token would have expired anyway, and rows past that
    are pruned here, so the list stays as small as the live sessions."""
    jti, exp = claims.get("jti"), claims.get("exp")
    if not jti:
        return False
    expires = datetime.fromtimestamp(exp, timezone.utc) if exp else None
    conn.execute(
        "INSERT INTO revoked_tokens (jti, revoked_at, expires_at) VALUES (%s, now(), %s) ON CONFLICT (jti) DO NOTHING",
        (jti, expires),
    )
    conn.execute("DELETE FROM revoked_tokens WHERE expires_at IS NOT NULL AND expires_at < now()")
    return True


def is_revoked(conn: psycopg.Connection, jti: str | None) -> bool:
    if not jti:
        return False
    return conn.execute("SELECT 1 FROM revoked_tokens WHERE jti = %s", (jti,)).fetchone() is not None


def list_revoked_jtis(conn: psycopg.Connection) -> list[str]:
    """Everything the gateway's js_periodic poller should treat as
    revoked - consumed by GET /internal/revoked-jtis. Deliberately no
    pruning of long-past-expiry rows here: the table stays small at this
    project's scale, and the gateway's own exp check already makes a
    stale entry here harmless, just redundant."""
    rows = conn.execute("SELECT jti FROM revoked_tokens").fetchall()
    return [r[0] for r in rows]

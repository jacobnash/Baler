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

from baler import json_store

SCHEMA = """
CREATE TABLE IF NOT EXISTS orgs (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    id         TEXT PRIMARY KEY,
    org_id     TEXT NOT NULL REFERENCES orgs(id),
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL REFERENCES orgs(id),
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL,   -- admin | operator | viewer
    created_at    TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS user_sites (
    user_id TEXT NOT NULL REFERENCES users(id),
    site_id TEXT NOT NULL REFERENCES sites(id),
    PRIMARY KEY (user_id, site_id)
);
CREATE TABLE IF NOT EXISTS api_keys (
    id            TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL REFERENCES orgs(id),
    site_ids      TEXT[],          -- NULL/empty = whole org
    role          TEXT NOT NULL,   -- admin | operator | viewer | service
    jti           TEXT NOT NULL UNIQUE,
    expires_at    TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL,
    last_used_at  TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS revoked_tokens (
    jti        TEXT PRIMARY KEY,
    revoked_at TIMESTAMPTZ NOT NULL
);
"""

SESSION_TTL_SECONDS = 12 * 3600

# 600k rounds is OWASP's 2023+ recommendation for PBKDF2-HMAC-SHA256 -
# stdlib-only (hashlib), no new dependency for something this codebase
# only needs to do at login time, not in a hot loop.
_PBKDF2_ITERATIONS = 600_000


def ensure_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA)


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
    conn.execute("INSERT INTO orgs (id, name, created_at) VALUES (%s, %s, %s)", (org_id, name, now))
    user = create_user(conn, org_id, admin_email, admin_password, "admin")
    return {"id": org_id, "name": name, "admin_user_id": user["id"]}


def create_site(conn: psycopg.Connection, org_id: str, name: str) -> dict:
    site_id = json_store.new_id()
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO sites (id, org_id, name, created_at) VALUES (%s, %s, %s, %s)",
        (site_id, org_id, name, now),
    )
    return {"id": site_id, "org_id": org_id, "name": name}


# --- users ---


def create_user(conn: psycopg.Connection, org_id: str, email: str, password: str, role: str) -> dict:
    user_id = json_store.new_id()
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO users (id, org_id, email, password_hash, role, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
        (user_id, org_id, email, hash_password(password), role, now),
    )
    return {"id": user_id, "org_id": org_id, "email": email, "role": role}


def get_user_by_email(conn: psycopg.Connection, email: str) -> dict | None:
    row = conn.execute(
        "SELECT id, org_id, email, password_hash, role FROM users WHERE email = %s", (email,)
    ).fetchone()
    if not row:
        return None
    return {"id": row[0], "org_id": row[1], "email": row[2], "password_hash": row[3], "role": row[4]}


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


def list_revoked_jtis(conn: psycopg.Connection) -> list[str]:
    """Everything the gateway's js_periodic poller should treat as
    revoked - consumed by GET /internal/revoked-jtis. Deliberately no
    pruning of long-past-expiry rows here: the table stays small at this
    project's scale, and the gateway's own exp check already makes a
    stale entry here harmless, just redundant."""
    rows = conn.execute("SELECT jti FROM revoked_tokens").fetchall()
    return [r[0] for r in rows]

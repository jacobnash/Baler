"""
0005 self-service password reset and multi-factor login (#278).

- orgs.mfa_required: an org admin can require every user to have MFA on.
- users: the TOTP secret (sealed with BALER_MFA_KEY when that is set),
  a secret being enrolled but not yet confirmed, when MFA was turned on,
  the last time step accepted (so a code works once), and a failure
  counter with a lockout time (rate limit on codes).
- user_recovery_codes: one-time recovery codes, stored as SHA-256 hashes.
- password_resets: emailed reset tokens, stored as SHA-256 hashes, with
  their expiry and when they were used (single use).
- auth_events: the audit trail for MFA enable/disable/reset, recovery
  code use, password reset requests and completions, and the org's MFA
  requirement changing.

Self-contained: new columns have defaults or are nullable, so existing
users and orgs keep working exactly as before (no MFA, not required).
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 5
NAME = "password reset and multi-factor login"

ORGS = "ALTER TABLE orgs ADD COLUMN IF NOT EXISTS mfa_required BOOLEAN NOT NULL DEFAULT FALSE"

USERS = """
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS totp_secret         TEXT,
    ADD COLUMN IF NOT EXISTS totp_pending_secret TEXT,
    ADD COLUMN IF NOT EXISTS totp_enabled_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS totp_last_step      BIGINT,
    ADD COLUMN IF NOT EXISTS mfa_failures        INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS mfa_locked_until    TIMESTAMPTZ"""

RECOVERY = """
CREATE TABLE IF NOT EXISTS user_recovery_codes (
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash  TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    used_at    TIMESTAMPTZ,
    PRIMARY KEY (user_id, code_hash)
)"""

RESETS = """
CREATE TABLE IF NOT EXISTS password_resets (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ
)"""

RESETS_IDX = "CREATE INDEX IF NOT EXISTS password_resets_user_idx ON password_resets (user_id, created_at)"

EVENTS = """
CREATE TABLE IF NOT EXISTS auth_events (
    id       BIGSERIAL PRIMARY KEY,
    at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    org_id   TEXT,
    user_id  TEXT,
    actor_id TEXT,
    event    TEXT NOT NULL,
    detail   JSONB,
    ip       TEXT
)"""

EVENTS_IDX = "CREATE INDEX IF NOT EXISTS auth_events_org_idx ON auth_events (org_id, at DESC)"


def plan(conn) -> list[Step]:
    return [
        Step(ORGS, "orgs.mfa_required"),
        Step(USERS, "users: TOTP secret, enrolment, replay guard, lockout"),
        Step(RECOVERY, "user_recovery_codes: hashed one-time recovery codes"),
        Step(RESETS, "password_resets: hashed single-use reset tokens"),
        Step(RESETS_IDX, "password_resets by user and time (rate limit)"),
        Step(EVENTS, "auth_events: MFA and password-reset audit trail"),
        Step(EVENTS_IDX, "auth_events by org and time"),
    ]

"""
Self-service password reset by email (#278).

- request(email): if the address belongs to an active user, and they
  haven't asked more than MAX_PER_HOUR times this hour, a random 256-bit
  token is emailed as a link and only its SHA-256 is stored, with an
  expiry (BALER_PASSWORD_RESET_TTL_SECONDS, default 30 min, never more
  than 60). Asking again replaces any unused token. Whatever happened, the
  caller gets the same answer, and the mail goes out on a background
  thread so the response time doesn't tell either.
- confirm(token, new_password): consumes the token in one UPDATE (unused,
  unexpired, so it works once even under a race), sets the password, ends
  every session the user had, and records the reset. MFA stays on: the
  next login still asks for a code.

Installs without email (baler.mailer not configured) keep admin-led
reset: request() is refused for everyone alike with a message saying
so, and GET /auth/password-reset/config tells the login page.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import psycopg

from baler import auth, mailer, mfa

MAX_TTL_SECONDS = 3600
MAX_PER_HOUR = 3
NOT_CONFIGURED = "self-service password reset isn't set up on this install; ask an admin of your org to reset it"
ACCEPTED = "if that email belongs to an account here, we've sent it a link to reset the password"
INVALID = "this reset link is invalid, already used or expired; ask for a new one"


class ResetError(ValueError):
    pass


def ttl_seconds() -> int:
    raw = os.environ.get("BALER_PASSWORD_RESET_TTL_SECONDS", "").strip()
    try:
        ttl = int(raw) if raw else 1800
    except ValueError:
        ttl = 1800
    return max(60, min(ttl, MAX_TTL_SECONDS))


def enabled() -> bool:
    return mailer.configured() and bool(public_url())


def public_url() -> str:
    return (os.environ.get("BALER_PUBLIC_URL") or "").strip().rstrip("/")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def reset_link(token: str) -> str:
    # In the fragment, not the query: browsers don't send it to the server
    # or in a Referer, so the token stays out of access logs.
    return f"{public_url()}/ui/reset.html#token={token}"


def _send(to: str, link: str, ttl: int) -> None:
    body = (f"Someone (hopefully you) asked to reset the Baler password for {to}.\n\n"
            f"Open this link within {ttl // 60} minutes to choose a new password:\n\n{link}\n\n"
            "The link works once. If you didn't ask for this, ignore this email: your password stays as it is.\n")
    try:
        mailer.send([to], "Reset your Baler password", body)
    except mailer.MailError as exc:
        print(f"password reset: couldn't send the email: {exc}", flush=True)


def request(conn: psycopg.Connection, email: str, ip: str | None = None,
            send: Callable[[str, str, int], None] | None = None, background: bool = True) -> str:
    """Returns the message to show (the same for every email). Raises
    ResetError when self-service reset isn't configured (also the same for
    every email)."""
    if not enabled():
        raise ResetError(NOT_CONFIGURED)
    if not isinstance(email, str) or "@" not in email or len(email) > 320:
        return ACCEPTED
    user = auth.get_user_by_email(conn, email)
    if not user:
        return ACCEPTED
    recent = conn.execute("SELECT count(*) FROM password_resets WHERE user_id = %s AND created_at > now() - interval '1 hour'",
                          (user["id"],)).fetchone()
    if recent and int(recent[0]) >= MAX_PER_HOUR:
        mfa.record_event(conn, "password_reset_rate_limited", org_id=user["org_id"], user_id=user["id"], ip=ip)
        return ACCEPTED
    token = secrets.token_urlsafe(32)
    ttl = ttl_seconds()
    with conn.transaction():
        # A new request replaces any unused token (marked used, kept for the rate limit).
        conn.execute("UPDATE password_resets SET used_at = now() WHERE user_id = %s AND used_at IS NULL", (user["id"],))
        conn.execute("INSERT INTO password_resets (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
                     (hash_token(token), user["id"], datetime.now(timezone.utc) + timedelta(seconds=ttl)))
        mfa.record_event(conn, "password_reset_requested", org_id=user["org_id"], user_id=user["id"], ip=ip)
    sender = send or _send
    if background:
        threading.Thread(target=sender, args=(user["email"], reset_link(token), ttl), daemon=True,
                         name="password-reset-mail").start()
    else:
        sender(user["email"], reset_link(token), ttl)
    return ACCEPTED


def confirm(conn: psycopg.Connection, token: str, new_password: str, ip: str | None = None) -> None:
    auth.check_new_password(new_password)
    if not isinstance(token, str) or not token or len(token) > 200:
        raise ResetError(INVALID)
    with conn.transaction():
        row = conn.execute(
            "UPDATE password_resets SET used_at = now() WHERE token_hash = %s AND used_at IS NULL AND expires_at > now() "
            "RETURNING user_id", (hash_token(token),)).fetchone()
        if not row:
            raise ResetError(INVALID)
        user = conn.execute("SELECT id, org_id FROM users WHERE id = %s AND disabled_at IS NULL", (row[0],)).fetchone()
        if not user:
            raise ResetError(INVALID)
        auth.set_password(conn, user[1], user[0], new_password)  # ends every session
        mfa.record_event(conn, "password_reset_completed", org_id=user[1], user_id=user[0], ip=ip)

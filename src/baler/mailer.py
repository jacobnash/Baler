"""
Outbound email over SMTP, for operator alerts (baler.alerts) and account
mail such as password resets. Configured per install in .env (BALER_SMTP_*);
with BALER_SMTP_HOST blank, email is off and `configured()` says so, so a
caller can fall back (alerts: webhook only; password reset: admin-led).

Plain text only, one message per call, a short timeout, and STARTTLS (or
implicit TLS on port 465) unless BALER_SMTP_TLS=none. The caller decides
what a failure means: send() raises MailError and never retries itself.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

TIMEOUT_SECONDS = 10.0


class MailError(Exception):
    pass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def configured() -> bool:
    return bool(_env("BALER_SMTP_HOST") and _env("BALER_SMTP_FROM"))


def build(to: list[str], subject: str, body: str, sender: str | None = None) -> EmailMessage:
    # Header injection: a subject or address with a line break is refused,
    # not folded - these can carry text from a request.
    for value in [subject, *to]:
        if "\r" in value or "\n" in value:
            raise MailError("line break in a mail header")
    msg = EmailMessage()
    msg["From"] = sender or _env("BALER_SMTP_FROM")
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain="baler")
    msg.set_content(body)
    return msg


def send(to: list[str], subject: str, body: str) -> None:
    """Send one plain-text message. Raises MailError (not configured, or
    the server refused / couldn't be reached)."""
    if not configured():
        raise MailError("email is not configured (BALER_SMTP_HOST / BALER_SMTP_FROM)")
    recipients = [a.strip() for a in to if a and a.strip()]
    if not recipients:
        raise MailError("no recipients")
    msg = build(recipients, subject, body)
    host = _env("BALER_SMTP_HOST")
    tls = (_env("BALER_SMTP_TLS") or "starttls").lower()
    port = int(_env("BALER_SMTP_PORT") or ("465" if tls == "ssl" else "587" if tls == "starttls" else "25"))
    user, password = _env("BALER_SMTP_USER"), os.environ.get("BALER_SMTP_PASSWORD", "")
    context = ssl.create_default_context()
    try:
        smtp: smtplib.SMTP
        if tls == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_SECONDS, context=context)
        else:
            smtp = smtplib.SMTP(host, port, timeout=TIMEOUT_SECONDS)
        with smtp:
            if tls == "starttls":
                smtp.starttls(context=context)
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        raise MailError(f"{type(exc).__name__}: {exc}") from exc

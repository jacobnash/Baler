"""
Outbound webhook delivery - HMAC-signed, retried, timed out. Runs off the
MQTT callback thread (see fault_detector.py): a slow/dead URL must never
stall fault evaluation for every other point behind it.
"""

import hashlib
import hmac
import ipaddress
import json
import socket
import time
from urllib.parse import urlparse

import requests

TIMEOUT_SECONDS = 5.0
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0


def sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def validate_url(url: str, allow_private: bool = False) -> None:
    """Raises ValueError if `url` isn't a safe webhook delivery target.
    `allow_private` is off by default (production/registration path) and
    only ever flipped on explicitly by tests hitting a local receiver -
    without it, every resolved address must be a public, non-reserved IP
    and the scheme must be https. Re-run on every delivery attempt, not
    just at registration: a hostname that resolves safely at registration
    time could resolve to 127.0.0.1 by the time a retry fires (DNS
    rebinding), and only re-checking at send time actually closes that."""
    parsed = urlparse(url)
    allowed_schemes = ("http", "https") if allow_private else ("https",)
    if parsed.scheme not in allowed_schemes or not parsed.hostname:
        raise ValueError(f"webhook url must use {'/'.join(allowed_schemes)} with a host: {url!r}")

    try:
        addrs = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"cannot resolve webhook host {parsed.hostname!r}: {exc}") from exc

    if allow_private:
        return
    for family, _, _, _, sockaddr in addrs:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValueError(f"webhook host {parsed.hostname!r} resolves to a non-public address ({ip}) - not allowed")


def matches_filter(webhook: dict, rule_id: str) -> bool:
    """No filter (missing/empty/None) means 'every fault' - the safe,
    permissive default for a webhook that didn't ask to be scoped."""
    filt = webhook.get("filter")
    if not filt:
        return True
    filter_rule_id = filt.get("rule_id")
    return filter_rule_id is None or filter_rule_id == rule_id


def deliver(
    url: str,
    secret: str,
    payload: dict,
    max_attempts: int = MAX_ATTEMPTS,
    backoff_base_seconds: float = BACKOFF_BASE_SECONDS,
    timeout_seconds: float = TIMEOUT_SECONDS,
    allow_private: bool = False,
) -> tuple[bool, str | None]:
    """Signs the EXACT raw bytes sent, not a re-serialized dict - a real,
    common signing bug: serializing the same dict twice isn't guaranteed
    byte-identical (key order, float formatting), so the signature and
    the body must come from one serialization, sent as raw `data=`, not
    `json=` (which would re-serialize internally). Returns
    (success, last_error) - the caller decides what to do with a
    failure (fault_detector.py records it via faults.record_webhook_failure)."""
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Baler-Signature": sign(secret, body)}

    last_error = None
    for attempt in range(max_attempts):
        if attempt > 0:
            time.sleep(backoff_base_seconds * (2 ** (attempt - 1)))
        try:
            validate_url(url, allow_private=allow_private)
            resp = requests.post(url, data=body, headers=headers, timeout=timeout_seconds)
            resp.raise_for_status()
            return True, None
        except ValueError as exc:
            # Not a transient network failure - the target is disallowed
            # (or has just become disallowed via DNS rebinding). Retrying
            # won't fix a static scheme/host check, so stop immediately
            # instead of burning the remaining attempts and backoff sleeps.
            return False, str(exc)
        except requests.RequestException as exc:
            last_error = str(exc)

    return False, last_error

"""
TOTP (RFC 6238) on HOTP (RFC 4226), stdlib only (#278). Small enough to
read in one sitting, and checked against both RFCs' published test vectors
in tests/test_totp.py. SHA-1, 6 digits, 30-second steps: the parameters
every authenticator app supports by default.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import time
from urllib.parse import quote, urlencode

DIGITS = 6
STEP_SECONDS = 30
# Codes from this many steps either side of now are accepted too: a phone
# clock a little off, or a code typed just as it rolled over.
WINDOW = 1


def new_secret() -> str:
    """A fresh 160-bit secret (RFC 4226's recommended length), base32."""
    return base64.b32encode(os.urandom(20)).decode("ascii")


def _key(secret_b32: str) -> bytes:
    s = secret_b32.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def hotp(key: bytes, counter: int, digits: int = DIGITS, digest=hashlib.sha1) -> str:
    """RFC 4226 section 5.3: HMAC the 8-byte counter, dynamic truncation."""
    mac = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def step(at: float | None = None) -> int:
    return int((time.time() if at is None else at) // STEP_SECONDS)


def code_at(secret_b32: str, at: float | None = None) -> str:
    return hotp(_key(secret_b32), step(at))


def match(secret_b32: str, code: str, at: float | None = None, after_step: int | None = None) -> int | None:
    """The time step `code` is valid for (within WINDOW of now), or None.
    `after_step`: the last step already used; that step and earlier ones
    are refused, so each code works once."""
    code = "".join(str(code).split())
    if len(code) != DIGITS or not code.isdigit():
        return None
    try:
        key = _key(secret_b32)
    except (ValueError, TypeError):
        return None
    now = step(at)
    found = None
    for s in range(now - WINDOW, now + WINDOW + 1):
        # compare every candidate (no early exit), constant time each
        if hmac.compare_digest(hotp(key, s), code) and (after_step is None or s > after_step):
            found = s
    return found


def provisioning_uri(secret_b32: str, account: str, issuer: str = "Baler") -> str:
    """otpauth:// URI an authenticator app adds from a QR code or a tap."""
    label = quote(f"{issuer}:{account}")
    query = urlencode({"secret": secret_b32, "issuer": issuer, "algorithm": "SHA1", "digits": DIGITS,
                       "period": STEP_SECONDS})
    return f"otpauth://totp/{label}?{query}"

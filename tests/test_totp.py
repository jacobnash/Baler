"""baler.totp against the published test vectors (#278)."""

import base64
import hashlib

import pytest

from baler import totp

# RFC 4226 appendix D: secret "12345678901234567890", counters 0-9.
RFC4226 = ["755224", "287082", "359152", "969429", "338314", "254676", "287922", "162583", "399871", "520489"]
SECRET = b"12345678901234567890"
SECRET_B32 = base64.b32encode(SECRET).decode()  # gitleaks:allow - RFC 4226/6238 published test secret


@pytest.mark.parametrize(("counter", "expected"), list(enumerate(RFC4226)))
def test_hotp_rfc4226_vectors(counter, expected):
    assert totp.hotp(SECRET, counter) == expected


# RFC 6238 appendix B, SHA-1 column (8 digits).
RFC6238 = [(59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
           (1234567890, "89005924"), (2000000000, "69279037"), (20000000000, "65353130")]


@pytest.mark.parametrize(("t", "expected"), RFC6238)
def test_totp_rfc6238_vectors(t, expected):
    assert totp.hotp(SECRET, t // 30, digits=8, digest=hashlib.sha1) == expected


def test_match_accepts_the_window_and_refuses_replay():
    t = 1111111111
    code = totp.code_at(SECRET_B32, t)
    used = totp.match(SECRET_B32, code, at=t)
    assert used == t // 30
    assert totp.match(SECRET_B32, code, at=t + 30) == used  # one step late still counts
    assert totp.match(SECRET_B32, code, at=t + 95) is None  # too late
    assert totp.match(SECRET_B32, code, at=t, after_step=used) is None  # already used
    assert totp.match(SECRET_B32, code[:3] + " " + code[3:], at=t) == used  # spaces typed are fine


@pytest.mark.parametrize("bad", ["", "12345", "1234567", "abcdef", None])
def test_match_rejects_malformed(bad):
    assert totp.match(SECRET_B32, bad, at=59) is None


def test_new_secret_and_uri():
    s = totp.new_secret()
    assert len(base64.b32decode(s)) == 20 and s != totp.new_secret()
    uri = totp.provisioning_uri(s, "ana@example.org")
    assert uri.startswith("otpauth://totp/Baler%3Aana%40example.org?") and f"secret={s}" in uri and "issuer=Baler" in uri

"""
Live HTTP delivery against a real local receiver (same ThreadingHTTPServer
scaffold as test_openapi.py's live_server fixture, with a handler that
just records what it got) - proves the signature verifies against the raw
bytes actually received, not just that sign() and deliver() individually
look right in isolation.
"""

import hashlib
import hmac
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from baler.webhooks import deliver, matches_filter, sign, validate_url


def test_sign_produces_a_known_hmac_sha256_hex_digest():
    secret = "shh"
    body = b'{"event":"fault.opened"}'
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    assert sign(secret, body) == expected


def test_matches_filter_no_filter_matches_every_rule():
    assert matches_filter({"filter": None}, "any-rule") is True
    assert matches_filter({}, "any-rule") is True


def test_matches_filter_scoped_to_one_rule_id():
    webhook = {"filter": {"rule_id": "high-temp"}}
    assert matches_filter(webhook, "high-temp") is True
    assert matches_filter(webhook, "some-other-rule") is False


def _make_receiver():
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            received.append({"body": body, "signature": self.headers.get("X-Baler-Signature")})
            self.send_response(200)
            self.end_headers()

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, received


@pytest.mark.integration
def test_deliver_success_signature_verifies_against_received_bytes():
    server, received = _make_receiver()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        secret = "webhook-secret"
        payload = {"event": "fault.opened", "rule_id": "high-temp", "point_uri": "urn:point:zone-1"}

        success, error = deliver(url, secret, payload, allow_private=True)

        assert success is True
        assert error is None
        assert len(received) == 1
        expected_signature = sign(secret, received[0]["body"])
        assert received[0]["signature"] == expected_signature
        assert json.loads(received[0]["body"]) == payload
    finally:
        server.shutdown()


@pytest.mark.integration
def test_deliver_retries_then_reports_failure_against_an_unreachable_port():
    # bind and immediately close a port to get one nothing is listening on
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    success, error = deliver(
        f"http://127.0.0.1:{port}/",
        "secret",
        {"event": "fault.opened"},
        max_attempts=3,
        backoff_base_seconds=0.05,  # fast test, real retries still happen
        timeout_seconds=1.0,
        allow_private=True,
    )

    assert success is False
    assert error is not None


def test_validate_url_rejects_non_https_by_default():
    with pytest.raises(ValueError):
        validate_url("http://example.com/hook")


def test_validate_url_rejects_loopback_and_private_targets():
    with pytest.raises(ValueError):
        validate_url("https://localhost/hook")
    with pytest.raises(ValueError):
        validate_url("https://127.0.0.1/hook")
    with pytest.raises(ValueError):
        validate_url("https://10.0.0.5/hook")


def test_validate_url_allow_private_permits_loopback_http():
    validate_url("http://127.0.0.1:9999/hook", allow_private=True)  # must not raise


def test_deliver_rejects_disallowed_url_without_retrying():
    success, error = deliver("http://127.0.0.1:9999/hook", "secret", {"event": "fault.opened"})

    assert success is False
    assert "https" in error or "non-public" in error or "resolve" in error

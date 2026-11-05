"""
Fast pure-unit tests for the Haystack JSON scalar decoder, plus live SCRAM
round-trips against a hand-rolled fake Haxall server (same ThreadingHTTPServer
scaffold as test_webhooks.py's _make_receiver()) - the fake server derives
its own SCRAM math independently from the same stdlib primitives, so a
passing round-trip actually proves the client's crypto interops with a
correct implementation, not just that it doesn't crash.

test_about_against_a_real_running_haxall_instance is the one genuine e2e
test: real network calls to a real Haxall/SkySpark server, skipped unless
someone points it at one via the same HAXALL_OSS_URL/HAXALL_SU_USERNAME/
HAXALL_SU_PASSWORD env vars e2e-haxall/ already uses (no Haxall instance is
provisioned by this repo's docker-compose.yml).
"""

import os

import pytest

from baler.fake_haystack_server import (
    TEST_PASSWORD,
    TEST_USER,
)
from baler.fake_haystack_server import (
    make_fake_haxall_server as _make_fake_haxall_server,
)
from baler.haystack_client import (
    HaystackAuthError,
    HaystackClient,
    _decode_haystack_scalar,
)


def test_decode_haystack_scalar_marker():
    assert _decode_haystack_scalar("m:") is True


def test_decode_haystack_scalar_number_with_unit():
    assert _decode_haystack_scalar("n:71.5 °F") == 71.5


def test_decode_haystack_scalar_ref_drops_display_name():
    assert _decode_haystack_scalar("r:abc123 AHU-1 Discharge Temp") == "abc123"


def test_decode_haystack_scalar_datetime_becomes_epoch_seconds():
    from datetime import datetime, timezone

    expected = datetime(2026, 8, 24, 0, 0, 0, tzinfo=timezone.utc).timestamp()
    assert _decode_haystack_scalar("t:2026-08-24T00:00:00Z UTC") == expected


def test_decode_haystack_scalar_passes_through_native_json_types():
    assert _decode_haystack_scalar(True) is True
    assert _decode_haystack_scalar("plain string, no kind prefix") == "plain string, no kind prefix"


@pytest.mark.integration
def test_about_succeeds_after_full_scram_handshake():
    server, _ = _make_fake_haxall_server({"about": {"rows": [{"productName": "FakeHaxall", "version": "n:4.0"}]}})
    try:
        client = HaystackClient(f"http://127.0.0.1:{server.server_address[1]}", TEST_USER, TEST_PASSWORD)
        result = client.about()
        assert result["productName"] == "FakeHaxall"
        assert result["version"] == 4.0
    finally:
        server.shutdown()


@pytest.mark.integration
def test_read_and_his_read_reuse_the_authenticated_session():
    server, _ = _make_fake_haxall_server(
        {
            "read": {"rows": [{"id": "r:p1 Zone Temp", "curVal": "n:71.0"}]},
            "hisRead": {"rows": [{"ts": "t:2026-08-24T00:00:00Z UTC", "val": "n:71.5"}]},
        }
    )
    try:
        client = HaystackClient(f"http://127.0.0.1:{server.server_address[1]}", TEST_USER, TEST_PASSWORD)
        points = client.read("point and temp")
        assert points[0]["id"] == "p1"
        history = client.his_read("@p1", "today")
        assert history[0]["val"] == 71.5
    finally:
        server.shutdown()


@pytest.mark.integration
def test_wrong_password_raises_a_clear_auth_error():
    server, _ = _make_fake_haxall_server()
    try:
        client = HaystackClient(f"http://127.0.0.1:{server.server_address[1]}", TEST_USER, "wrong-password")
        with pytest.raises(HaystackAuthError):
            client.about()
    finally:
        server.shutdown()


@pytest.mark.integration
def test_revoked_bearer_token_triggers_one_reauth_then_succeeds():
    server, bearer_tokens = _make_fake_haxall_server({"about": {"rows": [{"productName": "FakeHaxall"}]}})
    try:
        client = HaystackClient(f"http://127.0.0.1:{server.server_address[1]}", TEST_USER, TEST_PASSWORD)
        client.about()
        bearer_tokens.clear()  # simulate the server expiring/revoking the token
        result = client.about()  # should silently re-auth once and succeed
        assert result["productName"] == "FakeHaxall"
    finally:
        server.shutdown()


HAXALL_OSS_URL = os.environ.get("HAXALL_OSS_URL", "http://localhost:8280")
HAXALL_SU_USERNAME = os.environ.get("HAXALL_SU_USERNAME")
HAXALL_SU_PASSWORD = os.environ.get("HAXALL_SU_PASSWORD", "")


@pytest.mark.integration
@pytest.mark.skipif(
    not HAXALL_SU_USERNAME,
    reason="requires a real running Haxall instance - set HAXALL_SU_USERNAME (and HAXALL_SU_PASSWORD, "
    "HAXALL_OSS_URL if not localhost:8280) to the same env vars e2e-haxall/ uses to run this",
)
def test_about_against_a_real_running_haxall_instance():
    client = HaystackClient(HAXALL_OSS_URL, HAXALL_SU_USERNAME, HAXALL_SU_PASSWORD)
    result = client.about()
    assert "productName" in result

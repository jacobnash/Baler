"""
Unit test for mqtt_listener.py's reconnect fix: on_connect must re-issue
the subscribe on every call, not just the first one - a fresh (non
clean_session) broker session drops the old subscription on every
reconnect, and there's no other code path that restores it.
"""

import pytest

from baler.mqtt_listener import make_on_connect


class FakeClient:
    def __init__(self):
        self.subscribe_calls = []

    def subscribe(self, topic, qos=0):
        self.subscribe_calls.append((topic, qos))


def test_on_connect_subscribes():
    client = FakeClient()
    on_connect = make_on_connect("fbf/#")

    on_connect(client, None, {}, 0, None)

    assert client.subscribe_calls == [("fbf/#", 1)]


def test_on_connect_resubscribes_on_every_call():
    client = FakeClient()
    on_connect = make_on_connect("fbf/#")

    on_connect(client, None, {}, 0, None)  # first connect
    on_connect(client, None, {}, 0, None)  # simulated reconnect

    assert client.subscribe_calls == [("fbf/#", 1), ("fbf/#", 1)]


class _Msg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload


@pytest.mark.parametrize("payload", [b"1", b"[1, 2]", b'"text"', b"null", b"not json", b"\xff\xfe"])
def test_a_malformed_message_is_skipped_not_fatal(payload):
    """A crash here crash-loops the listener: its persistent session
    redelivers the same message after every restart (CI, 2026-11-06)."""
    from baler.mqtt_listener import make_on_message

    on_message = make_on_message(store=None, ts_conn=None)
    on_message(None, None, _Msg("fbf/site/ahu/sat", payload))  # must not raise


def test_a_data_error_while_handling_is_skipped(monkeypatch):
    from baler import mqtt_listener

    def boom(*a, **k):
        raise ValueError("bad value")

    monkeypatch.setattr(mqtt_listener, "ingest_reading", boom, raising=False)
    on_message = mqtt_listener.make_on_message(store=None, ts_conn=None)
    on_message(None, None, _Msg("fbf/site/ahu/sat", b'{"value": 1, "ts": 1}'))  # logged, not raised

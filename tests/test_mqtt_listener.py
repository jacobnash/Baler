"""
Unit test for mqtt_listener.py's reconnect fix: on_connect must re-issue
the subscribe on every call, not just the first one - a fresh (non
clean_session) broker session drops the old subscription on every
reconnect, and there's no other code path that restores it.
"""

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

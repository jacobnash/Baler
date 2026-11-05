"""point_write.Commander: publish a point_write command to the owning site's
topic and return only when that command's own result arrives."""

import json
import threading
import time

import pytest

from baler import point_write as pw


class FakeBroker:
    """Stands in for paho + a Windrow instance answering on its result topic."""

    def __init__(self, reply=None, delay=0.0):
        self.reply, self.delay, self.published = reply, delay, []
        self.on_message = None

    def factory(self, on_message):
        self.on_message = on_message
        return self

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, json.loads(payload)))
        if self.reply is None:
            return
        cmd = json.loads(payload)

        def answer():
            time.sleep(self.delay)
            site_inst = topic.rsplit("/", 1)[0]

            class Msg:
                pass

            # an unrelated result first: must not release this caller
            stray = Msg()
            stray.topic, stray.payload = f"{site_inst}/result", json.dumps({"ok": True, "id": "someone-else"}).encode()
            self.on_message(None, None, stray)
            msg = Msg()
            msg.topic, msg.payload = f"{site_inst}/result", json.dumps({**self.reply, "id": cmd["id"]}).encode()
            self.on_message(None, None, msg)

        threading.Thread(target=answer, daemon=True).start()


def test_publishes_to_owning_site_and_returns_its_result():
    broker = FakeBroker(reply={"ok": True, "result": {"present_value": 72.0}}, delay=0.05)
    c = pw.Commander(client_factory=broker.factory, timeout=2, ttl=30)
    assert c.point_write("site-a", "i1", "fbf/a/sp", 72.0, 8) == {"present_value": 72.0}
    topic, cmd = broker.published[0]
    assert topic == "cmd/site-a/i1/point-write"
    assert cmd["action"] == "point_write" and cmd["priority"] == 8 and cmd["value"] == 72.0
    assert time.time() < cmd["expires_at"] <= time.time() + 30


def test_refusal_raises():
    broker = FakeBroker(reply={"ok": False, "error": "fbf/a/dat is a analog-input, which is not commandable"})
    c = pw.Commander(client_factory=broker.factory, timeout=2)
    with pytest.raises(pw.CommandError, match="not commandable"):
        c.point_write("site-a", "i1", "fbf/a/dat", 1.0, 8)


def test_silence_times_out_and_says_the_command_expires():
    c = pw.Commander(client_factory=FakeBroker(reply=None).factory, timeout=0.1, ttl=30)
    with pytest.raises(pw.CommandError, match="expires after 30s"):
        c.point_write("site-a", "i1", "fbf/a/sp", 1.0, 8)

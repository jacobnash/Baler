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


def test_targeted_command_keeps_its_target_id_and_correlates_on_request_id():
    """update_connection's `id` is the connection; the reply is matched by
    request_id (a new Windrow echoes both)."""
    class Echo(FakeBroker):
        def publish(self, topic, payload, qos=0):
            cmd = json.loads(payload)
            self.published.append((topic, cmd))

            class Msg:
                pass

            def answer():
                m = Msg()
                m.topic = topic.rsplit("/", 1)[0] + "/result"
                m.payload = json.dumps({"ok": True, "result": {"id": cmd["id"]}, "id": cmd["id"],
                                        "request_id": cmd["request_id"]}).encode()
                self.on_message(None, None, m)

            threading.Thread(target=answer, daemon=True).start()

    broker = Echo(reply={})
    c = pw.Commander(client_factory=broker.factory, timeout=2)
    assert c.command("home", "pi-1", "update_connection", {"id": "conn-7", "poll_interval": 10}) == {"id": "conn-7"}
    topic, cmd = broker.published[0]
    assert topic == "cmd/home/pi-1/update-connection" and cmd["id"] == "conn-7" and cmd["request_id"] != "conn-7"


def test_status_heartbeats_are_tracked_and_cleared():
    broker = FakeBroker(reply=None)
    c = pw.Commander(client_factory=broker.factory)
    assert c.statuses(settle=0) == {}

    class Msg:
        pass

    m = Msg()
    m.topic, m.payload = "cmd/home/pi-1/status", json.dumps({"online": True, "devices": 2}).encode()
    broker.on_message(None, None, m)
    got = c.statuses(settle=0)[("home", "pi-1")]
    assert got["online"] is True and got["devices"] == 2 and got["received_at"] > 0
    m.payload = b""
    broker.on_message(None, None, m)
    assert c.statuses(settle=0) == {}

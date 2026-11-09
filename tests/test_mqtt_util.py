"""mqtt_util.publish_confirmed: a one-shot publish that returns only once the
broker has the messages (the e2e tests' publishers used to lose them)."""

import json
import socket
import threading
import time

import pytest

from baler.mqtt_util import make_client, publish_confirmed


def test_publish_confirmed_raises_when_the_broker_never_acknowledges():
    # Accepts the TCP connection and never answers: no CONNACK, no PUBACK.
    # Fire-and-forget would have "succeeded" here; this must not.
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    held = []
    threading.Thread(target=lambda: held.append(server.accept()), daemon=True).start()
    try:
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            publish_confirmed("127.0.0.1", server.getsockname()[1], [("fbf/x/y", "1")], timeout=0.5)
        assert time.monotonic() - t0 < 5
    finally:
        server.close()
        for conn, _ in held:
            conn.close()


@pytest.mark.integration
def test_publish_confirmed_messages_are_on_the_broker_when_it_returns():
    """Every retained message is there for a subscriber that arrives after
    publish_confirmed returns - the guarantee the real-broker e2e tests
    rely on. Many back-to-back sessions, the shape that used to drop some."""
    run = f"fbf/test-mqtt-util-{int(time.time() * 1000)}"
    topics = [f"{run}/s{i}/{leaf}" for i in range(25) for leaf in ("a", "b", "a/tags", "b/tags")]
    for i in range(25):
        publish_confirmed("localhost", 1883, [(t, json.dumps({"value": n})) for n, t in enumerate(topics[4 * i:4 * i + 4])],
                          retain=True)

    got: set[str] = set()
    sub = make_client()
    sub.on_message = lambda c, u, m: got.add(m.topic)
    sub.connect("localhost", 1883)
    sub.subscribe(f"{run}/#", qos=1)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and len(got) < len(topics):
        sub.loop(timeout=0.1)
    sub.disconnect()
    try:
        assert got == set(topics)
    finally:
        publish_confirmed("localhost", 1883, [(t, b"") for t in topics], retain=True)

"""
Shared MQTT client construction for every baler-side daemon that
talks to the broker directly (mqtt_listener.py, fault_detector.py,
derivation_engine.py, and the integration tests that stand in for them).
One place to add credentials once mosquitto stops allowing anonymous
clients, instead of patching each call site separately.
"""

import os
from typing import Any

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

# MQTT v5's "never expire" session expiry interval: a persistent session
# that outlives any disconnect, as clean_session=False did in 3.1.1.
SESSION_NEVER_EXPIRES = 0xFFFFFFFF


def make_client(client_id: str | None = None, clean_session: bool | None = None,
                manual_ack: bool = False, v5: bool = False) -> mqtt.Client:
    """manual_ack: QoS 1 messages are acknowledged only by client.ack(mid,
    qos), not when on_message returns - for a client that stores messages
    after on_message (mqtt_listener's batches) and must not tell the broker
    a message is safe before it is.

    v5: speak MQTT 5 (mqtt_listener's shared-subscription workers). A v5
    client's session persistence is chosen at connect time (connect_v5),
    so clean_session doesn't apply to it."""
    kwargs: dict[str, Any] = {}
    if manual_ack:
        kwargs["manual_ack"] = True
    if client_id is not None:
        kwargs["client_id"] = client_id
    if v5:
        kwargs["protocol"] = mqtt.MQTTv5
    elif clean_session is not None:
        kwargs["clean_session"] = clean_session
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, **kwargs)
    username = os.environ.get("MQTT_USERNAME")
    if username:
        client.username_pw_set(username, os.environ.get("MQTT_PASSWORD", ""))
    return client


def connect_v5(client: mqtt.Client, host: str, port: int, persistent: bool = True, keepalive: int = 60) -> None:
    """Connect a v5 client. persistent=True resumes (or starts) a session
    that never expires - the v5 spelling of clean_session=False with a
    stable client id. A v5 session with no expiry interval ends at the
    disconnect, which would drop every unacknowledged reading."""
    props = Properties(PacketTypes.CONNECT)
    props.SessionExpiryInterval = SESSION_NEVER_EXPIRES if persistent else 0
    client.connect(host, port, keepalive=keepalive, clean_start=not persistent, properties=props)


def disconnect_ending_session(client: mqtt.Client) -> None:
    """DISCONNECT with session expiry 0: the broker deletes this client's
    session now (its subscriptions and anything still queued for it)."""
    props = Properties(PacketTypes.DISCONNECT)
    props.SessionExpiryInterval = 0
    client.disconnect(properties=props)

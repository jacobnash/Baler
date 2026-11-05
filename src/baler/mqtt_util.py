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


def make_client(client_id: str | None = None, clean_session: bool | None = None) -> mqtt.Client:
    kwargs: dict[str, Any] = {}
    if client_id is not None:
        kwargs["client_id"] = client_id
    if clean_session is not None:
        kwargs["clean_session"] = clean_session
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, **kwargs)
    username = os.environ.get("MQTT_USERNAME")
    if username:
        client.username_pw_set(username, os.environ.get("MQTT_PASSWORD", ""))
    return client

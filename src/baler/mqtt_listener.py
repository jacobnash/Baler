"""
One daemon, every protocol. BACnet, Modbus, and native MQTT sources all
publish the same envelope (see Windrow's docs/adding-a-new-source.md), so one
listener subscribing to a topic pattern is the whole ingestion surface.
"""

import argparse
import json
import os

from baler import mqtt_util, tracing
from baler.ingest import (
    ingest_equip_tags,
    ingest_haystack_equip_tags,
    ingest_reading,
    ingest_tags,
    link_equip_ref,
    link_point_to_equip,
    topic_to_point_uri,
)
from baler.remote_store import RemoteStore
from baler.timeseries import connect

tracer = tracing.get_tracer(__name__)


def make_on_connect(topic_pattern):
    def on_connect(client, userdata, flags, reason_code, properties):
        with tracer.start_as_current_span("mqtt_listener.on_connect") as span:
            span.set_attribute("reason_code", str(reason_code))
            # Re-subscribing here (not just once before loop_forever()) is
            # required: a reconnect gets a fresh broker-side session unless
            # both sides agree on clean_session=False + a stable client_id,
            # and even then re-issuing the subscribe is what actually
            # restores delivery - on_connect fires on every reconnect, not
            # just the first connect.
            client.subscribe(topic_pattern, qos=1)
            print(f"connected ({reason_code}), subscribed to {topic_pattern}")

    return on_connect


def on_disconnect(client, userdata, flags, reason_code, properties=None):
    with tracer.start_as_current_span("mqtt_listener.on_disconnect") as span:
        span.set_attribute("reason_code", str(reason_code))
        print(f"disconnected ({reason_code}) - paho will auto-reconnect")


def make_on_message(store, ts_conn):
    def on_message(client, userdata, msg):
        """One bad message is logged and skipped, never fatal: this client
        has a persistent session, so a message that crashed it would be
        redelivered after every restart and stop ingest for good (a bare
        `1` on an fbf/ topic did exactly that in CI). Infrastructure errors
        (Postgres/Oxigraph down) still raise, so the container restarts."""
        try:
            _handle(msg)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            print(f"skipped bad message on {msg.topic}: {exc!r}")

    def _handle(msg):
        with tracer.start_as_current_span("mqtt_listener.on_message") as span:
            span.set_attribute("topic", msg.topic)
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                span.set_attribute("error", str(exc))
                print(f"malformed payload on {msg.topic}: {exc!r}")
                return
            if not isinstance(payload, dict):
                span.set_attribute("error", "payload is not a JSON object")
                print(f"malformed payload on {msg.topic}: expected a JSON object, got {type(payload).__name__}")
                return

            value = payload.get("value")
            ts = payload.get("ts")

            # BACnet/Modbus-sourced equip: one message per connection,
            # {"tags": {...}, "points": [label, ...]} - equip identity is
            # the connection's own topic_prefix (see
            # ingest.topic_prefix_to_equip_uri), hasPoint is asserted
            # directly since the point list is already known at publish
            # time (windrow's connection_manager._tag_points sibling call).
            if msg.topic.endswith("/equip/tags"):
                topic_prefix = msg.topic[: -len("/equip/tags")]
                try:
                    data = json.loads(value)
                except (json.JSONDecodeError, TypeError) as exc:
                    span.set_attribute("error", str(exc))
                    print(f"malformed equip payload on {msg.topic}: {exc!r}")
                    return
                equip_uri = ingest_equip_tags(store, topic_prefix, data.get("tags", {}))
                for label in data.get("points", []):
                    link_point_to_equip(store, topic_to_point_uri(f"{topic_prefix}/{label}"), equip_uri)
                return

            # Haystack-sourced equip: one message per equip/site rec,
            # {topic_prefix}/equip/{haystackRef}/tags - equip identity is
            # the rec's own Haystack id (see ingest.haystack_ref_to_equip_uri),
            # matching link_equip_ref's resolution on the point side; no
            # points list needed here since points link themselves via
            # their own equipRef tag, not the equip side pushing membership.
            if "/equip/" in msg.topic and msg.topic.endswith("/tags"):
                ref = msg.topic[: -len("/tags")].rsplit("/equip/", 1)[1]
                try:
                    tags = json.loads(value)
                except (json.JSONDecodeError, TypeError) as exc:
                    span.set_attribute("error", str(exc))
                    print(f"malformed haystack equip payload on {msg.topic}: {exc!r}")
                    return
                ingest_haystack_equip_tags(store, ref, tags)
                return

            if msg.topic.endswith("/tags"):
                real_topic = msg.topic[: -len("/tags")]
                try:
                    tags = json.loads(value)
                except (json.JSONDecodeError, TypeError) as exc:
                    span.set_attribute("error", str(exc))
                    print(f"malformed tags payload on {msg.topic}: {exc!r}")
                    return
                point_uri = ingest_tags(store, real_topic, tags, ts)
                # No-op unless this point carries an equipRef and the
                # matching equip's own tags have already landed - safe to
                # call unconditionally on every tag message, not just the
                # first one (a point re-tagged after its equip arrives
                # should still get linked).
                link_equip_ref(store, point_uri)
                return

            ingest_reading(store, ts_conn, msg.topic, value, ts)

    return on_message


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST", "localhost"))
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--topic-pattern", default="fbf/#", help="MQTT subscription filter, e.g. fbf/#")
    args = parser.parse_args()

    tracing.init_tracing("baler-mqtt-listener")

    store = RemoteStore()
    ts_conn = connect()

    # clean_session=False + a stable client_id makes the session (and
    # mosquitto's default queued-message retention, ~1000 messages) survive
    # both a mid-process reconnect and a listener restart, instead of every
    # run getting a fresh random id and a fresh (empty) broker-side session.
    client = mqtt_util.make_client(client_id="baler-mqtt-listener", clean_session=False)
    client.on_connect = make_on_connect(args.topic_pattern)
    client.on_disconnect = on_disconnect
    client.on_message = make_on_message(store, ts_conn)
    client.connect(args.mqtt_host, args.mqtt_port)
    print(f"listening on {args.topic_pattern} @ {args.mqtt_host}:{args.mqtt_port}")
    client.loop_forever()


if __name__ == "__main__":
    main()

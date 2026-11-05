"""
Thin MQTT publisher for a local weather station - "add your own" station
instead of relying on a WWW weather API. Wraps mqtt_listener.py's real
topic-suffix envelope (equip/tags, <point>/tags, <point> readings) so a
real device integration only has to call publish_reading() in its own
poll loop; it doesn't need to know the envelope's double-JSON-encoding.

Not a daemon - a library of three functions plus a `--demo` CLI that
publishes one made-up station so you can see the shape end-to-end without
real hardware. Your real integration imports these functions and replaces
the demo's fake sensor reads with real ones; MQTT_HOST is Baler's
own local broker (docker-compose.yml's `mosquitto` service), so once your
device is on the same network this never touches the WWW.

Topic prefix defaults to `fbf/weather/<station-id>` - `mqtt_listener.py`
only subscribes to `fbf/#` by default (docker-compose.yml's mqtt_listener
service passes no --topic-pattern override), so publishing under any other
top-level prefix silently goes nowhere. Not actually Windrow-specific data;
just reusing the one topic filter the listener is already watching.

See docs-site/pages/weather-stations.mdx for the tag vocabulary
(weatherStation, weather + <metric> markers) these functions assume.
"""

import argparse
import json
import os
import time

import paho.mqtt.client as mqtt


def publish_equip_tags(client: mqtt.Client, topic_prefix: str, tags: dict, point_labels: list[str]) -> None:
    payload = {"tags": tags, "points": point_labels}
    client.publish(f"{topic_prefix}/equip/tags", json.dumps({"value": json.dumps(payload)}), retain=True)


def publish_point_tags(client: mqtt.Client, topic_prefix: str, label: str, tags: dict) -> None:
    client.publish(f"{topic_prefix}/{label}/tags", json.dumps({"value": json.dumps(tags)}), retain=True)


def publish_reading(client: mqtt.Client, topic_prefix: str, label: str, value, ts: float | None = None) -> None:
    client.publish(f"{topic_prefix}/{label}", json.dumps({"value": value, "ts": ts}))


# Demo points: real Haystack v4 weatherStation protos (project-haystack.org/
# doc/lib-phIoT/weatherStation) - same markers rules/haystack_to_brick.yaml
# classifies. Swap the fixed values for real sensor reads in your own loop.
_DEMO_POINTS = {
    "temp": ({"weather": True, "air": True, "temp": True, "sensor": True, "unit": "°F"}, 68.4),
    "humidity": ({"weather": True, "air": True, "humidity": True, "sensor": True, "unit": "%"}, 47.0),
    "wind-speed": ({"weather": True, "wind": True, "speed": True, "sensor": True, "unit": "mph"}, 6.2),
    "wind-direction": ({"weather": True, "wind": True, "direction": True, "sensor": True, "unit": "°"}, 210.0),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST", "localhost"))
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--station-id", default="backyard")
    parser.add_argument("--demo", action="store_true", help="publish one round of made-up readings and exit")
    args = parser.parse_args()

    if not args.demo:
        parser.error("no real device wiring here - pass --demo, or import publish_reading() into your own poll loop")

    topic_prefix = f"fbf/weather/{args.station_id}"
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.connect(args.mqtt_host, args.mqtt_port)
    client.loop_start()

    publish_equip_tags(client, topic_prefix, {"weatherStation": True, "dis": f"Weather Station ({args.station_id})"}, list(_DEMO_POINTS))
    for label, (tags, value) in _DEMO_POINTS.items():
        publish_point_tags(client, topic_prefix, label, tags)
        publish_reading(client, topic_prefix, label, value, ts=time.time())

    time.sleep(0.5)  # let the publishes flush before disconnecting
    client.loop_stop()
    client.disconnect()
    print(f"published demo weather station {args.station_id!r} on {topic_prefix}/*")


if __name__ == "__main__":
    main()

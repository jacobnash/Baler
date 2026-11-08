"""
One daemon, every protocol. BACnet, Modbus, and native MQTT sources all
publish the same envelope (see Windrow's docs/adding-a-new-source.md), so one
listener subscribing to a topic pattern is the whole ingestion surface.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import threading
import time
import traceback
from functools import lru_cache

from opentelemetry import trace

from baler import bacnet_alarms, mapping, mqtt_util, tracing
from baler.ingest import (
    ingest_equip_tags,
    ingest_haystack_equip_tags,
    ingest_reading,
    ingest_readings,
    ingest_tags,
    link_equip_ref,
    link_point_to_equip,
    topic_to_point_uri,
)
from baler.remote_store import RemoteStore
from baler.timeseries import connect

tracer = tracing.get_tracer(__name__)

# BACnet's BinaryPV states, as Windrow publishes a binary object's
# present-value (bacpypes3 Enumerated -> its name). Stored as a Bool, the
# way Haxall's BACnet connector models it: a text "active" got no chart, a
# text history list and no samples in Query rollups (Baler#81), while the
# same fan status over Modbus (0/1) charted fine. Multi-state values are
# unsigned integers in BACnet, so these two names mean binary only.
BINARY_STATES = {"active": True, "inactive": False}


def binary_state(value):
    """"active"/"inactive" -> True/False; anything else unchanged."""
    return BINARY_STATES.get(value, value) if isinstance(value, str) else value


@lru_cache(maxsize=2)
def _rules(path: str):
    return mapping.load_rules(path)


def _classify(store, uri, rules_path: str) -> None:
    """mapping.classify_on_ingest; a failure here never drops the tags
    message that was already stored (autotag can still classify it)."""
    try:
        cls = mapping.classify_on_ingest(store, uri, _rules(rules_path))
    except Exception as exc:  # noqa: BLE001 - classification is best-effort on this path
        print(f"auto-classify of {uri} skipped: {exc!r}")
        return
    if cls:
        print(f"auto-classified {uri} as {cls}")


def make_on_connect(topic_pattern, batcher: ReadingBatcher | None = None):
    def on_connect(client, userdata, flags, reason_code, properties):
        if batcher is not None:
            batcher.generation += 1
        with tracer.start_as_current_span("mqtt_listener.on_connect") as span:
            span.set_attribute("reason_code", str(reason_code))
            # Subscribing here (not just once before loop_forever()) is
            # required whenever the broker has no session for us - first
            # start, a broker that lost its state - since on_connect fires on
            # every (re)connect. But only then: a SUBSCRIBE makes the broker
            # resend every retained message under fbf/#, every point's tags
            # included, and handling that replay after each listener restart
            # held up readings until the broker's queue overflowed and it
            # dropped them (Baler#190: most of the readings acknowledged
            # during a kill -9 of the listener were lost). A resumed
            # persistent session (clean_session=False, stable client id)
            # still has its subscription and its queued backlog.
            present = flags.get("session present") if isinstance(flags, dict) else getattr(flags, "session_present", False)
            span.set_attribute("session_present", bool(present))
            if present:
                print(f"connected ({reason_code}), resumed session on {topic_pattern}", flush=True)
                return
            client.subscribe(topic_pattern, qos=1)
            print(f"connected ({reason_code}), subscribed to {topic_pattern}", flush=True)

    return on_connect


def on_disconnect(client, userdata, flags, reason_code, properties=None):
    with tracer.start_as_current_span("mqtt_listener.on_disconnect") as span:
        span.set_attribute("reason_code", str(reason_code))
        print(f"disconnected ({reason_code}) - paho will auto-reconnect")


class ReadingBatcher:
    """Readings, written in batches (Baler#190): on_message only queues a
    reading; this thread writes up to `max_rows` of them per transaction, at
    least every `max_wait` seconds, and only then acknowledges their MQTT
    messages. One round trip and one commit per batch instead of per
    reading - the per-reading INSERT round trip capped one listener at
    ~1,000 readings/s, with mosquitto dropping what queued past its limit.

    Nothing is acknowledged before it is committed (manual acks, durable
    commits), so a crash anywhere - this process, Postgres - leaves the
    unacknowledged messages with the broker, which redelivers them to this
    persistent session on reconnect; the writes are idempotent on
    (point, ts). The queue is bounded: when it's full, on_message blocks,
    paho stops reading the socket, and the backlog waits at the broker.
    """

    def __init__(self, client, store, ts_conn, max_rows: int = 5000, max_wait: float = 0.25, max_queued: int = 50_000):
        self.client, self.store, self.ts_conn = client, store, ts_conn
        self.max_rows, self.max_wait = max_rows, max_wait
        self.queue: queue.Queue = queue.Queue(maxsize=max_queued)
        # Bumped on every (re)connect: a message id is only meaningful on the
        # connection it arrived on (ids are reused), and the broker resends
        # everything unacknowledged after a reconnect anyway.
        self.generation = 0
        self.rows = 0
        self.batches = 0
        self.thread = threading.Thread(target=self._run, name="reading-batcher", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def put(self, msg, topic: str, value, ts) -> None:
        self.queue.put((self.generation, msg.mid, msg.qos, topic, value, ts))

    def _run(self) -> None:
        try:
            last_report, last_rows, last_batches = time.monotonic(), 0, 0
            while True:
                now = time.monotonic()
                if now - last_report >= 60:
                    # until /ops/status shows ingest lag and queue depth (#190)
                    n, b = self.rows - last_rows, self.batches - last_batches
                    print(f"ingest: {n / (now - last_report):.0f} readings/s, {b} batches "
                          f"(avg {n / max(b, 1):.0f} rows), queue {self.queue.qsize()}", flush=True)
                    last_report, last_rows, last_batches = now, self.rows, self.batches
                batch = [self.queue.get()]
                deadline = time.monotonic() + self.max_wait
                while len(batch) < self.max_rows:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    try:
                        batch.append(self.queue.get(timeout=left))
                    except queue.Empty:
                        break
                self.flush(batch)
        except Exception:  # noqa: BLE001 - infrastructure failure: exit, let the broker redeliver
            # Same contract as the old synchronous path (Postgres/Oxigraph
            # down -> the container restarts), from a thread: nothing in
            # this batch was acknowledged, so nothing is lost.
            traceback.print_exc()
            os._exit(1)

    def flush(self, batch: list) -> None:
        with tracer.start_as_current_span("mqtt_listener.flush") as span:
            span.set_attribute("readings", len(batch))
            ingest_readings(self.store, self.ts_conn, [(topic, value, ts) for _, _, _, topic, value, ts in batch])
            for gen, mid, qos, *_ in batch:
                if qos and gen == self.generation:
                    self.client.ack(mid, qos)
            self.rows += len(batch)
            self.batches += 1


def make_on_message(store, ts_conn, batcher: ReadingBatcher | None = None):
    """Without a batcher every reading is written before on_message returns
    (and acknowledged then, by paho); with one (main()), readings are
    queued for it and everything else is handled here and acknowledged
    explicitly - the client runs with manual acks."""

    def on_message(client, userdata, msg):
        """One bad message is logged and skipped, never fatal: this client
        has a persistent session, so a message that crashed it would be
        redelivered after every restart and stop ingest for good (a bare
        `1` on an fbf/ topic did exactly that in CI). Infrastructure errors
        (Postgres/Oxigraph down) still raise, so the container restarts."""
        try:
            queued = _handle(msg)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            print(f"skipped bad message on {msg.topic}: {exc!r}")
            queued = False
        if batcher is not None and not queued and msg.qos:
            client.ack(msg.mid, msg.qos)

    def _handle(msg):
        # A span per reading was ~30% of the listener's CPU at 2,000
        # readings/s (span creation plus the exporter encoding them, py-spy,
        # Baler#190). With a batcher a reading is traced as part of its
        # batch (mqtt_listener.flush); tags/equip messages keep their span.
        if batcher is not None and not msg.topic.endswith("/tags"):
            return _handle_in(msg, trace.INVALID_SPAN)
        with tracer.start_as_current_span("mqtt_listener.on_message") as span:
            span.set_attribute("topic", msg.topic)
            return _handle_in(msg, span)

    def _handle_in(msg, span):
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
            _classify(store, equip_uri, mapping.EQUIP_RULES_PATH)
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
            equip_uri = ingest_haystack_equip_tags(store, ref, tags)
            _classify(store, equip_uri, mapping.EQUIP_RULES_PATH)
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
            _classify(store, point_uri, mapping.DEFAULT_RULES_PATH)
            return

        # A controller alarm forwarded by Windrow (Baler#168) is not a
        # reading: fault_detector raises/clears it as a fault. Storing it
        # here would invent a `<label>/alarm` point with dicts for history.
        if bacnet_alarms.is_alarm(msg.topic, value):
            return False

        if batcher is not None:
            batcher.put(msg, msg.topic, binary_state(value), ts)
            return True
        ingest_reading(store, ts_conn, msg.topic, binary_state(value), ts)
        return False

    return on_message


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST", "localhost"))
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--topic-pattern", default="fbf/#", help="MQTT subscription filter, e.g. fbf/#")
    # Rows per transaction, and the longest a reading waits for its batch.
    # A batch can't usefully exceed the broker's max_inflight_messages
    # (mosquitto/mosquitto.conf): unacknowledged messages are in flight.
    parser.add_argument("--batch-rows", type=int, default=5000)
    parser.add_argument("--batch-ms", type=float, default=250.0)
    args = parser.parse_args()

    tracing.init_tracing("baler-mqtt-listener")

    store = RemoteStore()
    ts_conn = connect()

    # clean_session=False + a stable client_id makes the session (and
    # mosquitto's default queued-message retention, ~1000 messages) survive
    # both a mid-process reconnect and a listener restart, instead of every
    # run getting a fresh random id and a fresh (empty) broker-side session.
    # Manual acks: a reading is acknowledged once its batch is committed
    # (ReadingBatcher), so an unstored reading is never one the broker has
    # forgotten.
    client = mqtt_util.make_client(client_id="baler-mqtt-listener", clean_session=False, manual_ack=True)
    batcher = ReadingBatcher(client, store, ts_conn, max_rows=args.batch_rows, max_wait=args.batch_ms / 1000)
    client.on_connect = make_on_connect(args.topic_pattern, batcher)
    client.on_disconnect = on_disconnect
    client.on_message = make_on_message(store, ts_conn, batcher)
    batcher.start()
    client.connect(args.mqtt_host, args.mqtt_port)
    print(f"listening on {args.topic_pattern} @ {args.mqtt_host}:{args.mqtt_port}, "
          f"batches of up to {args.batch_rows} readings / {args.batch_ms} ms", flush=True)
    client.loop_forever()


if __name__ == "__main__":
    main()

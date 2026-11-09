"""
One daemon, every protocol. BACnet, Modbus, and native MQTT sources all
publish the same envelope (see Windrow's docs/adding-a-new-source.md), so one
listener subscribing to a topic pattern is the whole ingestion surface.

Scale-out (Baler#233): `python -m baler.mqtt_listener` is a supervisor. It
starts BALER_LISTENER_WORKERS worker processes (default 1), each an MQTT 5
client on the shared subscription `$share/baler/fbf/#` with its own stable
client id (baler-mqtt-listener-<n>) and a persistent session, so the broker
spreads messages across them and keeps each one's unacknowledged messages
for it across a crash. One process is one GIL: past ~15k readings/s the
listener was CPU-bound in paho's per-message work, not in Postgres. The
supervisor also starts the tag path (baler.tag_ingest), retires the
sessions of workers that no longer exist, watches the broker's drop counter
and restarts a child that exits. See docs-site/pages/scale-test.mdx.
"""

from __future__ import annotations

import argparse
import functools
import json
import multiprocessing as mp
import os
import queue
import signal
import threading
import time
import traceback
from datetime import datetime, timezone
from functools import lru_cache

from opentelemetry import trace

from baler import (
    bacnet_alarms,
    heartbeat,
    mapping,
    mqtt_util,
    quality,
    reading_feed,
    tag_ingest,
    tracing,
)
from baler.ingest import (
    ingest_reading,
    ingest_readings,
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

# Mark a queued controller alarm (ReadingBatcher.put_alarm) and a queued
# tags/equip message (put_tags) apart from a reading's quality flag.
ALARM = object()
TAGS = object()

# Client ids. Worker n is CLIENT_PREFIX-n, always: its persistent session at
# the broker (subscription, unacknowledged and queued messages) is found by
# that id after a crash. LEGACY_CLIENT_ID is the single listener before
# #233, with a plain (not shared) subscription.
CLIENT_PREFIX = "baler-mqtt-listener"
LEGACY_CLIENT_ID = CLIENT_PREFIX
RETAINED_CLIENT_ID = f"{CLIENT_PREFIX}-retained"
SHARE_GROUP = "baler"
# Sessions of worker ids from the worker count up to this are retired at
# start (see retire_session): a worker that no longer exists still gets its
# share of every message from the broker, queued until it overflows.
MAX_WORKERS = 32
HEARTBEAT_EVERY = 10.0


def worker_client_id(index: int) -> str:
    return f"{CLIENT_PREFIX}-{index}"


def shared(topic_pattern: str, group: str = SHARE_GROUP) -> str:
    return f"$share/{group}/{topic_pattern}"


def binary_state(value):
    """"active"/"inactive" -> True/False; anything else unchanged."""
    return BINARY_STATES.get(value, value) if isinstance(value, str) else value


@lru_cache(maxsize=2)
def _rules(path: str):
    return mapping.load_rules(path)


def make_on_connect(topic_pattern, batcher: ReadingBatcher | None = None, on_fresh=None):
    """`on_fresh(client)` runs when the broker had no session for this
    client (first start, or a broker that lost its state)."""

    def on_connect(client, userdata, flags, reason_code, properties):
        if batcher is not None:
            batcher.generation += 1
            batcher.connected = True
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
            # persistent session (stable client id) still has its
            # subscription and its queued backlog. (A shared subscription
            # gets no retained messages at all: see RetainedSnapshot.)
            present = flags.get("session present") if isinstance(flags, dict) else getattr(flags, "session_present", False)
            span.set_attribute("session_present", bool(present))
            if present:
                print(f"connected ({reason_code}), resumed session on {topic_pattern}", flush=True)
                return
            client.subscribe(topic_pattern, qos=1)
            print(f"connected ({reason_code}), subscribed to {topic_pattern}", flush=True)
            if on_fresh is not None:
                on_fresh(client)

    return on_connect


def make_on_disconnect(batcher: ReadingBatcher | None = None):
    def on_disconnect(client, userdata, flags, reason_code, properties=None):
        if batcher is not None:
            batcher.connected = False
        with tracer.start_as_current_span("mqtt_listener.on_disconnect") as span:
            span.set_attribute("reason_code", str(reason_code))
            print(f"disconnected ({reason_code}) - paho will auto-reconnect", flush=True)

    return on_disconnect


on_disconnect = make_on_disconnect()


class ReadingBatcher:
    """Readings, written in batches (Baler#190): on_message only queues a
    reading; this thread writes up to `max_rows` of them per transaction, at
    least every `max_wait` seconds, and only then acknowledges their MQTT
    messages. One round trip and one commit per batch instead of per
    reading - the per-reading INSERT round trip capped one listener at
    ~1,000 readings/s, with mosquitto dropping what queued past its limit.

    Tags/equip messages and controller alarms ride the same batch: with the
    reading feed on, they go into the batch's feed row in the same
    transaction (tags for baler.tag_ingest, alarms for fault_detector), so
    a tags storm costs this path a few JSON values per batch, never an
    Oxigraph round trip (#233).

    Nothing is acknowledged before it is committed (manual acks, durable
    commits), so a crash anywhere - this process, Postgres - leaves the
    unacknowledged messages with the broker, which redelivers them to this
    persistent session on reconnect; the writes are idempotent on
    (point, ts). The queue is bounded: when it's full, on_message blocks,
    paho stops reading the socket, and the backlog waits at the broker.
    That wait is counted (blocked_s): it's the first sign this worker can't
    keep up, before the broker's own queue limit drops anything.
    """

    def __init__(self, client, store, ts_conn, max_rows: int = 5000, max_wait: float = 0.25, max_queued: int = 50_000,
                 feed: bool = False, feed_retention_s: float = reading_feed.DEFAULT_RETENTION_S,
                 prune_every: float = 5.0, name: str = "reading-batcher"):
        self.client, self.store, self.ts_conn = client, store, ts_conn
        self.max_rows, self.max_wait = max_rows, max_wait
        # The reading feed (#243): each committed batch, readings and
        # controller alarms, is appended to reading_feed in the same
        # transaction, for fault_detector and derivation_engine to read
        # instead of a QoS 0 subscription of their own. prune_every 0: this
        # batcher doesn't prune (one pruner per listener, worker 0).
        self.feed, self.feed_retention_s, self.prune_every = feed, feed_retention_s, prune_every
        self._last_prune = 0.0
        self.queue: queue.Queue = queue.Queue(maxsize=max_queued)
        # Bumped on every (re)connect: a message id is only meaningful on the
        # connection it arrived on (ids are reused), and the broker resends
        # everything unacknowledged after a reconnect anyway.
        self.generation = 0
        self.connected = False
        # Counters. received is written by paho's thread only, done by this
        # thread only: received - done is what's queued or being written.
        self.received = 0
        self.done = 0
        self.rows = 0
        self.batches = 0
        self.tags = 0
        self.alarms = 0
        self.blocked_s = 0.0
        self.blocked_events = 0
        self.flush_s = 0.0
        self.last_message_at = 0.0
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _put(self, item) -> None:
        self.received += 1
        self.last_message_at = time.monotonic()
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            t0 = time.monotonic()
            self.queue.put(item)
            self.blocked_s += time.monotonic() - t0
            self.blocked_events += 1

    def put(self, msg, topic: str, value, ts, flag: str | None = None) -> None:
        self._put((self.generation, msg.mid, msg.qos, topic, value, ts, flag))

    def put_alarm(self, msg, topic: str, event, ts) -> None:
        """A controller alarm (Baler#168): not stored as a reading, but it
        rides the batch into the feed, acknowledged once that commits."""
        self._put((self.generation, msg.mid, msg.qos, topic, event, ts, ALARM))

    def put_tags(self, msg, topic: str, value, ts) -> None:
        """A tags/equip message (#233): into the batch's feed row for
        tag_ingest (or, without the feed, applied by this thread), and
        acknowledged once that commits."""
        self._put((self.generation, msg.mid, msg.qos, topic, value, ts, TAGS))

    @property
    def pending(self) -> int:
        return self.received - self.done

    def _run(self) -> None:
        try:
            last_report, last_rows, last_batches = time.monotonic(), 0, 0
            while True:
                now = time.monotonic()
                if now - last_report >= 60:
                    n, b = self.rows - last_rows, self.batches - last_batches
                    print(f"ingest: {n / (now - last_report):.0f} readings/s, {b} batches "
                          f"(avg {n / max(b, 1):.0f} rows), queue {self.queue.qsize()}, "
                          f"blocked {self.blocked_s:.1f} s in all", flush=True)
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
                if self.feed and self.prune_every and time.monotonic() - self._last_prune >= self.prune_every:
                    self._last_prune = time.monotonic()
                    self.prune()
        except Exception:  # noqa: BLE001 - infrastructure failure: exit, let the broker redeliver
            # Same contract as the old synchronous path (Postgres/Oxigraph
            # down -> the worker restarts), from a thread: nothing in
            # this batch was acknowledged, so nothing is lost.
            traceback.print_exc()
            os._exit(1)

    def flush(self, batch: list) -> None:
        t0 = time.monotonic()
        with tracer.start_as_current_span("mqtt_listener.flush") as span:
            readings, alarms, tags = [], [], []
            for _, _, _, topic, value, ts, flag in batch:
                if flag is ALARM:
                    alarms.append([topic, value, ts])
                elif flag is TAGS:
                    tags.append([topic, value, ts])
                else:
                    readings.append((topic, value, ts, flag))
            span.set_attribute("readings", len(readings))
            span.set_attribute("tags", len(tags))
            if self.feed:
                ingest_readings(self.store, self.ts_conn, readings,
                                then=lambda conn: reading_feed.append_from_stage(conn, alarms, tags))
            else:
                if readings:
                    ingest_readings(self.store, self.ts_conn, readings)
                if tags:
                    tag_ingest.apply(self.store, tags)
            for gen, mid, qos, *_ in batch:
                if qos and gen == self.generation:
                    self.client.ack(mid, qos)
            self.rows += len(readings)
            self.tags += len(tags)
            self.alarms += len(alarms)
            self.batches += 1
            self.done += len(batch)
        self.flush_s += time.monotonic() - t0

    def prune(self) -> None:
        """Drop feed batches every consumer has read, and any past the
        retention window (counted against the consumers that hadn't). A
        failure here is logged, never fatal: ingest matters more."""
        try:
            _, missed = reading_feed.prune(self.ts_conn, self.feed_retention_s)
        except Exception as exc:  # noqa: BLE001 - pruning is housekeeping
            print(f"reading feed: prune failed: {exc!r}", flush=True)
            return
        for consumer, readings, batches in missed:
            print(f"reading feed: {consumer} MISSED {readings} reading(s) in {batches} batch(es): "
                  f"more than {self.feed_retention_s:.0f} s behind, pruned unread", flush=True)

    def stats(self) -> dict:
        return {"received": self.received, "rows": self.rows, "batches": self.batches, "tags": self.tags,
                "alarms": self.alarms, "queue": self.queue.qsize(), "blocked_s": round(self.blocked_s, 2),
                "blocked_events": self.blocked_events, "flush_s": round(self.flush_s, 2),
                "connected": self.connected}


def make_on_message(store, ts_conn, batcher: ReadingBatcher | None = None, retained_only: bool = False):
    """Without a batcher every reading is written before on_message returns
    (and acknowledged then, by paho); with one (the workers), readings,
    tags and alarms are queued for it and anything else is handled here
    and acknowledged explicitly - the client runs with manual acks.
    retained_only: skip (acknowledge) live messages, for RetainedSnapshot."""

    def on_message(client, userdata, msg):
        """One bad message is logged and skipped, never fatal: this client
        has a persistent session, so a message that crashed it would be
        redelivered after every restart and stop ingest for good (a bare
        `1` on an fbf/ topic did exactly that in CI). Infrastructure errors
        (Postgres/Oxigraph down) still raise, so the worker restarts."""
        try:
            queued = False if retained_only and not msg.retain else _handle(msg)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            print(f"skipped bad message on {msg.topic}: {exc!r}")
            queued = False
        if batcher is not None and not queued and msg.qos:
            client.ack(msg.mid, msg.qos)

    def _handle(msg):
        # A span per reading was ~30% of the listener's CPU at 2,000
        # readings/s (span creation plus the exporter encoding them, py-spy,
        # Baler#190). With a batcher everything is traced as part of its
        # batch (mqtt_listener.flush, tag_ingest.apply).
        if batcher is not None:
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
            return False
        if not isinstance(payload, dict):
            span.set_attribute("error", "payload is not a JSON object")
            print(f"malformed payload on {msg.topic}: expected a JSON object, got {type(payload).__name__}")
            return False

        value = payload.get("value")
        ts = payload.get("ts")

        # Tags and equip messages (BACnet/Modbus connection equip, Haystack
        # equip/site recs, point tags): the tag path, baler.tag_ingest.
        if tag_ingest.is_tags(msg.topic):
            if batcher is not None:
                batcher.put_tags(msg, msg.topic, value, ts)
                return True
            tag_ingest.apply(store, [(msg.topic, value, ts)])
            return False

        # A controller alarm forwarded by Windrow (Baler#168) is not a
        # reading: fault_detector raises/clears it as a fault. Storing it
        # here would invent a `<label>/alarm` point with dicts for history.
        if bacnet_alarms.is_alarm(msg.topic, value):
            if batcher is not None and batcher.feed:
                batcher.put_alarm(msg, msg.topic, value, ts)
                return True
            return False

        flag = quality.from_envelope(payload.get("quality"))
        if batcher is not None:
            batcher.put(msg, msg.topic, binary_state(value), ts, flag)
            return True
        ingest_reading(store, ts_conn, msg.topic, binary_state(value), ts, quality=flag)
        return False

    return on_message


# ----------------------------------------------------------------- workers


class RetainedSnapshot:
    """A shared subscription gets no retained messages (MQTT 5, and what
    mosquitto does), so a listener whose sessions are new - a first start,
    a broker that lost its state, the upgrade from one listener - would
    never see the tags Windrow published (retained) before it subscribed.
    Worker 0, when its session is new, takes them once on a plain
    subscription of its own: a clean session, retained messages only (live
    ones go to the shared group and are skipped here), stored through a
    batcher like any others, until none has arrived for `idle_s`."""

    def __init__(self, args, store, idle_s: float = 3.0):
        self.args, self.store, self.idle_s = args, store, idle_s
        self.thread = threading.Thread(target=self._run, name="retained-snapshot", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        try:
            a = self.args
            client = mqtt_util.make_client(client_id=RETAINED_CLIENT_ID, manual_ack=True, v5=True)
            batcher = ReadingBatcher(client, self.store, connect(), max_rows=a.batch_rows, max_wait=a.batch_ms / 1000,
                                     feed=a.feed, prune_every=0, name="retained-batcher")
            subscribed = threading.Event()
            client.on_connect = lambda c, *_: c.subscribe(a.topic_pattern, qos=1)
            client.on_subscribe = lambda *_: subscribed.set()
            client.on_message = make_on_message(self.store, None, batcher, retained_only=True)
            batcher.start()
            mqtt_util.connect_v5(client, a.mqtt_host, a.mqtt_port, persistent=False)
            client.loop_start()
            t0 = time.monotonic()
            subscribed.wait(60)
            batcher.last_message_at = time.monotonic()
            while time.monotonic() - batcher.last_message_at < self.idle_s or batcher.pending:
                time.sleep(0.5)
            client.disconnect()
            client.loop_stop()
            print(f"retained snapshot: {batcher.received} retained messages ({batcher.rows} readings, "
                  f"{batcher.tags} tags) in {time.monotonic() - t0:.1f} s", flush=True)
        except Exception:  # noqa: BLE001 - worker 0 restarts; its session now exists, see docs
            traceback.print_exc()
            os._exit(1)


def _worker_args(args) -> argparse.Namespace:
    return args if isinstance(args, argparse.Namespace) else argparse.Namespace(**args)


def _session(client_id: str, args, store, ts_conn, prune: bool) -> tuple:
    """A manual-ack MQTT 5 client and its batcher. Manual acks: a reading
    is acknowledged once its batch is committed (ReadingBatcher), so an
    unstored reading is never one the broker has forgotten."""
    client = mqtt_util.make_client(client_id=client_id, manual_ack=True, v5=True)
    batcher = ReadingBatcher(client, store, ts_conn, max_rows=args.batch_rows, max_wait=args.batch_ms / 1000,
                             feed=args.feed, feed_retention_s=args.feed_retention, prune_every=5.0 if prune else 0)
    client.on_message = make_on_message(store, ts_conn, batcher)
    return client, batcher


def run_worker(index: int, args) -> None:
    """One worker: a persistent MQTT 5 session on the shared subscription
    (stable id, never expires: what the broker queued for this worker, and
    what it hadn't acknowledged, is still there when it comes back), its
    own batcher and Postgres connection, a heartbeat every few seconds.
    Worker 0 also prunes the reading feed (one pruner per listener)."""
    args = _worker_args(args)
    tracing.init_tracing("baler-mqtt-listener")
    store = RemoteStore()
    client_id = worker_client_id(index)
    client, batcher = _session(client_id, args, store, connect(), prune=index == 0)
    client.on_disconnect = make_on_disconnect(batcher)
    on_fresh = (lambda c: RetainedSnapshot(args, store).start()) if index == 0 and args.retained_snapshot else None
    client.on_connect = make_on_connect(shared(args.topic_pattern, args.share_group), batcher, on_fresh=on_fresh)
    batcher.start()
    threading.Thread(target=_heartbeats, args=(f"mqtt_listener/{index}", client_id, batcher), daemon=True,
                     name="heartbeat").start()
    mqtt_util.connect_v5(client, args.mqtt_host, args.mqtt_port)
    print(f"worker {index} ({client_id}): {shared(args.topic_pattern, args.share_group)} @ "
          f"{args.mqtt_host}:{args.mqtt_port}, batches of up to {args.batch_rows} readings / {args.batch_ms} ms", flush=True)
    client.loop_forever()


def retire_session(client, batcher: ReadingBatcher, args, idle_s: float = 3.0, timeout_s: float = 600.0) -> bool:
    """End a session no worker uses any more - a worker index past the
    current count, or the pre-#233 single listener's. The broker still
    queues that client its share of every message (it round-robins a shared
    group over offline members too, and the old listener's plain
    subscription gets everything), until the queue overflows and it drops.
    So: resume the session, unsubscribe, store what was queued (acked after
    commit like any batch), then disconnect with session expiry 0. A
    client id with no session costs one connect. Returns whether there was
    a session."""
    present = threading.Event()
    connected = threading.Event()

    def on_connect(c, userdata, flags, reason_code, properties):
        batcher.generation += 1
        if getattr(flags, "session_present", False):
            present.set()
        connected.set()

    client.on_connect = on_connect
    mqtt_util.connect_v5(client, args.mqtt_host, args.mqtt_port)
    client.loop_start()
    try:
        if not connected.wait(30):
            raise RuntimeError(f"retire {client._client_id!r}: no CONNACK")
        if not present.is_set():
            return False
        print(f"retiring the session of {client._client_id.decode()}: draining what the broker queued for it", flush=True)
        client.unsubscribe([shared(args.topic_pattern, args.share_group), args.topic_pattern])
        batcher.start()
        t0 = time.monotonic()
        batcher.last_message_at = time.monotonic()
        while (time.monotonic() - batcher.last_message_at < idle_s or batcher.pending) and time.monotonic() - t0 < timeout_s:
            time.sleep(0.5)
        if batcher.pending:
            raise RuntimeError(f"retire {client._client_id!r}: {batcher.pending} messages still unstored after {timeout_s} s")
        print(f"retired {client._client_id.decode()}: stored {batcher.rows} readings, {batcher.tags} tags "
              f"in {time.monotonic() - t0:.1f} s", flush=True)
        return True
    finally:
        if connected.is_set():
            mqtt_util.disconnect_ending_session(client)
        client.loop_stop()


def run_retire(args, client_ids: list[str]) -> None:
    """The supervisor's one-off sweep: retire_session for each id, in turn."""
    args = _worker_args(args)
    tracing.init_tracing("baler-mqtt-listener")
    store, ts_conn = RemoteStore(), connect()
    retired = []
    for cid in client_ids:
        client, batcher = _session(cid, args, store, ts_conn, prune=False)
        if retire_session(client, batcher, args):
            retired.append(cid)
    print(f"retire sweep: {len(client_ids)} unused listener client id(s) checked, "
          f"{len(retired)} session(s) retired {retired}", flush=True)


def _heartbeats(service: str, client_id: str, batcher: ReadingBatcher) -> None:
    """Per-worker heartbeat (service_heartbeats, 'mqtt_listener/<n>'): what
    it stored and how hard it worked over the last interval - readings/s,
    the process's CPU %, time blocked on a full queue. The scale ladder
    reads these for per-worker throughput and CPU (#233)."""
    conn = None
    last_t, last_cpu, last = time.monotonic(), time.process_time(), batcher.stats()
    while True:
        time.sleep(HEARTBEAT_EVERY)
        now, cpu, st = time.monotonic(), time.process_time(), batcher.stats()
        dt = max(now - last_t, 1e-9)
        detail = {**st, "client_id": client_id, "pid": os.getpid(),
                  "readings_per_s": round((st["rows"] - last["rows"]) / dt, 1),
                  "messages_per_s": round((st["received"] - last["received"]) / dt, 1),
                  "cpu_pct": round(100 * (cpu - last_cpu) / dt, 1),
                  "blocked_s_delta": round(st["blocked_s"] - last["blocked_s"], 2),
                  # share of the interval the batcher spent writing: near
                  # 100 with spare CPU means Postgres, not Python, is the wall
                  "flush_busy_pct": round(100 * (st["flush_s"] - last["flush_s"]) / dt, 1),
                  "interval_s": round(dt, 1)}
        last_t, last_cpu, last = now, cpu, st
        try:
            conn = conn or connect()
            heartbeat.beat(conn, service, detail)
        except Exception as exc:  # noqa: BLE001 - liveness reporting is best-effort
            print(f"heartbeat {service}: {exc!r}", flush=True)
            conn = None


def run_tags(args) -> None:
    args = _worker_args(args)
    tracing.init_tracing("baler-tag-ingest")
    tag_ingest.run(RemoteStore(), connect())


def _child(kind: str, index: int, args: dict, retire_ids: list[str] | None = None) -> None:
    if kind == "worker":
        run_worker(index, args)
    elif kind == "tags":
        run_tags(args)
    elif kind == "retire":
        run_retire(args, retire_ids or [])


# ----------------------------------------------------------------- supervisor


class BrokerDrops:
    """The broker's own count of messages it dropped ($SYS/broker/publish/
    messages/dropped: queue limits, mostly), read by the supervisor so an
    overload shows in the listener's log, heartbeat and /ops/status - not
    only as readings that quietly never arrived. Broker-wide: a QoS 0
    subscriber falling behind counts too."""

    TOPIC = "$SYS/broker/publish/messages/dropped"

    def __init__(self, args):
        self.first: int | None = None
        self.total: int | None = None
        self.last_logged: int | None = None
        self.last_drop_at: str | None = None
        self.client = mqtt_util.make_client(client_id=f"{CLIENT_PREFIX}-sys-{os.getpid()}")
        self.client.on_connect = lambda c, *_: c.subscribe(self.TOPIC)
        self.client.on_message = self._msg
        try:
            self.client.connect(args.mqtt_host, args.mqtt_port)
            self.client.loop_start()
        except OSError as exc:
            print(f"broker drop counter unavailable: {exc!r}", flush=True)

    def _msg(self, c, u, msg):
        try:
            n = int(msg.payload)
        except ValueError:
            return
        if self.first is None:
            self.first = self.last_logged = n
        elif n > (self.total or 0):
            self.last_drop_at = datetime.now(timezone.utc).isoformat()
        self.total = n

    def check(self) -> None:
        if self.total is None or self.last_logged is None or self.total <= self.last_logged:
            return
        print(f"BROKER DROPPED {self.total - self.last_logged} message(s) (total {self.total - (self.first or 0)} since "
              "this listener started): the broker's queue for a slow subscriber overflowed. If readings are missing, "
              "the listener is behind - raise BALER_LISTENER_WORKERS (docs-site scale-test page).", flush=True)
        self.last_logged = self.total

    def detail(self) -> dict:
        return {"broker_dropped_since_start": (self.total - self.first) if self.total is not None and self.first is not None else None,
                "broker_dropped_total": self.total, "broker_last_drop_at": self.last_drop_at}


class Supervisor:
    """Starts and keeps the children: workers 0..N-1, the tag path, and a
    one-off retire sweep. A child that exits is restarted after a backoff
    (1 s doubling to 30 s) and counted - in the log and in the
    'mqtt_listener' heartbeat, which the scale ladder treats like a
    container restart. Each worker's persistent session means a restart
    loses nothing it hadn't acknowledged."""

    def __init__(self, args):
        self.args = args
        self.ctx = mp.get_context("spawn")
        self.children: dict[str, dict] = {}
        self.restarts: dict[str, int] = {}
        self.stopping = False

    def spawn(self, name: str, kind: str, index: int = 0, retire_ids: list[str] | None = None) -> None:
        p = self.ctx.Process(target=_child, args=(kind, index, vars(self.args), retire_ids), name=name, daemon=False)
        p.start()
        self.children[name] = {"proc": p, "kind": kind, "index": index, "retire_ids": retire_ids,
                               "backoff": self.children.get(name, {}).get("backoff", 1.0),
                               "started": time.monotonic(), "due": None}

    def tick(self) -> None:
        now = time.monotonic()
        for name, c in list(self.children.items()):
            p = c["proc"]
            if c["due"] is not None:
                if now >= c["due"]:
                    self.spawn(name, c["kind"], c["index"], c["retire_ids"])
                continue
            if p.is_alive():
                if now - c["started"] > 60:
                    c["backoff"] = 1.0  # stayed up: the next failure restarts fast again
                continue
            code = p.exitcode
            if code == 0 and c["kind"] == "retire":  # the sweep is done
                del self.children[name]
                continue
            self.restarts[name] = self.restarts.get(name, 0) + 1
            print(f"supervisor: {name} exited (code {code}); restart #{self.restarts[name]} in {c['backoff']:.0f} s",
                  flush=True)
            c["due"] = now + c["backoff"]
            c["backoff"] = min(30.0, c["backoff"] * 2)

    def stop(self, *_):
        self.stopping = True

    def terminate(self) -> None:
        for c in self.children.values():
            if c["proc"].is_alive():
                c["proc"].terminate()
        for c in self.children.values():
            c["proc"].join(timeout=10)

    def pids(self) -> dict:
        return {name: c["proc"].pid for name, c in self.children.items() if c["proc"].is_alive()}


def retire_ids(workers: int, max_workers: int = MAX_WORKERS) -> list[str]:
    """Client ids whose sessions no current worker resumes."""
    return [LEGACY_CLIENT_ID] + [worker_client_id(i) for i in range(workers, max_workers)]


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST", "localhost"))
    parser.add_argument("--mqtt-port", type=int, default=int(os.environ.get("MQTT_PORT", "") or 1883))
    parser.add_argument("--topic-pattern", default="fbf/#", help="MQTT subscription filter, e.g. fbf/#")
    parser.add_argument("--workers", type=int, default=int(os.environ.get("BALER_LISTENER_WORKERS", "") or 1),
                        help="worker processes on the shared subscription (BALER_LISTENER_WORKERS, default 1)")
    parser.add_argument("--share-group", default=SHARE_GROUP)
    # Rows per transaction, and the longest a reading waits for its batch.
    # A batch can't usefully exceed the broker's max_inflight_messages
    # (mosquitto/mosquitto.conf): unacknowledged messages are in flight.
    parser.add_argument("--batch-rows", type=int, default=5000)
    parser.add_argument("--batch-ms", type=float, default=250.0)
    parser.add_argument("--no-feed", dest="feed", action="store_false",
                        help="don't append committed batches to the reading feed (fault_detector and "
                             "derivation_engine read it; without it they see nothing, and tags are applied "
                             "by the workers themselves)")
    parser.add_argument("--feed-retention", type=float, default=reading_feed.DEFAULT_RETENTION_S,
                        help="seconds a feed batch is kept for a consumer that hasn't read it "
                             "(BALER_FEED_RETENTION_S); past that it is pruned and counted as missed")
    parser.add_argument("--no-retire", dest="retire", action="store_false",
                        help="don't retire the sessions of worker ids past --workers (and the pre-#233 listener's)")
    parser.add_argument("--no-retained-snapshot", dest="retained_snapshot", action="store_false",
                        help="don't take the broker's retained messages when worker 0's session is new")
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= MAX_WORKERS:
        parser.error(f"--workers must be 1..{MAX_WORKERS}")
    return args


def main(argv=None) -> None:
    args = parse_args(argv)
    log = functools.partial(print, flush=True)
    conn = connect()
    if args.feed:
        # Registered before any worker can commit a batch: a new consumer
        # starts at the feed's head, so registering later would skip the
        # tags committed in between.
        reading_feed.FeedConsumer(conn, tag_ingest.CONSUMER, log=log, tags_only=True).start()
    sup = Supervisor(args)
    signal.signal(signal.SIGTERM, sup.stop)
    signal.signal(signal.SIGINT, sup.stop)
    if args.retire:
        sup.spawn("retire", "retire", retire_ids=retire_ids(args.workers))
    for i in range(args.workers):
        sup.spawn(f"worker-{i}", "worker", i)
    if args.feed:
        sup.spawn("tags", "tags")
    drops = BrokerDrops(args)
    log(f"mqtt_listener: {args.workers} worker(s) on {shared(args.topic_pattern, args.share_group)}"
        + (", tag path on the reading feed" if args.feed else ""))
    last_beat = 0.0
    while not sup.stopping:
        time.sleep(1)
        sup.tick()
        drops.check()
        if time.monotonic() - last_beat >= HEARTBEAT_EVERY:
            last_beat = time.monotonic()
            heartbeat.beat(conn, "mqtt_listener", {"workers": args.workers, "pids": sup.pids(),
                                                   "restarts": dict(sup.restarts), **drops.detail()})
    log("mqtt_listener: stopping")
    sup.terminate()


if __name__ == "__main__":
    main()

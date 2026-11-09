"""
Scale-test load generator (Baler#190 R1): N synthetic points over real MQTT,
in exactly the envelope Windrow publishes (windrow/mqtt_sink.py):

  {prefix}/equip/tags   {"point": "equip/tags", "value": "<json {tags, points}>", "ts": t}
  {prefix}/{label}/tags {"point": "<label>/tags", "value": "<json tags>", "ts": t}
  {prefix}/{label}      {"point": "<label>", "value": <reading>, "ts": t}

QoS 1, retained, as Windrow does. Tags and equip messages go first (the
graph grows as it would for real equipment), then readings for every point
once per --interval, spread evenly across the interval rather than in a
burst.

Runs --procs worker processes, each owning a slice of the equips, and
measures itself: messages published, messages the broker acknowledged
(PUBACK), and how far behind schedule it is. If the achieved rate is short of
the target, the generator - not Baler - is the limit, and the ladder says so.

  python scripts/scale/loadgen.py --points 10000 --procs 2 --duration 60
  python scripts/scale/loadgen.py --points 10000 --phase tags
  python scripts/scale/loadgen.py --points 25000 --tags-from 10000 --phase tags

Broker host/port/credentials default to the isolated scale stack's
(scripts/scale/scale.env, `scalegen` account); see docs-site/pages/scale-test.mdx.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

STATS_EVERY = 1.0


def _client(args, proc: int) -> mqtt.Client:
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"scale-loadgen-{os.getpid()}-{proc}")
    c.username_pw_set(args.username, args.password)
    # Paho's defaults (20 in flight) cap one connection at ~20 msgs per
    # round trip; a generator must never be the bottleneck by default.
    c.max_inflight_messages_set(args.inflight)
    c.max_queued_messages_set(args.queue)
    return c


class Acks:
    """PUBACKs per second of the payload's own ts: what the broker took
    responsibility for, bucketed so the ladder can compare it with the rows
    stored for the same seconds (data loss = acked - stored)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending: dict[int, int] = {}
        self.early: set[int] = set()
        self.buckets: dict[int, int] = {}
        self.acked = 0

    def on_publish(self, client, userdata, mid, reason_code=None, properties=None):
        with self.lock:
            sec = self.pending.pop(mid, None)
            if sec is None:
                self.early.add(mid)  # acked before publish() returned
                return
            self._count(sec)

    def sent(self, mid: int, sec: int):
        with self.lock:
            if mid in self.early:
                self.early.discard(mid)
                self._count(sec)
            else:
                self.pending[mid] = sec

    def _count(self, sec: int):
        self.acked += 1
        self.buckets[sec] = self.buckets.get(sec, 0) + 1

    def take_buckets(self) -> dict[int, int]:
        with self.lock:
            b, self.buckets = self.buckets, {}
            return b


def _publish(client, acks: Acks, topic: str, payload: bytes, sec: int, stop) -> bool:
    """Publish with backpressure: a full paho queue waits, never drops."""
    while not stop.is_set():
        info = client.publish(topic, payload, qos=1, retain=True)
        if info.rc == mqtt.MQTT_ERR_SUCCESS:
            acks.sent(info.mid, sec)
            return True
        if info.rc == mqtt.MQTT_ERR_QUEUE_SIZE:
            time.sleep(0.002)
            continue
        time.sleep(0.05)  # disconnected: paho reconnects in its own thread
    return False


def worker(proc: int, equips: range, args, out: mp.Queue, stop) -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    shape = common.Shape(args.points, args.points_per_equip, args.equips_per_site)
    client = _client(args, proc)
    acks = Acks()
    client.on_publish = acks.on_publish
    client.connect(args.host, args.port, keepalive=60)
    client.loop_start()
    for _ in range(100):
        if client.is_connected():
            break
        time.sleep(0.1)
    sent = 0
    behind_max = 0.0
    last = time.monotonic()
    phase = "tags"

    def report(force=False, behind=0.0):
        nonlocal last
        now = time.monotonic()
        if force or now - last >= STATS_EVERY:
            last = now
            out.put({"proc": proc, "phase": phase, "t": time.time(), "sent": sent, "acked": acks.acked,
                     "behind_s": behind, "behind_max_s": behind_max, "buckets": acks.take_buckets()})

    items = []  # (topic, label, global idx, pattern) for readings
    for g in equips:
        pts = shape.equip_points(g)
        prefix = shape.equip_prefix(g)
        items.extend((f"{prefix}/{label}", label, idx, t[4] if args.pattern == "mix" else args.pattern,
                      common.is_faulting(idx, t, args.fault_share))
                     for idx, label, t in pts)
    if args.phase in ("tags", "both"):
        tag_rate = max(1.0, args.tags_rate / args.procs)
        t0 = time.monotonic()
        n = 0
        for g in equips:
            pts = shape.equip_points(g)
            if pts[-1][0] < args.tags_from:
                continue
            prefix = shape.equip_prefix(g)
            msgs = [(f"{prefix}/equip/tags", "equip/tags", json.dumps(common.equip_tags(shape, g, [p[1] for p in pts])))]
            msgs += [(f"{prefix}/{label}/tags", f"{label}/tags", json.dumps(common.point_tags(shape, g, idx, label, t)))
                     for idx, label, t in pts if idx >= args.tags_from]
            for topic, point, value in msgs:
                wait = t0 + n / tag_rate - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                ts = time.time()
                if not _publish(client, acks, topic, common.envelope(point, value, ts), int(ts), stop):
                    break
                n += 1
                sent += 1
                report()
        deadline = time.monotonic() + 60
        while acks.acked < sent and time.monotonic() < deadline and not stop.is_set():
            time.sleep(0.05)
        report(force=True)
    if args.phase in ("readings", "both") and items:
        phase = "readings"
        n = len(items)
        rate = n / args.interval
        t0 = time.monotonic()
        end = t0 + args.duration if args.duration else float("inf")
        rs = 0  # readings sent this phase (sent also counts the tags above)
        while not stop.is_set() and time.monotonic() < end:
            now = time.monotonic()
            due = int((now - t0) * rate) + 1
            behind = (due - rs) / rate if due > rs else 0.0
            behind_max = max(behind_max, behind)
            burst = 0
            while rs < due and burst < 2000:  # yield to report() every so often
                topic, label, idx, pattern, faulting = items[rs % n]
                ts = time.time()
                value = (faulting and common.fault_value(idx, rs // n)) or common.value_at(pattern, idx, rs // n)
                if not _publish(client, acks, topic, common.envelope(label, value, ts), int(ts), stop):
                    break
                rs += 1
                sent += 1
                burst += 1
            report(behind=behind)
            if rs >= due:
                time.sleep(min(0.02, max(0.0, (rs + 1) / rate - (time.monotonic() - t0))))
    deadline = time.monotonic() + 15
    while acks.acked < sent and time.monotonic() < deadline:
        time.sleep(0.05)
    phase = "done"
    report(force=True)
    client.loop_stop()
    client.disconnect()


def split(total: int, parts: int) -> list[range]:
    step, extra = divmod(total, parts)
    out, start = [], 0
    for i in range(parts):
        end = start + step + (1 if i < extra else 0)
        out.append(range(start, end))
        start = end
    return [r for r in out if len(r)]


class Totals:
    """Parent-side aggregate, written to --stats-file every second."""

    def __init__(self, args, procs: int):
        self.args = args
        self.procs: dict[int, dict] = {}
        self.buckets: dict[int, int] = {}
        self.history: list[tuple[float, int, int]] = []  # (t, sent, acked)
        self.started = time.time()
        self.n_procs = procs

    def add(self, m: dict) -> None:
        for sec, c in m.pop("buckets").items():
            self.buckets[int(sec)] = self.buckets.get(int(sec), 0) + c
        self.procs[m["proc"]] = m

    def snapshot(self) -> dict:
        now = time.time()
        sent = sum(p["sent"] for p in self.procs.values())
        acked = sum(p["acked"] for p in self.procs.values())
        self.history.append((now, sent, acked))
        self.history = [h for h in self.history if now - h[0] <= 30]
        old = next((h for h in self.history if now - h[0] >= 9), self.history[0])
        dt = max(now - old[0], 1e-9)
        phases = {p["phase"] for p in self.procs.values()}
        target = self.args.points / self.args.interval
        cpu = os.times()
        return {
            "points": self.args.points, "interval_s": self.args.interval, "procs": self.n_procs,
            "target_rate": target, "phase": ",".join(sorted(phases)), "t": now, "started": self.started,
            "sent": sent, "acked": acked,
            "sent_rate_10s": (sent - old[1]) / dt, "acked_rate_10s": (acked - old[2]) / dt,
            "behind_s": max((p["behind_s"] for p in self.procs.values()), default=0.0),
            "behind_max_s": max((p["behind_max_s"] for p in self.procs.values()), default=0.0),
            "cpu_s": cpu.children_user + cpu.children_system + cpu.user + cpu.system,
            "acked_by_second": {str(k): v for k, v in sorted(self.buckets.items())},
        }


def write_stats(path: str, snap: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(snap, f)
    os.replace(tmp, path)


def parse_args(argv=None):
    env = common.read_env()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--points", type=int, required=True)
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between readings of one point")
    ap.add_argument("--points-per-equip", type=int, default=10)
    ap.add_argument("--equips-per-site", type=int, default=50)
    ap.add_argument("--pattern", choices=common.PATTERNS, default="mix", help="value pattern (mix = per point kind)")
    ap.add_argument("--fault-share", type=float, default=0.0,
                    help="share of points that read out of range half the time (#273: faults, webhooks)")
    ap.add_argument("--phase", choices=["tags", "readings", "both"], default="both")
    ap.add_argument("--tags-from", type=int, default=0, help="publish tags only for points with index >= this (ladder steps)")
    ap.add_argument("--tags-rate", type=float, default=2000.0, help="tags/equip messages per second, all procs together")
    ap.add_argument("--duration", type=float, default=0, help="seconds of readings (0 = until Ctrl-C/SIGTERM)")
    ap.add_argument("--procs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(env.get("SCALE_MQTT_PORT", "21883")))
    ap.add_argument("--username", default="scalegen")
    ap.add_argument("--password", default=os.environ.get("SCALE_GEN_MQTT_PASSWORD") or env.get("SCALE_GEN_MQTT_PASSWORD", ""))
    ap.add_argument("--inflight", type=int, default=1000)
    ap.add_argument("--queue", type=int, default=20000)
    ap.add_argument("--stats-file", help="JSON stats, rewritten every second")
    args = ap.parse_args(argv)
    if args.port in common.LIVE_PORTS and not os.environ.get("SCALE_ALLOW_LIVE_PORT"):
        ap.error(f"port {args.port} is a live install's broker port; the generator only targets the scale stack")
    return args


def main(argv=None) -> None:
    args = parse_args(argv)
    shape = common.Shape(args.points, args.points_per_equip, args.equips_per_site)
    slices = split(shape.equips, min(args.procs, shape.equips))
    stop = mp.Event()
    out: mp.Queue = mp.Queue()
    procs = [mp.Process(target=worker, args=(i, r, args, out, stop), daemon=True) for i, r in enumerate(slices)]
    for p in procs:
        p.start()

    def _stop(*_):
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    totals = Totals(args, len(procs))
    print(f"loadgen: {args.points} points, {shape.equips} equips, {shape.sites} sites, {len(procs)} procs, "
          f"target {args.points / args.interval:.0f} msg/s, phase {args.phase}", flush=True)
    last_print = 0.0
    while any(p.is_alive() for p in procs) or not out.empty():
        try:
            while True:
                totals.add(out.get(timeout=0.5))
        except queue.Empty:
            pass
        if not totals.procs:
            continue
        snap = totals.snapshot()
        if args.stats_file:
            write_stats(args.stats_file, snap)
        if time.time() - last_print >= 10:
            last_print = time.time()
            print(f"loadgen: phase {snap['phase']} sent {snap['sent']} acked {snap['acked']} "
                  f"rate {snap['sent_rate_10s']:.0f}/s (target {snap['target_rate']:.0f}) behind {snap['behind_s']:.1f}s",
                  flush=True)
    for p in procs:
        p.join(timeout=5)
    snap = totals.snapshot()
    if args.stats_file:
        write_stats(args.stats_file, snap)
    print(f"loadgen: done, sent {snap['sent']} acked {snap['acked']}", flush=True)


if __name__ == "__main__":
    main()

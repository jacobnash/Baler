"""
The Haxall baseline's ingest (Baler#236): Windrow's per-point MQTT envelope
into Haxall's history with batched hisWrite, so Haxall isn't hobbled by a
bridge doing one Axon eval per reading (windrow/haxall_mqtt_bridge.py does,
with ts=now(): fine for a demo, not for a ladder).

The same contract as Baler's listener, so the crash test means the same on
both sides: a persistent MQTT session, manual acks, and a message is
acknowledged only after the hisWrite holding its reading has returned.
Readings keep their payload ts. Each point's id is derived from its topic
(topic_ref: fbf/scale/s00000/e00000/sat -> @scale.s00000.e00000.sat), the
id the ladder provisions it under, so no lookup table is needed.

hisWrite modes: "batch" sends one Haystack hisWrite per flush, a grid with
one value column per point (column meta `id`, the spec's batch write);
"per-point" one hisWrite per point per flush; "auto" tries batch and falls
back to per-point for good if the server refuses it. A batch the server
rejects (an unknown id) is retried per point, and readings of points it
still refuses are acknowledged and counted as `rejected` - lost, and the
crash test counts them so. Haxall unreachable: exit without acking, the
broker redelivers to the restarted bridge.

Every 5 s it prints `BRIDGE_STATS {json}` (readings/s, queue, batch size,
hisWrite latency, CPU): the ladder reads its own container's log, so the
bridge's limit is reported apart from Haxall's. Nothing is written to disk.

  python scripts/scale/haxall_bridge.py   # settings from the environment, see compose.haxall.yml
"""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

TOPIC_ROOT = "fbf/"
CLIENT_ID = "scale-haxall-bridge"
_REF_BAD = re.compile(r"[^A-Za-z0-9_:\-.~]")


def topic_ref(topic: str) -> str:
    """The Haxall id a point's topic is provisioned under (no '@')."""
    return _REF_BAD.sub("_", topic.removeprefix(TOPIC_ROOT).replace("/", "."))


def _num(v) -> str:
    return f"n:{float(v)!r}"


def encode_value(v) -> Any:
    """Haystack JSON (ver 3.0) for a reading, or None if Haxall's history
    can't take it (a string state): those are skipped and counted."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return _num(v)
    return None


def encode_ts(ts: float) -> str:
    iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return f"t:{iso} UTC"


def batch_grid(rows: list[tuple[str, float, Any]]) -> dict:
    """One hisWrite for many points: a ts column and one value column per
    point, its id in the column meta; each row carries one reading."""
    cols: dict[str, str] = {}
    out_rows = []
    for ref, ts, val in rows:
        col = cols.setdefault(ref, f"v{len(cols)}")
        out_rows.append({"ts": encode_ts(ts), col: val})
    return {"meta": {"ver": "3.0"},
            "cols": [{"name": "ts"}] + [{"name": c, "id": f"r:{ref}"} for ref, c in cols.items()],
            "rows": out_rows}


def point_grids(rows: list[tuple[str, float, Any]]) -> dict[str, dict]:
    """One hisWrite grid per point (grid meta `id`), rows in ts order."""
    by: dict[str, list] = {}
    for ref, ts, val in rows:
        by.setdefault(ref, []).append((ts, val))
    return {ref: {"meta": {"ver": "3.0", "id": f"r:{ref}"}, "cols": [{"name": "ts"}, {"name": "val"}],
                  "rows": [{"ts": encode_ts(ts), "val": val} for ts, val in sorted(items)]}
            for ref, items in by.items()}


class HisWriteError(RuntimeError):
    """The server answered with a Haystack error grid (not unreachable)."""


class Writer:
    """hisWrite through a HaystackClient-like object with _post(op, grid)."""

    def __init__(self, client, mode: str = "auto"):
        self.client, self.mode = client, mode
        self.batch_refused: str | None = None

    def _post(self, grid: dict) -> None:
        body = self.client._post("hisWrite", grid)
        meta = body.get("meta") or {}
        if "err" in meta:
            raise HisWriteError(str(meta.get("dis", "hisWrite failed")))

    def write(self, rows: list[tuple[str, float, Any]]) -> int:
        """Writes rows; returns how many were rejected (and so dropped)."""
        if not rows:
            return 0
        if self.mode in ("auto", "batch"):
            try:
                self._post(batch_grid(rows))
                return 0
            except HisWriteError as exc:
                if self.mode == "auto" and len({r[0] for r in rows}) > 1:
                    # Maybe no batch hisWrite at all: settle it with one
                    # point's rows, which the batch form also covers.
                    ref = rows[0][0]
                    try:
                        self._post(batch_grid([r for r in rows if r[0] == ref]))
                    except HisWriteError:
                        pass
                    else:
                        return self._per_point(rows)  # batch works: some id in this flush is bad
                    self.mode, self.batch_refused = "per-point", str(exc)[:200]
                    print(f"haxall bridge: batch hisWrite refused ({exc}); one hisWrite per point from now on", flush=True)
        return self._per_point(rows)

    def _per_point(self, rows) -> int:
        rejected = 0
        for ref, grid in point_grids(rows).items():
            try:
                self._post(grid)
            except HisWriteError as exc:
                rejected += len(grid["rows"])
                print(f"haxall bridge: hisWrite @{ref} refused: {exc}", flush=True)
        return rejected


class Bridge:
    """Queue + one writer thread, the shape of Baler's ReadingBatcher."""

    def __init__(self, mqtt_client, writer: Writer, max_rows: int = 5000, max_wait: float = 0.25,
                 max_queued: int = 200_000):
        self.client, self.writer = mqtt_client, writer
        self.max_rows, self.max_wait = max_rows, max_wait
        self.queue: queue.Queue = queue.Queue(maxsize=max_queued)
        self.generation = 0
        self.received = self.written = self.rejected = self.skipped = self.batches = 0
        self.write_s = 0.0
        self.last_write_s = 0.0
        self.last_batch = 0

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        self.generation += 1
        client.subscribe(f"{TOPIC_ROOT}scale/#", qos=1)

    def on_message(self, client, userdata, msg):
        queued = False
        try:
            if not (msg.topic.endswith("/tags") or msg.topic.endswith("/batch")):
                payload = json.loads(msg.payload.decode("utf-8"))
                val = encode_value(payload.get("value")) if isinstance(payload, dict) else None
                ts = payload.get("ts") if isinstance(payload, dict) else None
                if val is not None and isinstance(ts, (int, float)):
                    self.received += 1
                    self.queue.put((self.generation, msg.mid, msg.qos, topic_ref(msg.topic), float(ts), val))
                    queued = True
                else:
                    self.skipped += 1
        except (ValueError, UnicodeDecodeError):
            self.skipped += 1
        if not queued and msg.qos:
            client.ack(msg.mid, msg.qos)

    def flush(self, batch: list) -> None:
        t0 = time.monotonic()
        rejected = self.writer.write([(ref, ts, val) for _, _, _, ref, ts, val in batch])
        self.last_write_s = time.monotonic() - t0
        self.write_s += self.last_write_s
        for gen, mid, qos, *_ in batch:
            if qos and gen == self.generation:
                self.client.ack(mid, qos)
        self.written += len(batch) - rejected
        self.rejected += rejected
        self.batches += 1
        self.last_batch = len(batch)

    def run(self) -> None:
        while True:
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

    def stats(self) -> dict:
        return {"received": self.received, "written": self.written, "rejected": self.rejected,
                "skipped": self.skipped, "batches": self.batches, "queue": self.queue.qsize(),
                "write_s": round(self.write_s, 3), "last_write_s": round(self.last_write_s, 3),
                "last_batch": self.last_batch, "mode": self.writer.mode, "batch_refused": self.writer.batch_refused}


def _report(bridge: Bridge, every: float = 5.0) -> None:
    last_t, last_cpu, last = time.monotonic(), time.process_time(), bridge.stats()
    while True:
        time.sleep(every)
        now, cpu, st = time.monotonic(), time.process_time(), bridge.stats()
        dt = max(now - last_t, 1e-9)
        st.update(written_per_s=round((st["written"] - last["written"]) / dt, 1),
                  batches_per_s=round((st["batches"] - last["batches"]) / dt, 2),
                  cpu_pct=round(100 * (cpu - last_cpu) / dt, 1),
                  write_busy_pct=round(100 * (st["write_s"] - last["write_s"]) / dt, 1), t=round(time.time(), 1))
        print("BRIDGE_STATS " + json.dumps(st), flush=True)
        last_t, last_cpu, last = now, cpu, st


def main() -> None:
    import paho.mqtt.client as mqtt

    from baler.haystack_client import HaystackClient

    env = os.environ
    hs = HaystackClient(env.get("HAXALL_URL", "http://haxall:8080/api/sys"), env.get("HAXALL_USER", "su"),
                        env["HAXALL_PASSWORD"])
    for _ in range(120):  # Haxall takes a while to boot
        try:
            hs.about()
            break
        except Exception as exc:  # noqa: BLE001 - not up yet
            print(f"haxall bridge: waiting for Haxall ({exc!r})", flush=True)
            time.sleep(2)
    else:
        sys.exit("haxall bridge: Haxall never answered")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=CLIENT_ID, clean_session=False,
                         manual_ack=True)
    client.username_pw_set(env.get("MQTT_USERNAME", "baler"), env.get("MQTT_PASSWORD", ""))
    client.max_inflight_messages_set(int(env.get("HAXALL_BRIDGE_INFLIGHT", "20000")))
    bridge = Bridge(client, Writer(hs, env.get("HAXALL_BRIDGE_MODE", "auto")),
                    max_rows=int(env.get("HAXALL_BRIDGE_BATCH_ROWS", "5000")),
                    max_wait=float(env.get("HAXALL_BRIDGE_BATCH_MS", "250")) / 1000)
    client.on_connect = bridge.on_connect
    client.on_message = bridge.on_message

    def writer():
        try:
            bridge.run()
        except Exception:  # noqa: BLE001 - Haxall unreachable: exit unacked, the broker redelivers
            import traceback

            traceback.print_exc()
            os._exit(1)

    threading.Thread(target=writer, daemon=True, name="hiswrite").start()
    threading.Thread(target=_report, args=(bridge,), daemon=True, name="stats").start()
    client.connect(env.get("MQTT_HOST", "mosquitto"), int(env.get("MQTT_PORT", "1883")))
    print(f"haxall bridge: fbf/scale/# -> {hs.base_url}, mode {bridge.writer.mode}, batches of "
          f"{bridge.max_rows} / {bridge.max_wait * 1000:.0f} ms", flush=True)
    client.loop_forever()


if __name__ == "__main__":
    main()

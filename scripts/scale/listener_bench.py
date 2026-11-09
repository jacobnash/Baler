"""
Listener throughput, without the ladder (Baler#233): how many readings/s
`python -m baler.mqtt_listener` stores with 1..N worker processes, against
a real broker and Postgres (durable commits), the generator kept out of the
measurement.

For each worker count it (1) gives each worker id a persistent session on
the shared subscription and ends every other listener session, (2) queues
--messages readings at the broker while no listener runs (QoS 1, retained,
Windrow's envelope, --points distinct points, unique timestamps), (3)
starts the listener and times how fast the rows land in point_history -
the middle 80% of them, past warm-up - while sampling each worker's
heartbeat (readings/s, CPU) and the broker's CPU. Nothing else competes
for the listener's CPU, so the number is the listener's capacity, not the
generator's.

  python scripts/scale/listener_bench.py --workers 1,2,3,4 --messages 300000
  python scripts/scale/listener_bench.py --workers 1 --envelope batch   # Windrow's batched envelope (#234)

--messages counts readings either way. With --envelope batch the same
readings go as one message per 10-point equip per tick ({prefix}/batch,
not retained), so the broker and the listener see a tenth of the messages
for the same rows.

Needs the scale stack's broker/Postgres/Oxigraph up (`ladder.py up`, or
just `docker compose ... up -d postgres mosquitto oxigraph migrate`) and
the listener's own environment (BALER_PG_DSN, OXIGRAPH_URL, MQTT_HOST,
MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD), since it runs the listener on
this host with PYTHONPATH=src. Writes a JSON result to results/.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

PREFIX = "fbf/bench"


def _v5(cid: str, args) -> mqtt.Client:
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid, protocol=mqtt.MQTTv5)
    c.username_pw_set(args.username, args.password)
    return c


def _connect(c: mqtt.Client, args, clean: bool) -> bool:
    got = threading.Event()
    present = []
    c.on_connect = lambda cl, u, flags, rc, p: (present.append(flags.session_present), got.set())
    props = Properties(PacketTypes.CONNECT)
    props.SessionExpiryInterval = 0 if clean else 0xFFFFFFFF
    c.connect(args.host, args.port, clean_start=clean, properties=props)
    c.loop_start()
    if not got.wait(10):
        raise SystemExit(f"no CONNACK from {args.host}:{args.port}")
    return bool(present and present[0])


def prepare_sessions(args, workers: int) -> None:
    """Worker 0..N-1: a persistent session subscribed to the shared group;
    every other listener id: no session (so the listener's retire sweep
    has nothing to drain and the run measures only the workers)."""
    for cid in ["baler-mqtt-listener", "baler-mqtt-listener-retained"] + [f"baler-mqtt-listener-{i}" for i in range(32)]:
        c = _v5(cid, args)
        _connect(c, args, clean=True)
        c.disconnect()
        c.loop_stop()
    for i in range(workers):
        c = _v5(f"baler-mqtt-listener-{i}", args)
        _connect(c, args, clean=False)
        done = threading.Event()
        c.on_subscribe = lambda *a, done=done: done.set()
        c.subscribe(f"$share/baler/{args.topic_pattern}", qos=1)
        done.wait(10)
        c.disconnect()
        c.loop_stop()


def _publisher(proc: int, procs: int, args, base: float, out) -> None:
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"bench-pub-{os.getpid()}")
    c.username_pw_set(args.pub_username, args.pub_password)
    c.max_inflight_messages_set(1000)
    c.max_queued_messages_set(20000)
    acked = [0]
    c.on_publish = lambda *a: acked.__setitem__(0, acked[0] + 1)
    c.connect(args.host, args.port)
    c.loop_start()
    sent = msgs = 0
    if args.envelope == "batch":
        # The same readings (reading k: point k % points, ts base + k / points),
        # one message per 10-point equip per tick: message m is equip
        # m % equips at tick m // equips.
        equips = -(-args.points // 10)
        per_mid: dict[int, int] = {}  # readings in each unacknowledged message
        early: set[int] = set()  # acked before publish() returned
        lock = threading.Lock()

        def on_pub(cl, u, mid, *a):
            with lock:
                if mid in per_mid:
                    acked[0] += per_mid.pop(mid)
                else:
                    early.add(mid)

        c.on_publish = on_pub
        for m in range(proc, -(-args.messages // args.points) * equips, procs):
            tick, e = divmod(m, equips)
            ks = [tick * args.points + e * 10 + j for j in range(10) if e * 10 + j < args.points]
            ks = [k for k in ks if k < args.messages]
            if not ks:
                continue
            payload = common.batch_envelope(base + tick, {f"p{k % args.points % 10}": round(70 + (k % 97) / 10, 2)
                                                          for k in ks})
            while True:
                info = c.publish(f"{PREFIX}/e{e:05d}/batch", payload, qos=1, retain=False)
                if info.rc == mqtt.MQTT_ERR_SUCCESS:
                    break
                time.sleep(0.002)
            with lock:
                if info.mid in early:
                    early.discard(info.mid)
                    acked[0] += len(ks)
                else:
                    per_mid[info.mid] = len(ks)
            sent += len(ks)
            msgs += 1
    else:
        for k in range(proc, args.messages, procs):
            p = k % args.points
            payload = common.envelope("zone-temp", round(70 + (k % 97) / 10, 2), base + k / args.points)
            while c.publish(f"{PREFIX}/e{p // 10:05d}/p{p % 10}", payload, qos=1, retain=True).rc != mqtt.MQTT_ERR_SUCCESS:
                time.sleep(0.002)
            sent += 1
            msgs += 1
    end = time.time() + 120
    while acked[0] < sent and time.time() < end:
        time.sleep(0.05)
    c.loop_stop()
    c.disconnect()
    out.put((sent, acked[0], msgs))


def publish_backlog(args, base: float) -> dict:
    t0 = time.time()
    out: mp.Queue = mp.Queue()
    procs = [mp.Process(target=_publisher, args=(i, args.publishers, args, base, out)) for i in range(args.publishers)]
    for p in procs:
        p.start()
    res = [out.get() for _ in procs]
    for p in procs:
        p.join()
    sent, acked = sum(r[0] for r in res), sum(r[1] for r in res)
    return {"sent": sent, "acked": acked, "mqtt_messages": sum(r[2] for r in res), "envelope": args.envelope,
            "publish_s": round(time.time() - t0, 1)}


def docker_cpu(container: str) -> float | None:
    out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}", container],
                         capture_output=True, text=True, check=False).stdout.strip()
    try:
        return float(out.rstrip("%"))
    except ValueError:
        return None


def run_one(args, workers: int, conn) -> dict:
    import statistics

    print(f"\n=== {workers} worker(s): {args.messages} readings queued at the broker ===", flush=True)
    prepare_sessions(args, workers)
    # A window of history no other run used: ts = base + k / points.
    base = float(int(time.time()) - 2 * 86400 + int(time.time() * 1000) % 86400)
    since = datetime.fromtimestamp(base, tz=timezone.utc)
    until = datetime.fromtimestamp(base + args.messages / args.points + 1, tz=timezone.utc)
    pub = publish_backlog(args, base)
    print(f"queued: {pub}", flush=True)
    conn.execute("DELETE FROM service_heartbeats WHERE service LIKE 'mqtt_listener%%'")

    def stored() -> int:
        return conn.execute("SELECT count(*) FROM point_history WHERE point_uri LIKE %s AND ts >= %s AND ts < %s",
                            (f"urn:point:{PREFIX}/%", since, until)).fetchone()[0]

    def writes() -> int:
        # point_history rows written (its chunks' pg_stat counters): cheap
        # enough to poll, unlike a count(*) over the run's rows, which cost
        # Postgres more CPU than some of the workers did.
        return int(conn.execute("""
            SELECT coalesce(sum(s.n_tup_ins + s.n_tup_upd), 0) FROM pg_stat_user_tables s
            JOIN timescaledb_information.chunks c ON c.chunk_schema = s.schemaname AND c.chunk_name = s.relname
            WHERE c.hypertable_name = 'point_history'""").fetchone()[0])

    w0 = writes()
    env = dict(os.environ, PYTHONPATH=str(common.REPO / "src"))
    log = open(args.log, "a")  # noqa: SIM115 - the listener's stdout for the whole run
    lst = subprocess.Popen([sys.executable, "-m", "baler.mqtt_listener", "--workers", str(workers),
                            "--topic-pattern", args.topic_pattern], env=env, cwd=common.REPO,
                           stdout=log, stderr=subprocess.STDOUT)
    t_start = time.time()
    marks: list[tuple[float, int]] = []
    beats: dict[str, list[dict]] = {}
    broker_cpu: list[float] = []
    pg_cpu: list[float] = []
    last_beat_poll = 0.0
    n = 0
    try:
        while time.time() - t_start < args.timeout:
            n = writes() - w0
            marks.append((time.time(), n))
            if time.time() - last_beat_poll >= 5:
                last_beat_poll = time.time()
                for svc, d in conn.execute("SELECT service, detail FROM service_heartbeats "
                                           "WHERE service LIKE 'mqtt_listener/%%'"):
                    if d and (not beats.get(svc) or beats[svc][-1].get("rows") != d.get("rows")):
                        beats.setdefault(svc, []).append(d)
                c = docker_cpu(args.broker_container) if args.broker_container else None
                if c is not None:
                    broker_cpu.append(c)
                c = docker_cpu(args.postgres_container) if args.postgres_container else None
                if c is not None:
                    pg_cpu.append(c)
            if n >= pub["acked"]:
                break
            time.sleep(0.5)
        for _ in range(20):  # the exact count, once the stats caught up
            n = stored()
            if n >= pub["acked"]:
                break
            time.sleep(1)
    finally:
        lst.send_signal(signal.SIGTERM)
        try:
            lst.wait(timeout=30)
        except subprocess.TimeoutExpired:
            lst.kill()
        log.close()

    def at(frac: float) -> tuple[float, int]:
        goal = pub["acked"] * frac
        return next(((t, k) for t, k in marks if k >= goal), marks[-1])

    (t1, n1), (t2, n2) = at(0.1), at(0.9)
    rate = (n2 - n1) / (t2 - t1) if t2 > t1 else None
    per_worker = {}
    for svc, ds in sorted(beats.items()):
        busy = [d for d in ds if d.get("readings_per_s", 0) > 0]
        if busy:
            per_worker[svc] = {"readings_per_s_mean": round(statistics.mean(d["readings_per_s"] for d in busy)),
                               "readings_per_s_max": round(max(d["readings_per_s"] for d in busy)),
                               "cpu_pct_mean": round(statistics.mean(d["cpu_pct"] for d in busy), 1),
                               "flush_busy_pct_mean": round(statistics.mean(d.get("flush_busy_pct", 0) for d in busy), 1),
                               "rows": ds[-1]["rows"], "blocked_s": ds[-1].get("blocked_s")}
    result = {"workers": workers, **pub, "stored": n, "missing": pub["acked"] - n,
              "drain_s": round(marks[-1][0] - t_start, 1),
              "stored_rps_mid80": round(rate) if rate else None,
              "stored_rps_per_worker": round(rate / workers) if rate else None,
              "per_worker": per_worker,
              "broker_cpu_pct_mean": round(statistics.mean(broker_cpu), 1) if broker_cpu else None,
              "broker_cpu_pct_max": round(max(broker_cpu), 1) if broker_cpu else None,
              "postgres_cpu_pct_mean": round(statistics.mean(pg_cpu), 1) if pg_cpu else None,
              "loadavg_end": os.getloadavg()}
    print(json.dumps(result, indent=1), flush=True)
    return result


def main(argv=None) -> None:
    env = common.read_env()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workers", default="1,2,3,4", help="comma list of worker counts to run")
    ap.add_argument("--messages", type=int, default=300_000)
    ap.add_argument("--points", type=int, default=20_000)
    ap.add_argument("--publishers", type=int, default=2)
    ap.add_argument("--envelope", choices=["point", "batch"], default="point",
                    help="per-point messages, or Windrow's batched envelope: one message per 10-point equip "
                         "per tick (Baler#234)")
    ap.add_argument("--topic-pattern", default=f"{PREFIX}/#",
                    help="the listener's subscription (default: only the bench's topics, so a stray "
                         "scale generator doesn't skew it)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(env.get("SCALE_MQTT_PORT", "21883")))
    ap.add_argument("--username", default="baler")
    ap.add_argument("--password", default=os.environ.get("MQTT_PASSWORD") or env.get("MQTT_PASSWORD", ""))
    ap.add_argument("--pub-username", default="scalegen")
    ap.add_argument("--pub-password", default=env.get("SCALE_GEN_MQTT_PASSWORD", ""))
    ap.add_argument("--broker-container", default=f"{common.PROJECT}-mosquitto-1")
    ap.add_argument("--postgres-container", default=f"{common.PROJECT}-postgres-1")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--log", default=str(common.RESULTS / "listener-bench.log"))
    args = ap.parse_args(argv)
    import psycopg

    common.RESULTS.mkdir(exist_ok=True)
    conn = psycopg.connect(os.environ["BALER_PG_DSN"], autocommit=True)
    runs = [run_one(args, int(w), conn) for w in args.workers.split(",")]
    result = {"run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), "kind": "listener-bench",
              "hardware": common.hardware(), "commits": common.commits(),
              "config": {k: v for k, v in vars(args).items() if "password" not in k}, "runs": runs}
    path = common.RESULTS / f"{result['run_id']}-listener-bench.json"
    path.write_text(json.dumps(result, indent=1, default=str))
    print("\n| Envelope | MQTT messages | Workers | Stored/s (mid 80%) | Per worker | Worker CPU % (each) | Batcher writing % (each) | "
          "Postgres CPU % | Broker CPU % | Missing |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in runs:
        cpus = ", ".join(str(w["cpu_pct_mean"]) for w in r["per_worker"].values())
        busy = ", ".join(str(w["flush_busy_pct_mean"]) for w in r["per_worker"].values())
        print(f"| {r['envelope']} | {r['mqtt_messages']} | {r['workers']} | {r['stored_rps_mid80']} | {r['stored_rps_per_worker']} | {cpus} | {busy} | "
              f"{r['postgres_cpu_pct_mean']} | {r['broker_cpu_pct_mean']} | {r['missing']} |")
    print(f"-> {path}")


if __name__ == "__main__":
    main()

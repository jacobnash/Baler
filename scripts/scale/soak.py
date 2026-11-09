"""
Long soak at one point count (Baler#245): does point_history's disk plateau
under sustained load once compression keeps up, and do ingest lag, read
probes, the continuous aggregates and late writes stay fine through the
compression cycles?

  python scripts/scale/ladder.py soak --points 100000 --duration 21600

Set the chunking under test in the scale env file before `up`, so every
service agrees on it (they keep the policy in step with their environment
at start):

  BALER_HISTORY_CHUNK_INTERVAL=1 hour
  BALER_HISTORY_COMPRESS_AFTER=3 hours

Every --sample-every s it records disk (point_history total, hot =
uncompressed chunks, compressed chunks, WAL, database), the chunks (oldest
uncompressed, the backlog of chunks old enough to compress but not yet),
the compression job (running?, each run's duration, chunks and rows, rows/s),
the continuous aggregates' watermarks, ingest lag (the canary) and
container restarts. Every --probe-every s it runs the read probes
(budgets.json's, plus `soak_probes`: last hour, the hot/compressed boundary
and pastWeek), noting which rounds overlapped a compression run. Every
--late-every s, once a chunk is compressed, it replays one reading of every
point into a compressed chunk over MQTT (the same (point, ts) again: a spool
or retained replay) and backfills one new reading between existing ones
(Windrow's trend backfill), then checks every one landed, once.

`evaluate_soak` (pure, tested) turns that into pass/fail against #245's
acceptance. Results go to scripts/scale/results/<run>-soak.json; see
docs-site/pages/scale-test.mdx.
"""

from __future__ import annotations

import json
import os
import signal
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
import ladder

LATE_REPLAY_VALUE = 999.25  # inside the scale-range rule's bounds: no fault storm
LATE_BACKFILL_VALUE = 998.75
LATE_BACKFILL_OFFSET_S = 1.234567  # well inside a 5 s sample spacing
# Acceptance (#245)
HOT_GROWTH_MAX = 1.25  # hot bytes, second half of the steady phase vs the first
GROWTH_VS_COMPRESSED_MAX = 2.0  # phase-aligned growth per stored row vs compressed bytes per row


# ----------------------------------------------------------------- settings and disk

def settings(conn) -> dict:
    row = conn.execute("""
        SELECT extract(epoch FROM d.time_interval),
               extract(epoch FROM (j.config->>'compress_after')::interval),
               extract(epoch FROM j.schedule_interval), j.job_id, j.config
        FROM timescaledb_information.dimensions d
        LEFT JOIN timescaledb_information.jobs j
               ON j.proc_name = 'policy_compression' AND j.hypertable_name = 'point_history'
        WHERE d.hypertable_name = 'point_history' AND d.column_name = 'ts'""").fetchone()
    return {"chunk_interval_s": float(row[0]), "compress_after_s": float(row[1]) if row[1] is not None else None,
            "schedule_s": float(row[2]) if row[2] is not None else None, "job_id": row[3], "job_config": row[4]}


def disk(conn) -> dict:
    """Bytes now: hot (uncompressed chunks), compressed chunks, total, WAL."""
    rows = conn.execute("""
        SELECT c.is_compressed, count(*), COALESCE(sum(d.total_bytes), 0)
        FROM timescaledb_information.chunks c
        JOIN chunks_detailed_size('point_history') d ON d.chunk_schema = c.chunk_schema AND d.chunk_name = c.chunk_name
        WHERE c.hypertable_name = 'point_history' GROUP BY 1""").fetchall()
    by = {bool(k): (int(n), int(b)) for k, n, b in rows}
    out = {"hot_chunks": by.get(False, (0, 0))[0], "hot_bytes": by.get(False, (0, 0))[1],
           "compressed_chunks": by.get(True, (0, 0))[0], "compressed_bytes": by.get(True, (0, 0))[1],
           "total_bytes": int(conn.execute("SELECT hypertable_size('point_history')").fetchone()[0] or 0),
           "db_bytes": int(conn.execute("SELECT pg_database_size(current_database())").fetchone()[0])}
    stats = conn.execute("SELECT before_compression_total_bytes, after_compression_total_bytes "
                         "FROM hypertable_compression_stats('point_history')").fetchone()
    out["before_compression_bytes"], out["after_compression_bytes"] = (int(stats[0] or 0), int(stats[1] or 0)) if stats else (0, 0)
    try:
        out["wal_bytes"] = int(conn.execute("SELECT COALESCE(sum(size), 0) FROM pg_ls_waldir()").fetchone()[0])
    except Exception:  # noqa: BLE001 - needs pg_monitor; the image's role is a superuser
        out["wal_bytes"] = None
    return out


def chunks(conn, compress_after_s: float | None) -> dict:
    """The chunk state that says whether compression keeps up: the oldest
    uncompressed chunk, and how many are old enough to compress but aren't."""
    after = timedelta(seconds=compress_after_s or 0)
    rows = conn.execute("""
        SELECT format('%I.%I', c.chunk_schema, c.chunk_name), c.range_start, c.range_end, c.is_compressed,
               COALESCE(s.n_tup_ins, 0)
        FROM timescaledb_information.chunks c
        LEFT JOIN pg_stat_user_tables s ON s.schemaname = c.chunk_schema AND s.relname = c.chunk_name
        WHERE c.hypertable_name = 'point_history' ORDER BY c.range_start""").fetchall()
    now = datetime.now(timezone.utc)
    hot = [r for r in rows if not r[3]]
    eligible = [r for r in hot if r[2] < now - after] if compress_after_s is not None else []
    return {"all": {name: {"start": start, "end": end, "compressed": comp, "inserts": ins}
                    for name, start, end, comp, ins in rows},
            "oldest_hot_end_age_s": round((now - hot[0][2]).total_seconds(), 1) if hot else None,
            "backlog": len(eligible)}


def compression_job(conn, job_id) -> dict:
    if job_id is None:
        return {}
    row = conn.execute("""
        SELECT job_status, CASE WHEN isfinite(last_run_started_at) THEN last_run_started_at END,
               extract(epoch FROM last_run_duration), last_run_status,
               total_runs, total_failures, CASE WHEN isfinite(next_start) THEN next_start END
        FROM timescaledb_information.job_stats WHERE job_id = %s""", (job_id,)).fetchone()
    if row is None:
        return {}
    return {"status": row[0], "last_started": row[1], "last_duration_s": float(row[2]) if row[2] is not None else None,
            "last_status": row[3], "runs": row[4], "failures": row[5], "next_start": row[6]}


def compression_running(conn) -> bool:
    """A compression policy run in progress, from pg_stat_activity (job_stats
    only says so on some versions)."""
    return bool(conn.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE backend_type LIKE '%Columnstore%' OR backend_type LIKE "
        "'%Compression%' OR (application_name LIKE '%Columnstore Policy%' OR application_name LIKE "
        "'%Compression Policy%')").fetchone()[0])


def watermarks(conn) -> dict:
    """Seconds behind now of each continuous aggregate's watermark."""
    out = {}
    try:
        for view, age in conn.execute("""
                SELECT ca.user_view_name,
                       extract(epoch FROM now() - _timescaledb_functions.to_timestamp(
                           _timescaledb_functions.cagg_watermark(ca.mat_hypertable_id)))
                FROM _timescaledb_catalog.continuous_agg ca
                WHERE ca.user_view_name IN ('point_history_5m', 'point_history_1h')"""):
            # never materialized yet: the watermark is the type's minimum
            out[view] = round(float(age), 1) if age is not None and age < 365 * 86400 else None
    except Exception as exc:  # noqa: BLE001 - catalog differs on another TimescaleDB
        out["error"] = repr(exc)[:200]
    return out


def cagg_check(conn, points: list[str]) -> dict:
    """The 5-minute aggregate against the raw fold, over the newest hour that
    is compressed and below the watermark: refreshes over compressed chunks
    keep them equal."""
    row = conn.execute("""
        SELECT max(c.range_end) FROM timescaledb_information.chunks c
        WHERE c.hypertable_name = 'point_history' AND c.is_compressed""").fetchone()
    if not row or row[0] is None:
        return {"checked": False, "why": "no compressed chunk"}
    end = row[0].replace(second=0, microsecond=0)
    end = end - timedelta(minutes=end.minute % 5)
    start = end - timedelta(hours=1)
    v = "COALESCE(value, value_bool::int::float8)"
    raw = {u: (int(n), round(float(t or 0), 6)) for u, n, t in conn.execute(
        f"SELECT point_uri, count({v}), sum({v}) FROM point_history "
        "WHERE point_uri = ANY(%s) AND ts >= %s AND ts < %s GROUP BY 1", (points, start, end))}
    agg = {u: (int(n), round(float(t or 0), 6)) for u, n, t in conn.execute(
        "SELECT point_uri, sum(vcount), sum(vsum) FROM point_history_5m "
        "WHERE point_uri = ANY(%s) AND bucket >= %s AND bucket < %s GROUP BY 1", (points, start, end))}
    return {"checked": True, "window": [start, end], "points": len(points), "equal": raw == agg,
            "raw_rows": sum(n for n, _ in raw.values())}


# ----------------------------------------------------------------- late writes

class LateWriter:
    """A replay and a backfill into a compressed chunk, over MQTT."""

    def __init__(self, ep: ladder.Endpoints, interval: float):
        import paho.mqtt.client as mqtt

        self.ep, self.interval = ep, interval
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"scale-late-{os.getpid()}")
        self.c.username_pw_set("scalegen", ep.gen_password)
        self.c.max_inflight_messages_set(1000)
        self.c.connect("127.0.0.1", ep.mqtt_port)
        self.c.loop_start()

    def close(self):
        self.c.loop_stop()
        self.c.disconnect()

    def run(self, conn, rate: float) -> dict:
        """One reading per point from a slice near the end of the newest compressed chunk:
        each replayed as is (same ts, new value) and backfilled 1.23 s later,
        published at `rate` messages/s (the soak's own rate)."""
        chunk = conn.execute("""
            SELECT format('%I.%I', chunk_schema, chunk_name), range_start, range_end
            FROM timescaledb_information.chunks WHERE hypertable_name = 'point_history' AND is_compressed
            ORDER BY range_end DESC LIMIT 1""").fetchone()
        if chunk is None:
            return {"skipped": "no compressed chunk yet"}
        name, start, end = chunk
        mid = end - timedelta(seconds=3 * self.interval)  # near its end: a chunk the soak began part-way into has data there
        t0 = time.time()
        rows = conn.execute(
            "SELECT DISTINCT ON (point_uri) point_uri, ts FROM point_history WHERE point_uri LIKE %s "
            "AND ts >= %s AND ts < %s ORDER BY point_uri, ts",
            (ladder.POINT_LIKE, mid, mid + timedelta(seconds=self.interval))).fetchall()
        select_s = time.time() - t0
        if not rows:
            return {"skipped": f"no readings in {name} at {mid}"}
        msgs = []
        for uri, ts in rows:
            topic = uri.removeprefix("urn:point:")
            label = topic.rsplit("/", 1)[1]
            t = ts.timestamp()
            msgs.append((topic, common.envelope(label, LATE_REPLAY_VALUE, t)))
            msgs.append((topic, common.envelope(label, LATE_BACKFILL_VALUE, round(t + LATE_BACKFILL_OFFSET_S, 6))))
        t_pub = time.time()
        infos = []
        for i, (topic, payload) in enumerate(msgs):
            infos.append(self.c.publish(topic, payload, qos=1, retain=False))  # never clobber the retained value
            ahead = t_pub + (i + 1) / rate - time.time()
            if ahead > 0:
                time.sleep(ahead)
        for info in infos:
            info.wait_for_publish(timeout=60)
        publish_s = time.time() - t_pub
        backfill = [(uri, ts + timedelta(seconds=LATE_BACKFILL_OFFSET_S)) for uri, ts in rows]
        uris = [u for u, _ in rows]
        replay_ts = [t for _, t in rows]
        backfill_ts = [t for _, t in backfill]
        check = """
            SELECT count(*), count(DISTINCT (p.point_uri, p.ts)), count(*) FILTER (WHERE p.value = %s)
            FROM point_history p JOIN unnest(%s::text[], %s::timestamptz[]) AS k(uri, ts)
                 ON p.point_uri = k.uri AND p.ts = k.ts"""
        landed = None
        deadline = time.time() + 300
        while time.time() < deadline:
            r = conn.execute(check, (LATE_REPLAY_VALUE, uris, replay_ts)).fetchone()
            b = conn.execute(check, (LATE_BACKFILL_VALUE, uris, backfill_ts)).fetchone()
            landed = {"replay": r, "backfill": b}
            if r[2] == len(rows) and b[2] == len(rows):
                break
            time.sleep(5)
        assert landed is not None
        r, b = landed["replay"], landed["backfill"]
        dupes = conn.execute(
            "SELECT count(*) - count(DISTINCT (point_uri, ts)) FROM point_history WHERE point_uri = ANY(%s) "
            "AND ts >= %s AND ts < %s", (uris, mid, mid + timedelta(seconds=self.interval + 2))).fetchone()[0]
        ok = r[0] == r[1] == r[2] == len(rows) and b[0] == b[1] == b[2] == len(rows) and dupes == 0
        return {"chunk": name, "chunk_range": [start, end], "points": len(rows), "messages": len(msgs),
                "select_s": round(select_s, 2), "publish_s": round(publish_s, 1),
                "landed_s": round(time.time() - t_pub, 1),
                "replayed": r[2], "backfilled": b[2], "duplicates": dupes, "ok": ok}


# ----------------------------------------------------------------- the run

def soak_probes(budgets: dict, shape: common.Shape, cfg: dict, now: float) -> dict:
    """budgets.json plus its soak_probes, with this round's time windows."""
    point0 = f"{shape.equip_prefix(0)}/{common.TEMPLATES[0][0]}"
    boundary = (cfg.get("compress_after_s") or 0) + 2 * cfg["chunk_interval_s"]
    ctx = {"point0": point0, "start_1h": f"{now - 3600:.0f}", "start_boundary": f"{now - boundary:.0f}"}
    probes = []
    for p in budgets["probes"] + budgets.get("soak_probes", []):
        p = dict(p, params={k: v.format(**ctx) if isinstance(v, str) else v for k, v in p["params"].items()})
        probes.append(p)
    return {"runs": budgets.get("runs", 5), "probes": probes}


def cmd_soak(args) -> None:
    config = ladder.guard()
    ep = ladder.cmd_up(args) if not args.no_up else ladder.Endpoints(config)
    ladder.ensure_rules(ep)
    budgets = json.loads(Path(args.budgets).read_text())
    conn = ladder.pg_connect(ep)
    cfg = settings(conn)
    if cfg["chunk_interval_s"] >= 7 * 86400 and not args.allow_default_chunks:
        raise SystemExit("point_history still has 7-day chunks: set BALER_HISTORY_CHUNK_INTERVAL and "
                         f"BALER_HISTORY_COMPRESS_AFTER in {common.ENV_FILE} and `up` again (or --allow-default-chunks)")
    shape = common.Shape(args.points, args.points_per_equip, args.equips_per_site)
    result = ladder.run_header(ep, args)
    result.update(kind="soak", points=args.points, settings=cfg, samples=[], probe_rounds=[], late=[], runs=[])
    outdir = common.RESULTS / result["run_id"]
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"soak: {args.points:,} points for {args.duration:.0f} s, chunk {cfg['chunk_interval_s']:.0f} s, "
          f"compress_after {cfg['compress_after_s']} s, policy every {cfg['schedule_s']} s", flush=True)

    existing = ladder.graph_counts(ep)["scale_points"]
    if existing < args.points:
        ladder.start_loadgen(ep, args, args.points, "tags", outdir / "gen-tags.json", tags_from=existing).wait()
        deadline = time.time() + args.graph_timeout
        while time.time() < deadline and ladder.graph_counts(ep)["scale_points"] < args.points:
            time.sleep(5)
    stats_file = outdir / "gen-readings.json"
    gen = ladder.start_loadgen(ep, args, args.points, "readings", stats_file)
    canary = ladder.Canary(ep)
    late = LateWriter(ep, args.interval)
    states0 = ladder.container_states()
    seen: dict[str, dict] = {}  # chunk -> last state seen (inserts while hot)
    last_job: dict = {}
    pending_runs: list[dict] = []
    state = {"running": False}
    probe_ctx: dict = {}
    stop = threading.Event()

    def watch_running():
        """Compression in progress, polled every 2 s (finer than samples), so
        probe rounds and lag samples can be attributed to it."""
        c = ladder.pg_connect(ep)
        while not stop.wait(2):
            try:
                state["running"] = compression_running(c)
                if state["running"]:
                    state["last_seen_running"] = time.time()
            except Exception:  # noqa: BLE001
                c = ladder.pg_connect(ep)
        c.close()

    watcher = threading.Thread(target=watch_running, daemon=True)
    watcher.start()
    t_start = time.time()
    next_probe = t_start + args.probe_every
    next_late = t_start + args.late_every
    rate = args.points / args.interval
    try:
        time.sleep(args.warmup)
        w0 = ladder.chunk_writes(conn)
        soak_start = time.time()
        while time.time() - soak_start < args.duration:
            time.sleep(args.sample_every)
            now = time.time()
            try:
                ch = chunks(conn, cfg["compress_after_s"])
                job = compression_job(conn, cfg["job_id"])
                disk_now, wm_now, writes_now = disk(conn), watermarks(conn), ladder.chunk_writes(conn)
            except Exception as exc:  # noqa: BLE001 - one bad catalog read must not end a 6 h soak
                print(f"  sample at t={now - soak_start:.0f}s skipped: {exc!r}", flush=True)
                result.setdefault("sample_errors", []).append({"t": round(now - soak_start, 1), "error": repr(exc)[:300]})
                conn = ladder.pg_connect(ep)
                continue
            # a finished run: attribute the chunks that turned compressed since the last one
            newly = [n for n, c in ch["all"].items() if c["compressed"] and not seen.get(n, {}).get("compressed", True)]
            for n in newly:
                pending_runs.append({"chunk": n, "inserts": seen[n]["inserts"],
                                     "at": round(now - soak_start, 1)})
            if job.get("last_started") and job.get("last_started") != last_job.get("last_started") \
                    and job.get("status") != "Running" and job.get("last_duration_s") is not None:
                rows = sum(p["inserts"] for p in pending_runs)
                result["runs"].append({"started": job["last_started"], "duration_s": round(job["last_duration_s"], 2),
                                       "chunks": len(pending_runs), "rows": rows, "status": job.get("last_status"),
                                       "rows_per_s": round(rows / job["last_duration_s"]) if job["last_duration_s"] else None,
                                       "t": round(now - soak_start, 1)})
                pending_runs = []
                last_job = job
            elif not last_job:
                last_job = job
            seen = {n: dict(c, inserts=max(c["inserts"], seen.get(n, {}).get("inserts", 0))) for n, c in ch["all"].items()}
            st = ladder.read_stats(stats_file)
            sample = {"t": round(now - soak_start, 1), "stored_total": writes_now - w0,
                      "gen_rate": round(st.get("sent_rate_10s", 0)), "gen_behind_s": round(st.get("behind_s", 0), 1),
                      "disk": disk_now, "oldest_hot_end_age_s": ch["oldest_hot_end_age_s"], "backlog": ch["backlog"],
                      "chunks": len(ch["all"]), "job": {k: v for k, v in job.items() if k != "next_start"},
                      "compressing": state["running"] or job.get("status") == "Running",
                      "watermark_age_s": wm_now, "lag": canary.stats(soak_start),
                      "docker": ladder.docker_stats() if args.docker_stats else {}}
            t1 = time.perf_counter()
            conn.execute("SELECT 1")
            sample["pg_roundtrip_s"] = round(time.perf_counter() - t1, 4)
            result["samples"].append(sample)
            # Docker's data root, where the stack's volumes grow (#269), not the checkout's disk
            disk_low, free = ladder.disk_check(args.min_free_gb)
            sample["free"] = free
            d = sample["disk"]
            print(f"  t={sample['t']:>7}s gen {sample['gen_rate']}/s lag p95 {sample['lag'].get('p95_s')} s "
                  f"hot {d['hot_bytes'] / 1e6:.0f} MB ({d['hot_chunks']}) compressed {d['compressed_bytes'] / 1e6:.0f} MB "
                  f"({d['compressed_chunks']}) backlog {ch['backlog']} oldest-hot {ch['oldest_hot_end_age_s']} s "
                  f"{'COMPRESSING ' if sample['compressing'] else ''}runs {len(result['runs'])} "
                  f"disk free: docker root {free['docker_root_free_gb']} GB, checkout {free['repo_free_gb']} GB",
                  flush=True)
            if now >= next_probe:
                was = state["running"] or bool(state.get("last_seen_running", 0) > now - args.sample_every)
                t_round = time.time()
                s = ladder.session(ep)
                if "cx_project" not in probe_ctx:
                    try:
                        probe_ctx["cx_project"] = ladder.ensure_cx_project(ep, s, shape)
                    except Exception as exc:  # noqa: BLE001 - a Cx failure is a result
                        probe_ctx["cx_project"] = None
                        result["cx_error"] = repr(exc)[:300]
                probes = ladder.run_probes(ep, s, soak_probes(budgets, shape, cfg, time.time()), probe_ctx)
                result["probe_rounds"].append({"t": round(t_round - soak_start, 1), "probes": probes,
                                               "during_compression": was or state["running"] or bool(
                                                   state.get("last_seen_running", 0) >= t_round)})
                next_probe = time.time() + args.probe_every
                over = [p["name"] for p in probes if not p.get("skipped") and (p["status"] != 200 or p["median_s"] > p["budget_s"])]
                print(f"  probes at t={t_round - soak_start:.0f}s{' (during compression)' if result['probe_rounds'][-1]['during_compression'] else ''}: "
                      f"{'all in budget' if not over else 'OVER: ' + ', '.join(over)}", flush=True)
            if now >= next_late and sample["disk"]["compressed_chunks"]:
                entry = late.run(conn, rate)
                entry["t"] = round(time.time() - soak_start, 1)
                result["late"].append(entry)
                next_late = time.time() + args.late_every
                print(f"  late writes into compressed chunk: {entry}", flush=True)
            if disk_low:
                result["disk_low"] = (free["docker_root_free_gb"] if free["docker_root_free_gb"] is not None
                                      else free["repo_free_gb"])
                break
            ladder.write_result(result)
        soak_end = time.time()
    finally:
        stop.set()
        gen.send_signal(signal.SIGTERM)
        try:
            gen.wait(timeout=60)
        except subprocess.TimeoutExpired:
            gen.kill()
        canary.drain(max_wait=args.abort_lag)
        canary.close()
        late.close()

    st = ladder.read_stats(stats_file)
    a, b = int(soak_start) + 1, int(soak_end)
    acked = ladder.acked_in(st, a, b)
    backfilled_in = sum(e.get("backfilled", 0) for e in result["late"] if e.get("chunk_range")
                        and e["chunk_range"][1].timestamp() > a and e["chunk_range"][0].timestamp() < b)
    t_count = time.time()
    stored = ladder.stored_in(conn, a, b) - backfilled_in
    result["ingest"] = {"window_s": b - a, "acked": acked, "stored": stored, "count_s": round(time.time() - t_count, 1),
                        "stored_ratio": round(stored / acked, 5) if acked else None,
                        "offered_rps": round(acked / max(b - a, 1), 1),
                        "stored_rps": round(result["samples"][-1]["stored_total"] / max(soak_end - soak_start, 1), 1)
                        if result["samples"] else None}
    result["lag"] = canary.stats(soak_start)
    pts = [f"urn:point:{shape.equip_prefix(g)}/{common.TEMPLATES[j][0]}" for g in range(min(5, shape.equips)) for j in range(2)]
    result["cagg_check"] = cagg_check(conn, pts)
    result["db"] = ladder.db_metrics(conn)
    states1 = ladder.container_states()
    result["containers"] = {svc: {**c, "restarts_delta": c["restarts"] - states0.get(svc, {}).get("restarts", 0)}
                            for svc, c in states1.items()}
    result["soak_s"] = round(soak_end - soak_start, 1)
    result["summary"] = summarize(result)
    result["status"], result["reasons"] = evaluate_soak(result)
    conn.close()
    path = ladder.write_result(result)
    print(f"\nsoak: {result['status']} {result['reasons']}\n{result['summary']}\n-> {path}")


# ----------------------------------------------------------------- evaluation

def _lag_p95(values: list[float]) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(len(v) * 0.95))]


def summarize(result: dict) -> dict:
    """The numbers the acceptance is judged on. Pure (tested)."""
    cfg = result["settings"]
    chunk, after, sched = cfg["chunk_interval_s"], cfg.get("compress_after_s") or 0, cfg.get("schedule_s") or 0
    samples = result.get("samples", [])
    warm = after + chunk + sched + chunk  # the first chunk compressed, and one more cycle
    steady = [s for s in samples if s["t"] >= warm]
    out: dict = {"warm_s": warm, "steady_samples": len(steady),
                 "steady_s": round(steady[-1]["t"] - steady[0]["t"], 1) if len(steady) > 1 else 0}
    runs = [r for r in result.get("runs", []) if r.get("rows")]
    out["compression_runs"] = len(result.get("runs", []))
    out["compression_rows_per_s_median"] = round(statistics.median(r["rows_per_s"] for r in runs)) if runs else None
    out["compression_duration_s_max"] = max((r["duration_s"] for r in result.get("runs", [])), default=None)
    # Ingest rate: what the broker acknowledged (stored_rps, from the chunks'
    # pg_stat counters, also counts the rows decompression and recompression
    # move, so it reads high once late writes hit compressed chunks).
    ing = result.get("ingest") or {}
    rate = ing.get("offered_rps") or ing.get("stored_rps")
    if out["compression_rows_per_s_median"] and rate:
        out["compression_margin"] = round(out["compression_rows_per_s_median"] / rate, 1)
    last = samples[-1]["disk"] if samples else {}
    if last.get("after_compression_bytes"):
        out["compression_ratio"] = round(last["before_compression_bytes"] / last["after_compression_bytes"], 1)
    rows_compressed = sum(r["rows"] for r in runs)
    if rows_compressed and last.get("compressed_bytes"):
        out["compressed_bytes_per_row"] = round(last["compressed_bytes"] / rows_compressed, 2)
        out["raw_bytes_per_row"] = round(last["before_compression_bytes"] / rows_compressed, 1)
    if len(steady) > 1:
        half = len(steady) // 2
        out["hot_bytes_max_first_half"] = max(s["disk"]["hot_bytes"] for s in steady[:half] or steady)
        out["hot_bytes_max_second_half"] = max(s["disk"]["hot_bytes"] for s in steady[half:])
        out["oldest_hot_end_age_s_max"] = max((s["oldest_hot_end_age_s"] or 0) for s in steady)
        third = max(1, len(steady) // 3)
        out["backlog_max_middle"] = max(s["backlog"] for s in steady[third:2 * third] or steady)
        out["backlog_max_last"] = max(s["backlog"] for s in steady[-third:])
        # Growth per stored row between two samples a whole number of chunk
        # intervals apart (the same point of the hot sawtooth), as late as
        # possible: the steady-state rate disk grows at.
        span = int((steady[-1]["t"] - steady[0]["t"]) // chunk) * chunk
        if span >= chunk:
            end = steady[-1]
            begin = min(steady, key=lambda s: abs(s["t"] - (end["t"] - span)))
            rows = rate * (end["t"] - begin["t"]) if rate else end["stored_total"] - begin["stored_total"]
            if rows > 0:
                out["aligned_window_s"] = round(end["t"] - begin["t"], 1)
                out["growth_bytes_per_row"] = round((end["disk"]["total_bytes"] - begin["disk"]["total_bytes"]) / rows, 2)
                out["growth_bytes_per_day_at_this_rate"] = round(out["growth_bytes_per_row"] * rows
                                                                 / max(end["t"] - begin["t"], 1) * 86400)
        lags_c = [s["lag"].get("p95_s") for s in steady if s.get("compressing") and s["lag"].get("p95_s") is not None]
        out["lag_p95_during_compression_max"] = max(lags_c, default=None)
        out["watermark_age_s_max"] = {v: max((s["watermark_age_s"].get(v) or 0) for s in steady)
                                      for v in ("point_history_5m", "point_history_1h")}
    rounds = result.get("probe_rounds", [])
    out["probe_rounds"] = len(rounds)
    out["probe_rounds_during_compression"] = sum(1 for r in rounds if r.get("during_compression"))
    out["compression_cycles_steady"] = sum(1 for r in result.get("runs", []) if r.get("t", 0) >= warm and r.get("chunks"))
    return out


def evaluate_soak(result: dict) -> tuple[str, list[str]]:
    """'pass' | 'fail' | 'inconclusive' against #245's acceptance. Pure (tested)."""
    reasons, other = [], []
    s = result.get("summary") or summarize(result)
    cfg = result["settings"]
    chunk, after, sched = cfg["chunk_interval_s"], cfg.get("compress_after_s") or 0, cfg.get("schedule_s") or 0
    if result.get("disk_low"):
        other.append("host disk low; stopped (not a Baler ceiling)")
    if s.get("steady_s", 0) < 2 * chunk or s.get("compression_cycles_steady", 0) < 2:
        other.append(f"soak too short: {s.get('steady_s', 0):.0f} s steady after a {s['warm_s']:.0f} s warm-up, "
                     f"{s.get('compression_cycles_steady', 0)} compression cycle(s); needs 2 chunk intervals and 2 cycles")
    # disk plateaus
    if s.get("hot_bytes_max_second_half") is not None and s["hot_bytes_max_second_half"] > \
            HOT_GROWTH_MAX * s["hot_bytes_max_first_half"]:
        reasons.append(f"hot window still growing: {s['hot_bytes_max_first_half'] / 1e6:.0f} MB -> "
                       f"{s['hot_bytes_max_second_half'] / 1e6:.0f} MB uncompressed")
    bound = after + chunk + 2 * sched + (s.get("compression_duration_s_max") or 0)
    if s.get("oldest_hot_end_age_s_max") is not None and s["oldest_hot_end_age_s_max"] > bound:
        reasons.append(f"compression behind: a chunk stayed uncompressed {s['oldest_hot_end_age_s_max']:.0f} s past its "
                       f"end (bound {bound:.0f} s = compress_after + chunk + 2 x schedule + longest run)")
    if s.get("backlog_max_last") is not None and s["backlog_max_last"] > max(s["backlog_max_middle"], 1):
        reasons.append(f"compression backlog growing: {s['backlog_max_middle']} -> {s['backlog_max_last']} chunks")
    if s.get("growth_bytes_per_row") is not None and s.get("compressed_bytes_per_row"):
        if s["growth_bytes_per_row"] > GROWTH_VS_COMPRESSED_MAX * s["compressed_bytes_per_row"]:
            reasons.append(f"disk grows {s['growth_bytes_per_row']} B per stored row, over "
                           f"{GROWTH_VS_COMPRESSED_MAX:g}x the compressed {s['compressed_bytes_per_row']} B/row")
    elif not other:
        reasons.append("no steady-state growth measurement (no compressed rows or no aligned window)")
    for r in result.get("runs", []):
        if r.get("status") not in (None, "Success"):
            reasons.append(f"compression run at t={r.get('t')} s: {r['status']}")
            break
    # ingest
    ing = result.get("ingest") or {}
    if ing and (ing.get("stored_ratio") is None or ing["stored_ratio"] < ladder.MIN_STORED_RATIO):
        reasons.append(f"data loss/backlog: stored {ing.get('stored_ratio')} of acknowledged readings")
    lag = result.get("lag") or {}
    if lag.get("lost"):
        reasons.append(f"{lag['lost']} lag canaries never stored")
    if lag and (lag.get("p95_s") is None or lag["p95_s"] >= ladder.MAX_P95_LAG_S):
        reasons.append(f"p95 ingest lag {lag.get('p95_s')} s")
    elif lag.get("last_third_median_s", 0) > lag.get("first_third_median_s", 0) + 10:
        reasons.append(f"growing backlog: lag median {lag['first_third_median_s']} -> {lag['last_third_median_s']} s")
    if (s.get("lag_p95_during_compression_max") or 0) >= ladder.MAX_P95_LAG_S:
        reasons.append(f"p95 lag {s['lag_p95_during_compression_max']} s during compression")
    # reads
    for rnd in result.get("probe_rounds", []):
        for p in rnd["probes"]:
            if p.get("skipped"):
                continue
            when = f" at t={rnd['t']:.0f} s" + (" (during compression)" if rnd.get("during_compression") else "")
            if p["status"] != 200:
                reasons.append(f"probe {p['name']}: HTTP {p['status']}{when}")
            elif p["median_s"] > p["budget_s"]:
                reasons.append(f"probe {p['name']}: median {p['median_s']} s > budget {p['budget_s']} s{when}")
    if result.get("probe_rounds") and not s.get("probe_rounds_during_compression"):
        other.append("no probe round overlapped a compression run: reads during compression not shown")
    # continuous aggregates
    cc = result.get("cagg_check") or {}
    if cc.get("checked") and not cc.get("equal"):
        reasons.append("point_history_5m disagrees with the raw fold over a compressed hour")
    wm = s.get("watermark_age_s_max") or {}
    for view, limit in (("point_history_5m", 300 + 2 * 300 + 300), ("point_history_1h", 3600 + 2 * 1800 + 600)):
        if wm.get(view) and wm[view] > limit:
            reasons.append(f"{view} not refreshing: watermark {wm[view]:.0f} s behind (limit {limit} s)")
    # late data
    late = [e for e in result.get("late", []) if not e.get("skipped")]
    if not late:
        reasons.append("no late writes into a compressed chunk were made")
    for e in late:
        if not e.get("ok"):
            reasons.append(f"late writes at t={e.get('t')} s: replayed {e.get('replayed')}/{e.get('points')}, backfilled "
                           f"{e.get('backfilled')}/{e.get('points')}, duplicates {e.get('duplicates')}")
    for svc, c in (result.get("containers") or {}).items():
        if c.get("restarts_delta"):
            reasons.append(f"{svc} restarted {c['restarts_delta']}x" + (" (OOM-killed)" if c.get("oom_killed") else ""))
        elif c.get("status") not in (None, "running") and svc != "migrate":
            reasons.append(f"{svc} {c['status']} (exit {c.get('exit_code')})")
    if other:
        return "inconclusive", other + reasons
    return ("fail" if reasons else "pass"), reasons


def add_parser(sub, common_args) -> None:
    p = sub.add_parser("soak", help="long soak at one point count: disk plateau and compression cycles (#245)")
    p.set_defaults(func=cmd_soak)
    common_args(p)
    p.add_argument("--points", type=int, required=True)
    p.add_argument("--duration", type=float, default=6 * 3600.0, help="seconds of soak after the warm-up (default 6 h)")
    p.add_argument("--sample-every", type=float, default=30.0)
    p.add_argument("--probe-every", type=float, default=600.0, help="seconds between read-probe rounds")
    p.add_argument("--late-every", type=float, default=1800.0, help="seconds between late-write rounds")
    p.add_argument("--docker-stats", action="store_true", help="docker stats every sample (slow at high counts)")
    p.add_argument("--graph-timeout", type=float, default=3600.0)
    p.add_argument("--abort-lag", type=float, default=300.0)
    p.add_argument("--min-free-gb", type=float, default=5.0, help="stop when Docker's data root has less free")
    p.add_argument("--budgets", default=str(common.HERE / "budgets.json"))
    p.add_argument("--no-up", action="store_true", help="the stack is already up")
    p.add_argument("--no-build", action="store_true", help="up without rebuilding the images")
    p.add_argument("--allow-default-chunks", action="store_true", help="soak with 7-day chunks anyway")

"""
Sizing calculator for the deployment profiles (#195): what a deployment of N
points reporting every S seconds, with K synthetic items per equip, needs.

    python -m baler.sizing --points 200000 --interval 300 --synthetic-per-equip 5

Every figure comes from a measurement in the scale work (#190) and says which
one, or it is labelled for what it is:

- measured     a number a run produced, cited by run or issue;
- extrapolated a measured rate scaled linearly past where it was measured;
- target       a planning target, not a measurement (one server per ~1M
               points, #190's top note);
- pending      a result still to come (#273's realistic profiles, the 6 h
               100k soak in #245). Fill these in when they land: PENDING below.

The page that explains the profiles and these numbers is
docs-site/pages/deployment-profiles.mdx. Nothing here touches a database.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass, field

from baler import history_chunks

GB = 10**9
DAY = 86400
YEAR = 365 * DAY

# ------------------------------------------------------------ measured inputs
# Each constant names its source. Change one only with a new measurement.

# point_history bytes per row uncompressed, table plus indexes: 228-265 B on
# the ladder (scale-test.mdx "Disk growth"), 250 B on nash-srv (#245).
RAW_BYTES_PER_ROW = history_chunks.BYTES_PER_ROW

# Compressed bytes per row depends on how many rows of one point a chunk
# holds (a compressed batch is up to 1,000 rows of one point):
#   full batches: 8.0-8.2 B on scripts/seed_perf.py data, noisy floats
#     (history-compression.mdx "What it saves"); 5 B on nash-srv's 7-day
#     chunks, mostly simulated data (#245). 8.2 is used: the worse of the two.
#   60 rows per point per chunk: 15.7 B (local soak 20261108T235904Z, #270).
# Between them it's interpolated on log(rows); below 60 rows it's unmeasured
# and 15.7 B is only a lower bound.
FULL_BATCH_ROWS = 1000
COMPRESSED_FULL_BATCH = 8.2
COMPRESSED_FULL_BATCH_BEST = 5.0
SMALL_BATCH_ROWS = 60
COMPRESSED_SMALL_BATCH = 15.7

# Listener: one worker process stored 14,921 readings/s at about one core on
# nash-srv (100k step, c36aec3, #190); locally 11,295/s with one worker and
# 6,077-7,083/s per worker with 2-3 workers on a starved 4-vCPU host (#272).
# Workers are planned at 6,000/s each, the low end of the local range.
LISTENER_PER_WORKER_PLAN = 6_000
LISTENER_PER_CORE = 14_921
# Most stored by any worker count in listener_bench (3 workers, local, #272).
# Past it the point_history upsert was the next limiter; more workers past
# this aren't measured to help.
LISTENER_MAX_MEASURED = 18_232

# Compression: one background job did ~33,000 rows/s on nash-srv's 1-CPU
# live box under load (#245, 13.2 M-row chunk in 6 min 39 s). The local
# soak's tiny chunks did 198k rows/s (#270); 33k is the one at real size.
COMPRESS_ROWS_PER_S = 33_000

# Postgres writes per stored row at 5k points: ~585 B WAL + 220 B heap and
# index pages + 130 B TEMP staging + 55 B checkpoints (#271).
PG_WRITE_BYTES_PER_ROW = 990

# Broker: ~550 B of mosquitto memory per queued reading, and each listener
# worker's queue is capped at 500,000 messages / 64 MiB (#272).
BROKER_BYTES_PER_QUEUED = 550
BROKER_QUEUE_MAX = 500_000

# Graph: the harness's 100k points with 10k equips made 1.27 M triples (#274).
TRIPLES_PER_POINT = 1_270_000 / 100_000

# Reads at 100k points after paging (#274): read?filter=point took 1.08 s
# through the gateway (budget 1.5 s), of which the whole-filter match was
# ~0.42 s and grows with points (~4 us a point); the rest is the page.
READ_POINT_BUDGET_S = 1.5
READ_POINT_AT_100K_S = 1.08
READ_MATCH_AT_100K_S = 0.42

# What passed a full ladder step (ingest, reads, no restarts):
#   10k points every 5 s (2,000 readings/s) on nash-srv, the stack on 6
#     hardware threads (c36aec3, #190);
#   25k points every 5 s (5,000 readings/s) on a shared 4-vCPU dev VM after
#     read paging (run 20261109T015355Z, #274).
PASSED_STEPS = ((10_000, 2_000, "nash-srv, 6 threads, c36aec3 (#190)"),
                (25_000, 5_000, "local 4-vCPU VM, run 20261109T015355Z (#274)"))
# Reads stayed in budget, idle, on a 100k-point graph (#274).
READS_MEASURED_POINTS = 100_000

# The scale stack's container caps (scripts/scale/compose.scale.yml) under
# which the 10k-100k steps ran without an OOM kill (#190): Postgres 6 GB,
# Oxigraph 4 GB, and 11.5 GB for every other long-running service together.
PG_MEMORY_FLOOR = 6 * GB
OXIGRAPH_MEMORY_AT_100K = 4 * GB
OTHER_SERVICES_MEMORY = int(11.5 * GB)
BASELINE_THREADS = 6

# Windrow's spool: WINDROW_SPOOL_OUTAGE_HOURS (24) of readings plus 10%,
# capped at WINDROW_SPOOL_MAX_ROWS (5 M), ~100 B a row on disk
# (windrow/mqtt_sink.py's own comment; a design figure, not a measurement).
SPOOL_SLACK = 1.1
SPOOL_MAX_ROWS = 5_000_000
SPOOL_BYTES_PER_ROW = 100

# Building export (building-export.mdx "Measured"): 22.4 M samples, 130 MB,
# exported in 34.8 s, imported in 134 s.
EXPORT_BYTES_PER_SAMPLE = 130e6 / 22.4e6
EXPORT_SAMPLES_PER_S = 22.4e6 / 34.8
IMPORT_SAMPLES_PER_S = 22.4e6 / 134
# Backup (backups.mdx): 2.0 M samples dumped in 12 s to 11 MB gzip'd.
BACKUP_BYTES_PER_SAMPLE = 11e6 / 2.0e6
BACKUP_SAMPLES_PER_S = 2.0e6 / 12

# Planning target, not a measurement (#190's top note, Jacob 2026-11-09):
# one server per roughly 1M points including synthetics, then split by
# portfolio or campus.
TARGET_POINTS_PER_SERVER = 1_000_000

# postgres runs with log_statement=all in docker-compose.yml: 8.4 MB every
# 10 s at 10k points, about 73 GB/day of log (#245).
STATEMENT_LOG_BYTES_PER_DAY_AT_10K = 73 * GB

# Results still to come. When #273 and the soak report, put the measured
# value here and the label switches from "pending" to "measured".
PENDING: dict[str, float | None] = {
    # safe total points per server, per #273 profile (interval s, synthetic per equip)
    "safe_points_typical_1min_n5": None,
    "safe_points_typical_1min_n10": None,
    "safe_points_typical_5min_n5": None,
    "safe_points_typical_5min_n10": None,
    # 6 h, 100k-point soak on nash-srv (#245 acceptance)
    "soak_compressed_bytes_per_row": None,
    "soak_compress_rows_per_s_12_threads": None,
}


# ------------------------------------------------------------------ model


@dataclass(frozen=True)
class Line:
    section: str
    label: str
    value: str
    kind: str  # measured | extrapolated | rule | target | pending | assumption
    basis: str


@dataclass
class Sizing:
    points: int
    interval_s: float
    synthetic_per_equip: int
    points_per_equip: int
    synthetic_history_share: float
    years: float
    buildings: int
    equips: int
    synthetic_points: int
    total_points: int
    readings_per_s: float
    rows_per_s: float
    raw_bytes_per_day: float
    compressed_bytes_per_row: float
    compressed_bytes_per_day: float
    hot_window_bytes: int
    disk_bytes_total: float
    pg_memory_bytes: int
    chunk_interval: str
    compress_after: str
    compress_schedule: str
    rows_per_point_per_chunk: float
    listener_workers: int
    compression_margin: float
    servers_target: int
    read_point_s: float
    spool_rows_per_building: int
    lines: list[Line] = field(default_factory=list)


def compressed_bytes_per_row(rows_per_point_per_chunk: float) -> tuple[float, str]:
    """Compressed bytes per row for a batch size, and its label."""
    r = rows_per_point_per_chunk
    if r >= FULL_BATCH_ROWS:
        return COMPRESSED_FULL_BATCH, "measured"
    if r <= SMALL_BATCH_ROWS:
        return COMPRESSED_SMALL_BATCH, "extrapolated" if r < SMALL_BATCH_ROWS else "measured"
    t = (math.log(r) - math.log(SMALL_BATCH_ROWS)) / (math.log(FULL_BATCH_ROWS) - math.log(SMALL_BATCH_ROWS))
    return COMPRESSED_SMALL_BATCH + t * (COMPRESSED_FULL_BATCH - COMPRESSED_SMALL_BATCH), "extrapolated"


def _gb(b: float) -> str:
    if b >= 1e12:
        return f"{b / 1e12:,.1f} TB"
    if b >= 1e9:
        return f"{b / 1e9:,.1f} GB"
    return f"{b / 1e6:,.0f} MB"


def _dur(s: float) -> str:
    if s < 120:
        return f"{s:,.0f} s"
    if s < 7200:
        return f"{s / 60:,.0f} min"
    if s < 2 * DAY:
        return f"{s / 3600:,.1f} h"
    return f"{s / DAY:,.1f} days"


def size(points: int, interval_s: float, synthetic_per_equip: int = 0, *, points_per_equip: int = 10,
         synthetic_history_share: float = 1.0, years: float = 1.0, pg_memory_bytes: int | None = None,
         buildings: int = 1, outage_hours: float = 24.0) -> Sizing:
    """The sizing for one deployment (one server's worth, or a whole
    portfolio: servers_target says how many servers that is)."""
    if points <= 0 or interval_s <= 0 or points_per_equip <= 0 or buildings <= 0:
        raise ValueError("points, interval, points per equip and buildings must be positive")
    if synthetic_per_equip < 0 or not 0 <= synthetic_history_share <= 1 or years < 0:
        raise ValueError("synthetic per equip >= 0, synthetic history share 0..1, years >= 0")
    lines: list[Line] = []

    def add(section: str, label: str, value: str, kind: str, basis: str) -> None:
        lines.append(Line(section, label, value, kind, basis))

    # -- load
    equips = math.ceil(points / points_per_equip)
    synthetic = equips * synthetic_per_equip
    total = points + synthetic
    readings = points / interval_s
    history_points = points + synthetic * synthetic_history_share
    rows = history_points / interval_s
    add("load", "equips", f"{equips:,}", "assumption", f"{points_per_equip} field points per equip (the ladder's default)")
    add("load", "synthetic points", f"{synthetic:,}", "assumption",
        f"{synthetic_per_equip} per equip (derivations, Cur/His rules); #273 counts them in a step's size")
    add("load", "total points", f"{total:,}", "assumption", "field + synthetic: what graph, memory and the split use")
    add("load", "readings/s over MQTT", f"{readings:,.0f}", "assumption", "field points / interval: the listener's load")
    add("load", "history rows/s", f"{rows:,.0f}", "assumption",
        f"field + {synthetic_history_share:.0%} of synthetics writing history at the same interval "
        "(derivations write rows; rules write faults only)")

    # -- disk
    raw_day = rows * DAY * RAW_BYTES_PER_ROW
    if pg_memory_bytes is None:
        hour_chunk = rows * 3600 * RAW_BYTES_PER_ROW
        pg_memory_bytes = max(PG_MEMORY_FLOOR, math.ceil(hour_chunk / history_chunks.MEMORY_SHARE / GB) * GB)
        pg_basis = ("the scale stack's 6 GB Postgres cap, or enough that a 1-hour chunk is 25% of it "
                    "(#245's rule); pass --pg-memory for what you have")
    else:
        pg_basis = "given (--pg-memory)"
    rec = history_chunks.recommend(rows, pg_memory_bytes)
    rpc = rec.chunk_s / interval_s
    cbr, ckind = compressed_bytes_per_row(rpc)
    soak_cbr = PENDING["soak_compressed_bytes_per_row"]
    comp_day = rows * DAY * cbr
    total_disk = rec.hot_bytes_max + comp_day * years * 365
    add("disk", "raw (uncompressed) per day", _gb(raw_day), "measured",
        f"{RAW_BYTES_PER_ROW} B/row: 228-265 on the ladder, 250 on nash-srv (#245)")
    add("disk", "rows per point per chunk", f"{rpc:,.0f}", "assumption",
        f"chunk {history_chunks._interval(rec.chunk_s)} / {interval_s:g} s; a compressed batch holds up to 1,000")
    cbasis = {
        "measured": (f"{COMPRESSED_FULL_BATCH} B/row with full batches (seed_perf 8.0-8.2; nash-srv 5.0), "
                     "history-compression.mdx, #245"
                     if rpc >= FULL_BATCH_ROWS else "15.7 B/row at 60 rows per point per chunk (soak 20261108T235904Z, #270)"),
        "extrapolated": ("below the smallest measured batch (60 rows, 15.7 B): a lower bound, likely more"
                         if rpc < SMALL_BATCH_ROWS else
                         "interpolated between 15.7 B at 60 rows (#270) and 8.2 B at full batches (#245)"),
    }[ckind]
    add("disk", "compressed per day", f"{_gb(comp_day)} ({cbr:.1f} B/row)", ckind, cbasis)
    add("disk", "compressed B/row at real chunk size, nash-srv soak",
        f"{soak_cbr:.1f} B/row" if soak_cbr is not None else "pending", "measured" if soak_cbr else "pending",
        "6 h, 100k-point soak on nash-srv (#245 acceptance)")
    add("disk", "hot (uncompressed) window, max", _gb(rec.hot_bytes_max), "extrapolated",
        "rows/s x (compress_after + chunk + schedule) x raw B/row (history_chunks.recommend); "
        "the plateau was measured flat on the local soak (#270), not yet at 100k")
    add("disk", f"point_history after {years:g} year(s)", _gb(total_disk), "extrapolated",
        "hot window + compressed/day x days; excludes WAL, graph, aggregates, logs and backups")
    if rpc < FULL_BATCH_ROWS:
        need = math.ceil(FULL_BATCH_ROWS * interval_s / 3600)
        add("disk", "chunk for full compressed batches", f">= {need:,} h", "assumption",
            "at slow reporting short chunks hold few rows per point and compress worse; a longer chunk "
            "needs more Postgres memory under the 25% rule")
    log_day = STATEMENT_LOG_BYTES_PER_DAY_AT_10K * (readings / 2_000)
    add("disk", "postgres statement log (log_statement=all)", f"~{_gb(log_day)}/day", "extrapolated",
        "73 GB/day measured at 10k points every 5 s (#245), scaled by readings/s; docker-compose.yml still "
        "sets it, so cap the log driver or override postgres' command")

    # -- history settings
    env = rec.env()
    add("history", "Postgres memory", _gb(pg_memory_bytes), "assumption", pg_basis)
    add("history", "BALER_HISTORY_CHUNK_INTERVAL", env["BALER_HISTORY_CHUNK_INTERVAL"] or "(blank: 7 days)",
        "rule", "python -m baler.history_chunks recommend: 1 chunk + indexes in 25% of Postgres memory (#245, #270)")
    add("history", "BALER_HISTORY_COMPRESS_AFTER", env["BALER_HISTORY_COMPRESS_AFTER"] or "(blank: 7 days)",
        "rule", "3 chunks (#270)")
    add("history", "BALER_HISTORY_COMPRESS_SCHEDULE", "(blank: half a chunk)", "rule", "TimescaleDB's own rule (#270)")
    for n in rec.notes:
        add("history", "note", n, "assumption", "history_chunks.recommend")
    margin = COMPRESS_ROWS_PER_S / rows
    add("history", "compression margin (one job)", f"{margin:,.1f}x", "extrapolated" if margin < 1 else "measured",
        f"{COMPRESS_ROWS_PER_S:,} rows/s per job on nash-srv's 1-CPU box (#245) / history rows/s; "
        "under ~2x, check `compression_margin` in a soak; 12-thread rate pending")

    # -- ingest
    workers = max(1, math.ceil(readings / LISTENER_PER_WORKER_PLAN))
    add("ingest", "BALER_LISTENER_WORKERS", str(workers), "measured" if readings <= LISTENER_MAX_MEASURED else "extrapolated",
        f"readings/s / {LISTENER_PER_WORKER_PLAN:,} per worker (6-11k/s per worker locally, 14.9k on nash-srv, #272)"
        + ("" if readings <= LISTENER_MAX_MEASURED else
           f"; above the {LISTENER_MAX_MEASURED:,}/s most stored by any worker count (#272): unmeasured"))
    add("ingest", "listener CPU", f"~{readings / LISTENER_PER_CORE:,.2f} cores", "extrapolated",
        f"~1 core per {LISTENER_PER_CORE:,} readings/s (nash-srv 100k step, #190)")
    add("ingest", "Postgres write I/O", f"~{rows * PG_WRITE_BYTES_PER_ROW / 1e6:,.1f} MB/s", "extrapolated",
        "~990 B written per row at 5k points (WAL, pages, staging, checkpoints, #271); estimated to grow "
        "toward a page per row at 100k, not measured yet")
    add("ingest", "broker memory, worst case", _gb(workers * BROKER_QUEUE_MAX * BROKER_BYTES_PER_QUEUED), "measured",
        "500,000 queued per worker x ~550 B (#272); steady state is under one batch")
    passed = next(((p, r, b) for p, r, b in PASSED_STEPS if total <= p and readings <= r), None)
    if passed:
        status, skind = f"inside a passing ladder step ({passed[0]:,} points, {passed[1]:,}/s): {passed[2]}", "measured"
    elif readings <= LISTENER_MAX_MEASURED and total <= READS_MEASURED_POINTS:
        status, skind = ("rate inside measured listener capacity and graph inside the 100k reads measured, "
                         "but no full ladder step passed here yet (#233 acceptance, #273)"), "pending"
    else:
        status, skind = "beyond anything measured: #273's realistic-profile results will say", "pending"
    add("ingest", "measured envelope", status, skind, "scale-test.mdx results")

    # -- graph and reads
    read_s = READ_POINT_AT_100K_S + READ_MATCH_AT_100K_S * (total / 100_000 - 1)
    add("graph", "triples", f"~{total * TRIPLES_PER_POINT / 1e6:,.1f} M", "extrapolated",
        "1.27 M triples for the harness's 100k points / 10k equips (#274)")
    add("graph", "read?filter=point (page 1)", f"~{read_s:,.2f} s (budget {READ_POINT_BUDGET_S} s)",
        "measured" if total == 100_000 else "extrapolated",
        "1.08 s at 100k, of which the match (0.42 s) grows with points (#274)")

    # -- hardware
    lcores = readings / LISTENER_PER_CORE
    ccores = rows / COMPRESS_ROWS_PER_S
    if passed:
        add("hardware", "CPU", f"{BASELINE_THREADS} hardware threads", "measured",
            "10k points every 5 s passed on 6 nash-srv threads (#190); 25k passed on 4 shared vCPUs (#274)")
    else:
        add("hardware", "CPU", f"~{BASELINE_THREADS + lcores + ccores:,.0f}+ hardware threads", "extrapolated",
            f"{BASELINE_THREADS} (measured baseline) + {lcores:.1f} listener + {ccores:.1f} compression; "
            "Postgres' own write cost at this rate is unmeasured (#273, soak)")
    oxi = max(OXIGRAPH_MEMORY_AT_100K, OXIGRAPH_MEMORY_AT_100K * total / 100_000)
    broker = workers * BROKER_QUEUE_MAX * BROKER_BYTES_PER_QUEUED
    ram = pg_memory_bytes + oxi + broker + OTHER_SERVICES_MEMORY
    add("hardware", "RAM", f"~{_gb(ram)}", "measured" if total <= 100_000 and pg_memory_bytes == PG_MEMORY_FLOOR
        else "extrapolated",
        f"Postgres {_gb(pg_memory_bytes)} + Oxigraph {_gb(oxi)} (4 GB held 100k points; linear past that is a guess, pending #273) + broker "
        f"{_gb(broker)} + {_gb(OTHER_SERVICES_MEMORY)} for the other services (scale stack caps, no OOM at 10k-100k)")
    add("hardware", "disk", f"{_gb(total_disk)} + headroom", "extrapolated", "see disk; SSD/NVMe for the write I/O above")

    # -- servers
    servers = max(1, math.ceil(total / TARGET_POINTS_PER_SERVER))
    add("servers", "servers (planning target)", str(servers), "target",
        "one per ~1M total points, then split by portfolio or campus (#190 top note); not yet measured")
    key = None
    if interval_s in (60, 300) and synthetic_per_equip in (5, 10):
        key = f"safe_points_typical_{'1min' if interval_s == 60 else '5min'}_n{synthetic_per_equip}"
    safe = PENDING.get(key) if key else None
    add("servers", "measured safe points per server", f"{safe:,.0f}" if safe else "pending",
        "measured" if safe else "pending", f"#273 profile {key}" if key else "#273 measures 60 s and 300 s, N = 5 and 10")

    # -- edge, export, backup (per building)
    b_readings = readings / buildings
    spool_rows = int(b_readings * outage_hours * 3600 * SPOOL_SLACK)
    add("edge", "Windrow spool per building", f"{spool_rows:,} rows, ~{_gb(spool_rows * SPOOL_BYTES_PER_ROW)}",
        "assumption", f"{outage_hours:g} h of {b_readings:,.0f} readings/s + 10% (windrow/mqtt_sink.py; ~100 B/row is "
        "its design figure)" + (f"; over the {SPOOL_MAX_ROWS:,}-row default cap: raise WINDROW_SPOOL_MAX_ROWS"
                               if spool_rows > SPOOL_MAX_ROWS else ""))
    spare = workers * LISTENER_PER_WORKER_PLAN - readings
    if spare > 0:
        add("edge", "replay of one building's outage", _dur(spool_rows / spare), "extrapolated",
            "spool rows / (listener plan capacity - live readings/s); add a worker to replay faster")
    b_samples = rows / buildings * YEAR * years
    add("export", "one building's export", f"~{_gb(b_samples * EXPORT_BYTES_PER_SAMPLE)}, "
        f"~{_dur(b_samples / EXPORT_SAMPLES_PER_S)} (import ~{_dur(b_samples / IMPORT_SAMPLES_PER_S)})", "extrapolated",
        "22.4 M samples: 130 MB, export 34.8 s, import 134 s (building-export.mdx)")
    all_samples = rows * YEAR * years
    add("backup", "pg_dump of the whole history", f"~{_gb(all_samples * BACKUP_BYTES_PER_SAMPLE)}, "
        f"~{_dur(all_samples / BACKUP_SAMPLES_PER_S)}", "extrapolated",
        "2.0 M samples: 11 MB gzip'd in 12 s (backups.mdx); a logical dump past a few billion rows is not a nightly job")

    return Sizing(points, interval_s, synthetic_per_equip, points_per_equip, synthetic_history_share, years, buildings,
                  equips, synthetic, total, readings, rows, raw_day, cbr, comp_day, rec.hot_bytes_max, total_disk,
                  pg_memory_bytes, env["BALER_HISTORY_CHUNK_INTERVAL"] or "7 days",
                  env["BALER_HISTORY_COMPRESS_AFTER"] or "7 days", history_chunks._interval(rec.schedule_s),
                  rpc, workers, margin, servers, read_s, spool_rows, lines)


def render(s: Sizing) -> str:
    out = [(f"# {s.points:,} points every {s.interval_s:g} s, {s.synthetic_per_equip} synthetic per equip, "
           f"{s.years:g} year(s), {s.buildings} building(s)"),
           "# kinds: measured = from a cited run; extrapolated = measured rate scaled past where it was measured;",
           "#        rule = #245's chunk sizing rule applied (history_chunks.recommend); assumption = an input",
           "#        target = planning target; pending = result still to come (#273, the 6 h soak)"]
    section = ""
    for ln in s.lines:
        if ln.section != section:
            section = ln.section
            out.append(f"\n[{section}]")
        out.append(f"  {ln.label:<44} {ln.value}")
        out.append(f"  {'':<44} ({ln.kind}) {ln.basis}")
    out.append("\n# .env")
    if s.chunk_interval == "7 days" and s.compress_after == "7 days":
        out.append("BALER_HISTORY_CHUNK_INTERVAL=\nBALER_HISTORY_COMPRESS_AFTER=")
    else:
        out.append(f"BALER_HISTORY_CHUNK_INTERVAL={s.chunk_interval}\nBALER_HISTORY_COMPRESS_AFTER={s.compress_after}")
    out.append(f"BALER_LISTENER_WORKERS={s.listener_workers}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m baler.sizing",
                                description="Deployment sizing from the scale work's measured numbers (#195)")
    p.add_argument("--points", type=int, required=True, help="field (real) points")
    p.add_argument("--interval", type=float, default=300.0, help="seconds between a point's readings (default 300)")
    p.add_argument("--synthetic-per-equip", type=int, default=0, help="derivations and rules per equip (5-10 typical)")
    p.add_argument("--points-per-equip", type=int, default=10, help="field points per equip (default 10)")
    p.add_argument("--synthetic-history-share", type=float, default=1.0,
                   help="share of synthetic items that write history, 0..1 (default 1: all, the upper bound)")
    p.add_argument("--years", type=float, default=1.0, help="history to keep on disk (default 1)")
    p.add_argument("--pg-memory", help="memory Postgres gets, e.g. 32g (default: sized from the rate)")
    p.add_argument("--buildings", type=int, default=1, help="buildings the points are spread over (default 1)")
    p.add_argument("--outage-hours", type=float, default=24.0, help="edge uplink outage to spool (default 24)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    a = p.parse_args(argv)
    try:
        s = size(a.points, a.interval, a.synthetic_per_equip, points_per_equip=a.points_per_equip,
                 synthetic_history_share=a.synthetic_history_share, years=a.years,
                 pg_memory_bytes=history_chunks._memory_bytes(a.pg_memory) if a.pg_memory else None,
                 buildings=a.buildings, outage_hours=a.outage_hours)
    except ValueError as exc:
        p.error(str(exc))
    print(json.dumps(asdict(s), indent=2) if a.json else render(s))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

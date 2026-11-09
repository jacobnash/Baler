"""
Evidence for the ladder's limiter verdict (Baler#269). While a step soaks,
`Sampler` records, every few seconds and cheaply (file reads on the host and
two small Postgres queries):

- per container: CPU against its cap, CFS throttling (cgroup cpu.stat
  nr_throttled / throttled time), memory against its cap, block writes;
- for the services that can limit a step (FOCUS): each process's CPU and its
  busiest thread's, against the practical limit of one core for a Python
  process (the GIL) whatever its cap;
- Postgres: what non-idle backends are waiting on (pg_stat_activity wait
  events, sampled), lock waits, and the listener's heartbeat (queue depth,
  seconds spent writing);
- the broker's queued messages (store minus retained) and drops;
- the disk holding Docker's data root: utilisation, write rate, free space.

Per step it also takes Postgres deltas (pg_stat_wal, bgwriter, pg_stat_io,
pg_stat_database) and the top pg_stat_statements, and after the probes pulls
each probe's spans from Jaeger for a per-span breakdown.

`summary()` is what limiter.verdict() reads. Its own cost is measured and
recorded (`overhead`): the sampler thread's CPU, its wall time per tick and
the time its Postgres queries took.

Everything degrades to "not available" rather than failing the step: a host
without cgroup access (rootless or remote Docker) still gets docker stats,
the Postgres evidence and the verdict, with lower confidence.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

# Services whose processes and threads are sampled (the rest get cgroup
# CPU/memory only): the ingest chain, the rule daemons and the read APIs.
FOCUS = ("mqtt_listener", "mosquitto", "postgres", "fault_detector", "derivation_engine", "haystack_api",
         "ingest_api", "oxigraph", "commissioning_api", "ops_api")
# Single-threaded-by-design processes: their practical CPU limit is one
# core, whatever their container's cap (a Python process and the GIL;
# mosquitto's single event loop).
ONE_CORE = ("mqtt_listener", "fault_detector", "derivation_engine", "haystack_api", "ingest_api",
            "commissioning_api", "ops_api", "mosquitto")
# A probe path's first segment -> the service whose spans explain it
PROBE_SERVICE = {"api": "baler-haystack-api", "ingest": "baler-ingest-api", "ops": "baler-ops-api",
                 "fault": "baler-fault-api", "cx": "baler-commissioning-api"}

CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC = Path("/proc")
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


# ------------------------------------------------------------- cgroup parsing (pure)

def parse_proc_cgroup(text: str) -> dict[str, str]:
    """/proc/<pid>/cgroup -> {controller: path}; cgroup v2's single line is
    under the key ''."""
    out = {}
    for line in text.splitlines():
        parts = line.strip().split(":", 2)
        if len(parts) != 3:
            continue
        _, controllers, path = parts
        if controllers == "":
            out[""] = path
        for c in controllers.split(","):
            if c and not c.startswith("name="):
                out[c] = path
    return out


def parse_kv(text: str) -> dict[str, int]:
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


def parse_cpu_stat(text: str) -> dict[str, float]:
    """cpu.stat (v1 or v2) -> periods, throttled periods, throttled seconds
    and, on v2, usage seconds."""
    kv = parse_kv(text)
    out = {"nr_periods": kv.get("nr_periods", 0), "nr_throttled": kv.get("nr_throttled", 0)}
    if "throttled_usec" in kv:
        out["throttled_s"] = kv["throttled_usec"] / 1e6
    else:
        out["throttled_s"] = kv.get("throttled_time", 0) / 1e9  # v1: ns
    if "usage_usec" in kv:
        out["usage_s"] = kv["usage_usec"] / 1e6
    return out


def parse_io_write_bytes(text: str, v2: bool) -> int:
    """Bytes written, summed over the devices listed. v2 io.stat
    ('254:0 rbytes=.. wbytes=..'); v1 blkio.throttle.io_service_bytes
    ('254:0 Write N', and a 'Total' line that is skipped)."""
    total = 0
    for line in text.splitlines():
        parts = line.split()
        if v2:
            for p in parts[1:]:
                if p.startswith("wbytes="):
                    total += int(p[7:])
        elif len(parts) == 3 and parts[1] == "Write" and ":" in parts[0]:
            total += int(parts[2])
    return total


def parse_task_stat(text: str) -> tuple[str, int]:
    """/proc/<pid>/task/<tid>/stat -> (comm, utime + stime ticks). comm
    may contain spaces and parentheses: split on the last ')'."""
    lpar, rpar = text.index("("), text.rindex(")")
    rest = text[rpar + 2:].split()
    # fields after comm start at field 3 (state); utime is 14, stime 15
    return text[lpar + 1:rpar], int(rest[11]) + int(rest[12])


_PG_CLIENT = re.compile(r"^postgres: (\S+) (\S+) \S+ ?(.*)$")


def process_label(service: str, cmdline: str) -> str:
    """A stable name for a process: postgres backends by their title
    (`postgres: checkpointer`, or `client <user>` for a connection, whose
    title also carries its address and current command), anything else by
    its command line."""
    cmdline = cmdline.strip()
    if service == "postgres" and cmdline.startswith("postgres:"):
        m = _PG_CLIENT.match(cmdline)
        if m and "(" in cmdline:  # host(port): a client connection
            return f"postgres client {m.group(1)}"
        return cmdline.split("  ")[0].strip()
    return cmdline[:80] or service


def read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


# ------------------------------------------------------------- containers

@dataclass
class Container:
    service: str
    cid: str
    pid: int
    cpus: float | None  # the cap, in cores
    mem_limit: int | None
    v2: bool = False
    dirs: dict[str, Path] = field(default_factory=dict)  # controller -> cgroup dir

    @classmethod
    def from_inspect(cls, c: dict) -> Container:
        hc = c.get("HostConfig", {})
        nano = hc.get("NanoCpus") or 0
        quota, period = hc.get("CpuQuota") or 0, hc.get("CpuPeriod") or 100_000
        cpus = nano / 1e9 if nano else (quota / period if quota > 0 else None)
        svc = c["Config"]["Labels"].get("com.docker.compose.service", c["Name"].lstrip("/"))
        out = cls(svc, c["Id"], c["State"].get("Pid") or 0, cpus, hc.get("Memory") or None)
        out.locate()
        return out

    def locate(self) -> None:
        text = read(PROC / str(self.pid) / "cgroup") if self.pid else None
        if not text:
            return
        paths = parse_proc_cgroup(text)
        if set(paths) == {""}:
            self.v2 = True
            d = CGROUP_ROOT / paths[""].lstrip("/")
            if d.is_dir():
                self.dirs = {"cpu": d, "cpuacct": d, "memory": d, "io": d}
            return
        for ctl in ("cpu", "cpuacct", "memory", "blkio"):
            if ctl not in paths:
                continue
            for mount in (ctl, "cpu,cpuacct" if ctl in ("cpu", "cpuacct") else ctl):
                d = CGROUP_ROOT / mount / paths[ctl].lstrip("/")
                if d.is_dir():
                    self.dirs["io" if ctl == "blkio" else ctl] = d
                    break

    def counters(self) -> dict | None:
        """Raw cumulative counters now, or None without cgroup access."""
        if "cpu" not in self.dirs:
            return None
        out: dict = {}
        stat = read(self.dirs["cpu"] / "cpu.stat")
        if stat is not None:
            out.update(parse_cpu_stat(stat))
        if not self.v2 and "cpuacct" in self.dirs:
            usage = read(self.dirs["cpuacct"] / "cpuacct.usage")
            if usage:
                out["usage_s"] = int(usage) / 1e9
        mem = self.dirs.get("memory")
        if mem:
            cur = read(mem / ("memory.current" if self.v2 else "memory.usage_in_bytes"))
            mstat = parse_kv(read(mem / "memory.stat") or "")
            if cur:
                inactive = mstat.get("inactive_file" if self.v2 else "total_inactive_file", 0)
                out["mem"] = max(0, int(cur) - inactive)  # what docker stats shows
        io = self.dirs.get("io")
        if io:
            text = read(io / ("io.stat" if self.v2 else "blkio.throttle.io_service_bytes"))
            if text is not None:
                out["io_w"] = parse_io_write_bytes(text, self.v2)
        return out

    def pids(self) -> list[int]:
        d = self.dirs.get("cpu")
        text = read(d / "cgroup.procs") if d else None
        return [int(x) for x in text.split()] if text else []


def discover(compose_ps_ids: list[str]) -> list[Container]:
    if not compose_ps_ids:
        return []
    out = subprocess.run(["docker", "inspect", *compose_ps_ids], capture_output=True, text=True, check=False).stdout
    return [Container.from_inspect(c) for c in json.loads(out or "[]") if c.get("State", {}).get("Running")]


class Threads:
    """Per-process and busiest-thread CPU for one container, from
    /proc/<pid>/task/*/stat deltas."""

    def __init__(self):
        self.prev: dict[int, int] = {}  # tid -> ticks
        self.labels: dict[int, str] = {}

    def sample(self, c: Container, dt: float) -> list[dict]:
        procs = []
        now: dict[int, int] = {}
        for pid in c.pids():
            if pid not in self.labels:
                cmd = read(PROC / str(pid) / "cmdline")
                self.labels[pid] = process_label(c.service, (cmd or "").replace("\0", " "))
            total, hot, hot_comm, n = 0, 0, "", 0
            try:
                tids = os.listdir(PROC / str(pid) / "task")
            except OSError:
                continue
            for tid in tids:
                text = read(PROC / str(pid) / "task" / tid / "stat")
                if not text:
                    continue
                comm, ticks = parse_task_stat(text)
                t = int(tid)
                now[t] = ticks
                d = ticks - self.prev[t] if t in self.prev else 0
                n += 1
                total += d
                if d > hot:
                    hot, hot_comm = d, comm
            procs.append({"pid": pid, "label": self.labels[pid], "threads": n,
                          "cpu": round(total / CLK_TCK / dt, 3), "hot_thread": round(hot / CLK_TCK / dt, 3),
                          "hot_comm": hot_comm})
        first = not self.prev
        self.prev = now
        if first:
            return []
        if c.service == "postgres":
            return group_postgres(procs)
        return sorted(procs, key=lambda p: -p["cpu"])[:3]


def group_postgres(procs: list[dict]) -> list[dict]:
    """Postgres is a process per backend: sum by label (client backends of
    one user together), keep the busiest single process of each group."""
    groups: dict[str, dict] = {}
    for p in procs:
        g = groups.setdefault(p["label"], {"label": p["label"], "procs": 0, "cpu": 0.0, "hot_proc": 0.0})
        g["procs"] += 1
        g["cpu"] = round(g["cpu"] + p["cpu"], 3)
        g["hot_proc"] = max(g["hot_proc"], p["cpu"])
    return sorted(groups.values(), key=lambda g: -g["cpu"])[:5]


# ------------------------------------------------------------- disk

def parse_diskstats(text: str, name: str) -> dict | None:
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 14 and f[2] == name:
            return {"writes": int(f[7]), "sectors_w": int(f[9]), "write_ms": int(f[10]), "io_ms": int(f[12]),
                    "reads": int(f[3]), "sectors_r": int(f[5])}
    return None


def block_device(path: str) -> str | None:
    """The /proc/diskstats name of the device holding `path`; through
    /proc/mounts when the filesystem's own device is virtual (overlay,
    btrfs subvolumes)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    major, minor = os.major(st.st_dev), os.minor(st.st_dev)
    if major:
        link = Path(f"/sys/dev/block/{major}:{minor}")
        if link.exists():
            return link.resolve().name
    best, dev = "", None
    for line in (read(PROC / "mounts") or "").splitlines():
        src, mnt = line.split()[:2]
        if src.startswith("/dev/") and (path == mnt or path.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
            best, dev = mnt, Path(src).resolve().name
    return dev


# ------------------------------------------------------------- postgres

PG_TICK = """
SELECT (SELECT coalesce(json_agg(w), '[]') FROM (
          SELECT backend_type AS b, coalesce(wait_event_type, 'CPU') AS t, coalesce(wait_event, 'running') AS e,
                 count(*) AS n
          FROM pg_stat_activity
          WHERE pid <> pg_backend_pid() AND coalesce(state, '') NOT IN ('idle')
            AND coalesce(wait_event_type, '') NOT IN ('Activity')
            AND NOT (backend_type = 'client backend' AND state IS NULL)
          GROUP BY 1, 2, 3) w),
       (SELECT count(*) FROM pg_locks WHERE NOT granted),
       (SELECT detail FROM service_heartbeats WHERE service = 'mqtt_listener')
"""


def pg_counters(conn) -> dict:
    """Cumulative Postgres counters for per-step deltas. Each block is
    optional (version, permissions)."""
    out: dict = {}

    def one(key, sql):
        try:
            cur = conn.execute(sql)
            row = cur.fetchone()
            out[key] = {d.name: (float(v) if isinstance(v, Decimal) else v)
                        for d, v in zip(cur.description, row, strict=True)
                        if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)} if row else {}
        except Exception:  # noqa: BLE001, S110 - a view this Postgres doesn't have
            pass

    one("wal", "SELECT wal_records, wal_fpi, wal_bytes, wal_buffers_full, wal_write, wal_sync FROM pg_stat_wal")
    one("bgwriter", "SELECT * FROM pg_stat_bgwriter")
    one("checkpointer", "SELECT num_timed, num_requested, buffers_written FROM pg_stat_checkpointer")
    one("database", "SELECT xact_commit, xact_rollback, blks_read, blks_hit, tup_inserted, tup_updated, "
                    "temp_files, temp_bytes, deadlocks FROM pg_stat_database WHERE datname = current_database()")
    try:
        rows = conn.execute("SELECT backend_type, object, context, sum(writes * op_bytes), sum(extends * op_bytes), "
                            "sum(fsyncs) FROM pg_stat_io GROUP BY 1, 2, 3").fetchall()
        out["io"] = {f"{b}/{o}/{c}": {"write_bytes": int(w or 0), "extend_bytes": int(e or 0), "fsyncs": int(f or 0)}
                     for b, o, c, w, e, f in rows if (w or e or f)}
    except Exception:  # noqa: BLE001, S110 - pg_stat_io is PG16+
        pass
    return out


def counter_delta(a: dict, b: dict) -> dict:
    """b - a for every numeric leaf present in both."""
    out = {}
    for k, vb in b.items():
        va = a.get(k)
        if isinstance(vb, dict):
            d = counter_delta(va or {}, vb)
            if d:
                out[k] = d
        elif isinstance(vb, (int, float)) and not isinstance(vb, bool):
            d = vb - (va if isinstance(va, (int, float)) else 0)
            out[k] = round(d, 3) if isinstance(d, float) else d
    return out


def statements_reset(conn) -> bool:
    """Starts pg_stat_statements' window for this step; False if the
    library isn't loaded (it is in the scale stack: compose.scale.yml)."""
    for sql in ("SELECT pg_stat_statements_reset()", "CREATE EXTENSION IF NOT EXISTS pg_stat_statements",
                "SELECT pg_stat_statements_reset()"):
        try:
            conn.execute(sql)
            if "reset" in sql:
                return True
        except Exception:  # noqa: BLE001, S112 - not loaded, or no permission: try the next
            continue
    return False


def statements_top(conn, n: int = 10) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT calls, total_exec_time, mean_exec_time, rows, shared_blks_written, temp_blks_written, "
            "left(regexp_replace(query, '\\s+', ' ', 'g'), 200) FROM pg_stat_statements "
            "ORDER BY total_exec_time DESC LIMIT %s", (n,)).fetchall()
    except Exception:  # noqa: BLE001
        return []
    return [{"calls": c, "total_ms": round(t, 1), "mean_ms": round(m, 3), "rows": r, "blks_written": w,
             "temp_blks_written": tw, "query": q} for c, t, m, r, w, tw, q in rows]


# ------------------------------------------------------------- sampler

def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


class Sampler:
    """Background evidence sampler for one step. start() after warm-up,
    phase("probes") before the read probes, stop() after them."""

    def __init__(self, containers: list[Container], pg_connect, broker_snapshot=None, every: float = 5.0,
                 docker_root: str = "/var/lib/docker"):
        self.containers = containers
        self.pg_connect, self.broker_snapshot = pg_connect, broker_snapshot
        self.every = every
        self.docker_root = docker_root
        self.device = block_device(docker_root)
        self.threads = {c.service: Threads() for c in containers if c.service in FOCUS}
        self.ticks: list[dict] = []
        self.phase_name = "soak"
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="evidence", daemon=True)
        self._prev: dict = {}
        self.cpu_s = 0.0
        self.wall_s: list[float] = []
        self.pg_s = 0.0
        self.errors: list[str] = []
        self.pg_begin: dict = {}
        self.pg_end: dict = {}
        self.statements_on = False
        self.statements: list[dict] = []
        self.started = self.stopped = 0.0

    # lifecycle
    def start(self) -> Sampler:
        try:
            conn = self.pg_connect()
            self.pg_begin = pg_counters(conn)
            self.statements_on = statements_reset(conn)
            conn.close()
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"pg begin: {exc!r}"[:200])
        self.started = time.time()
        self._thread.start()
        return self

    def phase(self, name: str) -> None:
        self.phase_name = name

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=30)
        self.stopped = time.time()
        try:
            conn = self.pg_connect()
            self.pg_end = pg_counters(conn)
            if self.statements_on:
                self.statements = statements_top(conn)
            conn.close()
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"pg end: {exc!r}"[:200])

    def _run(self) -> None:
        conn = None
        while True:
            t_wall, t_cpu = time.perf_counter(), time.thread_time()
            try:
                conn = conn or self.pg_connect()
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"pg connect: {exc!r}"[:200])
                conn = None
            try:
                tick = self.tick(conn)
                if tick:
                    self.ticks.append(tick)
            except Exception as exc:  # noqa: BLE001 - evidence must never fail the step
                self.errors.append(repr(exc)[:200])
                conn = None
            self.cpu_s += time.thread_time() - t_cpu
            self.wall_s.append(time.perf_counter() - t_wall)
            if self._stop.wait(self.every):
                break
        if conn is not None:
            conn.close()

    def tick(self, conn) -> dict | None:
        now = time.time()
        prev, first = self._prev, not self._prev
        dt = now - prev.get("t", now)
        cur: dict = {"t": now, "containers": {}}
        tick: dict = {"t": round(now, 1), "phase": self.phase_name, "containers": {}}
        for c in self.containers:
            k = c.counters()
            if k is None:
                continue
            cur["containers"][c.service] = k
            p = prev.get("containers", {}).get(c.service)
            entry: dict = {"mem": k.get("mem"), "mem_cap": c.mem_limit, "cap": c.cpus}
            if p and dt > 0:
                if "usage_s" in k:
                    entry["cpu"] = round((k["usage_s"] - p.get("usage_s", 0)) / dt, 3)
                periods = k["nr_periods"] - p.get("nr_periods", 0)
                entry["throttled"] = round((k["nr_throttled"] - p.get("nr_throttled", 0)) / periods, 3) if periods else 0.0
                entry["throttled_s"] = round(k["throttled_s"] - p.get("throttled_s", 0), 3)
                entry["periods"] = periods
                if "io_w" in k:
                    entry["io_w_bps"] = round((k["io_w"] - p.get("io_w", 0)) / dt)
            if c.service in self.threads:
                procs = self.threads[c.service].sample(c, dt if dt > 0 else 1.0)
                if procs:
                    entry["procs"] = procs
            tick["containers"][c.service] = entry
        if self.device:
            ds = parse_diskstats(read(PROC / "diskstats") or "", self.device)
            if ds:
                cur["disk"] = ds
                p = prev.get("disk")
                if p and dt > 0:
                    dw = ds["writes"] - p["writes"]
                    tick["disk"] = {"util": round((ds["io_ms"] - p["io_ms"]) / (dt * 1000), 3),
                                    "write_bps": round((ds["sectors_w"] - p["sectors_w"]) * 512 / dt),
                                    "w_await_ms": round((ds["write_ms"] - p["write_ms"]) / dw, 2) if dw else 0.0}
        free = None
        try:
            free = os.statvfs(self.docker_root)
            tick.setdefault("disk", {})["free_gb"] = round(free.f_bavail * free.f_frsize / 1e9, 2)
        except OSError:
            pass
        if self.broker_snapshot:
            b = self.broker_snapshot() or {}
            try:
                stored, retained = int(b.get("store/messages/count", 0)), int(b.get("retained messages/count", 0))
                dropped = int(b.get("publish/messages/dropped", 0))
                cur["dropped"] = dropped
                tick["broker"] = {"queued": max(0, stored - retained),
                                  "dropped": dropped - prev.get("dropped", dropped)}
            except (TypeError, ValueError):
                pass
        if conn is not None:
            t0 = time.perf_counter()
            waits, locks, hb = conn.execute(PG_TICK).fetchone()
            self.pg_s += time.perf_counter() - t0
            tick["pg"] = {"waits": {f"{w['t']}:{w['e']}": w["n"] for w in (waits or [])},
                          "waits_by_backend": {f"{w['b']}|{w['t']}:{w['e']}": w["n"] for w in (waits or [])},
                          "lock_waits": locks}
            if hb:
                cur["listener"] = hb
                p = prev.get("listener")
                lt = {"queue": hb.get("queue"), "queue_peak": hb.get("queue_peak"), "queue_max": hb.get("queue_max")}
                if p and dt > 0:
                    lt["rows_per_s"] = round((hb.get("rows", 0) - p.get("rows", 0)) / dt, 1)
                    lt["flush_busy"] = round(max(0.0, hb.get("flush_s", 0) - p.get("flush_s", 0)) / dt, 3)
                tick["listener"] = lt
        self._prev = cur
        return None if first else tick

    # ---------------------------------------------------------- summary

    def summary(self) -> dict:
        return summarize(self.ticks, pg_delta=counter_delta(self.pg_begin, self.pg_end) if self.pg_end else {},
                         statements=self.statements, overhead=self.overhead(), device=self.device,
                         errors=self.errors[:10])

    def overhead(self) -> dict:
        dur = max((self.stopped or time.time()) - (self.started or time.time()), 1e-9)
        return {"ticks": len(self.wall_s), "every_s": self.every, "cpu_s": round(self.cpu_s, 3),
                "cpu_share_of_one_core": round(self.cpu_s / dur, 5),
                "tick_wall_median_s": round(statistics.median(self.wall_s), 4) if self.wall_s else None,
                "tick_wall_max_s": round(max(self.wall_s), 4) if self.wall_s else None,
                "pg_query_s": round(self.pg_s, 3)}


def summarize(ticks: list[dict], pg_delta: dict | None = None, statements: list | None = None,
              overhead: dict | None = None, device: str | None = None, errors: list | None = None) -> dict:
    """Per phase: per-container CPU (mean, p90) against its cap, throttled
    share, memory peak against its cap, busiest process and thread; disk
    utilisation and write rate; broker queue and drops; Postgres wait-event
    shares and lock waits; listener queue and write-busy share. Pure (the
    verdict tests feed it recorded ticks)."""
    out: dict = {"phases": {}, "device": device}
    for phase in sorted({t.get("phase", "soak") for t in ticks}):
        ts = [t for t in ticks if t.get("phase", "soak") == phase]
        out["phases"][phase] = summarize_phase(ts)
    if pg_delta:
        out["pg_delta"] = pg_delta
        wal = pg_delta.get("wal") or {}
        io = pg_delta.get("io") or {}
        data_w = sum(v.get("write_bytes", 0) + v.get("extend_bytes", 0) for v in io.values())
        if wal or io:
            # The write amplification view (#269's "Postgres write gap"):
            # logical WAL, WAL page writes (each write() is at least one
            # 8 kB page: a commit rewrites the partly filled page), data
            # file writes from pg_stat_io.
            out["pg_writes"] = {"wal_bytes": wal.get("wal_bytes"), "wal_write_calls": wal.get("wal_write"),
                                "wal_sync_calls": wal.get("wal_sync"), "wal_fpi": wal.get("wal_fpi"),
                                "wal_page_bytes_min": (wal.get("wal_write") or 0) * 8192,
                                "data_write_bytes": data_w,
                                "temp_bytes": (pg_delta.get("database") or {}).get("temp_bytes")}
    if statements:
        out["pg_top_statements"] = statements
    if overhead:
        out["overhead"] = overhead
    if errors:
        out["errors"] = errors
    return out


def summarize_phase(ts: list[dict]) -> dict:
    out: dict = {"ticks": len(ts), "containers": {}}
    services = {s for t in ts for s in t.get("containers", {})}
    for svc in sorted(services):
        es = [t["containers"][svc] for t in ts if svc in t.get("containers", {})]
        cpu = [e["cpu"] for e in es if e.get("cpu") is not None]
        periods = sum(e.get("periods", 0) for e in es)
        throttled_n = sum(round(e.get("throttled", 0) * e.get("periods", 0)) for e in es)
        cap = next((e.get("cap") for e in es if e.get("cap")), None)
        mem_cap = next((e.get("mem_cap") for e in es if e.get("mem_cap")), None)
        mem = [e["mem"] for e in es if e.get("mem") is not None]
        c: dict = {"cap": cap}
        if cpu:
            c.update(cpu_mean=round(statistics.mean(cpu), 3), cpu_p90=round(_pct(cpu, 0.9), 3),
                     cpu_max=round(max(cpu), 3))
            if cap:
                c["cpu_of_cap_mean"] = round(c["cpu_mean"] / cap, 3)
        if periods:
            c["throttled"] = round(throttled_n / periods, 3)
            c["throttled_s"] = round(sum(e.get("throttled_s", 0) for e in es), 2)
        if mem:
            c["mem_max"] = max(mem)
            if mem_cap:
                c["mem_of_cap_max"] = round(max(mem) / mem_cap, 3)
        iow = [e["io_w_bps"] for e in es if e.get("io_w_bps") is not None]
        if iow:
            c["io_w_bps_mean"] = round(statistics.mean(iow))
        procs = [p for e in es for p in e.get("procs", [])]
        if procs:
            by: dict[str, list[dict]] = {}
            for p in procs:
                by.setdefault(p["label"], []).append(p)
            top = []
            for label, ps in by.items():
                cpus = [p["cpu"] for p in ps]
                hot = [p.get("hot_thread", p.get("hot_proc", 0)) for p in ps]
                top.append({"label": label, "cpu_mean": round(sum(cpus) / len(es), 3), "cpu_max": round(max(cpus), 3),
                            "hot_thread_mean": round(sum(hot) / len(es), 3), "hot_thread_max": round(max(hot), 3)})
            c["procs"] = sorted(top, key=lambda p: -p["cpu_mean"])[:4]
        out["containers"][svc] = c
    disk = [t["disk"] for t in ts if t.get("disk", {}).get("util") is not None]
    if disk:
        out["disk"] = {"util_mean": round(statistics.mean(d["util"] for d in disk), 3),
                       "util_p90": round(_pct([d["util"] for d in disk], 0.9), 3),
                       "write_mb_s_mean": round(statistics.mean(d["write_bps"] for d in disk) / 1e6, 2),
                       "w_await_ms_p90": _pct([d["w_await_ms"] for d in disk], 0.9)}
    free = [t["disk"]["free_gb"] for t in ts if t.get("disk", {}).get("free_gb") is not None]
    if len(free) >= 2:
        hours = max((ts[-1]["t"] - ts[0]["t"]) / 3600, 1e-9)
        out.setdefault("disk", {}).update(free_gb_first=free[0], free_gb_last=free[-1],
                                          growth_gb_per_h=round((free[0] - free[-1]) / hours, 2))
    br = [t["broker"] for t in ts if "broker" in t]
    if br:
        q = [b["queued"] for b in br]
        out["broker"] = {"queued_max": max(q), "queued_last": q[-1], "queued_first": q[0],
                         "dropped": sum(b.get("dropped", 0) for b in br),
                         "ticks_with_drops": sum(1 for b in br if b.get("dropped"))}
    pg = [t["pg"] for t in ts if "pg" in t]
    if pg:
        waits: dict[str, int] = {}
        by_backend: dict[str, int] = {}
        for p in pg:
            for k, n in p.get("waits", {}).items():
                waits[k] = waits.get(k, 0) + n
            for k, n in p.get("waits_by_backend", {}).items():
                by_backend[k] = by_backend.get(k, 0) + n
        total = sum(waits.values())
        out["pg"] = {"active_per_tick": round(total / len(pg), 2),
                     "waits": {k: round(v / total, 3) for k, v in sorted(waits.items(), key=lambda kv: -kv[1])[:8]}
                     if total else {},
                     "waits_by_backend": dict(sorted(by_backend.items(), key=lambda kv: -kv[1])[:8]),
                     "lock_waits_max": max(p.get("lock_waits", 0) for p in pg),
                     "lock_waits_mean": round(statistics.mean(p.get("lock_waits", 0) for p in pg), 2)}
    li = [t["listener"] for t in ts if "listener" in t]
    if li:
        busy = [x["flush_busy"] for x in li if x.get("flush_busy") is not None]
        qs = [x["queue"] for x in li if x.get("queue") is not None]
        peaks = [x.get("queue_peak") or 0 for x in li]
        out["listener"] = {"queue_max": max(peaks + qs, default=0), "queue_last": qs[-1] if qs else None,
                           "queue_first": qs[0] if qs else None, "queue_cap": li[-1].get("queue_max"),
                           "flush_busy_mean": round(statistics.mean(busy), 3) if busy else None,
                           "flush_busy_p90": _pct(busy, 0.9)}
    return out


# ------------------------------------------------------------- traces

def _attrs(span: dict) -> dict:
    return {a["key"]: next(iter(a.get("value", {}).values()), None) for a in span.get("attributes", [])}


def jaeger_spans(jaeger: str, service: str, t0: float, t1: float, limit: int = 50) -> list[dict]:
    """Spans of `service`'s traces that started in [t0, t1], from Jaeger
    v2's /api/v3/traces (OTLP JSON)."""
    import requests

    def rfc(t):
        return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    r = requests.get(f"{jaeger}/api/v3/traces", timeout=30, params={
        "query.service_name": service, "query.start_time_min": rfc(t0), "query.start_time_max": rfc(t1),
        "query.search_depth": limit})
    if r.status_code == 404:
        return []
    r.raise_for_status()
    spans = []
    for rs in r.json().get("result", {}).get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for s in ss.get("spans", []):
                spans.append({"trace": s.get("traceId"), "id": s.get("spanId"), "parent": s.get("parentSpanId") or None,
                              "name": s.get("name"), "start": int(s["startTimeUnixNano"]) / 1e9,
                              "end": int(s["endTimeUnixNano"]) / 1e9})
    return spans


def span_breakdown(spans: list[dict], top: int = 6) -> dict:
    """Where a probe's time went: per trace (one per request), each span's
    self time (its duration minus its children's); then per span name the
    median over requests, against the median request (root) duration. Pure."""
    traces: dict[str, list[dict]] = {}
    for s in spans:
        traces.setdefault(s["trace"], []).append(s)
    roots, per_name = [], {}
    for ss in traces.values():
        ids = {s["id"] for s in ss}
        root = [s for s in ss if not s["parent"] or s["parent"] not in ids]
        if not root:
            continue
        roots.append(max(s["end"] - s["start"] for s in root))
        child = {}
        for s in ss:
            if s["parent"] in ids:
                child[s["parent"]] = child.get(s["parent"], 0.0) + (s["end"] - s["start"])
        mine: dict[str, float] = {}
        for s in ss:
            mine[s["name"]] = mine.get(s["name"], 0.0) + max(0.0, (s["end"] - s["start"]) - child.get(s["id"], 0.0))
        for name, v in mine.items():
            per_name.setdefault(name, []).append(v)
    if not roots:
        return {}
    root_med = statistics.median(roots)
    n = len(roots)
    rows = [{"span": name, "self_s": round(statistics.median(vs + [0.0] * (n - len(vs))), 4)}
            for name, vs in per_name.items()]
    rows.sort(key=lambda r: -r["self_s"])
    for r in rows:
        r["share"] = round(r["self_s"] / root_med, 3) if root_med else None
    return {"requests": n, "root_median_s": round(root_med, 4), "spans": rows[:top]}


def probe_breakdowns(jaeger: str | None, probes: list[dict]) -> None:
    """Adds `trace` (span_breakdown) to each timed probe, in place. Jaeger
    batches spans for a few seconds: call this a few seconds after the last
    probe."""
    if not jaeger:
        return
    for p in probes:
        if p.get("skipped") or "t0" not in p:
            continue
        svc = PROBE_SERVICE.get(p.get("path", "/").strip("/").split("/")[0])
        if not svc:
            continue
        try:
            bd = span_breakdown(jaeger_spans(jaeger, svc, p["t0"], p["t1"]))
        except Exception as exc:  # noqa: BLE001
            bd = {"error": repr(exc)[:200]}
        if bd:
            if bd.get("root_median_s") is not None and p.get("median_s") is not None:
                # gateway, network, and whatever the API does outside its spans
                bd["outside_spans_s"] = round(max(0.0, p["median_s"] - bd["root_median_s"]), 4)
            p["trace"] = bd

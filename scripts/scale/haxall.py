"""
The Haxall target of the scale ladder (Baler#236): `ladder.py up|run|crash|
down --target haxall`. Same generator (loadgen.py, Windrow's per-point
envelope), point counts, 5 s rate, canary, comparable probes and caps as
the Baler run; Haxall in its own compose project (compose.haxall.yml,
"<SCALE_PROJECT>-haxall") behind the same guard; ingest through
haxall_bridge.py (batched hisWrite, acked after the write) in a capped
container of its own.

What maps to what:
- tags/equip: the ladder provisions sites, equips and points as Haxall
  records (Axon commit), ids from their topics (haxall_bridge.topic_ref),
  with the same markers and the scaleHis1/100/1000 groups;
- stored rows: hisRead counts over the soak's seconds (Haxall has no
  pg_stat equivalent), the same [a, b) window of payload ts as Baler's;
- lag: the canary point's newest hisRead ts against its publish time;
- probes: HAXALL_PROBES below, through Haxall's own Haystack API (read op,
  Axon eval for hisRead), same names as budgets.json's where comparable;
- resources: docker stats of haxall, haxall_bridge and mosquitto;
- durability: Haxall's settings as found, recorded, plus `crash`.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
import haxall_bridge

OVERRIDE = common.HERE / "compose.haxall.yml"
PROJECT = f"{common.PROJECT}-haxall"

# Baler's storage, ingest-API and read path in compose.scale.yml - what
# Haxall, which is all of those in one process, gets by default. The bridge
# gets mqtt_listener's cap; both brokers get the same one. Baler's rule
# daemons aren't counted: the Haxall run evaluates no rules.
HAXALL_CAP_FROM = ("POSTGRES", "OXIGRAPH", "HAYSTACK_API", "INGEST_API")
BRIDGE_CAP_FROM = "MQTT_LISTENER"

# Probes through Haxall's Haystack API. "read" ops are timed as GET read;
# "eval" ones as Axon through POST eval. Names match budgets.json's
# where the question is the same; Haxall has no curVal for points nothing
# drives (no connector), so the curVal probes time the same read request.
HAXALL_PROBES = [
    {"name": "read?filter=point", "op": "read", "filter": "point"},
    {"name": "Equipment tree: read?filter=equip", "op": "read", "filter": "equip"},
    {"name": "curVal, 100 points", "op": "read", "filter": "scaleHis100"},
    {"name": "curVal, 1000 points", "op": "read", "filter": "scaleHis1000"},
    {"name": "hisRead 1 point (today)", "op": "eval", "expr": "readAll(scaleHis1).hisRead(today())"},
    {"name": "hisRead 100 points (today)", "op": "eval", "expr": "readAll(scaleHis100).hisRead(today())"},
    {"name": "hisRead 1000 points (today)", "op": "eval", "expr": "readAll(scaleHis1000).hisRead(today())"},
    {"name": "readAll(equip) hisRead(today)", "op": "eval",
     "expr": "readAll(point and his and equipRef->ahu and scaleHis1000).hisRead(today())"},
]


# ----------------------------------------------------------------- stack

def compose_cmd(*args: str) -> list[str]:
    return ["docker", "compose", "-p", PROJECT, "--env-file", str(common.ENV_FILE), "-f", str(OVERRIDE), *args]


def haxall_caps(env: dict[str, str], nproc: int, overrides: dict[str, str] | None = None) -> dict[str, str]:
    """SCALE_CPUS/MEM_HAXALL and _HAXALL_BRIDGE: set in scale.env wins,
    else the sum of Baler's matching caps (compose.scale.yml defaults or
    scale.env), CPUs clamped to the host. Pure (tested)."""
    overrides = overrides or {}
    defaults: dict[str, str] = {}
    for line in common.OVERRIDE.read_text().splitlines():
        line = line.strip()
        for kind in ("cpus: ${SCALE_CPUS_", "mem_limit: ${SCALE_MEM_"):
            if line.startswith(kind):
                var, default = line[line.index("${") + 2:-1].split(":-")
                defaults[var] = default

    def val(var: str) -> str:
        return overrides.get(var) or env.get(var) or defaults.get(var, "0")

    def gib(s: str) -> float:
        s = s.strip().lower()
        mult = {"g": 1.0, "m": 1 / 1024, "k": 1 / 1024 / 1024}.get(s[-1:], 1 / 1024 ** 3)
        return float(s[:-1] if s[-1:] in "gmk" else s) * mult

    out = {}
    cpus = env.get("SCALE_CPUS_HAXALL") or f"{sum(float(val(f'SCALE_CPUS_{s}')) for s in HAXALL_CAP_FROM):g}"
    mem = env.get("SCALE_MEM_HAXALL") or f"{sum(gib(val(f'SCALE_MEM_{s}')) for s in HAXALL_CAP_FROM):g}g"
    out["SCALE_CPUS_HAXALL"] = f"{min(float(cpus), float(nproc)):g}"
    out["SCALE_MEM_HAXALL"] = mem
    bcpus = env.get("SCALE_CPUS_HAXALL_BRIDGE") or val(f"SCALE_CPUS_{BRIDGE_CAP_FROM}")
    out["SCALE_CPUS_HAXALL_BRIDGE"] = f"{min(float(bcpus), float(nproc)):g}"
    out["SCALE_MEM_HAXALL_BRIDGE"] = env.get("SCALE_MEM_HAXALL_BRIDGE") or val(f"SCALE_MEM_{BRIDGE_CAP_FROM}")
    out["SCALE_CPUS_MOSQUITTO"] = f"{min(float(val('SCALE_CPUS_MOSQUITTO')), float(nproc)):g}"
    return out


def stack_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(haxall_caps(common.read_env(), os.cpu_count() or 1))
    env["SCALE_PROJECT"] = common.PROJECT
    return env


def compose(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(compose_cmd(*args), cwd=common.REPO, env=stack_env(), check=check,
                          capture_output=capture, text=True)


def guard() -> dict:
    """The Baler stack's guard, on the Haxall project's effective config."""
    if not common.ENV_FILE.exists():
        raise SystemExit(f"{common.ENV_FILE} missing: run `python scripts/scale/ladder.py init` first")
    env = common.read_env()
    if env.get("BALER_VOLUME_PREFIX") != common.VOLUME_PREFIX:
        raise SystemExit(f"refusing: {common.ENV_FILE} must set BALER_VOLUME_PREFIX={common.VOLUME_PREFIX}")
    if not env.get("SCALE_HAXALL_SU_PASSWORD"):
        raise SystemExit(f"refusing: set SCALE_HAXALL_SU_PASSWORD in {common.ENV_FILE} (`ladder.py init --force` "
                         "writes one, or add a random one by hand)")
    if not (common.REPO / "windrow" / "docker" / "haxall.Dockerfile").exists():
        raise SystemExit("windrow/docker/haxall.Dockerfile missing: `git submodule update --init windrow`")
    out = subprocess.run(compose_cmd("config", "--format", "json"), cwd=common.REPO, env=stack_env(),
                         check=True, capture_output=True, text=True).stdout
    config = json.loads(out)
    problems = common.guard_problems(config)
    if problems:
        raise SystemExit("refusing to run against something that looks like a live install:\n  - " + "\n  - ".join(problems))
    return config


class Endpoints:
    """What loadgen, Sys and Canary need (mqtt_port, gen_password), plus
    Haxall's API."""

    def __init__(self, config: dict):
        ports = common.published_ports(config)
        env = common.read_env()
        self.mqtt_port = ports["mosquitto"][1883]
        self.haxall = f"http://127.0.0.1:{ports['haxall'][8080]}/api/sys"
        self.gen_password = env.get("SCALE_GEN_MQTT_PASSWORD", "")
        self.su_password = env.get("SCALE_HAXALL_SU_PASSWORD", "")
        self.jaeger = None

    def client(self):
        from baler.haystack_client import HaystackClient

        return HaystackClient(self.haxall, "su", self.su_password)


def eval_axon(client, expr: str) -> list[dict]:
    body = client._post("eval", {"meta": {"ver": "3.0"}, "cols": [{"name": "expr"}], "rows": [{"expr": f"s:{expr}"}]})
    meta = body.get("meta") or {}
    if "err" in meta:
        raise RuntimeError(f"axon error: {meta.get('dis')} in {expr[:200]}")
    return body.get("rows") or []


# ----------------------------------------------------------------- records

def axon_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


def axon_dict(tags: dict) -> str:
    """An Axon dict literal: True -> marker, '@x' -> ref, str -> Str,
    number -> Number. Only what provisioning needs."""
    parts = []
    for k, v in tags.items():
        if v is True:
            parts.append(k)
        elif isinstance(v, str) and v.startswith("@"):
            parts.append(f"{k}:{v}")
        elif isinstance(v, str):
            parts.append(f"{k}:{axon_str(v)}")
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            parts.append(f"{k}:{v!r}")
    return "{" + ", ".join(parts) + "}"


def records(shape: common.Shape, from_point: int = 0) -> list[dict]:
    """Sites, equips and points new in [from_point, shape.points), as
    Haxall records with ids from their topics: the same structure and
    markers the generator's tags give Baler. A site or equip is new when
    its first point is; nothing is re-added, so a step adds only its own."""
    out: list[dict] = []
    per_site = shape.equips_per_site * shape.points_per_equip
    for g in range(from_point // shape.points_per_equip, shape.equips):
        pts = [p for p in shape.equip_points(g) if p[0] >= from_point]
        site = g // shape.equips_per_site
        site_ref = "@" + haxall_bridge.topic_ref(shape.site_prefix(site))
        if g % shape.equips_per_site == 0 and site * per_site >= from_point:
            out.append({"id": site_ref, "dis": f"scale-s{site:05d}", "site": True, "scale": True, "tz": "UTC"})
        prefix = shape.equip_prefix(g)
        equip_ref = "@" + haxall_bridge.topic_ref(prefix)
        if g * shape.points_per_equip >= from_point:
            out.append({"id": equip_ref, "dis": prefix[len(common.TOPIC_ROOT) + 1:], "equip": True, "ahu": True,
                        "scale": True, "siteRef": site_ref})
        for idx, label, t in pts:
            tags = {k: v for k, v in common.point_tags(shape, g, idx, label, t).items() if k != "dis"}
            tags.update(id="@" + haxall_bridge.topic_ref(f"{prefix}/{label}"),
                        dis=f"{prefix[len(common.TOPIC_ROOT) + 1:]} {label}",
                        his=True, tz="UTC", equipRef=equip_ref, siteRef=site_ref)
            out.append(tags)
    return out


def canary_record() -> dict:
    return {"id": "@" + haxall_bridge.topic_ref(common.CANARY_TOPIC), "dis": "scale canary", "point": True, "his": True,
            "kind": "Number", "tz": "UTC", "scaleCanary": True}


def commit_exprs(recs: list[dict], chunk: int = 500) -> list[str]:
    """Axon commits adding `recs`, `chunk` records per eval, with their ids
    given (a record that already exists fails the commit: `down` first)."""
    return ["[" + ", ".join(f"diff(null, {axon_dict(r)}, {{add}})" for r in recs[i:i + chunk]) + "].commit"
            for i in range(0, len(recs), chunk)]


def provision(client, shape: common.Shape, from_point: int = 0) -> dict:
    t0 = time.time()
    recs = records(shape, from_point)
    if from_point == 0:
        recs.append(canary_record())
    for expr in commit_exprs(recs):
        eval_axon(client, expr)
    return {"records_added": len(recs), "provision_s": round(time.time() - t0, 1)}


def scale_points(client) -> int:
    rows = eval_axon(client, "readAll(point and scale).size")
    return int(float(str(rows[0]["val"]).removeprefix("n:").split(" ")[0])) if rows else 0


# ----------------------------------------------------------------- measures

def span_expr(a: float, b: float) -> str:
    return f"fromJavaMillis({int(a * 1000)}, \"UTC\")..fromJavaMillis({int(b * 1000)}, \"UTC\")"


def stored_expr(a: float, b: float) -> str:
    """Rows with ts in [a, b) over every scale point (not the canary): one
    hisRead per point - a measurement cost, paid after the soak."""
    return (f"readAll(point and scale and his).map(p => hisRead(p, {span_expr(a, b)})"
            f".findAll(r => r->ts < fromJavaMillis({int(b * 1000)}, \"UTC\")).size).fold(sum)")


def stored_in(client, a: float, b: float) -> int:
    rows = eval_axon(client, stored_expr(a, b))
    return int(float(str(rows[0]["val"]).removeprefix("n:").split(" ")[0])) if rows and rows[0].get("val") is not None else 0


def parse_bridge_stats(log: str) -> list[dict]:
    """BRIDGE_STATS lines of the bridge's log, oldest first. Pure."""
    out = []
    for line in log.splitlines():
        i = line.find("BRIDGE_STATS ")
        if i >= 0:
            try:
                out.append(json.loads(line[i + len("BRIDGE_STATS "):]))
            except json.JSONDecodeError:
                pass
    return out


def bridge_stats(since: str = "10m") -> list[dict]:
    r = compose("logs", "--no-color", "--since", since, "haxall_bridge", check=False, capture=True)
    return parse_bridge_stats(r.stdout)


def summarize_bridge(stats: list[dict]) -> dict:
    """The bridge's own limit over the soak, reported apart from Haxall's:
    its CPU, queue, batch size and hisWrite latency. Pure."""
    if not stats:
        return {}
    busy = [s for s in stats if s.get("written_per_s", 0) > 0] or stats

    def mean(k):
        vals = [s[k] for s in busy if s.get(k) is not None]
        return round(statistics.mean(vals), 2) if vals else None

    return {"written_per_s_mean": mean("written_per_s"), "cpu_pct_mean": mean("cpu_pct"),
            "cpu_pct_max": max((s.get("cpu_pct", 0) for s in busy), default=None),
            "write_busy_pct_mean": mean("write_busy_pct"), "queue_max": max((s.get("queue", 0) for s in stats), default=0),
            "last_batch_mean": mean("last_batch"), "last_write_s_mean": mean("last_write_s"),
            "rejected": stats[-1].get("rejected"), "skipped": stats[-1].get("skipped"), "mode": stats[-1].get("mode"),
            "batch_refused": stats[-1].get("batch_refused")}


def durability() -> dict:
    """Haxall's flush/fsync settings as found (#236): its version, the
    files under its project dir that mention sync or flush, the JVM
    options, and the volume its database is on (a sync-mounted one when
    SCALE_HAXALL_VAR_VOLUME is set). `comparable` needs a declared fsync
    setting (SCALE_HAXALL_DURABILITY in scale.env, naming how) on top of
    `crash` losing nothing - the crash result is recorded beside it."""
    found: dict = {}
    r = compose("exec", "-T", "haxall", "sh", "-c",
                "ls -la /haxall/var/scale 2>/dev/null; "
                "grep -rIil -E 'fsync|sync|flush' /haxall/etc /haxall/var/scale 2>/dev/null | head -20; "
                "grep -rIh -E 'fsync|flush' /haxall/etc 2>/dev/null | head -20", check=False, capture=True)
    found["probe"] = r.stdout.strip()[-2000:]
    found["java_tool_options"] = common.read_env().get("SCALE_HAXALL_JAVA_OPTS", "")
    found["var_volume"] = common.read_env().get("SCALE_HAXALL_VAR_VOLUME") or f"{common.VOLUME_PREFIX}_haxall-var"
    declared = common.read_env().get("SCALE_HAXALL_DURABILITY", "")
    return {"found": found, "declared_fsync": declared, "comparable": bool(declared),
            "note": "comparable only with fsync on (declared) and `crash --target haxall` losing nothing"}


def container_stats() -> dict:
    ids = compose("ps", "-q", check=False, capture=True).stdout.split()
    if not ids:
        return {}
    out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}", *ids],
                         capture_output=True, text=True, timeout=60, check=False).stdout
    stats = {}
    for line in out.splitlines():
        d = json.loads(line)
        name = d["Name"]
        svc = name[len(PROJECT) + 1:].rsplit("-", 1)[0] if name.startswith(PROJECT + "-") else name
        stats[svc] = {"cpu_pct": float(d["CPUPerc"].rstrip("%") or 0), "mem": d["MemUsage"],
                      "mem_pct": float(d["MemPerc"].rstrip("%") or 0), "block_io": d["BlockIO"]}
    return stats


def disk_bytes() -> int | None:
    r = compose("exec", "-T", "haxall", "du", "-sb", "/haxall/var/scale", check=False, capture=True)
    try:
        return int(r.stdout.split()[0])
    except (IndexError, ValueError):
        return None


def run_probes(client, runs: int = 5) -> list[dict]:
    out = []
    for p in HAXALL_PROBES:
        times, status, size = [], 200, 0
        for _ in range(runs):
            t0 = time.perf_counter()
            try:
                rows = (client._get("read", {"filter": p["filter"]}).get("rows", []) if p["op"] == "read"
                        else eval_axon(client, p["expr"]))
                size = len(json.dumps(rows))
            except Exception as exc:  # noqa: BLE001 - a failure is a result
                status = repr(exc)[:200]
            times.append(time.perf_counter() - t0)
        out.append({"name": p["name"], "op": p["op"], "query": p.get("filter") or p.get("expr"), "status": status,
                    "bytes": size, "cold_s": round(times[0], 3), "median_s": round(statistics.median(times[1:] or times), 3),
                    "max_s": round(max(times), 3)})
    return out


def make_canary(ladder, ep: Endpoints):
    """ladder.Canary, polling Haxall's hisRead of the canary point instead
    of point_history."""

    class HaxallCanary(ladder.Canary):
        def _poll(self):
            client = None
            ref = "@" + haxall_bridge.topic_ref(common.CANARY_TOPIC)
            while not self.stop.wait(0.5):
                try:
                    client = client or ep.client()
                    with self.lock:
                        pending = dict(self.pending)
                    if not pending:
                        continue
                    rows = client._get("hisRead", {"id": ref, "range": "today"}).get("rows", [])
                    seen = set()
                    for r in rows:
                        v = r.get("val")
                        if isinstance(v, str) and v.startswith("n:"):
                            seen.add(round(float(v[2:].split(" ")[0]), 6))
                    now = time.time()
                    with self.lock:
                        for ts, pub in list(self.pending.items()):
                            if ts in seen:
                                self.samples.append((pub, now - pub))
                                del self.pending[ts]
                            elif now - pub > self.timeout:
                                self.lost.append(pub)
                                del self.pending[ts]
                except Exception as exc:  # noqa: BLE001 - Haxall down mid-crash-test: keep trying
                    print(f"canary poll (haxall): {exc!r}")
                    client = None
                    time.sleep(1)

    return HaxallCanary(ep)


# ----------------------------------------------------------------- commands

def cmd_up(args) -> Endpoints:
    config = guard()
    build = [] if getattr(args, "no_build", False) else ["--build"]
    compose("up", "-d", *build)
    ep = Endpoints(config)
    for _ in range(180):
        try:
            ep.client().about()
            break
        except Exception:  # noqa: BLE001 - booting
            time.sleep(2)
    else:
        raise SystemExit(f"Haxall never answered at {ep.haxall}")
    print(f"haxall stack up: {ep.haxall}, broker 127.0.0.1:{ep.mqtt_port}, caps {haxall_caps(common.read_env(), os.cpu_count() or 1)}")
    return ep


def cmd_down(args) -> None:
    compose("down", "-v", "--remove-orphans", *(["--rmi", "local"] if getattr(args, "rmi", False) else []), check=False)


def run_header(ladder, args) -> dict:
    nproc = os.cpu_count() or 1
    return {"run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), "kind": "ladder", "target": "haxall",
            "hardware": common.hardware(), "commits": common.commits(), "durability": durability(),
            "cpu_caps": haxall_caps(common.read_env(), nproc), "cpuset": common.read_env().get("SCALE_CPUSET", ""),
            "config": {k: v for k, v in vars(args).items() if k != "func"},
            "command": " ".join([Path(sys.executable).name, *sys.argv])}


def evaluate(ladder, step: dict) -> tuple[str, list[str]]:
    """ladder.evaluate's ingest, lag and restart rules (Haxall has no
    budgets of its own: its probes are compared, not judged)."""
    probes = step.pop("probes", None)
    try:
        return ladder.evaluate(step)
    finally:
        if probes is not None:
            step["probes"] = probes


def run_step(ladder, ep: Endpoints, args, points: int, prev: int, outdir: Path, safety=None) -> dict:
    import signal

    shape = common.Shape(points, args.points_per_equip, args.equips_per_site)
    step: dict = {"points": points, "target": "haxall", "offered_target_rps": points / args.interval,
                  "started": datetime.now(timezone.utc).isoformat()}
    print(f"\n=== haxall step {points:,} points ===", flush=True)
    client = ep.client()
    step["provision"] = provision(client, shape, prev)
    stats_file = outdir / f"gen-readings-{points}.json"
    gen = ladder.start_loadgen(ep, args, points, "readings", stats_file)
    sysmon = ladder.Sys(ep)
    canary = make_canary(ladder, ep)
    samples: list[dict] = []
    try:
        time.sleep(args.warmup)
        soak_start = time.time()
        sys0 = sysmon.snapshot()
        while time.time() - soak_start < args.soak:
            time.sleep(args.sample_every)
            st = ladder.read_stats(stats_file)
            samples.append({"t": round(time.time() - soak_start, 1), "gen_rate": round(st.get("sent_rate_10s", 0)),
                            "gen_behind_s": round(st.get("behind_s", 0), 1), "broker": sysmon.snapshot(),
                            "docker": container_stats(), "lag": canary.stats(soak_start)})
            if safety is not None:
                tripped, live = safety.check()
                samples[-1]["live"] = live
                if tripped:
                    step["live_safety_tripped"] = live
                    break
            if (samples[-1]["lag"].get("max_s") or 0) > args.abort_lag:
                break
        soak_end = time.time()
        sys1 = sysmon.snapshot()
        step["bridge"] = summarize_bridge(bridge_stats(f"{int(soak_end - soak_start) + 30}s"))
        step["probes"] = run_probes(client)
    finally:
        gen.send_signal(signal.SIGTERM)
        try:
            gen.wait(timeout=60)
        except subprocess.TimeoutExpired:
            gen.kill()
        canary.drain(max_wait=args.abort_lag)
        canary.close()
        sysmon.close()
    st = ladder.read_stats(stats_file)
    a, b = int(soak_start) + 1, int(soak_end)
    acked = ladder.acked_in(st, a, b)
    stored = stored_in(client, a, b)
    dur = max(b - a, 1)
    dropped = None
    try:
        dropped = int(sys1.get("publish/messages/dropped", 0)) - int(sys0.get("publish/messages/dropped", 0))
    except (TypeError, ValueError):
        pass
    step["ingest"] = {"window_s": b - a, "acked": acked, "stored": stored,
                      "stored_ratio": round(stored / acked, 5) if acked else None,
                      "offered_rps": round(acked / dur, 1), "stored_rps": round(stored / dur, 1),
                      "stored_rate_ratio": round(stored / acked, 4) if acked else None,
                      "broker_dropped_delta": dropped}
    step["generator"] = {"target_rps": points / args.interval, "achieved_rps": round(acked / dur, 1),
                         "offered_ratio": round(acked / dur / (points / args.interval), 4)}
    step["lag"] = canary.stats(soak_start)
    step["resources"] = summarize_docker(samples)
    step["disk_bytes"] = disk_bytes()
    step["samples"] = samples
    step["status"], step["reasons"] = evaluate(ladder, step)
    return step


def summarize_docker(samples: list[dict]) -> dict:
    out: dict = {}
    for s in samples:
        for svc, d in (s.get("docker") or {}).items():
            o = out.setdefault(svc, {"cpu": [], "mem_pct": [], "mem": ""})
            o["cpu"].append(d["cpu_pct"])
            o["mem_pct"].append(d["mem_pct"])
            o["mem"] = d["mem"]
    return {svc: {"cpu_pct_mean": round(statistics.mean(o["cpu"]), 1), "cpu_pct_max": round(max(o["cpu"]), 1),
                  "mem_pct_max": round(max(o["mem_pct"]), 1), "mem_last": o["mem"].split(" / ")[0]}
            for svc, o in out.items() if o["cpu"]}


def cmd_run(ladder, args) -> None:
    config = guard()
    ep = cmd_up(args) if not args.no_up else Endpoints(config)
    nproc = os.cpu_count() or 1
    steps = [ladder.parse_count(s) for s in args.steps.split(",")] if args.steps else ladder.default_steps(nproc)
    existing = scale_points(ep.client())
    if existing > steps[0] and not args.reuse:
        raise SystemExit(f"Haxall already has {existing:,} scale points, more than the first step: "
                         "`ladder.py down --target haxall` then `up`, or --reuse")
    safety = ladder.LiveSafety(args.live_url, args.live_load_factor, args.live_latency_factor,
                               enabled=not args.no_live_check)
    result = run_header(ladder, args)
    result.update(steps=[], live_safety=safety.describe())
    outdir = common.RESULTS / result["run_id"]
    outdir.mkdir(parents=True, exist_ok=True)
    if not result["durability"]["comparable"]:
        print("note: Haxall's fsync setting isn't declared (SCALE_HAXALL_DURABILITY): results labelled not comparable",
              flush=True)
    prev = 0
    for points in steps:
        step = run_step(ladder, ep, args, points, prev, outdir, safety)
        result["steps"].append(step)
        print(f"haxall step {points:,}: {step['status']} {step.get('reasons')}\n  -> {ladder.write_result(result)}",
              flush=True)
        prev = points
        if step["status"] != "pass" and not args.continue_on_fail:
            break


def cmd_crash(ladder, args) -> None:
    """`docker kill -s KILL` haxall (and/or haxall_bridge) mid-run, restart,
    and count readings the broker acknowledged that never reached Haxall's
    history - the same measure as Baler's crash test."""
    import signal

    config = guard()
    ep = Endpoints(config)
    result = run_header(ladder, args)
    result.update(kind="crash", crash=[])
    outdir = common.RESULTS / result["run_id"]
    outdir.mkdir(parents=True, exist_ok=True)
    client = ep.client()
    have = scale_points(client)
    if have < args.points:
        provision(client, common.Shape(args.points, args.points_per_equip, args.equips_per_site), have)
    for target in args.kill.split(","):
        stats_file = outdir / f"gen-{target}.json"
        gen = ladder.start_loadgen(ep, args, args.points, "readings", stats_file)
        try:
            time.sleep(args.warmup)
            a = int(time.time())
            time.sleep(15)
            t_kill = time.time()
            cid = compose("ps", "-q", target, capture=True).stdout.strip()
            subprocess.run(["docker", "kill", "-s", "KILL", cid], check=True, capture_output=True)
            time.sleep(args.down_for)
            compose("up", "-d", "--no-deps", target, check=False)
            t_back = time.time()
            time.sleep(30)
            b = int(time.time())
        except BaseException:
            gen.kill()
            raise
        gen.send_signal(signal.SIGTERM)
        gen.wait(timeout=60)
        stored, client = None, None
        for _ in range(120):  # drain: the bridge catches up on what the broker kept for its session
            try:
                client = client or ep.client()
                n1 = stored_in(client, a, b)
                time.sleep(5)
                stored = stored_in(client, a, b)
                if stored == n1:
                    break
            except Exception:  # noqa: BLE001 - Haxall may still be booting
                client = None
                time.sleep(5)
        acked = ladder.acked_in(ladder.read_stats(stats_file), a, b)
        stored = stored or 0
        entry = {"target": target, "window": [a, b], "back_after_s": round(t_back - t_kill, 1), "acked": acked,
                 "stored": stored, "missing_acked": max(0, acked - stored),
                 "missing_ratio": round(max(0, acked - stored) / acked, 6) if acked else None,
                 "bridge": summarize_bridge(bridge_stats("5m"))}
        result["crash"].append(entry)
        print(f"kill {target}: acked {acked}, stored {stored}, missing {entry['missing_acked']}", flush=True)
    print(f"-> {ladder.write_result(result)}")


# ----------------------------------------------------------------- the table

def side_by_side(baler: dict, haxall: dict) -> list[str]:
    """Markdown rows for scale-test.mdx's Baler vs Haxall table, one per
    step both runs have. Pure (tested)."""
    def by_points(r):
        return {s["points"]: s for s in r.get("steps", [])}

    def probe(s, name):
        p = {x["name"]: x for x in s.get("probes", [])}.get(name)
        return f"{p['median_s']} s" if p and p.get("status") == 200 else ("-" if not p else "error")

    def ram_disk(s, target):
        res = s.get("resources") or {}
        if target == "haxall":
            mem = (res.get("haxall") or {}).get("mem_last") or "-"
            disk = s.get("disk_bytes")
        else:
            mem = "+".join(str((res.get(svc) or {}).get("mem_last") or "-") for svc in ("postgres", "oxigraph"))
            disk = (s.get("db") or {}).get("db_bytes")
        return f"{mem} / {round(disk / 1e9, 2) if disk else '-'} GB"

    rows = []
    hx = by_points(haxall)
    for pts, s in sorted(by_points(baler).items()):
        h = hx.get(pts)
        if h is None:
            continue
        rows.append(
            f"| {pts:,} | {s.get('ingest', {}).get('stored_rps', '-')} | {h.get('ingest', {}).get('stored_rps', '-')} | "
            f"{s.get('lag', {}).get('p95_s', '-')} s | {h.get('lag', {}).get('p95_s', '-')} s | "
            f"{probe(s, 'read?filter=point')} | {probe(h, 'read?filter=point')} | "
            f"{probe(s, 'hisRead 1000 points (today)')} | {probe(h, 'hisRead 1000 points (today)')} | "
            f"{ram_disk(s, 'baler')} | {ram_disk(h, 'haxall')} |")
    return rows

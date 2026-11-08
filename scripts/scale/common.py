"""
Shared pieces of the scale harness (Baler#190): the synthetic site/equip/point
shape, the exact MQTT envelope Windrow publishes, the isolated compose stack
(`-p scale`) and the guard that refuses to touch a live install.

Nothing here imports baler: the generator and the guard run on the host with
only paho-mqtt (and psycopg/requests for the ladder), which `pip install -e .`
already provides.
"""

from __future__ import annotations

import json
import math
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

# ---------------------------------------------------------------- the stack

PROJECT = "scale"
VOLUME_PREFIX = "scale"
ENV_FILE = HERE / "scale.env"
OVERRIDE = HERE / "compose.scale.yml"
RESULTS = HERE / "results"

# What a live install uses by default (docker-compose.yml with no overrides,
# and the two names a live checkout has had). The scale stack must differ
# from every one of these, or the guard refuses to run.
LIVE_PROJECTS = {"baler", "baler-deploy", "timberdoodle", "fbf", "windrow"}
LIVE_VOLUME_PREFIXES = {"baler", "timberdoodle"}
LIVE_PORTS = {1883, 8883, 7878, 5433, 16686, 4318, 3033, 8080, 8888, 8011, 47808, 47900}

# Topic root for everything the generator publishes: under fbf/ because the
# listener subscribes to fbf/# (as live does), and its own subtree so a
# stray generator pointed at the wrong broker is obvious in any graph.
TOPIC_ROOT = "fbf/scale"
CANARY_TOPIC = f"{TOPIC_ROOT}/canary/lag"


def compose_cmd(*args: str, env_file: Path = ENV_FILE, project: str = PROJECT) -> list[str]:
    return ["docker", "compose", "-p", project, "--env-file", str(env_file),
            "-f", str(REPO / "docker-compose.yml"), "-f", str(OVERRIDE), *args]


def read_env(path: Path = ENV_FILE) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def compose_config(env_file: Path = ENV_FILE, project: str = PROJECT) -> dict:
    out = subprocess.run(compose_cmd("config", "--format", "json", env_file=env_file, project=project),
                         check=True, capture_output=True, text=True, cwd=REPO).stdout
    return json.loads(out)


def published_ports(config: dict) -> dict[str, dict[int, int]]:
    """{service: {container_port: host_port}} from `compose config`."""
    out: dict[str, dict[int, int]] = {}
    for name, svc in config.get("services", {}).items():
        for p in svc.get("ports") or []:
            if p.get("published"):
                out.setdefault(name, {})[int(p["target"])] = int(p["published"])
    return out


def guard_problems(config: dict, env_file: Path = ENV_FILE, checkout_name: str | None = None) -> list[str]:
    """Every reason the effective compose config could be (or touch) a live
    install. Empty list = safe to run. Pure: tested without Docker."""
    problems = []
    project = config.get("name", "")
    checkout_name = checkout_name or REPO.name
    if project in LIVE_PROJECTS or project == checkout_name:
        problems.append(f"compose project {project!r} is a live install's default name")
    if "scale" not in project:
        problems.append(f"compose project {project!r} must contain 'scale'")
    for key, vol in (config.get("volumes") or {}).items():
        name = vol.get("name", key)
        prefix = name.split("_", 1)[0]
        if prefix in LIVE_VOLUME_PREFIXES or not name.startswith(f"{VOLUME_PREFIX}_"):
            problems.append(f"volume {name!r} is not under the {VOLUME_PREFIX}_ prefix (BALER_VOLUME_PREFIX)")
    for svc, ports in published_ports(config).items():
        for target, host in ports.items():
            if host in LIVE_PORTS:
                problems.append(f"{svc} publishes host port {host} (container {target}), a live default port")
    if env_file.resolve() == (REPO / ".env").resolve():
        problems.append("the env file is the checkout's .env (live secrets); use scripts/scale/scale.env")
    return problems


# ---------------------------------------------------------------- the shape

# One equip's worth of points, cycled: (label, Haystack markers, unit, kind,
# value pattern). Labels and markers mirror what Windrow's bacnet_tagger
# produces for an AHU-ish controller.
TEMPLATES = [
    ("sat", "discharge air temp sensor", "°F", "Number", "sine"),
    ("sat-sp", "discharge air temp sp writable", "°F", "Number", "const"),
    ("rat", "return air temp sensor", "°F", "Number", "sine"),
    ("mat", "mixed air temp sensor", "°F", "Number", "walk"),
    ("zone-temp", "zone air temp sensor", "°F", "Number", "walk"),
    ("fan-status", "discharge fan run sensor", None, "Bool", "bool"),
    ("fan-cmd", "discharge fan run cmd writable", None, "Bool", "bool"),
    ("oa-damper", "outside air damper cmd writable", "%", "Number", "step"),
    ("chw-valve", "chilled water valve cmd writable", "%", "Number", "step"),
    ("dsp", "discharge air pressure sensor", "inH₂O", "Number", "sine"),
]
PATTERNS = ("mix", "sine", "walk", "const", "step", "bool")
# hisRead probes read readAll(<marker>): the first N points carry it.
HIS_GROUPS = (1, 100, 1000)


@dataclass(frozen=True)
class Shape:
    points: int
    points_per_equip: int = 10
    equips_per_site: int = 50

    @property
    def equips(self) -> int:
        return math.ceil(self.points / self.points_per_equip)

    @property
    def sites(self) -> int:
        return math.ceil(self.equips / self.equips_per_site)

    def equip_prefix(self, g: int) -> str:
        return f"{TOPIC_ROOT}/s{g // self.equips_per_site:05d}/e{g % self.equips_per_site:05d}"

    def site_prefix(self, site: int) -> str:
        return f"{TOPIC_ROOT}/s{site:05d}"

    def equip_points(self, g: int) -> list[tuple[int, str, tuple]]:
        """(global point index, label, template) for equip g."""
        first = g * self.points_per_equip
        out = []
        for j in range(min(self.points_per_equip, self.points - first)):
            t = TEMPLATES[j % len(TEMPLATES)]
            label = t[0] if j < len(TEMPLATES) else f"{t[0]}-{j // len(TEMPLATES)}"
            out.append((first + j, label, t))
        return out


def point_tags(shape: Shape, g: int, idx: int, label: str, template: tuple) -> dict:
    _, markers, unit, kind, _ = template
    tags: dict = {m: True for m in markers.split()}
    tags.update({"point": True, "kind": kind, "scale": True,
                 "dis": f"{shape.equip_prefix(g)[len(TOPIC_ROOT) + 1:]} {label}"})
    if unit:
        tags["unit"] = unit
    for n in HIS_GROUPS:
        if idx < n:
            tags[f"scaleHis{n}"] = True
    return tags


def equip_tags(shape: Shape, g: int, labels: list[str]) -> dict:
    site = g // shape.equips_per_site
    return {"tags": {"windrowSite": f"scale-s{site:05d}", "windrowInstance": "scale-loadgen",
                     "equip": True, "ahu": True, "scale": True,
                     "dis": shape.equip_prefix(g)[len(TOPIC_ROOT) + 1:]},
            "points": labels}


def envelope(point: str, value, ts: float) -> bytes:
    """Windrow's mqtt_sink._publish payload, byte for byte (json.dumps of
    the same dict in the same key order)."""
    return json.dumps({"point": point, "value": value, "ts": ts}).encode()


def value_at(pattern: str, idx: int, tick: int):
    """Deterministic synthetic value for point idx at reading number tick."""
    if pattern == "const":
        return 70.0
    if pattern == "bool":
        return (tick // (6 + idx % 7)) % 2 == 0
    if pattern == "step":
        return float(((tick // 12) + idx) % 5 * 25)
    if pattern == "walk":
        # cheap deterministic pseudo-walk: sum of two slow sines with
        # per-point phase, so no state is kept per point
        return round(70 + 3 * math.sin((tick + idx * 7) / 37.0) + math.sin((tick + idx) / 5.3), 3)
    return round(70 + 5 * math.sin(2 * math.pi * (tick + idx % 120) / 120.0), 3)  # sine, 10 min @ 5 s


# ---------------------------------------------------------------- the host

def _run(cmd: list[str], cwd: Path | None = None) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=cwd, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def hardware() -> dict:
    """What the numbers were measured on: never compare a 1-CPU and a
    12-CPU run without seeing this (Baler#190)."""
    model = ""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    mem_kb = 0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                mem_kb = int(line.split()[1])
    except OSError:
        pass
    try:
        cpuset = sorted(os.sched_getaffinity(0))
    except AttributeError:
        cpuset = []
    docker_root = _run(["docker", "info", "--format", "{{.DockerRootDir}}"]) or "/var/lib/docker"
    disk = shutil.disk_usage(docker_root if os.path.exists(docker_root) else "/")
    return {
        "hostname": platform.node(),
        "nproc": os.cpu_count(),
        "cpu_affinity": f"{cpuset[0]}-{cpuset[-1]}" if cpuset else "",
        "cpu_model": model,
        "mem_total_gib": round(mem_kb / 1048576, 1),
        "kernel": platform.release(),
        "loadavg_at_start": os.getloadavg(),
        "docker_version": _run(["docker", "version", "--format", "{{.Server.Version}}"]),
        "docker_root": docker_root,
        "disk_total_gb": round(disk.total / 1e9, 1),
        "disk_free_gb": round(disk.free / 1e9, 1),
    }


def commits() -> dict:
    baler = _run(["git", "rev-parse", "HEAD"], REPO)
    dirty = bool(_run(["git", "status", "--porcelain", "--untracked-files=no"], REPO))
    # The windrow submodule's pinned commit, whether or not it's checked out.
    windrow = ""
    tree = _run(["git", "ls-tree", "HEAD", "windrow"], REPO).split()
    if len(tree) >= 3:
        windrow = tree[2]
    return {"baler": baler, "baler_dirty": dirty, "windrow": windrow,
            "baler_branch": _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], REPO)}

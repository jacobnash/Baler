"""#255: every long-running compose service must restart after a reboot or
power cut. Without a restart policy Docker leaves the container stopped, so
Baler stayed down on nash-srv until someone deployed by hand. `migrate` is a
one-shot job and must not restart.

Two layers: a services-free check of the YAML (runs in the unit job), and,
when Docker is available, the effective `docker compose config` of the
full nash-srv file set (base + override + windrow + lan, every profile)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "docker-compose.yml"
NASH = [ROOT / "deploy/nash-srv" / f for f in ("docker-compose.override.yml", "docker-compose.windrow.yml", "docker-compose.lan.yml")]
ONE_SHOT = {"migrate"}
RESTARTS = {"unless-stopped", "always"}


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that reads compose's own tags (!override, !reset) as plain
    values - the files use them, and they don't affect restart policies."""


def _compose_tag(loader, _suffix, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _compose_tag)


def _services(path: Path) -> dict:
    return (yaml.load(path.read_text(), Loader=_ComposeLoader) or {}).get("services") or {}


def test_every_base_service_restarts_except_one_shot_jobs():
    bad = {name: svc.get("restart") for name, svc in _services(BASE).items()
           if name not in ONE_SHOT and svc.get("restart") not in RESTARTS}
    assert bad == {}, f"long-running services with no restart policy: {bad}"
    for name in ONE_SHOT:
        assert _services(BASE)[name].get("restart") == "no"


@pytest.mark.parametrize("path", NASH, ids=lambda p: p.name)
def test_services_added_by_nash_srv_files_restart(path):
    """A service an override file adds (not just patches) needs its own
    policy; a patched base service inherits the base one unless overridden."""
    base = _services(BASE)
    bad = {}
    for name, svc in _services(path).items():
        restart = svc.get("restart", base.get(name, {}).get("restart"))
        if name not in ONE_SHOT and restart not in RESTARTS:
            bad[name] = restart
    assert bad == {}, f"{path.name}: services with no restart policy: {bad}"


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker CLI")
def test_effective_nash_srv_config_restarts_everything(tmp_path):
    """What compose actually resolves for nash-srv's full file set."""
    env = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line)
        if m:
            env[m.group(1)] = re.sub(r"\s+#.*$", "", m.group(2)).strip()
    env.update(BALER_JWT_SECRET="x", BALER_GATEWAY_SECRET="x", MQTT_PASSWORD="x",
               BALER_VOLUME_PREFIX="timberdoodle", WINDROW_STATE_VOLUME="fbf-state")
    for path in NASH:
        for var in re.findall(r"\$\{([A-Z_][A-Z0-9_]*):\?", path.read_text()):
            if not env.get(var):
                env[var] = "x"
    env_file = tmp_path / "compose.env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    cmd = ["docker", "compose", "--env-file", str(env_file), "-f", str(BASE)]
    for path in NASH:
        cmd += ["-f", str(path)]
    cmd += ["--profile", "*", "config", "--format", "json"]
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False, env={**os.environ, "COMPOSE_PROJECT_NAME": "restartcheck"})
    if out.returncode != 0 and "unknown flag" in out.stderr:
        pytest.skip("docker compose plugin too old for --format json")
    assert out.returncode == 0, out.stderr
    services = json.loads(out.stdout)["services"]
    bad = {n: s.get("restart") for n, s in services.items() if n not in ONE_SHOT and s.get("restart") not in RESTARTS}
    assert bad == {}, f"effective config leaves these stopped after a reboot: {bad}"
    assert services["migrate"].get("restart") == "no"

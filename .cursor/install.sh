#!/usr/bin/env bash
# Idempotent repository bootstrap for the Baler Cloud Agent environment.
# Installs Docker (the whole dev experience is `docker compose up`), the Python
# toolchain for bare-metal tests/scripts, seeds a local .env with generated
# HMAC secrets, and pre-builds the compose images so `start` is fast.
set -euxo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

export DEBIAN_FRONTEND=noninteractive
DPKGOPTS=(-o Dpkg::Options::=--force-confold -o Dpkg::Options::=--force-confdef)

# --- 1. Docker + nested-container prerequisites (system deps) ---------------
if ! command -v docker >/dev/null 2>&1; then
  sudo install -m 0755 -d /etc/apt/keyrings
  if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      | sudo gpg --batch --yes --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg
  fi
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update </dev/null
  sudo apt-get install -y "${DPKGOPTS[@]}" \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin </dev/null
fi

# fuse-overlayfs storage driver + iptables are what make dockerd work inside
# the nested Cloud Agent VM (overlay2 is unavailable here).
if ! command -v fuse-overlayfs >/dev/null 2>&1 || ! command -v iptables >/dev/null 2>&1; then
  sudo apt-get update </dev/null
  sudo apt-get install -y "${DPKGOPTS[@]}" fuse-overlayfs iptables uidmap </dev/null
fi

sudo mkdir -p /etc/docker
if [ ! -f /etc/docker/daemon.json ]; then
  echo '{ "storage-driver": "fuse-overlayfs" }' | sudo tee /etc/docker/daemon.json >/dev/null
fi

# Let the agent use `docker` without sudo in fresh login shells.
sudo groupadd -f docker
sudo usermod -aG docker "$(id -un)" || true

# --- 2. Python toolchain for bare-metal tests / scripts ---------------------
if ! dpkg -s python3.12-venv >/dev/null 2>&1; then
  sudo apt-get update </dev/null
  sudo apt-get install -y "${DPKGOPTS[@]}" python3.12-venv python3-pip </dev/null
fi

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
pip install --upgrade pip
# dev = pytest + openapi validators, llm = anthropic/pydantic (autotag +
# llm-classifier tests), validate = pyshacl (SHACL validate_api tests).
pip install -e ".[dev,llm,validate]"

# Sibling Windrow repo (mock-hospital BACnet feed) is optional - install it too
# when it's checked out next to this repo, so the mock-hospital demo works.
if [ -f ../windrow/pyproject.toml ]; then
  pip install -e "../windrow[dev]" || true
fi

# --- 3. Local .env with generated secrets (gitignored) ----------------------
# The gateway refuses to start without the two HMAC secrets. start.sh also
# (re)generates .env per boot via the same helper, since builds skip install.
# shellcheck source=.cursor/gen-env.sh
. "$REPO_DIR/.cursor/gen-env.sh"
ensure_baler_env "$REPO_DIR"

# --- 4. Pre-build compose images so `start` only has to boot them -----------
# dockerd doesn't survive into a later boot, so start it just for the build;
# `start` brings up its own daemon per boot.
if [ -e /proc/sys/net/bridge/bridge-nf-call-iptables ]; then
  sudo sysctl -w net.bridge.bridge-nf-call-iptables=0 >/dev/null || true
  sudo sysctl -w net.bridge.bridge-nf-call-ip6tables=0 >/dev/null || true
fi
if ! sudo docker info >/dev/null 2>&1; then
  sudo nohup dockerd >/tmp/dockerd-install.log 2>&1 &
  for _ in $(seq 1 30); do sudo docker info >/dev/null 2>&1 && break; sleep 1; done
fi
sudo docker compose build

echo "Baler install complete."

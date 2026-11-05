#!/usr/bin/env bash
# Per-boot startup for the Baler Cloud Agent environment.
# Starts the Docker daemon (no systemd in the VM) with the nested-container
# networking fix in place, then brings the whole compose stack up healthy.
set -euxo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# Ensure .env with the gateway's required HMAC secrets exists. This must run
# here (not only in install) because environment builds skip install on boot
# and .env is gitignored, so a fresh pod would otherwise have no secrets and
# `docker compose up` would refuse to start the gateway.
# shellcheck source=.cursor/gen-env.sh
. "$REPO_DIR/.cursor/gen-env.sh"
ensure_baler_env "$REPO_DIR"

# Container-to-container traffic on a compose bridge is dropped in this nested
# VM when bridged packets traverse the iptables FORWARD chain. Turning off
# bridge-nf makes same-network traffic bypass it (published ports still work).
sudo modprobe br_netfilter 2>/dev/null || true
if [ -e /proc/sys/net/bridge/bridge-nf-call-iptables ]; then
  sudo sysctl -w net.bridge.bridge-nf-call-iptables=0 >/dev/null || true
  sudo sysctl -w net.bridge.bridge-nf-call-ip6tables=0 >/dev/null || true
fi

# Start dockerd if it isn't already serving (idempotent across restarts).
if ! sudo docker info >/dev/null 2>&1; then
  sudo nohup dockerd >/tmp/dockerd.log 2>&1 &
  for _ in $(seq 1 60); do
    sudo docker info >/dev/null 2>&1 && break
    sleep 1
  done
fi
sudo docker info >/dev/null 2>&1 || { echo "dockerd failed to start"; sudo tail -n 40 /tmp/dockerd.log; exit 1; }

# Bring up all infra + APIs + gateway; --wait blocks until healthchecks pass.
sudo docker compose up -d --wait

# The nginx gateway resolves and caches each backend's IP at its own startup.
# If a reconciling `up` recreated a backend (new IP) without recreating the
# gateway, its routes 502 with "connection refused" against the stale IP (a
# documented gap, see CLAUDE.md). Restarting the gateway once the backends are
# healthy guarantees it has fresh upstream addresses; it's a no-op cost on a
# clean first boot where everything came up together.
sudo docker compose restart gateway

echo "Baler stack is up (gateway on http://localhost:8080)."

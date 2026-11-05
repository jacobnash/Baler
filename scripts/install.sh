#!/usr/bin/env bash
# First-run installer for a building engineer: ensure secrets exist, bring
# the stack up, open Ops in the browser. Requires Docker Compose only.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Install Docker Desktop (or Docker Engine), then re-run." >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi

ensure_secret() {
  local key="$1"
  if grep -q "^${key}=$" .env || ! grep -q "^${key}=" .env; then
    local val
    val="$(openssl rand -hex 32)"
    if grep -q "^${key}=" .env; then
      # Replace empty assignment
      sed -i.bak "s|^${key}=.*|${key}=${val}|" .env && rm -f .env.bak
    else
      printf '\n%s=%s\n' "$key" "$val" >> .env
    fi
    echo "Generated ${key}"
  fi
}

ensure_secret BALER_JWT_SECRET
ensure_secret BALER_GATEWAY_SECRET

if grep -q '^MQTT_PASSWORD=$' .env || ! grep -q '^MQTT_PASSWORD=' .env; then
  mqtt_val="$(openssl rand -hex 16)"
  if grep -q '^MQTT_PASSWORD=' .env; then
    sed -i.bak "s|^MQTT_PASSWORD=.*|MQTT_PASSWORD=${mqtt_val}|" .env && rm -f .env.bak
  else
    printf '\nMQTT_PASSWORD=%s\n' "$mqtt_val" >> .env
  fi
  echo "Generated MQTT_PASSWORD"
fi

echo "Starting Baler (this can take a minute on first build)…"
docker compose up -d --build --wait

OPS_URL="http://localhost:8080/ui/ops.html"
echo
echo "Stack is up."
echo "Open Ops: ${OPS_URL}"
echo "First visit: create an organization from the Ops sign-in screen."

if command -v open >/dev/null 2>&1; then
  open "$OPS_URL" || true
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$OPS_URL" >/dev/null 2>&1 || true
fi

#!/usr/bin/env bash
# One-time (not per-deploy) population of the public demo with real
# equipment/points/history data, instead of an empty app past the login
# screen. Run once, on nash-srv, after docker-compose.windrow.yml's services
# are up (i.e. after a deploy that includes them):
#
#   cd ~/baler-deploy/deploy/nash-srv && bash seed-demo-data.sh
#
# (or from the actual runner workspace - anywhere docker compose can see
# the same project). Safe-ish to re-run: provisioning/autotag are meant to
# be re-run as new equipment/points show up, but seed_derivations_and_faults.py
# POSTs new derivation/rule definitions every time - re-running it creates
# duplicates, so this script only runs it if BALER_ADMIN_EMAIL has
# no derivations yet.
#
# What this does:
#   1. Tells windrow.api to start polling the mock BACnet device and publish
#      its ~500 points to MQTT (fbf/mock_hospital's site spec, baked into
#      the windrow image at build time).
#   2. Waits for baler's mqtt_listener (already running) to have
#      actually ingested some of that.
#   3. Runs autotag (rule engine only, --no-llm - no ANTHROPIC_API_KEY
#      needed) so points get classified into Brick equipment/point
#      classes - derivations/fault rules below only fire against tagged
#      points.
#   4. Seeds real Brick derivations + fault rules
#      (scripts/seed_derivations_and_faults.py), using the same demo
#      admin account already in GitHub Actions secrets.

set -euo pipefail

DC="docker compose -f docker-compose.yml -f deploy/nash-srv/docker-compose.override.yml -f deploy/nash-srv/docker-compose.windrow.yml"
cd "$(dirname "$0")/../.."   # repo root, wherever this script actually lives

DEMO_ADMIN_EMAIL="${DEMO_ADMIN_EMAIL:?set DEMO_ADMIN_EMAIL in your shell before running (same value as the GitHub secret)}"
DEMO_ADMIN_PASSWORD="${DEMO_ADMIN_PASSWORD:?set DEMO_ADMIN_PASSWORD in your shell before running (same value as the GitHub secret)}"

echo "==> 1. Provisioning windrow.api against the mock device (idempotent - re-provisioning an already-known device/point is a no-op)"
$DC exec -T windrow_api python -m windrow.mock_hospital provision \
  --device-address 172.30.0.10:47808 --device-instance 3456

echo "==> 2. Waiting for mqtt_listener to have ingested something"
for i in $(seq 1 30); do
  count=$($DC exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select count(*) from point_history"' 2>/dev/null || echo 0)
  [ "${count:-0}" -gt 0 ] 2>/dev/null && { echo "    ${count} points ingested"; break; }
  sleep 2
done

echo "==> 3. Autotag (rule engine only, no LLM)"
$DC exec -T ingest_api python -m baler.autotag --no-llm

echo "==> 4. Derivations + fault rules"
existing=$($DC exec -T ingest_api python -c "
import os, urllib.request, json
req = urllib.request.Request('http://gateway/auth/login', method='POST',
    headers={'Content-Type': 'application/json'},
    data=json.dumps({'email': os.environ['DEMO_ADMIN_EMAIL'], 'password': os.environ['DEMO_ADMIN_PASSWORD']}).encode())
token = json.load(urllib.request.urlopen(req))['token']
req = urllib.request.Request('http://gateway/derivation/derivations', headers={'Authorization': f'Bearer {token}'})
print(len(json.load(urllib.request.urlopen(req))))
" -e DEMO_ADMIN_EMAIL="$DEMO_ADMIN_EMAIL" -e DEMO_ADMIN_PASSWORD="$DEMO_ADMIN_PASSWORD" 2>/dev/null || echo 0)

if [ "${existing:-0}" -gt 0 ]; then
  echo "    ${existing} derivations already exist - skipping (safe to delete them first and re-run if you actually want fresh ones)"
else
  $DC exec -T -e BALER_ADMIN_EMAIL="$DEMO_ADMIN_EMAIL" -e BALER_ADMIN_PASSWORD="$DEMO_ADMIN_PASSWORD" \
    ingest_api python scripts/seed_derivations_and_faults.py \
    --auth-api http://gateway/auth --derivation-api http://gateway/derivation --fault-api http://gateway/fault
fi

echo "==> Done. https://timberdoodle.nash.engineering/ui/ should show real equipment now."

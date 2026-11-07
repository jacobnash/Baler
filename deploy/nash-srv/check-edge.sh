#!/usr/bin/env bash
# End-to-end check for edge nodes on nash-srv (Baler#35): is each Pi
# reachable from the Windrow that should poll it, does that Windrow know
# the device, and do readings arrive on the broker. Read-only: it creates
# no connection and writes nothing. Run on nash-srv as a user in the
# docker group (jacob):
#
#   bash deploy/nash-srv/check-edge.sh --bacnet 192.168.128.228 \
#     --modbus 192.168.128.230 --prefix fbf/home
#
#   --bacnet IP    a BACnet/IP device (a Pi running install-edge.sh bacnet)
#   --modbus IP    a Modbus TCP device (install-edge.sh modbus), port 502
#   --prefix P     a topic prefix the readings should arrive under
#   --windrow S    which Windrow polls them: windrow_api (default, the
#                  bridge instance; devices added by IP) or windrow_lan
#                  (the host-network instance, docker-compose.lan.yml)
#   --wait N       seconds to wait for a reading (default 90)
#
# It talks to the containers with `docker exec` by compose labels, so it
# needs no env file. COMPOSE_PROJECT_NAME overrides the project (baler).
set -euo pipefail

BACNET="" MODBUS="" PREFIX="" SVC=windrow_api WAIT=90
while [ $# -gt 0 ]; do
  case "$1" in
    --bacnet) BACNET="$2"; shift 2;;
    --modbus) MODBUS="$2"; shift 2;;
    --prefix) PREFIX="$2"; shift 2;;
    --windrow) SVC="$2"; shift 2;;
    --wait) WAIT="$2"; shift 2;;
    -h|--help) sed -n '2,20p' "$0"; exit 0;;
    *) echo "unknown argument: $1 (see --help)" >&2; exit 2;;
  esac
done
[ -n "$BACNET$MODBUS$PREFIX" ] || { echo "nothing to check: give --bacnet, --modbus and/or --prefix" >&2; exit 2; }
PROJECT="${COMPOSE_PROJECT_NAME:-baler}"

container() {
  docker ps -q --filter "label=com.docker.compose.project=${PROJECT}" \
    --filter "label=com.docker.compose.service=$1" | head -n1
}

FAILED=0
ok() { echo "  ok    $*"; }
bad() { echo "  FAIL  $*"; FAILED=1; }

W=$(container "$SVC")
[ -n "$W" ] || { echo "no running ${SVC} container in compose project ${PROJECT}" >&2; exit 1; }

# The Windrow's own API, from inside its container, on whatever --port its
# command line gives (windrow_api 8001; windrow_lan 127.0.0.1:8011 by
# default, on the host network).
windrow_get() {
  docker exec -i "$W" python - "$1" <<'PY'
import json, os, sys, urllib.request
args = open("/proc/1/cmdline").read().split("\0")
port = args[args.index("--port") + 1] if "--port" in args else "8001"
req = urllib.request.Request(f"http://127.0.0.1:{port}{sys.argv[1]}",
                             headers={"X-Gateway-Secret": os.environ.get("WINDROW_GATEWAY_SECRET", "")})
print(json.dumps(json.load(urllib.request.urlopen(req, timeout=10))))
PY
}

reachable() {  # host port proto(tcp|udp-bacnet)
  docker exec -i "$W" python - "$@" <<'PY'
import socket, struct, sys
host, port, proto = sys.argv[1], int(sys.argv[2]), sys.argv[3]
if proto == "tcp":
    socket.create_connection((host, port), timeout=5).close()
else:
    # A unicast BACnet Who-Is (BVLC original-unicast, plain NPDU, APDU
    # unconfirmed who-is) from an ephemeral port: an I-Am back proves the
    # path both ways through the bridge's NAT. A device that answers with
    # a *broadcast* I-Am fails this from windrow_api - see edge-nodes.mdx.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(5)
    apdu = bytes([0x01, 0x00, 0x10, 0x08])
    s.sendto(bytes([0x81, 0x0A]) + struct.pack(">H", 4 + len(apdu)) + apdu, (host, port))
    data, _ = s.recvfrom(1500)
    if b"\x10\x00" not in data:
        sys.exit("no I-Am in the reply")
PY
}

echo "==> via ${SVC} (${W:0:12})"
if [ -n "$BACNET" ]; then
  if reachable "$BACNET" 47808 bacnet 2>/dev/null; then ok "BACnet device at ${BACNET} answers a unicast Who-Is"
  else bad "no I-Am from ${BACNET}:47808 (is windrow-bacnet running on the Pi? systemctl status windrow-bacnet)"; fi
  if windrow_get /devices | grep -q "\"address\": \"${BACNET}\""; then ok "${SVC} lists ${BACNET} in /devices"
  else bad "${SVC} hasn't recorded ${BACNET} (add it by IP in Connectors/Devices, or wait for the next discovery cycle)"; fi
  if windrow_get /connections | grep -q "\"${BACNET}"; then ok "${SVC} has a BACnet connection polling ${BACNET}"
  else bad "no BACnet connection for ${BACNET} yet (Connect... it, choosing a topic prefix under the site's)"; fi
fi
if [ -n "$MODBUS" ]; then
  if reachable "$MODBUS" 502 tcp 2>/dev/null; then ok "Modbus TCP ${MODBUS}:502 accepts a connection"
  else bad "can't open ${MODBUS}:502 (is windrow-modbus running on the Pi?)"; fi
  if windrow_get /modbus/connections | grep -q "\"${MODBUS}\""; then ok "${SVC} has a Modbus connection to ${MODBUS}"
  else bad "no Modbus connection for ${MODBUS} yet"; fi
fi
if [ -n "$PREFIX" ]; then
  M=$(container mosquitto)
  [ -n "$M" ] || { echo "no running mosquitto container" >&2; exit 1; }
  # MOSQUITTO_USERS is "baler:<password>" (docker-compose.yml)
  if docker exec "$M" sh -c 'pw="${MOSQUITTO_USERS#baler:}"; pw="${pw%% *}"; exec mosquitto_sub -h 127.0.0.1 -u baler -P "$pw" -t "$1/#" -C 1 -W "$2" -v' \
      sh "$PREFIX" "$WAIT" | head -c 300; then
    echo; ok "a reading arrived under ${PREFIX}/"
  else
    bad "nothing under ${PREFIX}/# within ${WAIT}s (a prefix outside the edge account's site is dropped by the broker)"
  fi
fi
exit "$FAILED"

#!/usr/bin/env bash
# Provisions the baler self-hosted GitHub Actions runner on nash-srv.
# Run as: sudo bash setup-runner.sh <RUNNER_REGISTRATION_TOKEN>
# (token from https://github.com/jacobnash/baler/settings/actions/runners/new,
# valid for ~1 hour, single use)
#
# Mirrors nash-srv's existing setup-lagom-runner.sh pattern exactly:
#   1. Reuses the shared `github-runner` OS user (already exists on this box
#      for lagom) - NOT in the `docker` group, since docker-group membership
#      is root-equivalent host access and this box runs unrelated projects.
#   2. Installs a root-owned wrapper at /usr/local/sbin/baler-deploy.sh
#      that is the *only* thing github-runner can run as root (via a
#      narrowly-scoped NOPASSWD sudoers rule) - it takes no arguments and
#      only touches baler's own compose files.
#   3. Real secrets live in ~/.config/baler/nash-srv.env, OUTSIDE the
#      runner's git checkout - actions/checkout runs `git clean -ffdx`,
#      which would delete them if they lived inside the workspace. This
#      script does not fill that file in - copy deploy/nash-srv/.env.example
#      there and edit it by hand.
#   4. Downloads, configures, and installs a second GitHub Actions runner
#      instance (own directory, own systemd service) under github-runner,
#      registered against jacobnash/baler with label `baler`.
#   5. (step 8) A third instance, `nash-srv-baler-verify` with label
#      `baler-verify` (not `baler`, so deploys never land on it), for the
#      Deploy workflow's verify job (#260): verify then runs beside the
#      deploy runner instead of occupying it, so the next deploy never waits
#      on it. Same user, no sudo rule, no docker access; it only reads the
#      live install over HTTPS. Once it's online, point the workflow at it:
#        gh variable set BALER_VERIFY_RUNNER --body baler-verify -R jacobnash/Baler
#
# Idempotent-ish: safe to re-run steps 1-5. Steps 6 and 8 each skip
# themselves if that runner is already configured, so adding the verify
# runner to an existing install is just a re-run with a fresh token:
#   sudo bash setup-runner.sh <RUNNER_REGISTRATION_TOKEN>

set -euo pipefail

# The token is only needed the first time (step 6 registers the runner);
# re-running to update the deploy wrapper works without one.
TOKEN="${1:-}"
RUNNER_USER="github-runner"
RUNNER_HOME="/home/${RUNNER_USER}"
RUNNER_DIR="${RUNNER_HOME}/actions-runner-baler"
VERIFY_RUNNER_DIR="${RUNNER_HOME}/actions-runner-baler-verify"
WRAPPER="/usr/local/sbin/baler-deploy.sh"
SUDOERS_FILE="/etc/sudoers.d/github-runner-baler"
ENV_DIR="${RUNNER_HOME}/.config/baler"
ENV_FILE="${ENV_DIR}/nash-srv.env"
# The runner checks out to _work/<repo>/<repo> using the repo's exact,
# case-sensitive name on GitHub: jacobnash/Baler, so a capital B.
WORKDIR="${RUNNER_DIR}/_work/Baler/Baler"
COMPOSE_BASE="${WORKDIR}/docker-compose.yml"
COMPOSE_OVERRIDE="${WORKDIR}/deploy/nash-srv/docker-compose.override.yml"
COMPOSE_WINDROW="${WORKDIR}/deploy/nash-srv/docker-compose.windrow.yml"
# Opt-in (Baler#78): a host-network Windrow for LAN broadcast discovery,
# added only when the env file has the line BALER_WINDROW_LAN=1.
COMPOSE_LAN="${WORKDIR}/deploy/nash-srv/docker-compose.lan.yml"

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root (sudo)" >&2
  exit 1
fi

echo "==> 1. Creating ${RUNNER_USER} user (no docker group)"
if id "$RUNNER_USER" &>/dev/null; then
  echo "    already exists, skipping"
else
  useradd -m -s /bin/bash "$RUNNER_USER"
fi

echo "==> 2. Prod secrets (${ENV_FILE})"
mkdir -p "$ENV_DIR"
chown "${RUNNER_USER}:${RUNNER_USER}" "$ENV_DIR"
chmod 700 "$ENV_DIR"
if [ -f "$ENV_FILE" ]; then
  echo "    already exists, leaving it alone"
else
  # Generated here, as root, so it's guaranteed to land under the runner's
  # own home (not whoever's $HOME happened to be running this script) -
  # this is exactly the path the deploy wrapper below reads from.
  cat > "$ENV_FILE" <<SECRETS
BALER_JWT_SECRET=$(openssl rand -hex 32)
BALER_GATEWAY_SECRET=$(openssl rand -hex 32)
MQTT_PASSWORD=$(openssl rand -hex 16)
POSTGRES_PASSWORD=$(openssl rand -hex 16)
SECRETS
  chown "${RUNNER_USER}:${RUNNER_USER}" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "    generated"
fi

echo "==> 3. Installing root-owned deploy wrapper at ${WRAPPER}"
cat > "$WRAPPER" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "${WORKDIR}"
DC=(docker compose -f "${COMPOSE_BASE}" -f "${COMPOSE_OVERRIDE}" -f "${COMPOSE_WINDROW}")
# Host-network Windrow for LAN discovery (Baler#78, docs-site edge-nodes):
# only when the env file opts in, so the default deploy is unchanged.
if grep -qx 'BALER_WINDROW_LAN=1' "${ENV_FILE}"; then
  DC+=(-f "${COMPOSE_LAN}")
fi
DC+=(--env-file "${ENV_FILE}")
# git runs as the checkout's owner, never as root: .git/config is writable
# by anything running as ${RUNNER_USER} (e.g. an npm postinstall in the docs
# build), and git honours hooks/fsmonitor/etc. from it (#28).
MARKER=/var/lib/baler/deployed-commit
mkdir -p /var/lib/baler
HEAD_SHA=\$(runuser -u "${RUNNER_USER}" -- git rev-parse HEAD)
PREV=\$(cat "\$MARKER" 2>/dev/null || true)
# Fast path (#25): when everything that changed since the last deploy is
# code or content the app containers bind-mount (.:/app) or read at
# startup, restarting them is enough - no image rebuild, no recreate of
# every container (~46s). Anything else (compose files, Dockerfile,
# pyproject.toml, windrow/, no previous deploy recorded) takes the full path.
FAST=0
CHANGED=""
if [ -n "\$PREV" ] && runuser -u "${RUNNER_USER}" -- git cat-file -e "\$PREV^{commit}" 2>/dev/null; then
  CHANGED=\$(runuser -u "${RUNNER_USER}" -- git diff --name-only "\$PREV" "\$HEAD_SHA")
  if [ -n "\$CHANGED" ] && ! printf '%s\\n' "\$CHANGED" | grep -qvE '^(src/|ui/|gateway/|rules/|ontology/|docs-site/|tests/|todo/|scripts/|[^/]*\\.md\$)'; then
    FAST=1
  fi
fi
# The HTTP APIs behind the gateway are replaced one at a time, each waited
# on until its healthcheck passes, auth_api last (Baler#108): every other
# route keeps working meanwhile (the gateway checks JWTs itself, with no
# call to auth_api), and logins are down only for auth_api's own few
# seconds. The gateway is never restarted for a backend: its upstreams
# re-resolve (gateway/nginx.conf), and it is only reloaded, below.
ROLLING="ingest_api fault_api derivation_api validate_api haystack_api ops_api commissioning_api auth_api"
if [ "\$FAST" = 1 ]; then
  echo "only bind-mounted code/content changed since \$PREV - restarting app services, no rebuild"
  RUNNING=\$("\${DC[@]}" ps --services --status running)
  # Daemons that serve no HTTP (mqtt_listener, fault_detector, ...) all at once.
  OTHERS=\$(printf '%s\\n' "\$RUNNING" | grep -vxE "postgres|oxigraph|mosquitto|jaeger|grafana|gateway|windrow.*|\${ROLLING// /|}" || true)
  [ -n "\$OTHERS" ] && "\${DC[@]}" restart \$OTHERS
  for svc in \$ROLLING; do
    printf '%s\\n' "\$RUNNING" | grep -qx "\$svc" || continue
    "\${DC[@]}" restart "\$svc"
    # Already running and unchanged, so this only waits for it to be healthy.
    "\${DC[@]}" up -d --no-deps --wait --wait-timeout 120 "\$svc"
  done
else
  "\${DC[@]}" pull --ignore-pull-failures
  # Build first: the old containers keep serving while images build.
  "\${DC[@]}" build
  ALL=\$("\${DC[@]}" config --services)
  # Everything but the HTTP APIs and the gateway (infra, migrate, daemons,
  # Windrow): recreated if changed, as before.
  REST=\$(printf '%s\\n' "\$ALL" | grep -vxE "gateway|\${ROLLING// /|}" || true)
  "\${DC[@]}" up -d \$REST
  for svc in \$ROLLING; do
    printf '%s\\n' "\$ALL" | grep -qx "\$svc" || continue
    # Recreated only if its image or definition changed; its dependencies
    # (postgres healthy, migrate done) are already up and aren't touched.
    "\${DC[@]}" up -d --wait --wait-timeout 120 "\$svc"
  done
  # Recreated only if its own compose definition changed.
  "\${DC[@]}" up -d --no-deps --wait --wait-timeout 60 gateway
fi
# Infrastructure services read config bind-mounted from the checkout, and
# 'up -d' only recreates a container when its compose definition changes -
# not when a mounted file does. So a changed mosquitto/ entrypoint or ACL
# never took effect (#59). Restart each one whose config dir changed.
for pair in mosquitto/:mosquitto jaeger/:jaeger grafana/:grafana; do
  if printf '%s\n' "\$CHANGED" | grep -q "^\${pair%%:*}"; then
    echo "\${pair%%:*} changed - restarting \${pair#*:}"
    "\${DC[@]}" restart "\${pair#*:}"
  fi
done
# The app containers bind-mount this checkout (.:/app) and run as root, so
# anything they write into it ends up root-owned - the *next*
# actions/checkout runs as github-runner and can't git-clean it, failing
# the whole next deploy with EACCES before it even starts. This chown is
# only a safety net: it races services that are still starting, so the real
# fix is that they don't write there at all (PYTHONPYCACHEPREFIX in
# docker-compose.yml's shared daemon env, #29).
chown -R "${RUNNER_USER}:${RUNNER_USER}" "${WORKDIR}"
# nginx.conf and njs are bind-mounted, so a change to them needs nginx to
# re-read them. A graceful reload, not a restart: in-flight requests finish
# and the listening socket never closes. nginx -t first, so a bad config
# fails the deploy here (set -e) while the running config keeps serving.
if [ -z "\$PREV" ] || printf '%s\\n' "\$CHANGED" | grep -q '^gateway/'; then
  echo "gateway/ changed - reloading nginx"
  "\${DC[@]}" exec -T gateway nginx -t
  "\${DC[@]}" exec -T gateway nginx -s reload
fi
# Hand back to the workflow (whose next step logs in) only once a login
# reaches auth_api: any status but a gateway-side 5xx or no answer at all.
# \`|| echo 000\` keeps a transient curl failure from tripping set -e.
for i in \$(seq 1 60); do
  code=\$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8080/auth/login \\
    -H 'Content-Type: application/json' -d '{}' || echo "000")
  case "\$code" in 000|502|503|504) sleep 1 ;; *) break ;; esac
done
echo "\$HEAD_SHA" > "\$MARKER"
EOF
chown root:root "$WRAPPER"
chmod 755 "$WRAPPER"
echo "    wrote $WRAPPER"

echo "==> 4. Installing sudoers rule at ${SUDOERS_FILE}"
echo "${RUNNER_USER} ALL=(root) NOPASSWD: ${WRAPPER}" > "$SUDOERS_FILE"
chmod 440 "$SUDOERS_FILE"
visudo -cf "$SUDOERS_FILE"

echo "==> 5. Static docs output dir (Caddy needs read access, no sudo needed to write it)"
mkdir -p /var/www/timberdoodle-docs
chown "${RUNNER_USER}:${RUNNER_USER}" /var/www/timberdoodle-docs
chmod 755 /var/www/timberdoodle-docs

# install_runner DIR NAME LABELS: download, register and start one runner
# instance under ${RUNNER_USER}, as its own systemd service. Skips itself
# when DIR is already configured.
install_runner() {
  local dir="$1" name="$2" labels="$3"
  if [ -f "${dir}/.runner" ]; then
    echo "    ${dir} is already configured - skipping (safe to re-run everything above)"
    return 0
  fi
  if [ -z "$TOKEN" ]; then
    echo "registering a new runner needs a token: sudo bash setup-runner.sh <RUNNER_REGISTRATION_TOKEN>" >&2
    return 1
  fi
  mkdir -p "$dir"
  chown "${RUNNER_USER}:${RUNNER_USER}" "$dir"
  case "$(uname -m)" in
    x86_64) RUNNER_ARCH="x64" ;;
    aarch64) RUNNER_ARCH="arm64" ;;
    *) echo "unsupported arch: $(uname -m)" >&2; exit 1 ;;
  esac
  RELEASE_JSON="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest)"
  RUNNER_VERSION="$(printf '%s' "$RELEASE_JSON" | jq -r '.tag_name | ltrimstr("v")')"
  ASSET_NAME="actions-runner-linux-${RUNNER_ARCH}-${RUNNER_VERSION}.tar.gz"
  # GitHub's own "new self-hosted runner" page shows this same tarball plus
  # a sha256 to validate it against - pulled from the release API here
  # instead of hardcoded, so it can't go stale alongside RUNNER_VERSION.
  RUNNER_SHA256="$(printf '%s' "$RELEASE_JSON" \
    | jq -r --arg name "$ASSET_NAME" '.assets[] | select(.name == $name) | .digest | ltrimstr("sha256:")')"
  if [ -z "$RUNNER_SHA256" ]; then
    echo "could not find a published sha256 digest for ${ASSET_NAME}" >&2
    exit 1
  fi
  runuser -u "$RUNNER_USER" -- bash -c "
    set -euo pipefail
    cd '${dir}'
    curl -fsSL -o runner.tar.gz \
      'https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${ASSET_NAME}'
    echo '${RUNNER_SHA256}  runner.tar.gz' | sha256sum -c -
    tar xzf runner.tar.gz && rm runner.tar.gz
    ./config.sh --url https://github.com/jacobnash/baler \
      --token '${TOKEN}' --name '${name}' \
      --labels '${labels}' --work _work --unattended --replace
  "
  (cd "$dir" && ./svc.sh install "$RUNNER_USER" && ./svc.sh start)
}

echo "==> 6. Installing the GitHub Actions runner (deploys)"
install_runner "$RUNNER_DIR" nash-srv-baler nash-srv,baler

echo "==> 7. MQTT/TLS for edge nodes (Baler#36): Caddy's cert, broker restarted on renewal"
# mosquitto terminates TLS on 8883 with the cert Caddy already keeps for
# this host. Its certificate directory is mounted read-only as-is
# (BALER_MQTT_TLS_DIR); the broker's entrypoint copies the pair in as root,
# so Caddy's 0600 caddy-owned files need no permission changes. Caddy has no
# post-renewal hook without a plugin, so a systemd path unit watches the cert
# and restarts mosquitto (clients reconnect; persistent sessions keep queued
# messages).
MQTT_TLS_HOST="${MQTT_TLS_HOST:-timberdoodle.nash.engineering}"
CADDY_CERT_DIR="/var/lib/caddy/.local/share/caddy/certificates/acme-v02.api.letsencrypt.org-directory/${MQTT_TLS_HOST}"
set_env() {  # set_env NAME VALUE - add or replace one line in the env file
  if grep -q "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else echo "$1=$2" >> "$ENV_FILE"; fi
}
if [ -f "${CADDY_CERT_DIR}/${MQTT_TLS_HOST}.crt" ]; then
  set_env BALER_MQTT_TLS_DIR "$CADDY_CERT_DIR"
  set_env BALER_MQTT_PUBLIC_HOST "$MQTT_TLS_HOST"
  set_env BALER_MQTT_PUBLIC_TLS_PORT 8883
  cat > /etc/systemd/system/baler-mqtt-tls.service <<UNIT
[Unit]
Description=Restart Baler's mosquitto to load a renewed TLS certificate
[Service]
Type=oneshot
ExecStart=/usr/bin/docker compose -f ${COMPOSE_BASE} -f ${COMPOSE_OVERRIDE} -f ${COMPOSE_WINDROW} --env-file ${ENV_FILE} restart mosquitto
UNIT
  cat > /etc/systemd/system/baler-mqtt-tls.path <<UNIT
[Unit]
Description=Watch Caddy's certificate for ${MQTT_TLS_HOST}
[Path]
PathChanged=${CADDY_CERT_DIR}/${MQTT_TLS_HOST}.crt
[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now baler-mqtt-tls.path
  echo "    8883 uses ${CADDY_CERT_DIR}; takes effect on the next deploy (or: systemctl start baler-mqtt-tls)"
else
  echo "    no Caddy cert at ${CADDY_CERT_DIR} - skipped (set MQTT_TLS_HOST=<host> to use another)"
fi

echo "==> 8. Installing the verify runner (the Deploy workflow's verify job, #260)"
# Optional: without a token on an install that already has the deploy
# runner, this is skipped rather than failing the re-run. Last, so that if
# GitHub refuses a token step 6 just used, everything else is already done:
# re-run with a fresh token.
if [ -f "${VERIFY_RUNNER_DIR}/.runner" ] || [ -n "$TOKEN" ]; then
  install_runner "$VERIFY_RUNNER_DIR" nash-srv-baler-verify nash-srv,baler-verify
  VERIFY_RUNNER_READY=1
else
  echo "    skipped (no token). To add it: sudo bash setup-runner.sh <RUNNER_REGISTRATION_TOKEN>"
  VERIFY_RUNNER_READY=0
fi

echo "==> Done. Next steps:"
echo "  1. If you skipped it above: fill in ${ENV_FILE} with real secrets."
echo "  2. Append the Caddy block from docs-site/pages/nash-srv-deploy.mdx to"
echo "     /etc/caddy/Caddyfile, then: sudo systemctl reload caddy"
echo "  3. Add DNS CNAMEs for timberdoodle.nash.engineering and"
echo "     docs.timberdoodle.nash.engineering (see the same doc)."
echo "  4. Add DEMO_ADMIN_EMAIL / DEMO_ADMIN_PASSWORD as GitHub Actions repo secrets."
if [ "$VERIFY_RUNNER_READY" = 1 ]; then
  echo "  5. Point the verify job at the verify runner (once, from anywhere with gh):"
  echo "     gh variable set BALER_VERIFY_RUNNER --body baler-verify -R jacobnash/Baler"
fi

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
#
# Idempotent-ish: safe to re-run steps 1-5. Step 6 skips itself if this
# runner is already configured.

set -euo pipefail

# The token is only needed the first time (step 6 registers the runner);
# re-running to update the deploy wrapper works without one.
TOKEN="${1:-}"
RUNNER_USER="github-runner"
RUNNER_HOME="/home/${RUNNER_USER}"
RUNNER_DIR="${RUNNER_HOME}/actions-runner-baler"
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
DC=(docker compose -f "${COMPOSE_BASE}" -f "${COMPOSE_OVERRIDE}" -f "${COMPOSE_WINDROW}" --env-file "${ENV_FILE}")
MARKER=/var/lib/baler/deployed-commit
mkdir -p /var/lib/baler
HEAD_SHA=\$(git -c safe.directory='*' rev-parse HEAD)
PREV=\$(cat "\$MARKER" 2>/dev/null || true)
# Fast path (#25): when everything that changed since the last deploy is
# code or content the app containers bind-mount (.:/app) or read at
# startup, restarting them is enough - no image rebuild, no recreate of
# every container (~46s). Anything else (compose files, Dockerfile,
# pyproject.toml, windrow/, no previous deploy recorded) takes the full path.
FAST=0
if [ -n "\$PREV" ] && git -c safe.directory='*' cat-file -e "\$PREV^{commit}" 2>/dev/null; then
  CHANGED=\$(git -c safe.directory='*' diff --name-only "\$PREV" "\$HEAD_SHA")
  if [ -n "\$CHANGED" ] && ! printf '%s\\n' "\$CHANGED" | grep -qvE '^(src/|ui/|gateway/|rules/|ontology/|docs-site/|tests/|todo/|scripts/|[^/]*\\.md\$)'; then
    FAST=1
  fi
fi
if [ "\$FAST" = 1 ]; then
  echo "only bind-mounted code/content changed since \$PREV - restarting app services, no rebuild"
  APPS=\$("\${DC[@]}" ps --services --status running | grep -vxE 'postgres|oxigraph|mosquitto|jaeger|grafana|gateway|windrow.*' || true)
  [ -n "\$APPS" ] && "\${DC[@]}" restart \$APPS
else
  "\${DC[@]}" pull --ignore-pull-failures
  "\${DC[@]}" up -d --build
fi
# The app containers bind-mount this checkout (.:/app) and run as root, so
# anything they write into it (e.g. __pycache__/*.pyc) ends up root-owned -
# the *next* actions/checkout runs as github-runner and can't git-clean
# those, failing the whole next deploy with EACCES before it even starts.
# Hand the tree back after every run so that never accumulates.
chown -R "${RUNNER_USER}:${RUNNER_USER}" "${WORKDIR}"
# Gateway caches upstream addresses at its own startup - if a backend got
# recreated above without gateway also restarting, its routes 404/502 even
# though the backend is healthy (see CLAUDE.md's gateway restart gotcha).
docker compose -f "${COMPOSE_BASE}" -f "${COMPOSE_OVERRIDE}" -f "${COMPOSE_WINDROW}" --env-file "${ENV_FILE}" restart gateway
# Block here instead of handing control back to the caller (the deploy
# workflow) while gateway is mid-restart - otherwise its very next step
# can race a connection reset. Any HTTP response (even a 4xx from a
# bogus login body) means it's accepting connections again; \`|| echo 000\`
# keeps a transient curl failure from tripping this script's own set -e.
for i in \$(seq 1 30); do
  code=\$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8080/auth/login \\
    -H 'Content-Type: application/json' -d '{}' || echo "000")
  [ "\$code" != "000" ] && break
  sleep 1
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

echo "==> 6. Installing the GitHub Actions runner"
if [ -f "${RUNNER_DIR}/.runner" ]; then
  echo "    ${RUNNER_DIR} is already configured - skipping (safe to re-run everything above)"
else
  if [ -z "$TOKEN" ]; then
    echo "registering a new runner needs a token: sudo bash setup-runner.sh <RUNNER_REGISTRATION_TOKEN>" >&2
    exit 1
  fi
  mkdir -p "$RUNNER_DIR"
  chown "${RUNNER_USER}:${RUNNER_USER}" "$RUNNER_DIR"
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
    cd '${RUNNER_DIR}'
    curl -fsSL -o runner.tar.gz \
      'https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${ASSET_NAME}'
    echo '${RUNNER_SHA256}  runner.tar.gz' | sha256sum -c -
    tar xzf runner.tar.gz && rm runner.tar.gz
    ./config.sh --url https://github.com/jacobnash/baler \
      --token '${TOKEN}' --name nash-srv-baler \
      --labels nash-srv,baler --work _work --unattended --replace
  "
  (cd "$RUNNER_DIR" && ./svc.sh install "$RUNNER_USER" && ./svc.sh start)
fi

echo "==> Done. Next steps:"
echo "  1. If you skipped it above: fill in ${ENV_FILE} with real secrets."
echo "  2. Append the Caddy block from docs-site/pages/nash-srv-deploy.mdx to"
echo "     /etc/caddy/Caddyfile, then: sudo systemctl reload caddy"
echo "  3. Add DNS CNAMEs for timberdoodle.nash.engineering and"
echo "     docs.timberdoodle.nash.engineering (see the same doc)."
echo "  4. Add DEMO_ADMIN_EMAIL / DEMO_ADMIN_PASSWORD as GitHub Actions repo secrets."

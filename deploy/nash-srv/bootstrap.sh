#!/usr/bin/env bash
# One-shot bootstrap for the baler deploy on nash-srv. Run this
# ON nash-srv, as jacob (it sudos itself where needed - don't run the
# whole thing as root):
#
#   git clone git@github.com:jacobnash/baler.git   # skip if already cloned
#   cd baler
#   bash deploy/nash-srv/bootstrap.sh <RUNNER_REGISTRATION_TOKEN>
#
# Get <RUNNER_REGISTRATION_TOKEN> from:
#   https://github.com/jacobnash/baler/settings/actions/runners/new
# (valid ~1 hour, single use - grab it right before running this).
#
# What this does, in order:
#   1. Runs setup-runner.sh (creates the github-runner user if needed,
#      generates real prod secrets straight into that user's own home so
#      the deploy wrapper's --env-file path is guaranteed to match -
#      skipped if that file already exists, installs the root-owned deploy
#      wrapper + scoped sudoers rule, registers and starts the runner).
#   2. Appends baler's Caddy block to /etc/caddy/Caddyfile (skipped
#      if already present) and reloads Caddy.
#
# Does NOT touch DNS - add the two CNAMEs from
# docs-site/pages/nash-srv-deploy.mdx yourself first, in your DNS
# provider's UI (needs credentials this script doesn't have).

set -euo pipefail

TOKEN="${1:?Usage: bash bootstrap.sh <RUNNER_REGISTRATION_TOKEN>}"
CADDYFILE="/etc/caddy/Caddyfile"

echo "==> 1. Runner + prod secrets + deploy wrapper + sudoers (needs sudo)"
sudo bash deploy/nash-srv/setup-runner.sh "$TOKEN"

echo "==> 2. Caddy block"
if sudo grep -q "timberdoodle.nash.engineering" "$CADDYFILE" 2>/dev/null; then
  echo "    already present in ${CADDYFILE}, skipping"
else
  sudo tee -a "$CADDYFILE" > /dev/null <<'EOF'

# Baler
timberdoodle.nash.engineering {
    reverse_proxy 127.0.0.1:8080
}

docs.timberdoodle.nash.engineering {
    root * /var/www/timberdoodle-docs
    # dist/index.html is a client-side-only redirect (a bare
    # <script>window.location.href="/introduction"</script>, 200 OK,
    # zero real content) - Zudoku's `redirects` config always emits this
    # shape, there's no server-redirect mode. A crawler/fetch tool that
    # doesn't run JS gets nothing at the site root otherwise. A real
    # server-side redirect here takes priority and makes / work with zero
    # JS, same pattern as the main app's / -> /ui/ redirect in nginx.conf.
    redir / /introduction 301
    # Zudoku's static export writes page.html files, but every internal
    # link is extension-less (/introduction, /api/windrow, etc.) - a plain
    # file_server 404s on any of those unless something tries the .html
    # suffix first. Confirmed live: every page except the literal root
    # 404'd on a direct load/refresh/shared link before this was added.
    try_files {path} {path}.html {path}/index.html /index.html
    file_server
}
EOF
  sudo caddy validate --config "$CADDYFILE"
  sudo systemctl reload caddy
  echo "    appended and reloaded"
fi

echo "==> Done."
echo "Remaining manual step: DNS CNAMEs (see docs-site/pages/nash-srv-deploy.mdx)."
echo "Demo login is already set as GitHub secrets (DEMO_ADMIN_EMAIL/DEMO_ADMIN_PASSWORD)."
echo "Push to main once DNS has propagated and the first deploy will create that account."

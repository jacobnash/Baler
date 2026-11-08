#!/usr/bin/env bash
# Creates the public demo's admin account on first deploy, and does nothing
# once it exists. Run by .github/workflows/deploy.yml after "Deploy stack".
#
#   DEMO_ADMIN_EMAIL=... DEMO_ADMIN_PASSWORD=... deploy/nash-srv/seed-demo-account.sh
#
# A login answer sorts the account into one of three cases (Baler#173):
#   200 / 403    it exists: done.
#   401 from auth_api itself ({"error": "invalid email or password"})
#                the account is missing or has another password. Login
#                can't tell those apart (one answer for both, on purpose),
#                so POST /auth/orgs decides: 201 = it was missing and now
#                exists; 409 = the email is taken, so it exists.
#                auth.create_org refuses a taken admin email in one
#                transaction, so this can never make a second demo org.
#   anything else (5xx, 429, no answer = 000, timeout, a 401 without that
#                body) unknown: auth_api may be restarting. Retry with
#                backoff for SEED_RETRY_SECONDS, then fail. Never create.
set -euo pipefail

BASE="${BALER_URL:-http://127.0.0.1:8080}"
BUDGET="${SEED_RETRY_SECONDS:-120}"   # total time allowed for retries
DELAY="${SEED_RETRY_DELAY:-2}"        # first backoff; doubles, capped at 15 s
: "${DEMO_ADMIN_EMAIL:?set DEMO_ADMIN_EMAIL}" "${DEMO_ADMIN_PASSWORD:?set DEMO_ADMIN_PASSWORD}"

# JSON-escape via python so a quote or backslash in the secret can't break
# (or inject into) the request body.
json() { python3 -c 'import json,sys; print(json.dumps(dict(zip(sys.argv[1::2], sys.argv[2::2]))))' "$@"; }
LOGIN_BODY=$(json email "$DEMO_ADMIN_EMAIL" password "$DEMO_ADMIN_PASSWORD")
ORG_BODY=$(json name Demo admin_email "$DEMO_ADMIN_EMAIL" admin_password "$DEMO_ADMIN_PASSWORD")

RESP=$(mktemp)
trap 'rm -f "$RESP"' EXIT

# post PATH BODY -> sets CODE to the HTTP status (000 for no answer or a
# timeout) and leaves the body in $RESP. Sets globals rather than printing:
# a function run in $( ) would run the EXIT trap and delete $RESP.
post() {
  : > "$RESP"
  CODE=$(curl -s -o "$RESP" -w '%{http_code}' --max-time 10 -X POST "$BASE$1" \
    -H 'Content-Type: application/json' -d "$2" || true)
  CODE=${CODE:-000}
}

# A login 401 counts only when auth_api itself says the credentials are bad.
bad_credentials() { grep -q '"invalid email or password"' "$RESP"; }

# retry PATH BODY -> sets CODE to the first status that isn't "unknown", or
# the last unknown one once the budget is spent.
retry() {
  # Milliseconds, not whole seconds: with a whole-second clock a budget of N
  # really lasts anywhere from N-1 to N s, so a short budget could expire
  # after a single attempt (flaked test_seed_demo_account under load).
  local deadline=$(( $(date +%s%3N) + BUDGET * 1000 )) delay="$DELAY"
  while :; do
    post "$1" "$2"
    if [ "$CODE" = 401 ] && [ "$1" = /auth/login ] && ! bad_credentials; then
      CODE=401?  # not auth_api's answer: unknown
    fi
    case "$CODE" in
      5??|429|000|401\?) ;;
      *) return ;;
    esac
    if [ "$(date +%s%3N)" -ge "$deadline" ]; then return; fi
    echo "POST $1 -> $CODE (auth_api not answering yet), retrying in ${delay}s" >&2
    sleep "$delay"
    delay=$(python3 -c "print(min(float('$delay') * 2, 15))")
  done
}

retry /auth/login "$LOGIN_BODY"
case "$CODE" in
  200|403) echo "demo account exists"; exit 0 ;;
  401) ;;  # missing, or another password: POST /auth/orgs decides
  *) echo "demo account state unknown: login still $CODE after ${BUDGET}s - not creating anything" >&2; exit 1 ;;
esac

echo "login returned $CODE - creating the demo org unless its admin email is already taken"
retry /auth/orgs "$ORG_BODY"
case "$CODE" in
  201) echo "demo account created" ;;
  409) echo "demo admin email already exists (its password differs from DEMO_ADMIN_PASSWORD) - leaving it alone" ;;
  *) echo "creating the demo org failed: POST /auth/orgs returned $CODE" >&2; exit 1 ;;
esac

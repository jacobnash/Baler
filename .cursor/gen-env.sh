#!/usr/bin/env bash
# Idempotently ensure a local .env exists with the secrets the stack needs.
# Sourced by both install.sh and start.sh: .env is gitignored and, with
# environment builds, install does not run on every boot, so start must also
# be able to (re)create it. The three secrets below are just long random keys
# (not real credentials), so generating them locally is safe; existing values
# are preserved so secrets don't rotate on every boot.

# Ensure KEY has a non-empty value in ./.env:
#  - if KEY already has a non-empty value, leave it untouched;
#  - if a KEY= line exists but is empty (or only a comment), replace the line;
#  - if no KEY line exists at all (older .env.example), append it.
_td_set_env() {
  local key="$1" val="$2"
  if grep -qE "^${key}=.+" .env && ! grep -qE "^${key}=[[:space:]]*(#.*)?$" .env; then
    return 0
  fi
  if grep -qE "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${val}|" .env
  else
    printf '%s=%s\n' "$key" "$val" >> .env
  fi
}

ensure_baler_env() {
  local dir="$1"
  ( cd "$dir" || return 1
    if [ ! -f .env ]; then
      cp .env.example .env
    fi
    # The gateway refuses to start without these two HMAC secrets; mosquitto
    # (and every MQTT-connected daemon) refuses to start without MQTT_PASSWORD.
    _td_set_env BALER_JWT_SECRET "$(openssl rand -hex 32)"
    _td_set_env BALER_GATEWAY_SECRET "$(openssl rand -hex 32)"
    _td_set_env MQTT_PASSWORD "$(openssl rand -hex 16)"
    # Allow webhook registration/delivery to local receivers (dev/test only) -
    # see CLAUDE.md; the integration suite's webhook tests need this. Force to
    # 1 (the example line carries an inline comment, so match the whole line).
    if ! grep -qE '^BALER_ALLOW_PRIVATE_WEBHOOKS=1$' .env; then
      if grep -qE '^BALER_ALLOW_PRIVATE_WEBHOOKS=' .env; then
        sed -i 's|^BALER_ALLOW_PRIVATE_WEBHOOKS=.*|BALER_ALLOW_PRIVATE_WEBHOOKS=1|' .env
      else
        printf 'BALER_ALLOW_PRIVATE_WEBHOOKS=1\n' >> .env
      fi
    fi
  )
}

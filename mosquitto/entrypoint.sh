#!/bin/sh
# Regenerates /mosquitto/data/passwd from $MOSQUITTO_USERS on every start
# instead of committing hashed credentials to the repo. Format:
# "user1:pass1,user2:pass2,..." - see docker-compose.yml's mosquitto
# service for how it's built from .env.
set -e
# Every file this writes holds credentials: create them private, so the
# broker never sees a world-readable passwd even for a moment (#59).
umask 077

passwd_file=/mosquitto/data/passwd
# Built from scratch in a root-owned scratch dir, then installed for the
# broker (#79). mosquitto 2.1 warns when a file it opens isn't owned by the
# user opening it ("future versions will refuse"): mosquitto_passwd runs
# here as root, and regenerating the persisted, mosquitto-owned passwd in
# place made it warn on every restart after the first. The broker itself
# loads its password file after dropping to the mosquitto user (plugins load
# after drop_privileges in 2.1), so the installed copy must stay
# mosquitto-owned - a root-owned 0600 passwd would be unreadable to it.
work=$(mktemp -d)
: > "$work/passwd"

old_ifs=$IFS
IFS=,
for pair in $MOSQUITTO_USERS; do
  user=${pair%%:*}
  pass=${pair#*:}
  mosquitto_passwd -b "$work/passwd" "$user" "$pass"
done
IFS=$old_ifs
# The compose healthcheck's own account (Baler#109). Its password is random
# per start and lives only in a root-only file inside this container, so it
# never shows in `docker inspect` (env or healthcheck command). mosquitto/acl
# lets it read $SYS/broker/uptime and nothing else. Logging in with it, not
# being refused, is what proves the broker is up and checking passwords.
health_pw_file=/run/mosquitto-health/password
mkdir -p "${health_pw_file%/*}"
head -c 18 /dev/urandom | base64 | tr -d '/+=' > "$health_pw_file"
mosquitto_passwd -b "$work/passwd" healthcheck "$(cat "$health_pw_file")"
install -m 600 -o mosquitto -g mosquitto "$work/passwd" "$passwd_file"
rm -rf "$work"

# The ACL is bind-mounted from the checkout (0644, the host user's), and
# mosquitto 2.1 warns about world-readable security files (and says future
# versions will refuse them). The broker reads a private copy (#59).
cp /mosquitto/config/acl /mosquitto/data/acl
chmod 600 /mosquitto/data/acl
chown mosquitto:mosquitto /mosquitto/data/acl

# Edge-node accounts live in the dynamic-security plugin's file (Baler#36).
# Created empty on first start, then owned by the plugin: it persists every
# account change there, so it's never regenerated. No admin client - the
# backend account manages it (mosquitto/acl grants the control topic).
dynsec_file=/mosquitto/data/dynamic-security.json
if [ ! -s "$dynsec_file" ]; then
  echo '{"defaultACLAccess":{"publishClientSend":false,"publishClientReceive":false,"subscribe":false,"unsubscribe":true},"clients":[],"groups":[],"roles":[]}' > "$dynsec_file"
fi
chmod 600 "$dynsec_file"
chown mosquitto:mosquitto "$dynsec_file"

# TLS listener (8883, Baler#36) when a cert is mounted at /mosquitto/tls
# (BALER_MQTT_TLS_DIR in .env): cert.pem + key.pem, or a Caddy-style
# <host>.crt + <host>.key pair - so Caddy's own certificate directory can be
# mounted as-is. Copied (as root, which can read them whatever their owner)
# into the data volume for the mosquitto user: the broker loads its key
# only after dropping privileges, and an unreadable key stops it starting.
# A renewed cert takes effect on restart. No cert: plain 1883 only.
conf_dir=/mosquitto/data/conf.d
tls_dir=/mosquitto/data/tls
mkdir -p "$conf_dir" "$tls_dir"
cert=; key=
if [ -f /mosquitto/tls/cert.pem ] && [ -f /mosquitto/tls/key.pem ]; then
  cert=/mosquitto/tls/cert.pem; key=/mosquitto/tls/key.pem
else
  for c in /mosquitto/tls/*.crt; do
    [ -f "$c" ] && [ -f "${c%.crt}.key" ] && cert=$c && key=${c%.crt}.key && break
  done
fi
if [ -n "$cert" ]; then
  cp "$cert" "$tls_dir/cert.pem"
  cp "$key" "$tls_dir/key.pem"
  printf 'listener 8883\ncertfile %s/cert.pem\nkeyfile %s/key.pem\n' "$tls_dir" "$tls_dir" > "$conf_dir/tls.conf"
  echo "TLS listener on 8883 with $cert"
else
  rm -f "$tls_dir/cert.pem" "$tls_dir/key.pem"
  : > "$conf_dir/tls.conf"
fi
chown -R mosquitto:mosquitto "$conf_dir" "$tls_dir"

exec mosquitto -c /mosquitto/config/mosquitto.conf

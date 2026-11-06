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
: > "$passwd_file"

old_ifs=$IFS
IFS=,
for pair in $MOSQUITTO_USERS; do
  user=${pair%%:*}
  pass=${pair#*:}
  mosquitto_passwd -b "$passwd_file" "$user" "$pass"
done
IFS=$old_ifs
chmod 600 "$passwd_file"
chown mosquitto:mosquitto "$passwd_file"

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

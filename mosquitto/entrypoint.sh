#!/bin/sh
# Regenerates /mosquitto/data/passwd from $MOSQUITTO_USERS on every start
# instead of committing hashed credentials to the repo. Format:
# "user1:pass1,user2:pass2,..." - see docker-compose.yml's mosquitto
# service for how it's built from .env.
set -e

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

exec mosquitto -c /mosquitto/config/mosquitto.conf

Placeholder mount point for the broker's TLS certificate (Baler#36).

Don't put certificates here. Set `BALER_MQTT_TLS_DIR` in `.env` to a directory
outside the checkout holding `cert.pem` (full chain) and `key.pem`, readable
by uid/gid 1883 (mosquitto). The broker then also listens on 8883 with TLS.
On nash-srv, `deploy/nash-srv/setup-runner.sh` step 7 keeps
`/etc/baler/mqtt-tls` in sync with Caddy's Let's Encrypt cert.

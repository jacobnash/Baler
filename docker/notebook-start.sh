#!/bin/bash
# Refuse to start without a Jupyter token. Compose always passes
# JUPYTER_TOKEN (blank when BALER_JUPYTER_TOKEN is unset), and Jupyter
# treats an empty token as "no authentication at all", so a blank value
# must stop here instead of serving an open Python shell. (A `:?` in
# docker-compose.yml can't do this: it would break `docker compose up` for
# everyone who never uses the notebook profile.)
set -euo pipefail
if [ -z "${JUPYTER_TOKEN:-}" ]; then
    echo "jupyter: BALER_JUPYTER_TOKEN is not set - add one to .env (e.g. BALER_JUPYTER_TOKEN=\$(openssl rand -hex 24)) and run docker compose --profile notebook up -d again" >&2
    exit 1
fi
exec start-notebook.py "$@"

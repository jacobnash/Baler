"""
Where scripts/backup.py and scripts/restore_drill.py report their results
(#276, #196): a row each in the live database's service_heartbeats
("backup", "restore_drill"), which /ops/status reads (baler.ops_status), and
the same JSON in a file beside the backups.

Both scripts run on the Docker host, not on the compose network, so they
write through `docker compose exec postgres psql` exactly like backup.py's
pg_dump: whatever COMPOSE_PROJECT_NAME/COMPOSE_FILE/COMPOSE_ENV_FILES pick
for the dump picks the database for the report. Nothing here needs baler's
dependencies (stdlib only), so backup.py can use it from a bare checkout.

Reporting is best effort: a report that can't be written is a warning,
never a reason to fail (or lose) a backup.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from typing import Any

# -X: no ~/.psqlrc. Variables come in as argv ("$1"/"$2") and are quoted by
# psql itself (:'name'), so nothing in the detail can break out of the SQL.
_PSQL = 'psql -X -q -v ON_ERROR_STOP=1 -At -U "$POSTGRES_USER" "$POSTGRES_DB" -v service="$1" -v detail="$2"'
_UPSERT = """INSERT INTO service_heartbeats (service, beat_at, detail) VALUES (:'service', now(), :'detail'::jsonb)
ON CONFLICT (service) DO UPDATE SET beat_at = EXCLUDED.beat_at, detail = EXCLUDED.detail;
"""
_SELECT = "SELECT detail FROM service_heartbeats WHERE service = :'service';\n"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _psql(sql: str, service: str, detail: str, compose_project_dir: str) -> str:
    out = subprocess.run(["docker", "compose", "exec", "-T", "postgres", "sh", "-c", _PSQL, "sh", service, detail],
                         cwd=compose_project_dir, input=sql, capture_output=True, text=True, timeout=60, check=False)
    if out.returncode != 0:
        lines = (out.stderr or out.stdout).strip().splitlines()
        raise RuntimeError(lines[-1] if lines else f"exit {out.returncode}")
    return out.stdout.strip()


def previous(service: str, compose_project_dir: str) -> dict[str, Any] | None:
    """The last report, to carry "last good" times forward; None if none
    (or the database can't be reached)."""
    try:
        text = _psql(_SELECT, service, "", compose_project_dir)
        return json.loads(text) if text else None
    except Exception:  # noqa: BLE001 - best effort
        return None


def record(service: str, detail: dict[str, Any], compose_project_dir: str) -> bool:
    try:
        _psql(_UPSERT, service, json.dumps(detail, default=str), compose_project_dir)
        return True
    except Exception as exc:  # noqa: BLE001 - reporting must not fail the job
        print(f"warning: couldn't report {service} status to the database (/ops/status won't show it): {exc}",
              flush=True)
        return False


def write_file(path: str, detail: dict[str, Any]) -> None:
    try:
        tmp = path + ".partial"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(detail, f, indent=2, default=str)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"warning: couldn't write {path}: {exc}", flush=True)


def _alert_env(event: str) -> dict[str, str]:
    """The alert command's environment: ours minus the bucket credentials."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("BALER_BACKUP_S3_ACCESS_KEY_ID", "BALER_BACKUP_S3_SECRET_ACCESS_KEY",
                        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")}
    env["BALER_ALERT_EVENT"] = event
    return env


def alert(event: str, detail: dict[str, Any]) -> None:
    """The alerting hook. Runs BALER_BACKUP_ALERT_COMMAND (split like a
    shell would, but run without one) with BALER_ALERT_EVENT set and the
    report as JSON on stdin, e.g. a script that mails or pages. When the
    alerting issue (#277) lands, route this through it; until then this
    keeps a failure from being visible only in /ops/status."""
    cmd = os.environ.get("BALER_BACKUP_ALERT_COMMAND", "").strip()
    if not cmd:
        return
    try:
        subprocess.run(shlex.split(cmd), input=json.dumps({"event": event, **detail}, default=str), text=True,
                       env=_alert_env(event), timeout=60, check=True,
                       capture_output=True)
    except Exception as exc:  # noqa: BLE001 - an alert that fails is a warning
        print(f"warning: BALER_BACKUP_ALERT_COMMAND failed for {event}: {exc}", flush=True)

"""
Liveness for the daemons with no HTTP surface (fault_detector,
derivation_engine): each writes a heartbeat row every loop, and Ops reports
the daemon down when its row goes stale, instead of "heartbeat not wired
yet". One small upsert per loop.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from baler import schema

# Schema (service_heartbeats): baler/migrations/, from 0001 (#194). Change it
# with a new migration there, not here.


def ensure_schema(conn) -> None:
    schema.ensure(conn)


def beat(conn, service: str, detail: dict | None = None) -> None:
    """Never raises: a heartbeat must not take its daemon down."""
    try:
        conn.execute(
            "INSERT INTO service_heartbeats (service, beat_at, detail) VALUES (%s, %s, %s) "
            "ON CONFLICT (service) DO UPDATE SET beat_at = EXCLUDED.beat_at, detail = EXCLUDED.detail",
            (service, datetime.now(timezone.utc), json.dumps(detail, default=str) if detail is not None else None),
        )
    except Exception as exc:  # noqa: BLE001 - liveness reporting is best-effort
        print(f"heartbeat {service}: {exc!r}")


def latest(conn) -> dict[str, datetime]:
    try:
        return {s: t for s, t in conn.execute("SELECT service, beat_at FROM service_heartbeats").fetchall()}
    except Exception:  # noqa: BLE001 - table not created yet
        return {}

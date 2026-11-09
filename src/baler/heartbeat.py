"""
Liveness for the daemons with no HTTP surface (fault_detector,
derivation_engine): each writes a heartbeat row every loop, and Ops reports
the daemon down when its row goes stale, instead of "heartbeat not wired
yet". One small upsert per loop.
"""

from __future__ import annotations

import json
import threading
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


class Status:
    """A daemon's heartbeat detail, written from more than one thread
    (fault_detector's and derivation_engine's reading-feed thread and their
    main loop, #243): each part updates its own key, and either thread's
    beat carries the whole picture, so a stalled main loop still shows its
    last cycle and a stalled feed thread its lag."""

    def __init__(self, service: str, **detail):
        self.service = service
        self._lock = threading.Lock()
        self.detail: dict = dict(detail)

    def update(self, key: str, value) -> None:
        with self._lock:
            self.detail[key] = value

    def add(self, key: str, n: float) -> None:
        with self._lock:
            self.detail[key] = self.detail.get(key, 0) + n

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self.detail, default=str))

    def beat(self, conn) -> None:
        beat(conn, self.service, self.snapshot())


def details(conn) -> dict[str, dict]:
    """Each service's last heartbeat detail ({} if none or no table)."""
    try:
        return {s: d for s, d in conn.execute("SELECT service, detail FROM service_heartbeats").fetchall() if d}
    except Exception:  # noqa: BLE001 - table not created yet
        return {}

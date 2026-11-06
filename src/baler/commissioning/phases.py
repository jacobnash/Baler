"""
Commissioning phases: first-class, per project, editable (Jacob,
2026-11-06: keep phase explicit "but make them manageable").

A project's phases are an ordered list it owns - not a hard-coded enum -
seeded from DEFAULT_PHASES and editable (rename, reorder, add, remove,
skip). Exactly one is current. Phase matters because absence means
opposite things in each (#5's insight): a missing point during
construction is progress data, during operations it's an outage.

Pure functions over lists of dicts; persistence is db.py's job.
"""

from __future__ import annotations

import re
from datetime import datetime

DEFAULT_PHASES: list[tuple[str, str]] = [
    ("design", "Design"),
    ("construction", "Construction / installation"),
    ("startup", "Start-up / power-on"),
    ("functional", "Functional testing"),
    ("acceptance", "Acceptance"),
    ("warranty", "Warranty"),
    ("operations", "Operations"),
]

STATUSES = ("pending", "active", "complete", "skipped")
_KEY = re.compile(r"[a-z][a-z0-9_-]{0,39}")


def default_phases() -> list[dict]:
    return [_phase(k, n, i) for i, (k, n) in enumerate(DEFAULT_PHASES)]


def _phase(key: str, name: str, position: int, status: str = "pending", started_at=None, completed_at=None) -> dict:
    return {"key": key, "name": name, "position": position, "status": status,
            "started_at": started_at, "completed_at": completed_at}


def normalize(items, existing: list[dict] | None = None) -> list[dict]:
    """Validate a client-supplied phase list (order = list order) into
    stored rows. Timestamps of phases that already existed are kept, so a
    rename or reorder doesn't rewrite history. Raises ValueError."""
    if not isinstance(items, list) or not items:
        raise ValueError("phases must be a non-empty list")
    if len(items) > 30:
        raise ValueError("at most 30 phases")
    prior = {p["key"]: p for p in existing or []}
    out: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise TypeError("each phase must be an object with key and name")
        key = item.get("key")
        name = item.get("name")
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise ValueError(f"phase key {key!r} must match [a-z][a-z0-9_-]{{0,39}}")
        if key in seen:
            raise ValueError(f"duplicate phase key {key!r}")
        seen.add(key)
        if not isinstance(name, str) or not name.strip():
            name = (prior.get(key) or {}).get("name") or key
        status = item.get("status", (prior.get(key) or {}).get("status", "pending"))
        if status not in STATUSES:
            raise ValueError(f"phase status must be one of {', '.join(STATUSES)}")
        old = prior.get(key) or {}
        out.append(_phase(key, name.strip()[:120], i, status, old.get("started_at"), old.get("completed_at")))
    if sum(1 for p in out if p["status"] == "active") > 1:
        raise ValueError("at most one phase can be active - use POST .../phase to change the current one")
    return out


def set_current(phases: list[dict], key: str, now: datetime) -> list[dict]:
    """Make `key` the active phase. Moving forward completes the phase
    being left; moving back returns it to pending (re-opening an earlier
    phase is a real thing - a failed acceptance sends work back)."""
    by_key = {p["key"]: p for p in phases}
    if key not in by_key:
        raise ValueError(f"no phase {key!r} in this project")
    target = by_key[key]
    out = []
    for p in phases:
        p = dict(p)
        if p["key"] == key:
            p["status"] = "active"
            p["started_at"] = p.get("started_at") or now
            p["completed_at"] = None
        elif p["status"] == "active":
            if p["position"] < target["position"]:
                p["status"], p["completed_at"] = "complete", now
            else:
                p["status"] = "pending"
        out.append(p)
    return out


def current(phases: list[dict]) -> str | None:
    return next((p["key"] for p in phases if p["status"] == "active"), None)

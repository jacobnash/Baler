"""
Controller priority arrays, as Windrow reports them (Windrow#25, PR #34),
kept as a change log for the passive override tests (Baler#212).

A commandable point's reading carries an optional envelope key:

    {"point": "zone-temp-sp", "value": 75.0, "ts": 1762700000.0,
     "priority": {"active": 8, "levels": {"8": 75.0, "16": 72.0}}}

`levels` holds only the levels someone holds (level number as text);
`active` is the lowest held level, null when relinquish-default is in
control. Windrow re-reads every 60 s and publishes at once on an override
or its release. Windrow doesn't know who wrote a level; Baler knows its
own (point_write_levels, the active-run log), so the override detector
treats any other held level as someone else's.

point_priority_arrays gets a row only when a point's levels change, so it
answers "level 8 was held from T to T'" for the past. The listener keeps
the last levels it saw per point in memory, so the steady 60 s re-reads
cost nothing; record() also compares with the newest stored row, so a
restart (empty memory) doesn't duplicate one.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone


def levels_of(priority) -> dict | None:
    """The envelope's `priority` as stored levels {"8": {"value": 75.0}},
    or None when it isn't one (no key, or malformed: ignored, never fatal)."""
    if not isinstance(priority, dict) or not isinstance(priority.get("levels"), dict):
        return None
    out = {}
    for k, v in priority["levels"].items():
        try:
            level = int(k)
        except (TypeError, ValueError):
            continue
        if 1 <= level <= 16 and isinstance(v, (int, float, str, bool)):
            out[str(level)] = {"value": v}
    return out


def _when(ts) -> datetime:
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    return datetime.now(timezone.utc)


class ChangeLog:
    """Per process: the last levels seen per point, and the changes not
    yet written. observe() runs on the MQTT thread, take() on the writer."""

    def __init__(self, limit: int = 100_000):
        self._last: dict[str, str] = {}
        self._pending: list[tuple[str, datetime, dict]] = []
        self._lock = threading.Lock()
        self.limit = limit

    def observe(self, point_uri: str, priority, ts) -> bool:
        levels = levels_of(priority)
        if levels is None:
            return False
        key = json.dumps(levels, sort_keys=True)
        with self._lock:
            if self._last.get(point_uri) == key:
                return False
            if len(self._last) >= self.limit and point_uri not in self._last:
                self._last.clear()  # bounded: forgetting only costs a duplicate-checked write
            self._last[point_uri] = key
            self._pending.append((point_uri, _when(ts), levels))
            return True

    def take(self) -> list[tuple[str, datetime, dict]]:
        with self._lock:
            out, self._pending = self._pending, []
            return out


def record(conn, rows: list[tuple[str, datetime, dict]]) -> int:
    """Append each (point, observed_at, levels) whose levels differ from
    that point's newest stored row (and from the row before it in `rows`).
    Returns how many were added."""
    if not rows:
        return 0
    points = sorted({p for p, _, _ in rows})
    last = {u: lv for u, lv in conn.execute(
        "SELECT DISTINCT ON (point_uri) point_uri, levels FROM point_priority_arrays WHERE point_uri = ANY(%s)"
        " ORDER BY point_uri, observed_at DESC", (points,)).fetchall()}
    added = 0
    for point, at, levels in sorted(rows, key=lambda r: r[1]):
        if last.get(point) == levels:
            continue
        conn.execute("INSERT INTO point_priority_arrays (point_uri, observed_at, levels) VALUES (%s, %s, %s)"
                     " ON CONFLICT DO NOTHING", (point, at, json.dumps(levels)))
        last[point] = levels
        added += 1
    return added

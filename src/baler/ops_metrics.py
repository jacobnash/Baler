"""
GET /ops/metrics (#235): the overload signals /ops/status judges, as
Prometheus text exposition (format 0.0.4), for a scraper, the scale ladder
and the post-deploy verify job (#178) - one source for all three.

What it reads, all already written by the daemons themselves:

- ingest lag: now minus the newest stored reading's ts (point_history),
  and per source prefix (the topic's first two levels, e.g. fbf/home) from
  point_registry, whose last_ts is coarse by REGISTRY_GRANULARITY_S;
- mqtt_listener's heartbeats: the supervisor's (broker drops, restarts and
  the workers folded together) and each worker's (queue depth, the age of
  the oldest uncommitted reading, last batch write time, redeliveries);
- the reading feed: each rule daemon's lag and missed readings;
- fault_detector's and derivation_engine's heartbeats: cycle times and
  evaluations.

Process-wide numbers (the listener, the broker, the rule daemons) aren't
per org; the gateway keeps site-restricted logins out (policy.js,
resource "metrics"). Per-prefix lines are filtered by the caller's scope.
"""

from __future__ import annotations

import math
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from baler import heartbeat, reading_feed

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# Per-prefix ingest lag is one GROUP BY over point_registry (a row per
# point): cached this long, about the registry's own granularity, so a
# scraper every 15 s doesn't scan 1M rows each time.
PREFIX_CACHE_SECONDS = float(os.environ.get("OPS_METRICS_PREFIX_CACHE_SECONDS", "30"))
# At most this many prefixes, the stalest first: a series per prefix is
# what a scraper stores, and the stale ones are the ones to see.
MAX_PREFIXES = int(os.environ.get("OPS_METRICS_MAX_PREFIXES", "500"))

PREFIX_SQL = """
SELECT split_part(substr(point_uri, 11), '/', 1) || '/' || split_part(substr(point_uri, 11), '/', 2) AS prefix,
       max(last_ts), min(point_uri), count(*)
FROM point_registry WHERE point_uri LIKE 'urn:point:%'
GROUP BY 1
"""


class _PrefixCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: list[tuple[str, datetime, str, int]] | None = None
        self._at = 0.0

    def get(self, conn, ttl: float = PREFIX_CACHE_SECONDS) -> list[tuple[str, datetime, str, int]]:
        with self._lock:
            if self._rows is not None and time.monotonic() - self._at < ttl:
                return self._rows
        try:
            rows = [(p, t, s, int(n)) for p, t, s, n in conn.execute(PREFIX_SQL).fetchall()]
        except Exception:  # noqa: BLE001 - no registry yet (an empty install)
            conn.rollback()
            rows = []
        with self._lock:
            self._rows, self._at = rows, time.monotonic()
        return rows

    def clear(self) -> None:
        with self._lock:
            self._rows = None


prefix_cache = _PrefixCache()


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def collect(conn, scope=None, now: datetime | None = None) -> dict[str, Any]:
    """Everything render() needs, read in a few small queries."""
    now = now or datetime.now(timezone.utc)
    snap: dict[str, Any] = {"now": now, "newest_reading_at": None, "prefixes": [], "heartbeats": {},
                            "details": {}, "feed": {}}
    try:
        row = conn.execute("SELECT MAX(ts) FROM point_history").fetchone()
        if row and row[0] is not None:
            snap["newest_reading_at"] = _utc(row[0])
    except Exception:  # noqa: BLE001 - no history table yet
        conn.rollback()
    allows = (lambda uri: True) if scope is None or scope.unrestricted else scope.allows
    snap["prefixes"] = [(p, _utc(t), n) for p, t, sample, n in prefix_cache.get(conn) if allows(sample)]
    snap["heartbeats"] = heartbeat.latest(conn)
    conn.rollback()
    snap["details"] = heartbeat.details(conn)
    conn.rollback()
    snap["feed"] = reading_feed.consumer_status(conn)
    conn.rollback()
    return snap


# ----------------------------------------------------------------- rendering


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _num(v) -> str | None:
    if isinstance(v, bool):
        return "1" if v else "0"
    if not isinstance(v, (int, float)) or (isinstance(v, float) and math.isnan(v)):
        return None
    return repr(float(v)) if isinstance(v, float) else str(v)


class _Out:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._declared: set[str] = set()

    def add(self, name: str, kind: str, help_: str, value, labels: dict[str, str] | None = None) -> None:
        v = _num(value)
        if v is None:
            return
        if name not in self._declared:
            self._declared.add(name)
            self.lines.append(f"# HELP {name} {help_}")
            self.lines.append(f"# TYPE {name} {kind}")
        lab = ""
        if labels:
            lab = "{" + ",".join(f'{k}="{_esc(val)}"' for k, val in labels.items()) + "}"
        self.lines.append(f"{name}{lab} {v}")


def render(snap: dict[str, Any]) -> str:
    """Prometheus text exposition of a collect() snapshot. Pure."""
    now: datetime = snap["now"]
    out = _Out()
    newest = snap.get("newest_reading_at")
    if newest is not None:
        out.add("baler_ingest_newest_reading_age_seconds", "gauge",
                "Now minus the newest stored reading's ts: end-to-end ingest lag while readings flow.",
                round(max(0.0, (now - newest).total_seconds()), 3))
    prefixes = sorted(snap.get("prefixes") or [], key=lambda r: r[1])[:MAX_PREFIXES]
    for prefix, last, points in prefixes:
        out.add("baler_ingest_prefix_newest_reading_age_seconds", "gauge",
                "Now minus the newest reading per source prefix (point_registry: coarse by 30 s).",
                round(max(0.0, (now - last).total_seconds()), 1), {"prefix": prefix})
        out.add("baler_ingest_prefix_points", "gauge", "Points with history per source prefix.", points,
                {"prefix": prefix})

    beats: dict[str, datetime] = snap.get("heartbeats") or {}
    for service, at in sorted(beats.items()):
        out.add("baler_heartbeat_age_seconds", "gauge", "Seconds since a daemon's last heartbeat.",
                round(max(0.0, (now - _utc(at)).total_seconds()), 1), {"service": service})

    details: dict[str, dict] = snap.get("details") or {}
    lst = details.get("mqtt_listener") or {}
    g = "gauge"
    c = "counter"
    out.add("baler_listener_workers", g, "Listener worker processes configured.", lst.get("workers"))
    out.add("baler_listener_workers_reporting", g, "Listener workers with a heartbeat in the last 30 s.",
            lst.get("writers"))
    out.add("baler_listener_restarts_total", c, "Listener child processes restarted by the supervisor.",
            sum((lst.get("restarts") or {}).values()) if "restarts" in lst else None)
    out.add("baler_listener_queue_depth", g, "Readings queued in the busiest listener worker.", lst.get("queue"))
    out.add("baler_listener_queue_peak", g, "Deepest worker queue since the last heartbeat.", lst.get("queue_peak"))
    out.add("baler_listener_queue_capacity", g, "Per-worker queue cap; full means paho stops reading.",
            lst.get("queue_max"))
    out.add("baler_listener_queue_oldest_age_seconds", g,
            "Age of the oldest reading received but not yet committed, in any worker.", lst.get("oldest_age_s"))
    out.add("baler_listener_last_flush_seconds", g, "The slowest worker's last batch write.", lst.get("last_flush_s"))
    out.add("baler_listener_flush_seconds_total", c, "Seconds the workers spent writing batches.", lst.get("flush_s"))
    out.add("baler_listener_blocked_seconds_total", c, "Seconds workers' MQTT threads waited on a full queue.",
            lst.get("blocked_s"))
    out.add("baler_listener_messages_total", c, "MQTT messages received by the workers.", lst.get("received"))
    out.add("baler_listener_readings_total", c, "Readings committed by the workers.", lst.get("rows"))
    out.add("baler_listener_batches_total", c, "Batches committed by the workers.", lst.get("batches"))
    out.add("baler_listener_redelivered_total", c, "Messages the broker redelivered (DUP) to the workers.",
            lst.get("redelivered"))
    out.add("baler_listener_readings_per_second", g, "Readings committed per second (workers' last heartbeats).",
            lst.get("readings_per_s"))
    out.add("baler_listener_messages_per_second", g, "MQTT messages received per second.", lst.get("messages_per_s"))
    out.add("baler_listener_batches_per_second", g, "Batches committed per second.", lst.get("batches_per_s"))
    out.add("baler_broker_dropped_messages_total", c,
            "The broker's own dropped-message counter ($SYS/broker/publish/messages/dropped), broker-wide.",
            lst.get("broker_dropped_total"))
    out.add("baler_broker_dropped_since_listener_start", g,
            "Messages the broker dropped since the listener supervisor started.", lst.get("broker_dropped_since_start"))
    last_drop = lst.get("broker_last_drop_at")
    if last_drop:
        out.add("baler_broker_last_drop_age_seconds", g, "Seconds since the broker last dropped a message.",
                round(max(0.0, (now - datetime.fromisoformat(last_drop)).total_seconds()), 1))

    for consumer, f in sorted((snap.get("feed") or {}).items()):
        lab = {"consumer": consumer}
        out.add("baler_reading_feed_lag_seconds", g, "Age of the oldest feed batch a consumer hasn't read.",
                f.get("lag_s"), lab)
        out.add("baler_reading_feed_lag_readings", g, "Readings in feed batches a consumer hasn't read.",
                f.get("lag_readings"), lab)
        out.add("baler_reading_feed_missed_readings_total", c,
                "Readings pruned before a consumer read them (it fell past the feed's retention).",
                f.get("missed_readings"), lab)

    fd = details.get("fault_detector") or {}
    his = fd.get("his") or {}
    out.add("baler_rules_cycle_seconds", g, "A rule daemon's last cycle.", his.get("sweep_s"),
            {"service": "fault_detector", "cycle": "his"})
    out.add("baler_rules_cycle_seconds", g, "A rule daemon's last cycle.", (fd.get("quality") or {}).get("sweep_s"),
            {"service": "fault_detector", "cycle": "quality"})
    de = details.get("derivation_engine") or {}
    out.add("baler_rules_cycle_seconds", g, "A rule daemon's last cycle.", (de.get("tick") or {}).get("cycle_s"),
            {"service": "derivation_engine", "cycle": "scheduled"})
    out.add("baler_fault_his_rules_per_cycle", g, "His-mode rules evaluated in fault_detector's last sweep.",
            his.get("rules"))
    out.add("baler_fault_his_points_faulted_per_cycle", g, "Points faulted by the last His sweep.",
            his.get("points_faulted"))
    out.add("baler_fault_cur_evaluations_total", c, "Cur-mode rule evaluations from the reading feed.",
            fd.get("cur_evaluations"))
    out.add("baler_derivation_cur_rows_total", c, "Cur-mode derived rows written from the reading feed.",
            de.get("cur_rows"))
    out.add("baler_derivation_due_per_cycle", g, "Scheduled derivations evaluated in the last tick.",
            (de.get("tick") or {}).get("derivations_due"))
    return "\n".join(out.lines) + "\n"


def parse(text: str) -> dict[str, float]:
    """Prometheus text -> {'name{labels}': value}, for the scale ladder and
    tests (the subset render() writes: no timestamps, no exemplars)."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, val = line.rpartition(" ")
        try:
            out[key] = float(val)
        except ValueError:
            continue
    return out

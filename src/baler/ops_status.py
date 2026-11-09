"""
Assemble the Slice A Ops status document.

Probes sibling services over the compose network and reads building
signals from Postgres. Pure daemons (mqtt_listener, fault_detector,
derivation_engine) have no HTTP surface yet — Slice A infers them from
data freshness / health tables, and labels that honestly.
"""

from __future__ import annotations

import os
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import psycopg

from baler import heartbeat, reading_feed, timeseries

COMPONENTS: list[dict[str, Any]] = [
    {
        "id": "historian",
        "label": "Historian",
        "layer": "platform",
        "service": "postgres",
        "probe": "postgres",
    },
    {
        "id": "model_store",
        "label": "Model store",
        "layer": "platform",
        "service": "oxigraph",
        "probe": "http",
        "url": os.environ.get("OXIGRAPH_URL", "http://oxigraph:7878") + "/",
    },
    {
        "id": "live_bus",
        "label": "Live bus",
        "layer": "platform",
        "service": "mosquitto",
        "probe": "tcp",
        "host": os.environ.get("MQTT_HOST", "mosquitto"),
        "port": int(os.environ.get("MQTT_PORT", "1883")),
    },
    {
        "id": "sign_in",
        "label": "Sign-in",
        "layer": "platform",
        "service": "auth_api",
        "probe": "http",
        "url": os.environ.get("OPS_AUTH_URL", "http://auth_api:8006/openapi.yaml"),
    },
    {
        "id": "data_api",
        "label": "Data API",
        "layer": "platform",
        "service": "ingest_api",
        "probe": "http",
        "url": os.environ.get("OPS_INGEST_URL", "http://ingest_api:8000/openapi.yaml"),
    },
    {
        "id": "alarm_config",
        "label": "Alarm config",
        "layer": "platform",
        "service": "fault_api",
        "probe": "http",
        "url": os.environ.get("OPS_FAULT_URL", "http://fault_api:8002/openapi.yaml"),
    },
    {
        "id": "analysis_config",
        "label": "Analysis config",
        "layer": "platform",
        "service": "derivation_api",
        "probe": "http",
        "url": os.environ.get("OPS_DERIVATION_URL", "http://derivation_api:8003/openapi.yaml"),
    },
    {
        "id": "model_checker",
        "label": "Model checker",
        "layer": "platform",
        "service": "validate_api",
        "probe": "http",
        "url": os.environ.get("OPS_VALIDATE_URL", "http://validate_api:8005/openapi.yaml"),
    },
    {
        "id": "ingest_listener",
        "label": "Ingest listener",
        "layer": "jobs",
        "service": "mqtt_listener",
        "probe": "inferred_ingest",
    },
    {
        "id": "fault_detector",
        "label": "Fault detector",
        "layer": "jobs",
        "service": "fault_detector",
        "probe": "inferred_faults",
    },
    {
        "id": "derivation_engine",
        "label": "Analysis engine",
        "layer": "jobs",
        "service": "derivation_engine",
        "probe": "inferred_derivations",
    },
    {
        "id": "charts",
        "label": "Charts",
        "layer": "tools",
        "service": "grafana",
        "probe": "http",
        "url": os.environ.get("OPS_GRAFANA_URL", "http://grafana:3000/api/health"),
    },
    {
        "id": "traces",
        "label": "Traces",
        "layer": "tools",
        "service": "jaeger",
        "probe": "http",
        "url": os.environ.get("OPS_JAEGER_URL", "http://jaeger:16686/"),
    },
]

QUIET_INGEST_SECONDS = int(os.environ.get("OPS_QUIET_INGEST_SECONDS", "900"))
# How long a probe may take before it counts as down. Each probe's own
# timeouts don't cover everything: name resolution has none, and right after
# a deploy a container being recreated can leave a lookup hanging for tens
# of seconds (/ops/status took 8.8 s, and 28 s locally, Baler#106).
PROBE_DEADLINE_SECONDS = 2.5
# ops_api probes in the background this often and answers /status from the
# latest round, so no request waits on a probe (Baler#106).
PROBE_INTERVAL_SECONDS = 10.0


def _http_ok(url: str, timeout: float = 2.0) -> tuple[str, str | None]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if 200 <= resp.status < 400:
                return "ok", None
            return "down", f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return "ok", f"HTTP {exc.code} (reachable)"
        return "down", f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - probe boundary
        return "down", str(exc)


def _tcp_ok(host: str, port: int, timeout: float = 2.0) -> tuple[str, str | None]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "ok", None
    except OSError as exc:
        return "down", str(exc)


def _postgres_ok() -> tuple[str, str | None]:
    try:
        with psycopg.connect(timeseries.DEFAULT_DSN, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return "ok", None
    except Exception as exc:  # noqa: BLE001
        return "down", str(exc)


def _count_visible(conn, sql: str, scope) -> int:
    """COUNT of rows whose first column (a point/equip URI) the caller may
    see. Without site scoping that's a plain COUNT; with it, the URIs are
    filtered like every other read, so Ops doesn't show (or leak) other
    orgs' fault and point counts (Alarms already only shows your own)."""
    if scope is None or scope.unrestricted:
        row = conn.execute(f"SELECT COUNT(*) FROM ({sql}) q").fetchone()
        return int(row[0]) if row else 0
    return sum(1 for (uri,) in conn.execute(sql).fetchall() if uri and scope.allows(uri))


# The points with a sample in the last hour, from point_registry (#243)
# rather than a scan of that hour of point_history: at 100k points that
# hour was 36M rows and the scan 3.6 s of a 3.4 s endpoint (Baler#244).
# A registry row's last_ts is at most REGISTRY_GRANULARITY_S older than the
# point's newest sample, so last_ts inside the hour means seen, last_ts more
# than that older than the hour means not; only the points in between (ones
# that went quiet about an hour ago) are confirmed against point_history,
# one primary-key probe each. Exact, and the cost follows the number of
# points, never the number of history rows.
SEEN_LAST_HOUR_SQL = f"""
SELECT point_uri FROM point_registry WHERE last_ts > now() - interval '1 hour'
UNION ALL
SELECT r.point_uri FROM point_registry r
WHERE r.last_ts <= now() - interval '1 hour'
  AND r.last_ts > now() - interval '1 hour' - interval '{timeseries.REGISTRY_GRANULARITY_S} seconds'
  AND EXISTS (SELECT 1 FROM point_history h WHERE h.point_uri = r.point_uri AND h.ts > now() - interval '1 hour')
"""


def _building_signals(scope=None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "last_ingest_at": None,
        "points_seen_last_hour": 0,
        "open_faults": 0,
        "disabled_derivation_targets": 0,
        "org_count": 0,
    }
    try:
        with psycopg.connect(timeseries.DEFAULT_DSN, connect_timeout=2) as conn:
            row = conn.execute("SELECT MAX(ts) FROM point_history").fetchone()
            if row and row[0] is not None:
                ts = row[0]
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                out["last_ingest_at"] = ts.astimezone(timezone.utc).isoformat()
            out["points_seen_last_hour"] = _count_visible(conn, SEEN_LAST_HOUR_SQL, scope)
            try:
                out["open_faults"] = _count_visible(
                    conn, "SELECT point_uri FROM faults WHERE status = 'open'", scope)
            except psycopg.Error:
                conn.rollback()
            try:
                out["disabled_derivation_targets"] = _count_visible(
                    conn, "SELECT target_uri FROM derivation_target_health WHERE disabled = TRUE", scope)
            except psycopg.Error:
                conn.rollback()
            out["heartbeats"] = {s: t.astimezone(timezone.utc).isoformat()
                                 for s, t in heartbeat.latest(conn).items()}
            conn.rollback()  # latest() may have hit a missing table
            # The rule daemons' place in the reading feed (#243): lag, misses.
            out["reading_feed"] = reading_feed.consumer_status(conn)
            conn.rollback()
            # The listener supervisor's broker-drop count and workers (#233).
            out["listener"] = heartbeat.details(conn).get("mqtt_listener")
            conn.rollback()
            try:
                org_row = conn.execute("SELECT COUNT(*) FROM orgs").fetchone()
                out["org_count"] = int(org_row[0]) if org_row else 0
            except psycopg.Error:
                pass
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def _infer_ingest(building: dict[str, Any], *, platform_ok: bool) -> tuple[str, str | None]:
    if not platform_ok:
        return "down", "historian or live bus unreachable"
    last = building.get("last_ingest_at")
    if not last:
        return "quiet", "no points received yet"
    last_dt = datetime.fromisoformat(last)
    age = (datetime.now(timezone.utc) - last_dt).total_seconds()
    if age > QUIET_INGEST_SECONDS:
        return "quiet", f"last ingest {int(age)}s ago"
    stale = _listener_heartbeat_state(building)
    if stale:
        return stale
    behind = _queue_state(building)
    if behind:
        return behind
    dropped = _drop_state(building)
    if dropped:
        return dropped
    lst = building.get("listener") or {}
    queue = f", queue {lst['queue']}" if lst.get("queue") is not None else ""
    return "ok", f"last ingest {int(age)}s ago{queue}"


# The listener's own backlog (#235), from its heartbeat: how long the oldest
# reading received but not yet committed has waited, and how full the
# busiest worker's queue is. Past the degraded age, readings are later than
# the ladder's p95 lag budget (30 s); past the down age, ingest is falling
# behind for good. A full queue means paho has stopped reading the socket
# and the backlog is growing at the broker, where its limit drops it.
QUEUE_AGE_DEGRADED_SECONDS = float(os.environ.get("OPS_QUEUE_AGE_DEGRADED_SECONDS", "30"))
QUEUE_AGE_DOWN_SECONDS = float(os.environ.get("OPS_QUEUE_AGE_DOWN_SECONDS", "300"))
QUEUE_FILL_DEGRADED = 0.8


def _queue_state(building: dict[str, Any]) -> tuple[str, str] | None:
    lst = building.get("listener") or {}
    age = float(lst.get("oldest_age_s") or 0)
    queue, cap = int(lst.get("queue") or 0), int(lst.get("queue_max") or 0)
    if age > QUEUE_AGE_DOWN_SECONDS:
        return "down", (f"the listener's oldest uncommitted reading is {age:.0f} s old (queue {queue}): ingest is "
                        "not keeping up - see the scale-test page's limiter notes")
    if age > QUEUE_AGE_DEGRADED_SECONDS:
        return "degraded", f"the listener's oldest uncommitted reading is {age:.0f} s old (queue {queue})"
    if cap and queue >= QUEUE_FILL_DEGRADED * cap:
        return "degraded", (f"a listener worker's queue is {queue}/{cap}: it can't keep up - "
                            f"raise BALER_LISTENER_WORKERS ({lst.get('workers')} now)")
    return None


def _listener_heartbeat_state(building: dict[str, Any]) -> tuple[str, str] | None:
    """("down", why) when the listener supervisor's heartbeat is stale.
    A missing one isn't judged: data freshness above already covers it."""
    beat = (building.get("heartbeats") or {}).get("mqtt_listener")
    if beat is None:
        return None
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(beat)).total_seconds()
    if age > HEARTBEAT_STALE_SECONDS:
        return "down", f"the listener's last heartbeat was {int(age)}s ago"
    return None


def ingest_summary(building: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """The overload numbers on /ops/status (#235), the same ones
    /ops/metrics exports: end-to-end lag (now minus the newest stored
    reading), the listener's backlog and throughput, and broker drops."""
    now = now or datetime.now(timezone.utc)
    lst = building.get("listener") or {}
    last = building.get("last_ingest_at")
    lag = round(max(0.0, (now - datetime.fromisoformat(last)).total_seconds()), 1) if last else None
    return {"lag_s": lag, "queue": lst.get("queue"), "queue_max": lst.get("queue_max"),
            "queue_oldest_age_s": lst.get("oldest_age_s"), "last_flush_s": lst.get("last_flush_s"),
            "readings_per_s": lst.get("readings_per_s"), "redelivered": lst.get("redelivered"),
            "workers": lst.get("workers"), "broker_dropped_since_start": lst.get("broker_dropped_since_start"),
            "broker_last_drop_at": lst.get("broker_last_drop_at")}


# The ingest card stays degraded this long after the broker last dropped
# messages (#233): the broker's queue for a slow subscriber overflowed.
DROP_WINDOW_SECONDS = 3600


def _drop_state(building: dict[str, Any]) -> tuple[str, str] | None:
    lst = building.get("listener") or {}
    last = lst.get("broker_last_drop_at")
    if not last or (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() > DROP_WINDOW_SECONDS:
        return None
    return "degraded", (f"the broker dropped {lst.get('broker_dropped_since_start')} message(s) since the listener "
                        f"started, the last at {last}: a subscriber can't keep up - if it's the listener "
                        f"({lst.get('workers')} worker(s)), raise BALER_LISTENER_WORKERS")


# A daemon is down once its heartbeat is this stale: fault_detector beats
# every His sweep (30 s by default), derivation_engine every tick (5 s).
HEARTBEAT_STALE_SECONDS = int(os.environ.get("OPS_HEARTBEAT_STALE_SECONDS", "180"))


def _heartbeat_state(building: dict[str, Any], service: str) -> tuple[str, str] | None:
    """("down", why) when the daemon's heartbeat is missing or stale."""
    beat = (building.get("heartbeats") or {}).get(service)
    if beat is None:
        return "down", "no heartbeat - is the service running?"
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(beat)).total_seconds()
    if age > HEARTBEAT_STALE_SECONDS:
        return "down", f"last heartbeat {int(age)}s ago"
    return None


# A rule daemon this far behind the reading feed is degraded (#243), and one
# that missed readings (fell more than the feed's retention behind, so
# batches were pruned unread) stays degraded for FEED_MISS_WINDOW_SECONDS.
FEED_LAG_DEGRADED_SECONDS = float(os.environ.get("OPS_FEED_LAG_DEGRADED_SECONDS", "60"))
FEED_MISS_WINDOW_SECONDS = 3600


def _feed_state(building: dict[str, Any], consumer: str) -> tuple[str, str] | None:
    """("degraded", why) when the daemon missed readings lately or is
    behind on the reading feed; None when it's keeping up."""
    feed = (building.get("reading_feed") or {}).get(consumer)
    if not feed:
        return None
    last = feed.get("last_missed_at")
    if last and (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < FEED_MISS_WINDOW_SECONDS:
        return "degraded", (f"missed {feed['missed_readings']} reading(s) in all, the last at {last}: "
                            "fell more than the reading feed's retention behind")
    if feed.get("lag_s", 0) > FEED_LAG_DEGRADED_SECONDS:
        return "degraded", f"behind on the reading feed by {feed['lag_s']:.0f} s ({feed.get('lag_readings', 0)} readings)"
    return None


def _infer_faults(building: dict[str, Any], *, platform_ok: bool) -> tuple[str, str | None]:
    if not platform_ok:
        return "down", "historian unreachable"
    stale = _heartbeat_state(building, "fault_detector")
    if stale:
        return stale
    behind = _feed_state(building, "fault_detector")
    if behind:
        return behind
    n = building.get("open_faults", 0)
    return "ok", f"running; {n} open fault(s)"


def _infer_derivations(building: dict[str, Any], *, platform_ok: bool) -> tuple[str, str | None]:
    if not platform_ok:
        return "down", "historian unreachable"
    stale = _heartbeat_state(building, "derivation_engine")
    if stale:
        return stale
    behind = _feed_state(building, "derivation_engine")
    if behind:
        return behind
    disabled = building.get("disabled_derivation_targets", 0)
    if disabled:
        return "degraded", f"{disabled} target(s) auto-disabled"
    return "ok", "running; no disabled targets"


def _probe_one(
    comp: dict[str, Any], building: dict[str, Any], platform_states: dict[str, str]
) -> dict[str, Any]:
    probe = comp["probe"]
    detail = None
    state = "down"
    if probe == "postgres":
        state, detail = _postgres_ok()
    elif probe == "http":
        state, detail = _http_ok(comp["url"])
    elif probe == "tcp":
        state, detail = _tcp_ok(comp["host"], comp["port"])
    elif probe == "inferred_ingest":
        platform_ok = (
            platform_states.get("historian") == "ok"
            and platform_states.get("live_bus") == "ok"
        )
        state, detail = _infer_ingest(building, platform_ok=platform_ok)
    elif probe == "inferred_faults":
        platform_ok = platform_states.get("historian") == "ok"
        state, detail = _infer_faults(building, platform_ok=platform_ok)
    elif probe == "inferred_derivations":
        platform_ok = platform_states.get("historian") == "ok"
        state, detail = _infer_derivations(building, platform_ok=platform_ok)
    return {
        "id": comp["id"],
        "label": comp["label"],
        "layer": comp["layer"],
        "service": comp["service"],
        "state": state,
        "detail": detail,
    }


def building_cards(building: dict[str, Any]) -> list[dict[str, Any]]:
    """The Building row's cards, judged here rather than in the page
    (Baler#64): the same states feed the cards, Needs attention and the
    summary chip, so the page can't show a DEGRADED card under "All green"."""

    def card(id_: str, label: str, state: str, detail: str, href: str | None = None) -> dict[str, Any]:
        out = {"id": id_, "label": label, "layer": "building", "service": "building", "state": state, "detail": detail}
        if href:
            out["href"] = href
        return out

    last = building.get("last_ingest_at")
    seen = int(building.get("points_seen_last_hour") or 0)
    faults = int(building.get("open_faults") or 0)
    disabled = int(building.get("disabled_derivation_targets") or 0)
    return [
        card("last_ingest", "Last ingest", "ok" if last else "quiet", last or "none yet"),
        card("points_last_hour", "Points (last hour)", "ok" if seen > 0 else "quiet", str(seen)),
        card("open_faults", "Open faults", "degraded" if faults else "ok", str(faults), "alarms.html"),
        card("disabled_targets", "Disabled analysis targets", "degraded" if disabled else "ok", str(disabled),
             "derivations.html"),
    ]


def _rank_summary(states: list[str]) -> str:
    if any(s == "down" for s in states):
        return "down"
    if any(s in ("degraded", "quiet") for s in states):
        return "degraded"
    return "ok"


def _run_probes(components: list[dict[str, Any]], deadline: float = PROBE_DEADLINE_SECONDS) -> dict[str, dict[str, Any]]:
    """Every direct probe at once, each on its own daemon thread, for at most
    `deadline` seconds: one that hasn't answered by then is down ("no answer
    within ..."), and its thread is left to finish on its own instead of
    holding up the answer or a pool slot."""
    results: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()

    def run(comp):
        item = _probe_one(comp, {}, {})
        with lock:
            results[comp["id"]] = item

    threads = [threading.Thread(target=run, args=(c,), daemon=True, name=f"probe-{c['id']}") for c in components]
    for t in threads:
        t.start()
    end = time.monotonic() + deadline
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))
    with lock:
        out = dict(results)
    for comp in components:
        out.setdefault(comp["id"], {
            "id": comp["id"], "label": comp["label"], "layer": comp["layer"], "service": comp["service"],
            "state": "down", "detail": f"no answer within {deadline:g}s",
        })
    return out


class _ProbeCache:
    """The latest round of direct probes, refreshed by a background thread
    (start()). Probing is not per-caller (scope only changes the Building
    counts), so one round serves every request."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._results: dict[str, dict[str, Any]] | None = None
        self._at = 0.0
        self._interval = PROBE_INTERVAL_SECONDS
        self._thread: threading.Thread | None = None

    def start(self, interval: float = PROBE_INTERVAL_SECONDS, prime: bool = False) -> None:
        """Probe every `interval` seconds from now on. `prime`: run the
        first round here, before returning, so a request arriving the
        moment the API listens already has one (Baler#172)."""
        with self._lock:
            if self._thread is not None:
                return
            self._interval = interval
        if prime:
            try:
                self.refresh()
            except Exception as exc:  # noqa: BLE001 - the loop below retries
                print(f"ops probes: {exc}")
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._loop, args=(prime,), daemon=True, name="ops-probes")
            self._thread.start()

    def _loop(self, primed: bool = False) -> None:
        if primed:
            time.sleep(self._interval)
        while True:
            try:
                self.refresh()
            except Exception as exc:  # noqa: BLE001 - never let the prober die
                print(f"ops probes: {exc}")
            time.sleep(self._interval)

    def refresh(self) -> dict[str, dict[str, Any]]:
        results = _run_probes([c for c in COMPONENTS if not str(c["probe"]).startswith("inferred_")])
        with self._lock:
            self._results, self._at = results, time.monotonic()
        return results

    def latest(self) -> dict[str, dict[str, Any]] | None:
        """The last round, unless the prober isn't running or has fallen
        three rounds behind (then the caller probes for itself)."""
        with self._lock:
            if self._results is None or time.monotonic() - self._at > 3 * self._interval:
                return None
            return dict(self._results)


_probes = _ProbeCache()


def start_background_probes(interval: float = PROBE_INTERVAL_SECONDS, prime: bool = False) -> None:
    """For ops_api: probe every `interval` seconds from now on, so /status
    answers from the latest round instead of probing per request; `prime`
    runs the first round before returning."""
    _probes.start(interval, prime)


def collect_status(scope=None) -> dict[str, Any]:
    """`scope`: the caller's site scope (scope.scope_for); None = everything."""
    direct = [c for c in COMPONENTS if not str(c["probe"]).startswith("inferred_")]
    inferred = [c for c in COMPONENTS if str(c["probe"]).startswith("inferred_")]

    # The Building counts (Postgres) and, without a fresh background round,
    # the probes: side by side rather than one after the other.
    with ThreadPoolExecutor(max_workers=1) as pool:
        building_future = pool.submit(_building_signals, scope)
        cached = _probes.latest()
        results = cached if cached is not None else _run_probes(direct)
        building = building_future.result()

    platform_states = {
        r["id"]: r["state"] for r in results.values() if r["layer"] == "platform"
    }
    for comp in inferred:
        results[comp["id"]] = _probe_one(comp, building, platform_states)

    ordered = [results[c["id"]] for c in COMPONENTS]
    cards = building_cards(building)
    # A degraded Building card counts toward the summary and Needs attention
    # like any service: open faults are something to attend to, not "All
    # green". A quiet one (no data yet) doesn't: the Ingest listener card
    # already says that, from the same timestamps.
    flagged = [c for c in cards if c["state"] in ("degraded", "down")]
    summary = _rank_summary([c["state"] for c in ordered + flagged if c["layer"] != "tools"])
    attention = [c for c in ordered if c["state"] != "ok"] + flagged

    return {
        "summary": summary,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "building": {
            "last_ingest_at": building.get("last_ingest_at"),
            "points_seen_last_hour": building.get("points_seen_last_hour", 0),
            "open_faults": building.get("open_faults", 0),
            "disabled_derivation_targets": building.get("disabled_derivation_targets", 0),
            "needs_first_org": building.get("org_count", 0) == 0,
            "reading_feed": building.get("reading_feed", {}),
            "ingest": ingest_summary(building),
            "cards": cards,
        },
        "platform": [c for c in ordered if c["layer"] == "platform"],
        "jobs": [c for c in ordered if c["layer"] == "jobs"],
        "tools": [c for c in ordered if c["layer"] == "tools"],
        "attention": attention,
        "first_actions": [
            {
                "id": "connect_source",
                "label": "Add a connector",
                "href": "connectors.html",
                "description": "Connect a BACnet or Modbus controller through a Windrow edge node so live readings land.",
                "enabled": True,
            },
            {
                "id": "load_demo",
                "label": "Load demo site",
                "href": "#demo",
                "description": "Coming soon — SkySpark Demogen equivalent for a mock hospital.",
                "enabled": False,
            },
        ],
    }

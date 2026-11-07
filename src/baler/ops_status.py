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
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

import psycopg

from baler import heartbeat, timeseries

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
            out["points_seen_last_hour"] = _count_visible(
                conn, "SELECT DISTINCT point_uri FROM point_history WHERE ts > now() - interval '1 hour'", scope)
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
    return "ok", f"last ingest {int(age)}s ago"


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


def _infer_faults(building: dict[str, Any], *, platform_ok: bool) -> tuple[str, str | None]:
    if not platform_ok:
        return "down", "historian unreachable"
    stale = _heartbeat_state(building, "fault_detector")
    if stale:
        return stale
    n = building.get("open_faults", 0)
    return "ok", f"running; {n} open fault(s)"


def _infer_derivations(building: dict[str, Any], *, platform_ok: bool) -> tuple[str, str | None]:
    if not platform_ok:
        return "down", "historian unreachable"
    stale = _heartbeat_state(building, "derivation_engine")
    if stale:
        return stale
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


def collect_status(scope=None) -> dict[str, Any]:
    """`scope`: the caller's site scope (scope.scope_for); None = everything."""
    building = _building_signals(scope)

    direct = [c for c in COMPONENTS if not str(c["probe"]).startswith("inferred_")]
    inferred = [c for c in COMPONENTS if str(c["probe"]).startswith("inferred_")]
    results: dict[str, dict[str, Any]] = {}

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_probe_one, c, building, {}): c["id"] for c in direct}
        for fut in as_completed(futs):
            item = fut.result()
            results[item["id"]] = item

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

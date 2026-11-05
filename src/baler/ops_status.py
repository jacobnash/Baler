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

from baler import timeseries

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


def _building_signals() -> dict[str, Any]:
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
            count_row = conn.execute(
                "SELECT COUNT(DISTINCT point_uri) FROM point_history "
                "WHERE ts > now() - interval '1 hour'"
            ).fetchone()
            out["points_seen_last_hour"] = int(count_row[0]) if count_row else 0
            try:
                fault_row = conn.execute(
                    "SELECT COUNT(*) FROM faults WHERE status = 'open'"
                ).fetchone()
                out["open_faults"] = int(fault_row[0]) if fault_row else 0
            except psycopg.Error:
                pass
            try:
                der_row = conn.execute(
                    "SELECT COUNT(*) FROM derivation_target_health WHERE disabled = TRUE"
                ).fetchone()
                out["disabled_derivation_targets"] = int(der_row[0]) if der_row else 0
            except psycopg.Error:
                pass
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


def _infer_faults(building: dict[str, Any], *, platform_ok: bool) -> tuple[str, str | None]:
    if not platform_ok:
        return "down", "historian unreachable"
    n = building.get("open_faults", 0)
    return "ok", f"{n} open fault(s); detector heartbeat not wired yet"


def _infer_derivations(building: dict[str, Any], *, platform_ok: bool) -> tuple[str, str | None]:
    if not platform_ok:
        return "down", "historian unreachable"
    disabled = building.get("disabled_derivation_targets", 0)
    if disabled:
        return "degraded", f"{disabled} target(s) auto-disabled"
    return "ok", "no disabled targets; engine heartbeat not wired yet"


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


def _rank_summary(states: list[str]) -> str:
    if any(s == "down" for s in states):
        return "down"
    if any(s in ("degraded", "quiet") for s in states):
        return "degraded"
    return "ok"


def collect_status() -> dict[str, Any]:
    building = _building_signals()

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
    summary = _rank_summary([c["state"] for c in ordered if c["layer"] != "tools"])
    attention = [c for c in ordered if c["state"] != "ok"]

    return {
        "summary": summary,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "building": {
            "last_ingest_at": building.get("last_ingest_at"),
            "points_seen_last_hour": building.get("points_seen_last_hour", 0),
            "open_faults": building.get("open_faults", 0),
            "disabled_derivation_targets": building.get("disabled_derivation_targets", 0),
            "needs_first_org": building.get("org_count", 0) == 0,
        },
        "platform": [c for c in ordered if c["layer"] == "platform"],
        "jobs": [c for c in ordered if c["layer"] == "jobs"],
        "tools": [c for c in ordered if c["layer"] == "tools"],
        "attention": attention,
        "first_actions": [
            {
                "id": "connect_source",
                "label": "Connect a source",
                "href": "devices.html",
                "description": "Point MQTT / Windrow at this stack so live readings land.",
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

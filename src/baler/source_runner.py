"""
Runs every enabled source in the registry (sources.py, Baler#20): one
daemon for all kinds, each source on its own tick_seconds, health recorded
on every run (last_run_at / last_ok_at / last_error / last_result) for the
Ops page to show.

Reads the registry every RELOAD_SECONDS, so adding, editing, enabling or
disabling a source in the UI takes effect without a restart. A source
whose run raises is marked failing with the error and retried on its next
tick; it never takes the others down.

    python -m baler.source_runner
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable

from baler import sources, tracing
from baler.remote_store import RemoteStore
from baler.timeseries import connect

tracer = tracing.get_tracer(__name__)

RELOAD_SECONDS = 30.0


def _run_haystack(s: dict, store, ts_conn):
    from baler import haystack_puller
    from baler.haystack_client import HaystackClient
    from baler.haystack_pull_state import ensure_schema

    ensure_schema(ts_conn)
    client = HaystackClient(s["url"], s["user"], s.get("password") or "")
    return haystack_puller.pull_once(client, store, ts_conn, s["source_id"], s["point_filter"],
                                     s["equip_filter"], float(s["backfill_days"]))


def _run_sql(s: dict, store, ts_conn):
    from baler import sql_puller

    if hasattr(sql_puller, "ensure_schema"):
        sql_puller.ensure_schema(ts_conn)
    return {"readings": sql_puller.pull_once(s["dsn"], store, ts_conn, s["source_id"], s["query"],
                                             float(s["backfill_days"]))}


def _run_openmeteo(s: dict, store, ts_conn):
    from baler import openmeteo_puller
    from baler.openmeteo_client import OpenMeteoClient

    client = OpenMeteoClient(float(s["latitude"]), float(s["longitude"]))
    return openmeteo_puller.pull_once(client, store, ts_conn, s["station_id"], openmeteo_puller.DEFAULT_VARIABLE_TAGS)


def _run_energystar(s: dict, store, ts_conn):
    from baler import energystar_puller
    from baler.energystar_client import EnergyStarClient

    client = EnergyStarClient(s["username"], s["password"], live=bool(s.get("live")))
    account_id = s.get("account_id") or client.account()["id"]
    metrics = [m.strip() for m in str(s["metrics"]).split(",") if m.strip()]
    return {"readings": energystar_puller.pull_once(client, store, ts_conn, account_id, metrics)}


RUNNERS: dict[str, Callable] = {
    "haystack": _run_haystack,
    "sql": _run_sql,
    "openmeteo": _run_openmeteo,
    "energystar": _run_energystar,
}


def run_source(src: dict, store, ts_conn) -> tuple[bool, object, str | None]:
    """One run of one source: (ok, result, error). Never raises."""
    settings = sources.effective_settings(src)
    missing = sources.missing_required(src["kind"], src["settings"], {k for k, v in src["_secrets"].items() if v})
    if missing:
        return False, None, f"missing required settings: {', '.join(missing)}"
    with tracer.start_as_current_span("source_runner.run") as span:
        span.set_attribute("source_id", src["id"])
        span.set_attribute("kind", src["kind"])
        try:
            return True, RUNNERS[src["kind"]](settings, store, ts_conn), None
        except Exception as exc:  # noqa: BLE001 - a failing source is recorded, never fatal
            span.set_attribute("error", repr(exc))
            return False, None, f"{type(exc).__name__}: {exc}"


def tick(conn, store, due: dict[str, float], now: float) -> list[str]:
    """Run every enabled source whose time has come; returns the ids run."""
    ran = []
    for src in sources.list_sources(conn, enabled_only=True):
        if src["kind"] not in RUNNERS or due.get(src["id"], 0.0) > now:
            continue
        ok, result, error = run_source(src, store, conn)
        sources.record_run(conn, src["id"], ok, result, error)
        interval = float(sources.effective_settings(src).get("tick_seconds") or 300.0)
        due[src["id"]] = now + max(interval, 10.0)
        print(f"{src['kind']}:{src['name']} {'ok' if ok else 'FAILED: ' + str(error)}")
        ran.append(src["id"])
    return ran


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    tracing.init_tracing("baler-source-runner")
    store = RemoteStore()
    conn = connect()
    sources.ensure_schema(conn)
    due: dict[str, float] = {}
    print("source runner: running the registry's enabled sources (Ops -> Sources)")
    while True:
        try:
            tick(conn, store, due, time.time())
        except Exception as exc:  # noqa: BLE001 - e.g. Postgres restarting; try again next round
            print(f"source runner round failed: {exc!r}")
            try:
                conn = connect()
            except Exception as again:  # noqa: BLE001 - keep the old conn; retry next round
                print(f"source runner: reconnect failed: {again!r}")
        time.sleep(RELOAD_SECONDS)


if __name__ == "__main__":
    main()

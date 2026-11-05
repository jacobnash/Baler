"""
Pulls each property's ENERGY STAR score (and a few other whole-period
metrics) from Portfolio Manager and ingests them through the same
pipeline every other source uses (ingest.py). Scores update roughly
monthly per EPA's own metric definition ("all metrics reflect the 12
months ending on the given month and year") - this re-reads the latest
complete period on a coarse tick and records it as a fresh reading, not
an incremental time-window pull the way sql_puller.py/haystack_puller.py
need for real continuous history, so there's no checkpoint table here.

Poll-only, single account per process - same shape as the other pullers.
"""

import argparse
import os
import time
from datetime import date, datetime, timezone

from baler import ingest, tracing
from baler.energystar_client import EnergyStarClient
from baler.remote_store import RemoteStore
from baler.timeseries import connect

tracer = tracing.get_tracer(__name__)

DEFAULT_METRICS = ["score", "siteIntensity", "sourceIntensity", "totalGHGEmissionsIntensity"]


def _latest_complete_period(today: date) -> tuple[int, int]:
    return (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)


def pull_once(client: EnergyStarClient, store, ts_conn, account_id, metrics: list[str]) -> int:
    now = time.time()
    # UTC, same clock as `now` above - the period is derived from the moment
    # of the pull, not from wherever the host's local zone happens to be.
    year, month = _latest_complete_period(datetime.now(timezone.utc).date())

    with tracer.start_as_current_span("energystar_puller.pull_once") as span:
        span.set_attribute("account_id", str(account_id))
        span.set_attribute("period", f"{year}-{month:02d}")
        properties = client.property_list(account_id)
        span.set_attribute("property_count", len(properties))

        count = 0
        for prop in properties:
            values = client.property_metrics(prop["id"], year, month, metrics)
            topic_prefix = f"energystar/{account_id}/{prop['id']}"
            for metric_name, value in values.items():
                if value is None:
                    continue
                try:
                    value = float(value)
                except ValueError:
                    pass  # non-numeric metric (e.g. a date) - store as text
                ingest.ingest_reading(store, ts_conn, f"{topic_prefix}/{metric_name}", value, ts=now)
                count += 1
        return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", default=os.environ.get("ENERGYSTAR_USERNAME"), required=os.environ.get("ENERGYSTAR_USERNAME") is None)
    parser.add_argument("--password", default=os.environ.get("ENERGYSTAR_PASSWORD"), required=os.environ.get("ENERGYSTAR_PASSWORD") is None)
    parser.add_argument("--account-id", default=os.environ.get("ENERGYSTAR_ACCOUNT_ID"), help="auto-discovered via GET /account if unset")
    parser.add_argument("--live", action="store_true", default=os.environ.get("ENERGYSTAR_LIVE", "") == "1", help="hits /ws (real submitted data) instead of /wstest - default test")
    parser.add_argument("--metrics", default=",".join(DEFAULT_METRICS), help="comma-separated PM metric names, e.g. 'score,siteIntensity'")
    parser.add_argument("--tick-seconds", type=float, default=86400.0, help="scores update ~monthly - daily is already more than enough, not a real-time feed")
    parser.add_argument("--once", action="store_true", help="pull once and exit instead of polling forever")
    args = parser.parse_args()

    tracing.init_tracing("baler-energystar-puller")

    store = RemoteStore()
    ts_conn = connect()
    client = EnergyStarClient(args.username, args.password, live=args.live)
    metrics = args.metrics.split(",")

    account_id = args.account_id or client.account()["id"]

    if args.once:
        print(f"pulling account {account_id!r} once ({'live' if args.live else 'test'})")
        count = pull_once(client, store, ts_conn, account_id, metrics)
        print(f"ingested {count} readings")
        return

    print(f"pulling account {account_id!r} every {args.tick_seconds}s ({'live' if args.live else 'test'})")
    while True:
        try:
            count = pull_once(client, store, ts_conn, account_id, metrics)
            print(f"ingested {count} readings")
        except Exception as exc:  # noqa: BLE001 - one bad tick must not kill the daemon; logged below
            # A single bad tick (EPA maintenance window, transient auth
            # failure) shouldn't kill the daemon - next tick tries again.
            print(f"pull cycle failed: {exc!r}")
        time.sleep(args.tick_seconds)


if __name__ == "__main__":
    main()

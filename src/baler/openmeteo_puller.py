"""
Pulls current conditions from Open-Meteo (open-meteo.com) - the WWW
weather API default/fallback for whoever doesn't have a local weather
station wired up (see docs-site/pages/weather-stations.mdx for the local
device path). Free, no API key or signup for non-commercial use - the
only puller in this repo with zero credential setup, just a location.

Ingests through the same functions and tag vocabulary a local weather
station uses (weatherStation equip, weather+<metric> point tags), so
rules/haystack_to_brick.yaml classifies both identically and both are
equally queryable - the only difference is where the numbers come from,
and a deployment can run both at once (different --station-id -> no URI
collision) to compare a local sensor against the WWW estimate for the
same site.

No checkpoint table, unlike haystack_puller.py/sql_puller.py - same
reasoning as energystar_puller.py: this records the current value on a
coarse tick, there's no incremental time-window history to backfill.
"""

import argparse
import os
import time
from datetime import datetime, timezone

from baler import ingest, mapping, tracing
from baler.openmeteo_client import OpenMeteoClient
from baler.remote_store import RemoteStore
from baler.store import HAYSTACK
from baler.timeseries import connect

tracer = tracing.get_tracer(__name__)

EQUIP_RULES_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "rules", "haystack_equip_to_brick.yaml")

# Open-Meteo variable name -> the same weather-station marker tags
# docs-site/pages/weather-stations.mdx documents for a local device -
# real Haystack v4 markers (project-haystack.org/doc/lib-phIoT/
# weatherStation), verified once for rules/haystack_to_brick.yaml.
DEFAULT_VARIABLE_TAGS = {
    "temperature_2m": {"weather": True, "air": True, "temp": True, "sensor": True},
    "relative_humidity_2m": {"weather": True, "air": True, "humidity": True, "sensor": True},
    "wind_speed_10m": {"weather": True, "wind": True, "speed": True, "sensor": True},
    "wind_direction_10m": {"weather": True, "wind": True, "direction": True, "sensor": True},
}


def observed_at(current: dict, fallback: float) -> float:
    """Open-Meteo's own observation time (`current.time`, GMT as requested),
    not when we fetched it: a 15-minute-old observation shouldn't look
    fresh (#53). Falls back to the fetch time if absent or unparseable."""
    raw = current.get("time")
    try:
        dt = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return fallback
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def pull_once(client: OpenMeteoClient, store, ts_conn, station_id: str, variable_tags: dict[str, dict]) -> dict[str, int]:
    with tracer.start_as_current_span("openmeteo_puller.pull_once") as span:
        span.set_attribute("station_id", station_id)
        if hasattr(client, "current_with_units"):
            current, units = client.current_with_units(list(variable_tags))
        else:
            current, units = client.current(list(variable_tags)), {}
        now = observed_at(current, time.time())
        span.set_attribute("variable_count", len(variable_tags))

        topic_prefix = f"weather/{station_id}"
        equip_uri = ingest.ingest_equip_tags(store, topic_prefix, {"weatherStation": True, "dis": f"Weather ({station_id}, Open-Meteo)"})
        equip_outcome, _ = mapping.classify_point(store, equip_uri, rules=mapping.load_rules(EQUIP_RULES_PATH))

        counts = {"equip": equip_outcome}
        for var, tags in variable_tags.items():
            value = current.get(var)
            if value is None:
                continue
            topic = f"{topic_prefix}/{var}"
            unit = units.get(var)
            if unit:
                # replace, not add: switching metric <-> imperial must not
                # leave the point with two units (#53)
                store.update(f"DELETE WHERE {{ <{ingest.topic_to_point_uri(topic)}> <{HAYSTACK}unit> ?u }}")
                tags = {**tags, "unit": unit}
            point_uri = ingest.ingest_tags(store, topic, tags)
            ingest.link_point_to_equip(store, point_uri, equip_uri)
            outcome, _ = mapping.classify_point_with_fallback(store, point_uri)
            ingest.ingest_reading(store, ts_conn, topic, value, ts=now)
            counts[var] = outcome
        return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--latitude", type=float, default=os.environ.get("OPENMETEO_LATITUDE"), required=os.environ.get("OPENMETEO_LATITUDE") is None)
    parser.add_argument("--longitude", type=float, default=os.environ.get("OPENMETEO_LONGITUDE"), required=os.environ.get("OPENMETEO_LONGITUDE") is None)
    parser.add_argument("--station-id", default=os.environ.get("OPENMETEO_STATION_ID", "openmeteo"))
    parser.add_argument("--units", choices=["metric", "imperial"], default=os.environ.get("OPENMETEO_UNITS", "metric"),
                        help="°C + km/h, or °F + mph")
    parser.add_argument("--tick-seconds", type=float, default=900.0, help="Open-Meteo's own current-conditions data refreshes ~every 15 minutes, polling faster just re-reads the same value")
    parser.add_argument("--once", action="store_true", help="pull once and exit instead of polling forever")
    args = parser.parse_args()

    tracing.init_tracing("baler-openmeteo-puller")

    store = RemoteStore()
    ts_conn = connect()
    client = OpenMeteoClient(args.latitude, args.longitude, args.units)

    if args.once:
        print(f"pulling {args.station_id!r} @ ({args.latitude}, {args.longitude}) once")
        print(pull_once(client, store, ts_conn, args.station_id, DEFAULT_VARIABLE_TAGS))
        return

    print(f"pulling {args.station_id!r} @ ({args.latitude}, {args.longitude}) every {args.tick_seconds}s")
    while True:
        try:
            counts = pull_once(client, store, ts_conn, args.station_id, DEFAULT_VARIABLE_TAGS)
            print(counts)
        except Exception as exc:  # noqa: BLE001 - one bad tick must not kill the daemon; logged below
            # A single bad tick (Open-Meteo hiccup, transient network
            # failure) shouldn't kill the daemon - next tick tries again.
            print(f"pull cycle failed: {exc!r}")
        time.sleep(args.tick_seconds)


if __name__ == "__main__":
    main()

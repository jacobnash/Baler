"""
One-shot loader: pulls a bounded slice of real hourly electricity meter
readings from the Building Data Genome Project 2 (BDG2 -
github.com/buds-lab/building-data-genome-project-2, real energy meters
from 1,636 real non-residential buildings across 19 real sites) and
ingests them through the same pipeline every other source uses
(ingest.py, mapping.py) - real buildings, real Brick classification, no
mock data.

Not a puller (see docs-site/pages/adding-a-data-source.mdx) - BDG2 is a
static historical dataset, not a live API to poll, so there's no
checkpoint table: re-running this just re-ingests the same window
(idempotent - timeseries.write_point_value upserts on (point_uri, ts)).

electricity.csv is ~166MB (Git LFS-backed) - this fetches a bounded byte
prefix via a Range GET rather than the whole file; a demo doesn't need
two years of every one of ~1600 buildings' data. --byte-limit's default
covers roughly two weeks of hourly readings across every building in the
file, of which we only keep the ones for --site.

ponytail: BDG2 timestamps are naive local time per building's own
timezone (see metadata.csv's timezone column); treated as UTC here for
simplicity. Fine for a demo - if you need readings to line up with real
wall-clock time for a specific site, convert using that column instead.
"""

import argparse
import csv
import io
import os
from datetime import datetime, timezone

import requests

from baler import mapping, tracing
from baler.ingest import ingest_haystack_equip_tags, ingest_reading, ingest_tags, link_point_to_equip
from baler.remote_store import RemoteStore
from baler.timeseries import connect

METADATA_URL = "https://media.githubusercontent.com/media/buds-lab/building-data-genome-project-2/master/data/metadata/metadata.csv"
ELECTRICITY_URL = "https://media.githubusercontent.com/media/buds-lab/building-data-genome-project-2/master/data/meters/raw/electricity.csv"
EQUIP_RULES_PATH = os.path.join(os.path.dirname(__file__), "..", "rules", "haystack_equip_to_brick.yaml")


def fetch_metadata() -> dict[str, dict]:
    resp = requests.get(METADATA_URL)
    resp.raise_for_status()
    return {row["building_id"]: row for row in csv.DictReader(io.StringIO(resp.text))}


def fetch_electricity_slice(byte_limit: int) -> list[dict]:
    """Bounded Range GET, not the full ~166MB file. Drops the last row - a
    Range GET can truncate mid-line."""
    resp = requests.get(ELECTRICITY_URL, headers={"Range": f"bytes=0-{byte_limit}"})
    resp.raise_for_status()
    lines = resp.text.splitlines()[:-1]
    return list(csv.DictReader(lines))


def load_building(store, ts_conn, point_rules, equip_rules, building_id: str, meta: dict, rows: list[dict]) -> int:
    equip_uri = ingest_haystack_equip_tags(store, f"bdg2:{building_id}", {
        "elec": True, "meter": True,
        "dis": building_id, "site": meta["site_id"], "primaryUse": meta["primaryspaceusage"],
    })
    mapping.classify_point(store, equip_uri, rules=equip_rules)

    topic = f"bdg2/{building_id}/electricity"
    point_uri = ingest_tags(store, topic, {
        "elec": True, "energy": True, "sensor": True, "point": True, "his": True,
        "unit": "kWh", "dis": f"{building_id} Electricity",
    })
    mapping.classify_point_with_fallback(store, point_uri, rules=point_rules)
    link_point_to_equip(store, point_uri, equip_uri)

    count = 0
    for row in rows:
        raw = row.get(building_id)
        if not raw or raw.lower() == "nan":
            continue
        ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        ingest_reading(store, ts_conn, topic, float(raw), ts=ts.timestamp())
        count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Load a real slice of BDG2 electricity data into Baler")
    parser.add_argument("--site", default="Panther", help="BDG2 site_id to pull buildings from (see metadata.csv)")
    parser.add_argument("--limit", type=int, default=5, help="number of buildings to load")
    parser.add_argument("--byte-limit", type=int, default=5_000_000, help="bytes of electricity.csv to fetch (~2 weeks across all buildings at the default)")
    args = parser.parse_args()

    tracing.init_tracing("baler-bdg2-loader")
    store = RemoteStore()
    ts_conn = connect()
    point_rules = mapping.load_rules()
    equip_rules = mapping.load_rules(EQUIP_RULES_PATH)

    print("fetching BDG2 metadata...")
    metadata = fetch_metadata()

    print(f"fetching electricity.csv slice ({args.byte_limit:,} bytes)...")
    rows = fetch_electricity_slice(args.byte_limit)
    print(f"got {len(rows)} hourly rows, {rows[0]['timestamp']} to {rows[-1]['timestamp']}")

    available_columns = set(rows[0].keys())
    buildings = [
        bid for bid, meta in metadata.items()
        if meta["site_id"] == args.site and meta["electricity"] == "Yes" and bid in available_columns
    ][: args.limit]
    if not buildings:
        raise SystemExit(f"no buildings with electricity data found for site {args.site!r} - check metadata.csv for real site_id values")

    print(f"loading {len(buildings)} real buildings from site {args.site!r}: {buildings}")
    for bid in buildings:
        count = load_building(store, ts_conn, point_rules, equip_rules, bid, metadata[bid], rows)
        print(f"  {bid}: {count} readings")

    print("done")


if __name__ == "__main__":
    main()

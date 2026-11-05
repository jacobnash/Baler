"""
Pulls points, equipment, and history from a remote Haystack REST server
(Haxall, SkySpark, or any other Project Haystack-compliant server) through
haystack_client.HaystackClient, and ingests through the same pipeline every
other source uses (ingest.py, mapping.py). Lets Baler run passively
alongside an existing Haxall/SkySpark deployment: point it at a read-only API
user, and it classifies the same already-tagged points/equipment through
Baler's own Brick mapping for comparison - no change to the target
system, contrast fantom-his-ext/fantom-windrow-conn which install into it.

Poll-only (no watchSub), single remote source per process - see
todo/ for what's deliberately out of scope for v1.

`--once` runs a single pull-everything pass and exits instead of polling
forever - the same pipeline doubles as a one-time Haystack-to-Brick
migration tool this way, not just a live shadow of an existing deployment.
"""

import argparse
import os
import time
from datetime import datetime, timezone

from baler import mapping, tracing
from baler.haystack_client import HaystackClient
from baler.haystack_pull_state import (
    ensure_schema,
    get_checkpoint,
    set_checkpoint,
)
from baler.ingest import (
    ingest_haystack_equip_tags,
    ingest_reading,
    ingest_tags,
    link_equip_ref,
    topic_to_point_uri,
)
from baler.remote_store import RemoteStore
from baler.timeseries import connect

tracer = tracing.get_tracer(__name__)

EQUIP_RULES_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "rules", "haystack_equip_to_brick.yaml")


def _namespaced_ref(source_id: str, ref: str) -> str:
    return f"{source_id}:{ref}"


def _tally(counts: dict, outcome: str) -> None:
    counts[outcome] = counts.get(outcome, 0) + 1


def pull_equip(client: HaystackClient, store, source_id: str, equip_filter: str) -> dict[str, int]:
    equip_rules = mapping.load_rules(EQUIP_RULES_PATH)
    counts: dict[str, int] = {}
    for row in client.read(equip_filter):
        ref = row.get("id")
        if not ref:
            continue
        equip_uri = ingest_haystack_equip_tags(store, _namespaced_ref(source_id, ref), row)
        outcome, _ = mapping.classify_point(store, equip_uri, rules=equip_rules)
        _tally(counts, outcome)
    return counts


def pull_points(client: HaystackClient, store, ts_conn, source_id: str, point_filter: str, backfill_days: float) -> dict[str, int]:
    now = time.time()
    counts: dict[str, int] = {}
    for row in client.read(point_filter):
        ref = row.get("id")
        if not ref:
            continue

        # Namespace equipRef *before* ingest_tags writes it, so it matches
        # the equip URI pull_equip just created (haystack_ref_to_equip_uri
        # and link_equip_ref both key off this stored tag value verbatim).
        if row.get("equipRef"):
            row = {**row, "equipRef": _namespaced_ref(source_id, row["equipRef"])}

        topic = f"haystack/{source_id}/{ref}"
        point_uri = ingest_tags(store, topic, row)
        link_equip_ref(store, point_uri)
        outcome, _ = mapping.classify_point_with_fallback(store, point_uri)
        _tally(counts, outcome)

        checkpoint = get_checkpoint(ts_conn, str(point_uri))
        start = checkpoint if checkpoint is not None else now - backfill_days * 86400
        _pull_history(client, store, ts_conn, topic, ref, start, now)
        set_checkpoint(ts_conn, str(point_uri), now)
    return counts


def _pull_history(client: HaystackClient, store, ts_conn, topic: str, ref: str, start_ts: float, end_ts: float) -> None:
    if start_ts >= end_ts:
        return
    # ponytail: plain ISO-8601 "start,end" - Haystack's own range grammar
    # also accepts (and some servers may expect) a trailing tz-name per
    # timestamp ("...+00:00 UTC"); untested against a real server since
    # none was reachable while building this - the live e2e test in
    # tests/test_haystack_puller.py is where a real wire-format mismatch
    # would actually surface, adjust the format here if it does.
    start_iso = datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat()
    end_iso = datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat()
    with tracer.start_as_current_span("haystack_puller.pull_history") as span:
        span.set_attribute("point_uri", str(topic_to_point_uri(topic)))
        rows = client.his_read(f"@{ref}", range=f"{start_iso},{end_iso}")
        span.set_attribute("row_count", len(rows))
        for row in rows:
            ingest_reading(store, ts_conn, topic, row.get("val"), ts=row.get("ts"))


def pull_once(client: HaystackClient, store, ts_conn, source_id: str, point_filter: str, equip_filter: str, backfill_days: float) -> dict[str, dict[str, int]]:
    with tracer.start_as_current_span("haystack_puller.pull_once") as span:
        span.set_attribute("source_id", source_id)
        equip_counts = pull_equip(client, store, source_id, equip_filter)
        point_counts = pull_points(client, store, ts_conn, source_id, point_filter, backfill_days)
        return {"equip": equip_counts, "points": point_counts}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--haystack-url", default=os.environ.get("REMOTE_HAYSTACK_URL"), required=os.environ.get("REMOTE_HAYSTACK_URL") is None)
    parser.add_argument("--haystack-user", default=os.environ.get("REMOTE_HAYSTACK_USER"), required=os.environ.get("REMOTE_HAYSTACK_USER") is None)
    parser.add_argument("--haystack-password", default=os.environ.get("REMOTE_HAYSTACK_PASSWORD", ""))
    parser.add_argument("--source-id", default=os.environ.get("REMOTE_HAYSTACK_SOURCE_ID", "haxall"))
    parser.add_argument("--point-filter", default="point")
    parser.add_argument("--equip-filter", default="equip")
    parser.add_argument("--tick-seconds", type=float, default=300.0, help="polls a remote server, not our own MQTT broker - keep this coarse")
    parser.add_argument("--backfill-days", type=float, default=7.0, help="history lookback the first time a point is seen")
    parser.add_argument(
        "--once",
        action="store_true",
        help="pull everything currently on the source once and exit, instead of polling forever - "
        "for migrating an existing site's data into Brick rather than running alongside it live. "
        "Follow up with `python -m baler.autotag` to LLM-sweep anything left as fallback/miss.",
    )
    args = parser.parse_args()

    tracing.init_tracing("baler-haystack-puller")

    store = RemoteStore()
    ts_conn = connect()
    ensure_schema(ts_conn)

    client = HaystackClient(args.haystack_url, args.haystack_user, args.haystack_password)

    if args.once:
        print(f"pulling {args.source_id!r} from {args.haystack_url} once")
        result = pull_once(client, store, ts_conn, args.source_id, args.point_filter, args.equip_filter, args.backfill_days)
        print(f"equip: {result['equip']}")
        print(f"points: {result['points']}")
        return

    print(f"pulling {args.source_id!r} from {args.haystack_url} every {args.tick_seconds}s")
    while True:
        try:
            pull_once(client, store, ts_conn, args.source_id, args.point_filter, args.equip_filter, args.backfill_days)
        except Exception as exc:  # noqa: BLE001 - one bad tick must not kill the daemon; logged below
            # A single bad tick (remote server hiccup, transient auth
            # failure) shouldn't kill the daemon - next tick tries again.
            print(f"pull cycle failed: {exc!r}")
        time.sleep(args.tick_seconds)


if __name__ == "__main__":
    main()

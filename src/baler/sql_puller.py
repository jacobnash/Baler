"""
Pulls point readings from an arbitrary external SQL database and ingests
them through the same pipeline every other source uses (ingest.py). Unlike
Haystack, a bare SQL table doesn't self-describe points via tags - the
contract here is just "give me a query returning point, value, ts columns
for the window I ask for," filled in via %(since)s/%(until)s placeholders.

Poll-only, single remote source per process - same shape as
haystack_puller.py. Postgres-wire-compatible sources only (uses psycopg,
already a dependency); add a real multi-driver layer (e.g. SQLAlchemy) if
a non-Postgres-wire source is ever needed.
"""

import argparse
import os
import time

import psycopg
from psycopg.rows import dict_row

from baler import ingest, tracing
from baler.remote_store import RemoteStore
from baler.sql_pull_state import ensure_schema, get_checkpoint, set_checkpoint
from baler.timeseries import connect

tracer = tracing.get_tracer(__name__)


def pull_once(sql_dsn: str, store, ts_conn, source_id: str, query: str, backfill_days: float) -> int:
    now = time.time()
    since = get_checkpoint(ts_conn, source_id)
    if since is None:
        since = now - backfill_days * 86400

    with tracer.start_as_current_span("sql_puller.pull_once") as span:
        span.set_attribute("source_id", source_id)
        # Fresh connection per tick rather than held open across the poll
        # interval - at the coarse cadence this runs (minutes, like
        # haystack_puller), reconnect cost is trivial next to not having to
        # handle a remote connection going stale/firewalled between polls.
        with psycopg.connect(sql_dsn, row_factory=dict_row) as remote_conn:
            rows = remote_conn.execute(query, {"since": since, "until": now}).fetchall()
        span.set_attribute("row_count", len(rows))

        for row in rows:
            topic = f"sql/{source_id}/{row['point']}"
            ts = row["ts"]
            if hasattr(ts, "timestamp"):  # driver returned a datetime, not a raw epoch
                ts = ts.timestamp()
            ingest.ingest_reading(store, ts_conn, topic, row["value"], ts=ts)

        set_checkpoint(ts_conn, source_id, now)
        return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sql-dsn", default=os.environ.get("REMOTE_SQL_DSN"), required=os.environ.get("REMOTE_SQL_DSN") is None)
    parser.add_argument(
        "--sql-query",
        default=os.environ.get("REMOTE_SQL_QUERY"),
        required=os.environ.get("REMOTE_SQL_QUERY") is None,
        help="must SELECT point, value, ts columns; use %%(since)s/%%(until)s placeholders for the poll window",
    )
    parser.add_argument("--source-id", default=os.environ.get("REMOTE_SQL_SOURCE_ID", "sql"))
    parser.add_argument("--tick-seconds", type=float, default=300.0, help="polls a remote server, not our own MQTT broker - keep this coarse")
    parser.add_argument("--backfill-days", type=float, default=7.0, help="history lookback the first time this source is pulled")
    parser.add_argument("--once", action="store_true", help="pull once and exit instead of polling forever")
    args = parser.parse_args()

    tracing.init_tracing("baler-sql-puller")

    store = RemoteStore()
    ts_conn = connect()
    ensure_schema(ts_conn)

    if args.once:
        print(f"pulling {args.source_id!r} once")
        count = pull_once(args.sql_dsn, store, ts_conn, args.source_id, args.sql_query, args.backfill_days)
        print(f"ingested {count} readings")
        return

    print(f"pulling {args.source_id!r} every {args.tick_seconds}s")
    while True:
        try:
            count = pull_once(args.sql_dsn, store, ts_conn, args.source_id, args.sql_query, args.backfill_days)
            print(f"ingested {count} readings")
        except Exception as exc:  # noqa: BLE001 - one bad tick must not kill the daemon; logged below
            # A single bad tick (remote server hiccup, transient auth
            # failure) shouldn't kill the daemon - next tick tries again.
            print(f"pull cycle failed: {exc!r}")
        time.sleep(args.tick_seconds)


if __name__ == "__main__":
    main()

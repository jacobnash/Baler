"""
0002 reading feed and point registry (#243): what the rule daemons read
instead of a second QoS 0 firehose and a full scan of point_history.

- reading_feed: one row per committed listener batch (seq order = commit
  order), the batch's readings as parallel arrays plus any controller alarms
  it carried. Written in the same transaction as the batch's point_history
  upsert (baler.reading_feed.append_from_stage), so a consumer sees exactly
  the readings that were stored, never one the broker was told about and
  the database lost, or the other way round. Short-lived: pruned once every
  consumer has read a batch, or after the retention window
  (BALER_FEED_RETENTION_S) whatever happens.
- reading_feed_consumers: each consumer's offset (the last seq it finished)
  and what it missed: batches pruned before it read them are added to
  missed_readings/missed_batches by the pruner, in the same transaction as
  the delete, so a miss is counted exactly, never silent.
- point_registry: every point that has history and, coarsely, its newest
  ts (timeseries.REGISTRY_GRANULARITY_S: a writer only moves it forward by
  more than that, so the upsert per point per batch is usually an index
  probe and no write). fault_detector's His-mode sweep reads candidates
  from here instead of `SELECT DISTINCT point_uri FROM point_history`.
  Seeded from point_history with a DISTINCT ON over its primary key, which
  TimescaleDB answers with a skip scan (one probe per point per chunk, not
  a read of every row).
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 2
NAME = "reading feed and point registry"

READING_FEED = """
CREATE TABLE IF NOT EXISTS reading_feed (
    seq          BIGSERIAL PRIMARY KEY,
    committed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    n            INTEGER NOT NULL,
    point_uri    TEXT[] NOT NULL,
    ts           TIMESTAMPTZ[] NOT NULL,
    value        DOUBLE PRECISION[] NOT NULL,
    value_text   TEXT[] NOT NULL,
    value_bool   BOOLEAN[] NOT NULL,
    quality      TEXT[] NOT NULL,
    alarms       JSONB
) WITH (autovacuum_vacuum_scale_factor = 0, autovacuum_vacuum_threshold = 200)"""

CONSUMERS = """
CREATE TABLE IF NOT EXISTS reading_feed_consumers (
    consumer        TEXT PRIMARY KEY,
    seq             BIGINT NOT NULL,
    missed_readings BIGINT NOT NULL DEFAULT 0,
    missed_batches  BIGINT NOT NULL DEFAULT 0,
    last_missed_at  TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
)"""

REGISTRY = """
CREATE TABLE IF NOT EXISTS point_registry (
    point_uri TEXT PRIMARY KEY,
    last_ts   TIMESTAMPTZ NOT NULL
) WITH (fillfactor = 80)"""

SEED_REGISTRY = """
INSERT INTO point_registry (point_uri, last_ts)
SELECT DISTINCT ON (point_uri) point_uri, ts FROM point_history ORDER BY point_uri, ts DESC
ON CONFLICT (point_uri) DO NOTHING"""


def plan(conn) -> list[Step]:
    return [
        Step(READING_FEED, "reading_feed: committed listener batches for the rule daemons"),
        Step(CONSUMERS, "reading_feed_consumers: offsets and missed counts"),
        Step(REGISTRY, "point_registry: points with history, coarse newest ts"),
        Step(SEED_REGISTRY, "seed point_registry from point_history (skip scan)"),
    ]

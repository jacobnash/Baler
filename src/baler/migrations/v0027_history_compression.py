"""
0027 history compression events (#337): what baler.history_keeper did to
each point_history chunk, and why it failed when it did.

TimescaleDB's compression policy reports a failed run as "columnstore
policy failure: Failed to convert '1' chunks to columnstore" and puts the
chunk's own error only in a WARNING in the server log, which a busy log
can rotate away before anyone looks (nash-srv's 6 h soak, #337). The
keeper compresses, recompresses and reclaims chunks itself and records
each attempt here, with the error's SQLSTATE, message, detail and context,
so the real cause is in the database and on /ops/status, and a chunk's
retry state (consecutive failures, when it was last tried) is read from
its newest rows. Pruned after 30 days by the keeper.

- action: compress (an uncompressed chunk the policy left behind),
  recompress (a chunk with late rows), reclaim (VACUUM/REINDEX after a
  recompression), policy (a failed run of the TimescaleDB policy job,
  copied from timescaledb_information.job_errors).
- disk_before / disk_after: the chunk's bytes on disk (heap, indexes and
  its compressed relation) around the action; stats_bytes: what
  chunk_compression_stats says the compressed chunk takes.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 27
NAME = "history compression events"

EVENTS = """
CREATE TABLE IF NOT EXISTS history_compression_events (
    id          BIGSERIAL PRIMARY KEY,
    at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    chunk       TEXT NOT NULL,
    action      TEXT NOT NULL,
    ok          BOOLEAN NOT NULL,
    sqlstate    TEXT,
    message     TEXT,
    detail      TEXT,
    context     TEXT,
    duration_s  DOUBLE PRECISION,
    disk_before BIGINT,
    disk_after  BIGINT,
    stats_bytes BIGINT
)"""

BY_CHUNK = "CREATE INDEX IF NOT EXISTS history_compression_events_chunk ON history_compression_events (chunk, at DESC)"
BY_AT = "CREATE INDEX IF NOT EXISTS history_compression_events_at ON history_compression_events (at)"


def plan(conn) -> list[Step]:
    return [
        Step(EVENTS, "history_compression_events: each compress/recompress/reclaim attempt and its real error"),
        Step(BY_CHUNK, "history_compression_events: newest first per chunk (retry state)"),
        Step(BY_AT, "history_compression_events: by time (pruning, recent failures)"),
    ]

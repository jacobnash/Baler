"""
0003 incremental quality sweep (#268): what lets baler.quality judge each
row once instead of the last hour of every point on every sweep.

- quality_watermark: per sweeper, the ts up to which rows have been judged
  (`judged_to`). The next sweep judges (judged_to - overlap, now] and moves
  it, in the same transaction as nothing else: a sweep that dies before
  moving it is simply redone (flag writes only fill NULLs, so a repeat is
  harmless).
- quality_late: rows that were committed after the sweep that covered
  their ts had moved past them (late by more than the overlap window). The
  sweeper's reading-feed watcher finds them in the feed (every committed
  batch is a feed row, in the same transaction as its history write) and
  records them here, in the same statement that moves its feed offset, so
  none is lost between the two; the next sweep judges them and deletes
  them. Usually empty.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 3
NAME = "quality watermark and late rows"

WATERMARK = """
CREATE TABLE IF NOT EXISTS quality_watermark (
    sweeper    TEXT PRIMARY KEY,
    judged_to  TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)"""

LATE = """
CREATE TABLE IF NOT EXISTS quality_late (
    point_uri TEXT NOT NULL,
    ts        TIMESTAMPTZ NOT NULL,
    seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (point_uri, ts)
)"""


def plan(conn) -> list[Step]:
    return [
        Step(WATERMARK, "quality_watermark: how far the incremental quality sweep has judged"),
        Step(LATE, "quality_late: rows committed after the sweep passed their ts"),
    ]

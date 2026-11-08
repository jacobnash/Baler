"""
The reading feed (#243): how the rule daemons (fault_detector,
derivation_engine) get every stored reading without a second firehose.

Before this, each daemon took its own QoS 0 subscription to all telemetry
(`fbf/#`, `#`). The broker drops QoS 0 deliveries to a subscriber that
falls behind, and nothing in Baler counted them: at 10k points every 5 s
the scale ladder saw 42,558 such drops in 54 s, every one a reading the
Cur-mode rules and derivations never evaluated.

Now mqtt_listener appends each batch it commits to `reading_feed`, in the
same transaction as the batch's point_history upsert (append_from_stage):
one row per batch, its readings as parallel arrays built server-side from
the staging table, so the feed costs one more INSERT per batch and no extra
bytes on the wire. Each daemon is a FeedConsumer with a durable offset in
`reading_feed_consumers`: it reads the batches after its offset, in commit
order, evaluates them, and moves the offset. So:

- nothing a daemon sees can be lost by the broker, and it sees exactly what
  was stored (no reading evaluated that the database then rolled back);
- a daemon that stops or falls behind resumes where it was;
- falling behind is visible: lag (batches, readings, seconds) is read from
  the feed, and a batch pruned before a consumer read it (it fell more
  than the retention window behind, BALER_FEED_RETENTION_S) is added to
  that consumer's missed_readings by the pruner, in the same transaction
  as the delete. Never silent.

Rules see readings at least once (a listener redelivery after a crash can
append a batch twice); every rule effect is idempotent on a repeat.
"""

from __future__ import annotations

import functools
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Jsonb

# Serialises feed appends, so seq order is commit order even with more
# than one writer: a consumer that has read seq N can never later see a
# smaller seq commit. One writer (the listener) takes it uncontended.
FEED_LOCK_KEY = 243_190_001

DEFAULT_RETENTION_S = float(os.environ.get("BALER_FEED_RETENTION_S", "") or 600)
URI_PREFIX = "urn:point:"

_APPEND_SQL = """
INSERT INTO reading_feed (n, point_uri, ts, value, value_text, value_bool, quality, alarms)
SELECT count(*),
       COALESCE(array_agg(point_uri ORDER BY ts, point_uri), '{}'),
       COALESCE(array_agg(ts ORDER BY ts, point_uri), '{}'),
       COALESCE(array_agg(value ORDER BY ts, point_uri), '{}'),
       COALESCE(array_agg(value_text ORDER BY ts, point_uri), '{}'),
       COALESCE(array_agg(value_bool ORDER BY ts, point_uri), '{}'),
       COALESCE(array_agg(quality ORDER BY ts, point_uri), '{}'),
       %s
FROM point_history_stage
"""


def append_from_stage(conn, alarms: list | None = None) -> None:
    """Inside write_point_values' transaction, after the upsert: the staged
    rows (deduplicated on (point, ts), ordered by ts) become one feed row.
    `alarms` are the batch's controller alarms, [[topic, event, ts], ...]:
    not readings, so not in point_history, but they reach fault_detector
    the same way and with the same guarantees."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (FEED_LOCK_KEY,))
    conn.execute(_APPEND_SQL, (Jsonb(alarms) if alarms else None,))


_PRUNE_SQL = """
WITH gone AS (
    DELETE FROM reading_feed
    WHERE seq <= (SELECT COALESCE(min(seq), 0) FROM reading_feed_consumers)
       OR committed_at < now() - make_interval(secs => %s)
    RETURNING seq, n
), lost AS (
    SELECT c.consumer, count(*) AS batches, COALESCE(sum(g.n), 0) AS readings, max(g.seq) AS top
    FROM reading_feed_consumers c JOIN gone g ON g.seq > c.seq
    GROUP BY c.consumer
), upd AS (
    UPDATE reading_feed_consumers c
    SET missed_readings = c.missed_readings + lost.readings,
        missed_batches = c.missed_batches + lost.batches,
        last_missed_at = now(),
        seq = greatest(c.seq, lost.top)
    FROM lost WHERE c.consumer = lost.consumer
    RETURNING c.consumer, lost.readings, lost.batches
)
SELECT (SELECT count(*) FROM gone),
       COALESCE((SELECT json_agg(json_build_array(consumer, readings, batches)) FROM upd), '[]')
"""


def prune(conn, retention_s: float = DEFAULT_RETENTION_S) -> tuple[int, list]:
    """Delete what every consumer has read, and anything older than the
    retention window whatever, counting the latter against each consumer
    that hadn't read it. Returns (batches deleted, [[consumer, readings,
    batches] missed now]). The listener calls this every few seconds."""
    row = conn.execute(_PRUNE_SQL, (retention_s,)).fetchone()
    return int(row[0]), list(row[1] or [])


@dataclass
class Batch:
    seq: int
    committed_at: datetime
    readings: list[tuple[str, datetime | None, Any, str | None]]  # (point_uri, ts, value, quality), ts order
    alarms: list = field(default_factory=list)  # [[topic, event, ts], ...]


def _value(v, vt, vb):
    if vb is not None:
        return vb
    if vt is not None:
        return vt
    return v


@dataclass
class FeedStats:
    batches: int = 0
    readings: int = 0
    lag_batches: int = 0
    lag_readings: int = 0
    lag_s: float = 0.0
    missed_readings: int = 0
    missed_batches: int = 0
    last_missed_at: datetime | None = None

    def as_dict(self) -> dict:
        return {"batches": self.batches, "readings": self.readings, "lag_batches": self.lag_batches,
                "lag_readings": self.lag_readings, "lag_s": round(self.lag_s, 2),
                "missed_readings": self.missed_readings, "missed_batches": self.missed_batches,
                "last_missed_at": self.last_missed_at.isoformat() if self.last_missed_at else None}


class FeedConsumer:
    """One daemon's cursor on the feed. Not thread-safe: one thread (with
    its own connection) polls and commits."""

    def __init__(self, conn, name: str, max_batches: int = 20, log=None,
                 points_only: bool = False):
        """`points_only`: fetch just each reading's point (readings come as
        (point_uri, None, None, None)) - for a consumer that only needs to
        know what reported, like derivation_engine, it skips decoding the
        values."""
        self.conn, self.name, self.max_batches = conn, name, max_batches
        self.log = log or functools.partial(print, flush=True)
        self.points_only = points_only
        self.seq = 0
        self.stats = FeedStats()

    def start(self) -> None:
        """Register (a new consumer starts at the feed's head: it has
        nothing to catch up on) or resume from the stored offset, and
        report anything missed while it was away."""
        self.conn.execute(
            "INSERT INTO reading_feed_consumers (consumer, seq) "
            "VALUES (%s, (SELECT COALESCE(max(seq), 0) FROM reading_feed)) ON CONFLICT (consumer) DO NOTHING",
            (self.name,))
        seq, missed_r, missed_b, last = self.conn.execute(
            "SELECT seq, missed_readings, missed_batches, last_missed_at FROM reading_feed_consumers WHERE consumer = %s",
            (self.name,)).fetchone()
        self.seq = seq
        self.stats.missed_readings, self.stats.missed_batches, self.stats.last_missed_at = missed_r, missed_b, last
        if missed_r or missed_b:
            self.log(f"{self.name}: reading feed: {missed_r} reading(s) in {missed_b} batch(es) missed so far "
                     f"(last {last}): fell more than the retention window behind")

    def poll(self) -> list[Batch]:
        """The next batches after the offset, oldest first (at most
        max_batches). Doesn't move the offset: commit() does, once
        they're handled."""
        cur = self.conn.cursor(binary=True)
        if self.points_only:
            rows = cur.execute(
                "SELECT seq, committed_at, point_uri, alarms FROM reading_feed WHERE seq > %s ORDER BY seq LIMIT %s",
                (self.seq, self.max_batches)).fetchall()
            return [Batch(seq, at, [(u, None, None, None) for u in uris], alarms or []) for seq, at, uris, alarms in rows]
        rows = cur.execute(
            "SELECT seq, committed_at, point_uri, ts, value, value_text, value_bool, quality, alarms "
            "FROM reading_feed WHERE seq > %s ORDER BY seq LIMIT %s", (self.seq, self.max_batches)).fetchall()
        out = []
        for seq, at, uris, tss, vals, texts, bools, quals, alarms in rows:
            readings = [(u, t, _value(v, vt, vb), q) for u, t, v, vt, vb, q in zip(uris, tss, vals, texts, bools, quals)]
            out.append(Batch(seq, at, readings, alarms or []))
        return out

    def commit(self, batches: list[Batch]) -> None:
        """Move the offset past `batches` (handled), refresh lag, and log
        anything the pruner counted as missed since the last commit."""
        if not batches:
            return
        last = batches[-1].seq
        self.stats.batches += len(batches)
        self.stats.readings += sum(len(b.readings) for b in batches)
        seq, missed_r, missed_b, at = self.conn.execute(
            "UPDATE reading_feed_consumers SET seq = greatest(seq, %s), updated_at = now() WHERE consumer = %s "
            "RETURNING seq, missed_readings, missed_batches, last_missed_at", (last, self.name)).fetchone()
        self.seq = seq
        if missed_r > self.stats.missed_readings or missed_b > self.stats.missed_batches:
            self.log(f"{self.name}: reading feed: MISSED {missed_r - self.stats.missed_readings} reading(s) in "
                     f"{missed_b - self.stats.missed_batches} batch(es), pruned before they were read "
                     f"(more than the retention window behind); {missed_r} missed in all")
        self.stats.missed_readings, self.stats.missed_batches, self.stats.last_missed_at = missed_r, missed_b, at

    def refresh_lag(self) -> FeedStats:
        """What's waiting after the offset: batches, readings, and the age
        of the oldest. A PK range over the (short) feed."""
        n, readings, oldest = self.conn.execute(
            "SELECT count(*), COALESCE(sum(n), 0), min(committed_at) FROM reading_feed WHERE seq > %s",
            (self.seq,)).fetchone()
        self.stats.lag_batches, self.stats.lag_readings = int(n), int(readings)
        self.stats.lag_s = (datetime.now(timezone.utc) - oldest).total_seconds() if oldest else 0.0
        return self.stats

    def run(self, handle, stop=None, idle_s: float = 0.2, report=None, report_every: float = 10.0) -> None:
        """Poll, handle(batch) each, commit, forever (or until `stop` is
        set). `report(stats)` runs every `report_every` seconds with fresh
        lag figures (the daemon's heartbeat). An exception from handle
        propagates: the daemon exits and resumes from the offset on
        restart, the batch unacknowledged."""
        last_report = 0.0
        while stop is None or not stop.is_set():
            batches = self.poll()
            for batch in batches:
                handle(batch)
            self.commit(batches)
            if report is not None and time.monotonic() - last_report >= report_every:
                last_report = time.monotonic()
                report(self.refresh_lag())
            if len(batches) < self.max_batches:
                time.sleep(idle_s)


def topic_of(point_uri: str) -> str:
    return point_uri.removeprefix(URI_PREFIX)


def consumer_status(conn) -> dict[str, dict]:
    """Every consumer's offset, lag and misses, for /ops/status and the
    scale ladder. {} before the feed exists."""
    try:
        rows = conn.execute(
            """
            SELECT c.consumer, c.seq, c.missed_readings, c.missed_batches, c.last_missed_at, c.updated_at,
                   (SELECT count(*) FROM reading_feed f WHERE f.seq > c.seq),
                   (SELECT COALESCE(sum(n), 0) FROM reading_feed f WHERE f.seq > c.seq),
                   (SELECT min(committed_at) FROM reading_feed f WHERE f.seq > c.seq)
            FROM reading_feed_consumers c
            """).fetchall()
    except Exception:  # noqa: BLE001 - no feed table yet
        return {}
    now = datetime.now(timezone.utc)
    return {name: {"seq": seq, "missed_readings": mr, "missed_batches": mb,
                   "last_missed_at": lm.isoformat() if lm else None, "updated_at": up.isoformat(),
                   "lag_batches": int(lb), "lag_readings": int(lr),
                   "lag_s": round((now - oldest).total_seconds(), 1) if oldest else 0.0}
            for name, seq, mr, mb, lm, up, lb, lr, oldest in rows}

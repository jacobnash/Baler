"""
point_history compression upkeep (#337): the `history_keeper` service.

TimescaleDB's compression policy compresses each chunk once it is older
than compress_after, and recompresses a chunk that took late rows on its
next run. nash-srv's 6 h soak at 100k points (#337) showed three gaps:

1. **A failed run hides its cause.** The policy catches each chunk's error,
   logs it as a WARNING ("converting chunk ... failed", the real message in
   its DETAIL) and then fails the run with "columnstore policy failure:
   Failed to convert '1' chunks to columnstore" (SQLSTATE 22000). That last
   message is all `timescaledb_information.job_errors` keeps, and the scale
   stack logged every statement into a 150 MB rotating log, so the WARNING
   was gone. Nothing retried the chunk before the next scheduled run.
2. **Late rows inflate compressed chunks, and recompression doesn't give
   the disk back.** A row written into a compressed chunk (a spool replay,
   trend backfill, hisWrite) decompresses its point's whole batch (up to
   1,000 rows; with 1-hour chunks at 5 s, the point's whole hour) into the
   chunk's heap and indexes, so one late reading per point puts the whole
   chunk back on disk uncompressed: about 250 B per row against about 5.
   Recompression moves the rows back into the columnstore, but it only
   deletes them from the heap: the heap stays its size until VACUUM
   truncates it, and the old compressed batches stay as dead space in the
   compressed relation. chunk_compression_stats still reports the size at
   compression time. Measured locally (2,000 points x 1 hour, one late row
   per point): 12 MB compressed -> 229 MB after the late rows -> 241 MB
   after recompression -> 12 MB after VACUUM, REINDEX and VACUUM FULL of
   the compressed relation. nash-srv: 565 MB in the stats, 12 GB on disk.
3. Nothing reported the difference.

So every BALER_HISTORY_KEEPER_INTERVAL_S (300 s) the keeper:

- copies new failed runs of the policy job from job_errors into
  history_compression_events (migration 0027) and logs them;
- **retries**: compresses chunks the policy should have compressed by now
  (older than compress_after plus one policy schedule, still uncompressed),
  each in its own statement, recording the error's SQLSTATE, message,
  detail and context when it fails. A chunk that keeps failing is retried
  with backoff (RETRY_BASE_S doubling to RETRY_MAX_S) and listed on
  /ops/status with its real error until it compresses;
- **recompresses** chunks with late rows (status "partial") once they have
  settled: no writes for BALER_HISTORY_RECOMPRESS_SETTLE_S (600 s), or
  partial for BALER_HISTORY_RECOMPRESS_MAX_DEFER_S (3600 s) whatever, so a
  chunk in the middle of a backfill isn't recompressed (and decompressed
  again) on every pass;
- **reclaims** the space after a recompression, or whenever a compressed
  chunk takes more than BALER_HISTORY_RECLAIM_RATIO (1.5) x its stats size
  on disk: VACUUM the chunk (truncates the emptied heap), REINDEX it
  CONCURRENTLY (the heap's indexes, empty but full-size; never the plain
  form on a chunk still taking writes, see _reindex), VACUUM its compressed relation, and
  VACUUM FULL that relation only if it is still over the ratio;
- reports what it found (on-disk bytes of the compressed chunks next to
  the stats figure, partial and failing chunks, the policy's last error)
  in its heartbeat, which /ops/status shows as the History compression card.

It never runs a compress or recompress while the policy job is running (the
two would queue on the same chunk locks), does at most
BALER_HISTORY_KEEPER_MAX_CHUNKS chunks per pass, oldest first, and holds
locks for at most LOCK_TIMEOUT before giving up until the next pass.

**Cost of a late-write storm.** Each chunk a late round touches costs, until
it is recompressed, its uncompressed size on disk (one late row per point
decompresses the whole chunk: about 16 GB for an hour at 100k points), and
its recompression costs a full compression of the chunk (about 470 s for
that hour on nash-srv, at about 150k rows/s). The policy for a storm: let
it settle (SETTLE_S), recompress each touched chunk once, oldest first and
at most MAX_CHUNKS per pass, and keep disk headroom for the uncompressed
size of every chunk a storm can touch (see docs-site history-compression).
VACUUM FULL takes an ACCESS EXCLUSIVE lock on the compressed relation (reads
of that chunk wait) for as long as it takes to copy the compressed data,
seconds for an hour of 100k points, and is only used when plain VACUUM
leaves the relation over the ratio.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg import sql

from baler import heartbeat, timeseries

log = logging.getLogger(__name__)

SERVICE = "history_keeper"


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, "").strip() or default)


INTERVAL_S = _env_float("BALER_HISTORY_KEEPER_INTERVAL_S", 300.0)
SETTLE_S = _env_float("BALER_HISTORY_RECOMPRESS_SETTLE_S", 600.0)
MAX_DEFER_S = _env_float("BALER_HISTORY_RECOMPRESS_MAX_DEFER_S", 3600.0)
RECLAIM_RATIO = _env_float("BALER_HISTORY_RECLAIM_RATIO", 1.5)
MAX_CHUNKS = int(_env_float("BALER_HISTORY_KEEPER_MAX_CHUNKS", 4))
RETRY_BASE_S = 300.0
RETRY_MAX_S = 6 * 3600.0
# Below this much over the stats figure, reclaiming isn't worth a VACUUM.
RECLAIM_SLACK_BYTES = 8 * 1024 * 1024
# A chunk reclaimed once is reclaimed again only after growing past this
# much of what the reclaim left.
RECLAIM_AGAIN = 1.25
EVENT_RETENTION = "30 days"
LOCK_TIMEOUT = "5s"
STATUS_LOCK_TIMEOUT = "1s"
# Chunk status bits (_timescaledb_functions.chunk_status).
COMPRESSED, PARTIAL = 1, 8

# Each point_history chunk from the catalog alone (no lock on the chunk, so
# a chunk someone holds an exclusive lock on doesn't stall the pass):
# status, the compression stats' figure, its write counter (inserts,
# updates and deletes since the stats reset: are late rows still
# arriving?) and its compressed relation (TimescaleDB 2.31 names it
# <chunk>_compressed). Bytes on disk come per chunk, after (_SIZE_SQL).
_CHUNKS_SQL = """
SELECT format('%I.%I', c.chunk_schema, c.chunk_name),
       c.range_end,
       _timescaledb_functions.chunk_status(format('%I.%I', c.chunk_schema, c.chunk_name)::regclass),
       s.after_compression_total_bytes,
       COALESCE(st.n_tup_ins + st.n_tup_upd + st.n_tup_del, 0),
       to_regclass(format('%I.%I', c.chunk_schema, c.chunk_name || '_compressed'))::text
FROM timescaledb_information.chunks c
LEFT JOIN chunk_compression_stats('point_history') s ON s.chunk_schema = c.chunk_schema AND s.chunk_name = c.chunk_name
LEFT JOIN pg_stat_user_tables st ON st.schemaname = c.chunk_schema AND st.relname = c.chunk_name
WHERE c.hypertable_schema = 'public' AND c.hypertable_name = 'point_history'
ORDER BY c.range_start
"""
# A chunk's bytes on disk: its heap, indexes and toast (where late rows go)
# plus its compressed relation's (what chunks_detailed_size totals).
_SIZE_SQL = "SELECT pg_total_relation_size(%s::regclass), COALESCE(pg_total_relation_size(%s::regclass), 0)"

_POLICY_SQL = """
SELECT j.job_id, (j.config->>'compress_after')::interval, j.schedule_interval, s.job_status,
       s.last_run_status, s.total_failures, j.scheduled
FROM timescaledb_information.jobs j
LEFT JOIN timescaledb_information.job_stats s ON s.job_id = j.job_id
WHERE j.proc_name = 'policy_compression' AND j.hypertable_schema = 'public' AND j.hypertable_name = 'point_history'
"""

# A failed policy run not yet copied. job_errors keeps only the run's final
# error ("columnstore policy failure ..."); the chunk's own one went to the
# server log as a WARNING, and the keeper's retry of that chunk records it.
_POLICY_ERRORS_SQL = """
INSERT INTO history_compression_events (at, chunk, action, ok, sqlstate, message)
SELECT e.finish_time, 'point_history', 'policy', false, e.sqlerrcode, e.err_message
FROM timescaledb_information.job_errors e
WHERE e.job_id = %s AND e.finish_time IS NOT NULL
  AND e.finish_time > COALESCE((SELECT max(at) FROM history_compression_events WHERE action = 'policy'), '-infinity')
ORDER BY e.finish_time
RETURNING at, sqlstate, message
"""

# Per chunk: failed attempts since its last success, and the newest
# failure. A chunk with failures here is "failing" until an attempt works.
_FAILING_SQL = """
WITH last_ok AS (
    SELECT chunk, max(at) AS at FROM history_compression_events
    WHERE ok AND action <> 'policy' GROUP BY chunk
), fails AS (
    SELECT e.* FROM history_compression_events e LEFT JOIN last_ok o ON o.chunk = e.chunk
    WHERE NOT e.ok AND e.action <> 'policy' AND (o.at IS NULL OR e.at > o.at)
      AND to_regclass(e.chunk) IS NOT NULL  -- a chunk dropped since isn't failing
)
SELECT DISTINCT ON (chunk) chunk, action, at, sqlstate, message, detail,
       (SELECT count(*) FROM fails f2 WHERE f2.chunk = fails.chunk)
FROM fails ORDER BY chunk, at DESC
"""

_RECORD_SQL = """
INSERT INTO history_compression_events
    (chunk, action, ok, sqlstate, message, detail, context, duration_s, disk_before, disk_after, stats_bytes)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


@dataclass
class Chunk:
    name: str
    range_end: datetime
    status: int
    disk_bytes: int
    heap_bytes: int
    stats_bytes: int | None
    writes: int
    compressed_rel: str | None = None

    @property
    def compressed(self) -> bool:
        return bool(self.status & COMPRESSED)

    @property
    def partial(self) -> bool:
        return bool(self.status & PARTIAL)

    def bloated(self, ratio: float = RECLAIM_RATIO) -> bool:
        """Fully compressed but taking more than `ratio` x its stats size
        on disk: dead heap, an empty but full-size index, or dead batches."""
        if not self.compressed or self.partial or not self.stats_bytes:
            return False
        return self.disk_bytes > ratio * self.stats_bytes + RECLAIM_SLACK_BYTES


@dataclass
class Failing:
    chunk: str
    action: str
    at: datetime
    sqlstate: str | None
    message: str | None
    detail: str | None
    attempts: int

    def next_try(self) -> datetime:
        return self.at + timedelta(seconds=min(RETRY_BASE_S * 2 ** max(self.attempts - 1, 0), RETRY_MAX_S))


@dataclass
class Policy:
    job_id: int | None = None
    compress_after: timedelta | None = None
    schedule: timedelta | None = None
    running: bool = False
    last_run_status: str | None = None
    total_failures: int = 0
    paused: bool = False  # alter_job(scheduled => false): compression paused on purpose


@dataclass
class Settle:
    """Per compressed chunk: when its write counter last moved (settled: no
    writes for SETTLE_S); per partial chunk, when it was first seen partial.
    In memory: a restart costs one settle period."""
    writes: dict[str, tuple[int, datetime]] = field(default_factory=dict)
    partial_since: dict[str, datetime] = field(default_factory=dict)

    def observe(self, chunks: list[Chunk], now: datetime) -> None:
        compressed, partial = set(), set()
        for c in chunks:
            if not c.compressed:
                continue
            compressed.add(c.name)
            if c.partial:
                partial.add(c.name)
                self.partial_since.setdefault(c.name, now)
            prev = self.writes.get(c.name)
            if prev is None or prev[0] != c.writes:
                self.writes[c.name] = (c.writes, now)
        for name in list(self.partial_since):
            if name not in partial:
                self.partial_since.pop(name)
        for name in list(self.writes):
            if name not in compressed:
                self.writes.pop(name)

    def settled(self, name: str, now: datetime, settle_s: float = SETTLE_S) -> bool:
        return self.quiet_s(name, now) >= settle_s

    def quiet_s(self, name: str, now: datetime) -> float:
        prev = self.writes.get(name)
        return (now - prev[1]).total_seconds() if prev else 0.0

    def partial_s(self, name: str, now: datetime) -> float:
        since = self.partial_since.get(name)
        return (now - since).total_seconds() if since else 0.0


def plan(chunks: list[Chunk], policy: Policy, failing: dict[str, Failing], settle: Settle, now: datetime,
         settle_s: float = SETTLE_S, max_defer_s: float = MAX_DEFER_S, max_chunks: int = MAX_CHUNKS,
         ratio: float = RECLAIM_RATIO, reclaimed: dict[str, int] | None = None) -> list[tuple[Chunk, str, str]]:
    """What this pass does, oldest chunk first: [(chunk, action, why)],
    action one of compress | recompress | reclaim. Nothing while the policy
    is paused (scheduled => false: someone stopped compression on purpose,
    a migration or a test). Pure (tested)."""
    out: list[tuple[Chunk, str, str]] = []
    if policy.paused:
        return out
    heavy = 0
    for c in chunks:
        f = failing.get(c.name)
        if f is not None and now < f.next_try():
            continue  # backing off after a failure
        if not c.compressed:
            if policy.running or policy.compress_after is None:
                continue
            due = now - policy.compress_after - (policy.schedule or timedelta(0))
            if c.range_end < due and heavy < max_chunks:
                why = "retry after a failure" if f else "the policy hasn't compressed it a schedule after it was due"
                out.append((c, "compress", why))
                heavy += 1
        elif c.partial:
            if policy.running or heavy >= max_chunks:
                continue
            quiet, waited = settle.quiet_s(c.name, now), settle.partial_s(c.name, now)
            if quiet >= settle_s:
                out.append((c, "recompress", f"late rows, no writes for {quiet:.0f} s"))
            elif waited >= max_defer_s:
                out.append((c, "recompress", f"late rows still arriving, partial for {waited:.0f} s"))
            else:
                continue
            heavy += 1
        elif c.bloated(ratio) and c.disk_bytes > RECLAIM_AGAIN * (reclaimed or {}).get(c.name, 0):
            # (not again while it's no bigger than the last reclaim left it:
            # what's left is real, e.g. backfilled rows the stats predate)
            out.append((c, "reclaim", f"{c.disk_bytes / 1e6:.1f} MB on disk vs {c.stats_bytes / 1e6:.1f} MB "
                                      "compressed (stats)" if c.stats_bytes else "bloated"))
    return out


# ------------------------------------------------------------------ the database side

def chunks(conn) -> list[Chunk]:
    """Every point_history chunk with its bytes on disk. A chunk whose size
    can't be read (an exclusive lock held past lock_timeout) is listed
    with 0 bytes rather than holding up the rest."""
    out = []
    for name, end, status, stats, writes, comp in conn.execute(_CHUNKS_SQL).fetchall():
        try:
            heap, comp_bytes = conn.execute(_SIZE_SQL, (name, comp)).fetchone()
        except psycopg.Error:
            heap = comp_bytes = 0
        out.append(Chunk(name, end, int(status or 0), int(heap or 0) + int(comp_bytes or 0), int(heap or 0),
                         int(stats) if stats is not None else None, int(writes), comp))
    return out


def policy(conn) -> Policy:
    row = conn.execute(_POLICY_SQL).fetchone()
    if row is None:
        return Policy()
    job_id, after, sched, status, last, failures, scheduled = row
    return Policy(job_id, after, sched, status == "Running", last, int(failures or 0), scheduled is False)


def failing(conn) -> dict[str, Failing]:
    return {r[0]: Failing(*r) for r in conn.execute(_FAILING_SQL).fetchall()}


def _one_chunk(conn, name: str) -> Chunk | None:
    """The chunk now, or None (gone, or its sizes can't be read: the same
    lock that failed the action blocks chunks_detailed_size too)."""
    try:
        for c in chunks(conn):
            if c.name == name:
                return c
    except psycopg.Error:
        pass
    return None


def _run(conn, stmts: list) -> None:
    for s in stmts:
        conn.execute(s)


# Indexes REINDEX CONCURRENTLY left behind when it failed part-way: the new
# copy (_ccnew) or the old one it hadn't dropped yet (_ccold), both invalid.
_LEFTOVER_INDEXES_SQL = """
SELECT format('%%I.%%I', n.nspname, i.relname)
FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_namespace n ON n.oid = i.relnamespace
WHERE x.indrelid = %s::regclass AND NOT x.indisvalid AND i.relname ~ '_cc(new|old)[0-9]*$'
"""


def _reindex(conn, c: Chunk, settled: bool) -> str:
    """Rebuild the chunk's indexes (empty once its rows are back in the
    columnstore, but still full-size): which form ran, for the event.

    CONCURRENTLY, so writes into the chunk carry on meanwhile. Plain
    REINDEX holds a SHARE lock that blocks them for its whole run, and the
    writes into these chunks are late rows from the listener, which acks a
    reading over MQTT only after commit: a stalled batch is broker
    backpressure, and then drops (#245's soak). If CONCURRENTLY fails, its
    leftover invalid index copies are dropped, and plain REINDEX runs only
    on a settled chunk (no writes for SETTLE_S), where it blocks nothing in
    practice; otherwise it is skipped until a later reclaim. Needs an
    autocommit connection (CONCURRENTLY can't run in a transaction block)."""
    rel = sql.SQL(c.name)  # format('%I.%I'): already quoted
    try:
        conn.execute(sql.SQL("REINDEX TABLE CONCURRENTLY {}").format(rel))
        return "concurrently"
    except psycopg.Error as exc:
        why = (exc.diag.message_primary or str(exc)).strip()
    for (idx,) in conn.execute(_LEFTOVER_INDEXES_SQL, (c.name,)).fetchall():
        conn.execute(sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}").format(sql.SQL(idx)))
    if not settled:
        log.warning("history_keeper: REINDEX CONCURRENTLY of %s failed (%s); chunk still taking writes, so no plain "
                    "REINDEX (it would block them): left for a later reclaim", c.name, why)
        return f"skipped (concurrently failed: {why}; chunk not settled)"
    conn.execute(sql.SQL("REINDEX TABLE {}").format(rel))
    log.warning("history_keeper: REINDEX CONCURRENTLY of %s failed (%s); ran plain REINDEX on the settled chunk",
                c.name, why)
    return f"plain (concurrently failed: {why})"


def attempt(conn, c: Chunk, action: str, ratio: float = RECLAIM_RATIO, settled: bool = False) -> dict:
    """One action on one chunk, recorded in history_compression_events
    either way. Never raises for a database error: it's the result.
    `settled`: the chunk had no writes for SETTLE_S before this pass (see
    _reindex). A successful reclaim's event carries the REINDEX form that
    ran in `message` ("reindex: concurrently")."""
    if not conn.autocommit:
        raise ValueError("history_keeper needs an autocommit connection (VACUUM, REINDEX CONCURRENTLY)")
    t0 = time.monotonic()
    if action == "compress":
        stmts: list = [sql.SQL("SELECT compress_chunk({}::regclass, if_not_compressed => true)").format(sql.Literal(c.name))]
    elif action == "recompress":
        stmts = [sql.SQL("SELECT compress_chunk({}::regclass)").format(sql.Literal(c.name))]
    else:
        stmts = []
    out: dict[str, Any] = {"chunk": c.name, "action": action, "ok": True, "disk_before": c.disk_bytes}
    try:
        _run(conn, stmts)
        if action in ("recompress", "reclaim"):
            # the heap the late rows went into, its indexes, the dead batches
            rel = sql.SQL(c.name)  # format('%I.%I'): already quoted
            conn.execute(sql.SQL("VACUUM {}").format(rel))
            out["reindex"] = _reindex(conn, c, settled)
            if c.compressed_rel:
                conn.execute(sql.SQL("VACUUM {}").format(sql.SQL(c.compressed_rel)))
            after = _one_chunk(conn, c.name)
            if after and after.compressed_rel and after.bloated(ratio):
                conn.execute(sql.SQL("VACUUM FULL {}").format(sql.SQL(after.compressed_rel)))
                out["vacuum_full"] = True
    except psycopg.Error as exc:
        d = exc.diag
        out.update(ok=False, sqlstate=d.sqlstate, message=d.message_primary or str(exc).strip(),
                   detail=d.message_detail, context=d.context)
    out["duration_s"] = round(time.monotonic() - t0, 3)
    after = _one_chunk(conn, c.name)
    out["disk_after"] = after.disk_bytes if after else None
    out["stats_bytes"] = after.stats_bytes if after else c.stats_bytes
    try:
        message = out.get("message") or (f"reindex: {out['reindex']}" if out.get("reindex") else None)
        conn.execute(_RECORD_SQL, (c.name, action, out["ok"], out.get("sqlstate"), message,
                                   out.get("detail"), out.get("context"), out["duration_s"], out["disk_before"],
                                   out["disk_after"], out["stats_bytes"]))
    except psycopg.Error as exc:  # the action itself is what matters
        log.warning("history_keeper: couldn't record %s of %s: %s", action, c.name, exc)
    if out["ok"]:
        log.info("history_keeper: %s %s in %.1f s: %.1f MB -> %s MB on disk (stats %s MB)", action, c.name,
                 out["duration_s"], c.disk_bytes / 1e6,
                 f"{out['disk_after'] / 1e6:.1f}" if out["disk_after"] is not None else "?",
                 f"{out['stats_bytes'] / 1e6:.1f}" if out["stats_bytes"] else "?")
    else:
        log.error("history_keeper: %s %s FAILED (SQLSTATE %s): %s%s%s; retried with backoff", action, c.name,
                  out.get("sqlstate"), out.get("message"), f" DETAIL: {out['detail']}" if out.get("detail") else "",
                  f" CONTEXT: {out['context']}" if out.get("context") else "")
    return out


def summary(cs: list[Chunk], pol: Policy, fails: dict[str, Failing], settle: Settle, now: datetime) -> dict:
    """The heartbeat's picture: the compressed chunks' true bytes on disk
    next to the stats' figure, partial and failing chunks, the policy."""
    comp = [c for c in cs if c.compressed]
    on_disk = sum(c.disk_bytes for c in comp)
    stats = sum(c.stats_bytes or 0 for c in comp)
    return {
        "chunks": {"total": len(cs), "compressed": len(comp), "partial": sum(1 for c in comp if c.partial),
                   "hot": len(cs) - len(comp)},
        "compressed_on_disk_bytes": on_disk,
        "compressed_stats_bytes": stats,
        "on_disk_vs_stats": round(on_disk / stats, 2) if stats else None,
        "hot_bytes": sum(c.disk_bytes for c in cs if not c.compressed),
        "partial": [{"chunk": c.name, "partial_s": round(settle.partial_s(c.name, now)),
                     "quiet_s": round(settle.quiet_s(c.name, now)), "disk_bytes": c.disk_bytes,
                     "stats_bytes": c.stats_bytes} for c in comp if c.partial][:20],
        "failing": [{"chunk": f.chunk, "action": f.action, "attempts": f.attempts, "at": f.at.isoformat(),
                     "next_try_at": f.next_try().isoformat(), "sqlstate": f.sqlstate, "message": f.message,
                     "detail": f.detail} for f in fails.values()][:20],
        "policy": {"job_id": pol.job_id, "running": pol.running, "paused": pol.paused,
                   "last_run_status": pol.last_run_status, "total_failures": pol.total_failures},
    }


class Keeper:
    def __init__(self, conn, settle_s: float = SETTLE_S, max_defer_s: float = MAX_DEFER_S,
                 max_chunks: int = MAX_CHUNKS, ratio: float = RECLAIM_RATIO, status: heartbeat.Status | None = None):
        self.conn = conn
        self.settle_s, self.max_defer_s, self.max_chunks, self.ratio = settle_s, max_defer_s, max_chunks, ratio
        self.settle = Settle()
        self.reclaimed: dict[str, int] = {}  # chunk -> bytes on disk after its last recompress/reclaim
        self.status = status or heartbeat.Status(SERVICE, interval_s=INTERVAL_S, settle_s=settle_s,
                                                 max_defer_s=max_defer_s, reclaim_ratio=ratio)
        self.passes = 0

    def policy_errors(self, pol: Policy) -> list[tuple]:
        if pol.job_id is None:
            return []
        rows = self.conn.execute(_POLICY_ERRORS_SQL, (pol.job_id,)).fetchall()
        for at, state, msg in rows:
            log.error("history_keeper: the compression policy (job %s) failed at %s (SQLSTATE %s): %s. Each chunk's "
                      "own error is in the Postgres log as a WARNING \"converting chunk ... failed\"; the keeper "
                      "retries the chunk and records its error in history_compression_events", pol.job_id, at,
                      state, msg)
        return rows

    def run_pass(self, now: datetime | None = None, only: set[str] | None = None,
                 respect_pause: bool = True) -> dict:
        """One pass. `only`: act on just these chunks (tests, and an
        operator's `--chunk`); `respect_pause=False` acts even while the
        policy is paused (tests pause it to keep it off their chunks)."""
        t0 = time.monotonic()
        now = now or datetime.now(timezone.utc)
        pol = policy(self.conn)
        new_policy_errors = self.policy_errors(pol)
        cs = chunks(self.conn)
        work = [c for c in cs if only is None or c.name in only]
        plan_pol = pol if respect_pause else Policy(**{**pol.__dict__, "paused": False})
        self.settle.observe(cs, now)
        fails = failing(self.conn)
        done = []
        for c, action, why in plan(work, plan_pol, fails, self.settle, now, self.settle_s, self.max_defer_s,
                                   self.max_chunks, self.ratio, self.reclaimed):
            self.status.update("working_on", {"chunk": c.name, "action": action, "why": why,
                                              "since": datetime.now(timezone.utc).isoformat()})
            result = attempt(self.conn, c, action, self.ratio, self.settle.settled(c.name, now, self.settle_s))
            if result["ok"] and action in ("recompress", "reclaim") and result.get("disk_after") is not None:
                self.reclaimed[c.name] = result["disk_after"]
            done.append(result)
        self.status.update("working_on", None)
        if done:
            try:
                cs = chunks(self.conn)
            except psycopg.Error:
                pass  # this pass's own reading stands
            fails = failing(self.conn)
        self.conn.execute(f"DELETE FROM history_compression_events WHERE at < now() - interval '{EVENT_RETENTION}'")
        last_policy_error = self.conn.execute(
            "SELECT at, sqlstate, message FROM history_compression_events WHERE action = 'policy' "
            "ORDER BY at DESC LIMIT 1").fetchone()
        self.passes += 1
        out = {**summary(cs, pol, fails, self.settle, now), "at": now.isoformat(), "passes": self.passes,
               "pass_s": round(time.monotonic() - t0, 3),
               "done": [{k: v for k, v in d.items() if k != "context"} for d in done],
               "policy_errors_new": len(new_policy_errors)}
        if last_policy_error:
            out["policy"]["last_error"] = {"at": last_policy_error[0].isoformat(), "sqlstate": last_policy_error[1],
                                           "message": last_policy_error[2]}
        for k, v in out.items():
            self.status.update(k, v)
        return out


def compression_status(conn) -> dict:
    """History compression as /ops/status and the scale harness read it,
    straight from the database (no keeper needed): the true on-disk bytes
    of the compressed chunks next to the stats figure, and failing chunks
    with their real errors. {} without TimescaleDB or point_history. Sets
    the session's lock_timeout to STATUS_LOCK_TIMEOUT."""
    try:
        # A chunk's size waits on an exclusive lock; never hold Ops up behind one.
        conn.execute("SELECT set_config('lock_timeout', %s, false)", (STATUS_LOCK_TIMEOUT,))
        cs, pol = chunks(conn), policy(conn)
    except psycopg.Error:
        conn.rollback()
        return {}
    try:
        fails = failing(conn)
    except psycopg.Error:  # before migration 0027
        conn.rollback()
        fails = {}
    return summary(cs, pol, fails, Settle(), datetime.now(timezone.utc))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m baler.history_keeper", description=__doc__.split("\n\n")[0])
    p.add_argument("--interval", type=float, default=INTERVAL_S, help="seconds between passes")
    p.add_argument("--once", action="store_true", help="one pass, print it, exit")
    p.add_argument("--chunk", action="append", help="with --once: act on just this chunk (schema.name); repeatable")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from baler import (
        tracing,  # also records process starts and unhandled exceptions (#277)
    )

    tracing.init_tracing("baler-history-keeper")
    status = heartbeat.Status(SERVICE, interval_s=args.interval, settle_s=SETTLE_S, max_defer_s=MAX_DEFER_S,
                              reclaim_ratio=RECLAIM_RATIO)
    if not args.once:
        # Its own connection and thread: a recompression of a large chunk
        # runs for minutes, longer than Ops' heartbeat staleness.
        threading.Thread(target=_beat_forever, args=(status,), name="heartbeat", daemon=True).start()
    while True:
        try:
            conn = timeseries.connect()
            conn.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
            keeper = Keeper(conn, status=status)
            while True:
                try:
                    out = keeper.run_pass(only=set(args.chunk) if args.once and args.chunk else None)
                except psycopg.Error as exc:
                    if conn.closed:
                        raise
                    # e.g. a catalog read that hit lock_timeout behind DDL
                    log.warning("history_keeper: pass skipped (%s); next in %.0f s", exc, args.interval)
                    status.update("last_pass_error", str(exc).strip()[:300])
                    out = {"error": str(exc)}
                if args.once:
                    status.beat(conn)
                    print(json.dumps(out, indent=2, default=str))
                    return
                time.sleep(args.interval)
        except psycopg.Error as exc:
            log.warning("history_keeper: lost Postgres (%s); reconnecting in 10 s", exc)
            time.sleep(10)


BEAT_S = 30.0


def _beat_forever(status: heartbeat.Status) -> None:
    conn = None
    while True:
        try:
            if conn is None or conn.closed:
                conn = psycopg.connect(timeseries.DEFAULT_DSN, autocommit=True)
            status.beat(conn)
        except psycopg.Error:
            conn = None
        time.sleep(BEAT_S)

if __name__ == "__main__":
    main()

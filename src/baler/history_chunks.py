"""
point_history's chunk interval and compression schedule (#245): settings,
not schema, kept in step with the environment the way compress_after is
(timeseries.sync_settings, once per process per database).

TimescaleDB stores point_history in time chunks and the compression policy
compresses a chunk once all of it is older than compress_after. Three
numbers decide how much uncompressed ("hot") history sits on disk:

    hot window ~= compress_after + one chunk interval + one policy run apart

Each is a setting:

- BALER_HISTORY_CHUNK_INTERVAL: blank (the default) leaves the hypertable's
  interval as it is: 7 days, TimescaleDB's default, on every install made by
  Baler so far. Set it (any Postgres interval) and new chunks are that long.
  Existing chunks are never rewritten: set_chunk_time_interval only applies
  to chunks created after it, so the chunk open at the time keeps its range
  and the new length starts at its end.
- BALER_HISTORY_COMPRESS_AFTER (baler.timeseries): when a chunk compresses.
- BALER_HISTORY_COMPRESS_SCHEDULE: how often the policy job runs. Blank:
  TimescaleDB's own rule for a new policy, applied to the configured chunk
  interval (half a chunk below 1 day, else 12 hours), and only once
  BALER_HISTORY_CHUNK_INTERVAL is set; otherwise left as it is (12 hours
  on every install so far). Without this a policy created under 7-day chunks
  would keep running every 12 hours after the chunks became 1 hour long, and
  the hot window would grow by up to 12 hours.

So a small site that sets nothing behaves exactly as before: 7-day chunks,
compressed after 7 days, the policy every 12 hours. `recommend()` (and
`python -m baler.history_chunks recommend`) gives the settings for a
higher ingest rate from the sizing rule in #245: one chunk plus its indexes
in about 25% of Postgres' memory, compress_after a few chunks, and the
policy every half chunk.

Raw history is compressed, never downsampled, whatever these are.
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass

import psycopg
from psycopg import sql

from baler import ddl

log = logging.getLogger(__name__)

CHUNK_INTERVAL = os.environ.get("BALER_HISTORY_CHUNK_INTERVAL", "").strip()
COMPRESS_SCHEDULE = os.environ.get("BALER_HISTORY_COMPRESS_SCHEDULE", "").strip()

# One catalog read: the hypertable's interval and the policy job's schedule,
# against what the settings want (NULL: leave it as it is). The derived
# schedule is TimescaleDB's rule for a new compression policy, so a policy
# that timeseries._ensure_compression replaces comes back already matching.
_STATE_SQL = """
WITH want AS (SELECT %(chunk)s::interval AS chunk, %(schedule)s::interval AS schedule)
SELECT d.time_interval,
       w.chunk,
       j.job_id,
       j.schedule_interval,
       COALESCE(w.schedule, CASE WHEN w.chunk IS NULL THEN NULL
                                 WHEN w.chunk < interval '1 day' THEN w.chunk / 2
                                 ELSE interval '12 hours' END)
FROM want w
JOIN timescaledb_information.dimensions d ON d.hypertable_name = 'point_history' AND d.column_name = 'ts'
LEFT JOIN timescaledb_information.jobs j
       ON j.proc_name = 'policy_compression' AND j.hypertable_name = 'point_history'
"""
# Same as the compression policy's change: never queue writes behind it.
_LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"


def ensure(conn: psycopg.Connection, chunk: str | None = None, schedule: str | None = None) -> list[str]:
    """Bring point_history's chunk interval and the compression policy's
    schedule to the settings: a no-op catalog read when they already are.
    Returns what it changed. Never raises: ingest matters more."""
    chunk = CHUNK_INTERVAL if chunk is None else chunk
    schedule = COMPRESS_SCHEDULE if schedule is None else schedule
    if not chunk and not schedule:
        return []
    try:
        row = conn.execute(_STATE_SQL, {"chunk": chunk or None, "schedule": schedule or None}).fetchone()
    except (psycopg.errors.InvalidDatetimeFormat, psycopg.errors.InvalidParameterValue,
            psycopg.errors.DatetimeFieldOverflow, psycopg.errors.IntervalFieldOverflow):
        log.error("BALER_HISTORY_CHUNK_INTERVAL=%r / BALER_HISTORY_COMPRESS_SCHEDULE=%r: not a Postgres "
                  "interval; point_history chunking left as it is", chunk, schedule)
        return []
    except psycopg.errors.UndefinedTable:  # no TimescaleDB catalog
        return []
    if row is None:  # no point_history hypertable
        return []
    have_chunk, want_chunk, job_id, have_schedule, want_schedule = row
    stmts: list[str | sql.Composable] = []
    changed = []
    if want_chunk is not None and have_chunk != want_chunk:
        stmts.append(sql.SQL("SELECT set_chunk_time_interval('point_history', {}::interval)").format(sql.Literal(want_chunk)))
        changed.append(f"chunk interval {have_chunk} -> {want_chunk} (new chunks only)")
    if job_id is not None and want_schedule is not None and have_schedule != want_schedule:
        stmts.append(sql.SQL("SELECT alter_job({}, schedule_interval => {}::interval)").format(
            sql.Literal(job_id), sql.Literal(want_schedule)))
        changed.append(f"compression policy every {have_schedule} -> {want_schedule}")
    if not stmts:
        return []
    try:
        ddl.run(conn, _LOCK_TIMEOUT, *stmts)
    except psycopg.Error as exc:
        log.warning("point_history chunking left as it is this time (retried at the next process start): %s", exc)
        return []
    for c in changed:
        log.info("point_history: %s", c)
    return changed


# ------------------------------------------------------------------ sizing

# Chunk lengths recommend() picks from. Nothing below 1 hour: shorter chunks
# multiply the chunk count (8,760 a year at 1 hour already), which every
# query plan pays for; past that the row itself has to get narrower (#245's
# narrow-row step), not the chunks shorter.
CANDIDATES_S = (3600, 2 * 3600, 4 * 3600, 6 * 3600, 12 * 3600, 86400, 7 * 86400)
DEFAULT_CHUNK_S = 7 * 86400
# Uncompressed bytes per sample, table plus indexes: 228-265 measured by the
# scale ladder (#245), 250 on nash-srv.
BYTES_PER_ROW = 250
MEMORY_SHARE = 0.25


def _interval(seconds: int) -> str:
    for unit, n in (("days", 86400), ("hours", 3600), ("minutes", 60)):
        if seconds % n == 0:
            v = seconds // n
            return f"{v} {unit[:-1] if v == 1 else unit}"
    return f"{seconds} seconds"


@dataclass(frozen=True)
class Recommendation:
    rows_per_s: float
    memory_bytes: int
    chunk_s: int
    compress_after_s: int
    schedule_s: int
    chunk_bytes: int
    hot_bytes_max: int
    notes: tuple[str, ...]

    @property
    def is_default(self) -> bool:
        return self.chunk_s == DEFAULT_CHUNK_S

    def env(self) -> dict[str, str]:
        """The .env lines; blank where the shipped default already fits."""
        if self.is_default:
            return {"BALER_HISTORY_CHUNK_INTERVAL": "", "BALER_HISTORY_COMPRESS_AFTER": "",
                    "BALER_HISTORY_COMPRESS_SCHEDULE": ""}
        return {"BALER_HISTORY_CHUNK_INTERVAL": _interval(self.chunk_s),
                "BALER_HISTORY_COMPRESS_AFTER": _interval(self.compress_after_s),
                "BALER_HISTORY_COMPRESS_SCHEDULE": ""}


def recommend(rows_per_s: float, memory_bytes: int, bytes_per_row: float = BYTES_PER_ROW) -> Recommendation:
    """Settings for an ingest rate: the longest candidate chunk that, with
    its indexes, fits MEMORY_SHARE of Postgres' memory; compress_after three
    chunks (7 days, today's default, while chunks stay 7 days); the policy
    every half chunk (TimescaleDB's rule, 12 hours from 1-day chunks)."""
    budget = memory_bytes * MEMORY_SHARE
    fits = [c for c in CANDIDATES_S if rows_per_s * c * bytes_per_row <= budget]
    notes = []
    chunk = max(fits) if fits else CANDIDATES_S[0]
    if not fits:
        notes.append(f"even a 1-hour chunk ({rows_per_s * 3600 * bytes_per_row / 1e9:.1f} GB) is over "
                     f"{MEMORY_SHARE:.0%} of {memory_bytes / 1e9:.1f} GB: more memory for Postgres, or a narrower "
                     "row (#245), before shorter chunks")
    if chunk == DEFAULT_CHUNK_S:
        after, schedule = DEFAULT_CHUNK_S, 12 * 3600
        notes.append("the shipped defaults fit: leave the settings blank")
    else:
        after = 3 * chunk
        schedule = chunk // 2 if chunk < 86400 else 12 * 3600
        if after < 4 * 3600:
            notes.append("trend backfill hours late and spool replays will land in compressed chunks: "
                         "that works (the slower write path, see history-compression), it just costs more per row")
    chunk_bytes = int(rows_per_s * chunk * bytes_per_row)
    hot = int(rows_per_s * (after + chunk + schedule) * bytes_per_row)
    return Recommendation(rows_per_s, memory_bytes, chunk, after, schedule, chunk_bytes, hot, tuple(notes))


# ------------------------------------------------------------------ status

_STATUS_SQL = """
SELECT d.time_interval,
       (SELECT config->>'compress_after' FROM timescaledb_information.jobs
        WHERE proc_name = 'policy_compression' AND hypertable_name = 'point_history'),
       (SELECT schedule_interval FROM timescaledb_information.jobs
        WHERE proc_name = 'policy_compression' AND hypertable_name = 'point_history'),
       (SELECT max(range_end) FROM timescaledb_information.chunks WHERE hypertable_name = 'point_history'),
       (SELECT extract(epoch FROM now() - min(range_end)) FROM timescaledb_information.chunks
        WHERE hypertable_name = 'point_history' AND NOT is_compressed),
       (SELECT count(*) FROM timescaledb_information.chunks WHERE hypertable_name = 'point_history'),
       (SELECT count(*) FILTER (WHERE is_compressed) FROM timescaledb_information.chunks
        WHERE hypertable_name = 'point_history')
FROM timescaledb_information.dimensions d
WHERE d.hypertable_name = 'point_history' AND d.column_name = 'ts'
"""


def _memory_bytes(text: str | None) -> int:
    if text:
        t = text.strip().lower().removesuffix("b")
        mult = {"k": 1e3, "m": 1e6, "g": 1e9, "t": 1e12}.get(t[-1:], 1)
        return int(float(t.rstrip("kmgt")) * mult)
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 8 * 10**9


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m baler.history_chunks",
                                description="point_history chunk interval and compression settings (#245)")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("recommend", help="settings for an ingest rate")
    rate = r.add_mutually_exclusive_group(required=True)
    rate.add_argument("--rows-per-sec", type=float)
    rate.add_argument("--points", type=int, help="with --interval: points each reporting every --interval s")
    r.add_argument("--interval", type=float, default=5.0, help="seconds between a point's readings (default 5)")
    r.add_argument("--pg-memory", help="memory Postgres has, e.g. 16g (default: this host's)")
    r.add_argument("--bytes-per-row", type=float, default=BYTES_PER_ROW)
    s = sub.add_parser("status", help="the database's chunking and compression now")
    s.add_argument("--dsn", default=os.environ.get("BALER_PG_DSN", "postgresql://baler:baler@localhost:5433/baler"))
    args = p.parse_args(argv)
    if args.cmd == "recommend":
        rows = args.rows_per_sec if args.rows_per_sec is not None else args.points / args.interval
        rec = recommend(rows, _memory_bytes(args.pg_memory), args.bytes_per_row)
        print(f"# {rows:,.0f} rows/s, {rec.memory_bytes / 1e9:.1f} GB for Postgres, {args.bytes_per_row:g} B/row raw")
        print(f"# chunk {_interval(rec.chunk_s)} ~{rec.chunk_bytes / 1e9:.2f} GB raw; compress after "
              f"{_interval(rec.compress_after_s)}; policy every {_interval(rec.schedule_s)}; "
              f"hot window up to ~{rec.hot_bytes_max / 1e9:.1f} GB raw")
        for n in rec.notes:
            print(f"# note: {n}")
        for k, v in rec.env().items():
            print(f"{k}={v}")
        return 0
    with psycopg.connect(args.dsn, autocommit=True) as conn:
        row = conn.execute(_STATUS_SQL).fetchone()
        if row is None:
            print("no point_history hypertable")
            return 1
        interval, after, schedule, newest_end, oldest_hot_age, chunks, compressed = row
        print(f"chunk interval      {interval}  (BALER_HISTORY_CHUNK_INTERVAL here: {CHUNK_INTERVAL or 'blank'})")
        print(f"compress_after      {after}")
        print(f"policy schedule     {schedule}  (BALER_HISTORY_COMPRESS_SCHEDULE here: {COMPRESS_SCHEDULE or 'blank'})")
        print(f"chunks              {chunks}, {compressed} compressed; newest ends {newest_end}")
        if oldest_hot_age is None:
            print("every chunk is compressed")
        elif oldest_hot_age < 0:
            print("oldest uncompressed chunk is the open one")
        else:
            print(f"oldest uncompressed chunk ended {_interval(int(oldest_hot_age))} ago")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

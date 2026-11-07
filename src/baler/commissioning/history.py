"""
The one seam between commissioning logic and where point history lives
(the idea PR #5 got right). Checks only ever need "latest sample per
point" and "samples over a window", so that is the whole interface:
PostgresHistory reads point_history through timeseries' shared batch
helpers (no private copies), InMemoryHistory makes every check
unit-testable with hand-built series.

`latest` is the newest sample that carries a value: a bridge that
publishes null while it can't reach the device (Baler#83/#85) must not
make a dead point look like it is reporting. `latest_raw` is the newest
row of any kind, so the existence check can tell "silent" from "answering
with empty readings". Both take `before` (latest as of a past moment).

`window` reads at most `per_point_limit` samples per point, oldest
first. A caller that would otherwise judge a silent prefix of a long
window passes a set as `truncated` and gets the ids of every point that
had more (Baler#88).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

import psycopg

from baler import timeseries

Sample = tuple[datetime, object]

# How far back `latest` looks for a sample with a value. Unbounded, a point
# with nothing but empty rows would walk its whole history on every check;
# a real value older than this still shows up through latest_raw.
VALUE_LOOKBACK = timedelta(days=31)


class History(Protocol):
    def latest(self, point_uris: list[str], before: datetime | None = None) -> dict[str, Sample]: ...

    def latest_raw(self, point_uris: list[str], before: datetime | None = None) -> dict[str, Sample]: ...

    def window(self, point_uris: list[str], start: datetime, end: datetime,
               truncated: set[str] | None = None) -> dict[str, list[Sample]]: ...

    def availability(self, point_uris: list[str], start: datetime, end: datetime,
                     max_gap: timedelta) -> dict[str, tuple[int, float]]: ...


def coverage_pct(samples: int, covered: float, first: datetime | None, start: datetime, end: datetime,
                 max_gap: timedelta) -> float:
    """procedures.availability_pct from its running parts: `covered` = the
    sum over samples of min(time to the next sample or `end`, max_gap)."""
    total = (end - start).total_seconds()
    if total <= 0 or not samples or first is None:
        return 0.0
    covered += min((first - start).total_seconds(), max_gap.total_seconds()) if first > start else 0
    return round(100.0 * min(covered, total) / total, 2)


# Per point, the parts coverage_pct needs over [start, end), folded in
# Postgres from one ordered primary-key range scan. (The report used to
# fetch every raw sample - 1.9M rows for 380 points over a day - and fold
# them in Python: ~4.5 s, Baler#150.) date_part, not EXTRACT: float8
# arithmetic rather than numeric. {valued} is the "has a value" filter, or
# nothing: see _availability_split.
_AVAILABILITY_SQL = """
SELECT u.uri, a.n, a.covered, a.first
FROM unnest(%(uris)s::text[]) AS u(uri)
CROSS JOIN LATERAL (
    SELECT count(*) AS n, min(ts) AS first,
           sum(LEAST(date_part('epoch', COALESCE(nxt, %(end)s)) - date_part('epoch', ts), %(gap)s)) AS covered
    FROM (SELECT ts, lead(ts) OVER (ORDER BY ts) AS nxt FROM point_history
          WHERE point_uri = u.uri AND ts >= %(start)s AND ts < %(end)s{valued}) w
) a
"""
_EMPTY = "value IS NULL AND value_text IS NULL AND value_bool IS NULL"
_VALUED_SQL = _AVAILABILITY_SQL.format(valued=f" AND NOT ({_EMPTY})")
# Without the value test the scan reads only (point_uri, ts): an index-only
# scan of the primary key, no heap. Live rows of one point are spread one
# per heap page or so (every point's sample for an instant is written
# together), so the heap scan touched ~46k pages per point-week against
# ~1k index pages - the difference between a 3 s and a 10 s report once
# the cache is cold after a deploy (#150).
_INDEX_ONLY_SQL = _AVAILABILITY_SQL.format(valued="")

# Rows with no value at all (a bridge that publishes null while it can't
# reach the device, Baler#85), indexed on their own: few, so the index is
# small, and it says which points the index-only scan above can't judge.
# Built by ensure_empty_rows_index (commissioning_api, in the background);
# until it is there and valid every point takes the heap scan.
EMPTY_ROWS_INDEX = "point_history_empty"
_EMPTY_ROWS_DDL = (f"CREATE INDEX IF NOT EXISTS {EMPTY_ROWS_INDEX} ON point_history (point_uri, ts) "
                   f"WITH (timescaledb.transaction_per_chunk) WHERE {_EMPTY}")
_EMPTY_ROWS_VALID = f"SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('{EMPTY_ROWS_INDEX}')"
_EMPTY_POINTS_SQL = (f"SELECT DISTINCT point_uri FROM point_history WHERE point_uri = ANY(%(uris)s) "
                     f"AND ts >= %(start)s AND ts < %(end)s AND {_EMPTY}")
_EMPTY_ROWS_LOCK = 0x62616C66  # one builder at a time (ddl.LOCK_KEY + 1)


def ensure_empty_rows_index(conn) -> str:
    """Build EMPTY_ROWS_INDEX if it's missing, or rebuild it if a build was
    cut short (TimescaleDB leaves a per-chunk build's index invalid). One
    chunk per transaction, so ingest waits for one chunk's build at most,
    never the whole table's. `conn` must be in autocommit mode. Returns
    what it did."""
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (_EMPTY_ROWS_LOCK,)).fetchone()[0]:
        return "another process is building it"
    try:
        row = conn.execute(_EMPTY_ROWS_VALID).fetchone()
        if row and row[0]:
            return "present"
        if row:  # there, but invalid
            conn.execute(f"DROP INDEX IF EXISTS {EMPTY_ROWS_INDEX}")
        conn.execute(_EMPTY_ROWS_DDL)
        return "built"
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (_EMPTY_ROWS_LOCK,))


class PostgresHistory:
    def __init__(self, conn, per_point_limit: int = 5000):
        self.conn = conn
        self.per_point_limit = per_point_limit

    def latest(self, point_uris: list[str], before: datetime | None = None) -> dict[str, Sample]:
        since = (before or datetime.now().astimezone()) - VALUE_LOOKBACK
        return timeseries.read_latest_many(self.conn, list(point_uris), before=before, since=since, with_value=True)

    def latest_raw(self, point_uris: list[str], before: datetime | None = None) -> dict[str, Sample]:
        return timeseries.read_latest_many(self.conn, list(point_uris), before=before)

    def window(self, point_uris: list[str], start: datetime, end: datetime,
               truncated: set[str] | None = None) -> dict[str, list[Sample]]:
        raw = timeseries.read_range_many(self.conn, list(point_uris), start, end, self.per_point_limit)
        if truncated is not None:
            truncated.update(uri for uri, (_rows, cut) in raw.items() if cut)
        return {uri: rows for uri, (rows, _cut) in raw.items()}

    def availability(self, point_uris: list[str], start: datetime, end: datetime,
                     max_gap: timedelta) -> dict[str, tuple[int, float]]:
        """(samples with a value, procedures.availability_pct) per point over
        [start, end), every sample counted (no per_point_limit)."""
        if not point_uris:
            return {}
        params = {"uris": list(point_uris), "start": start, "end": end, "gap": max_gap.total_seconds()}
        outermost = self.conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        with self.conn.transaction():  # one snapshot: which points have empty rows, and the scans
            if outermost:  # inside a caller's transaction, its snapshot rules apply
                self.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            row = self.conn.execute(_EMPTY_ROWS_VALID).fetchone()
            if row and row[0]:
                empty = {u for (u,) in self.conn.execute(_EMPTY_POINTS_SQL, params)}
                rows = self.conn.execute(_INDEX_ONLY_SQL, {**params, "uris": [u for u in point_uris if u not in empty]}).fetchall()
                if empty:
                    rows += self.conn.execute(_VALUED_SQL, {**params, "uris": sorted(empty)}).fetchall()
            else:
                rows = self.conn.execute(_VALUED_SQL, params).fetchall()
        return {uri: (n, coverage_pct(n, float(covered or 0.0), first, start, end, max_gap)) for uri, n, covered, first in rows}


class InMemoryHistory:
    """`per_point_limit` mirrors PostgresHistory's truncation (oldest
    first) so tests can exercise it; None means unlimited."""

    def __init__(self, series: dict[str, list[Sample]] | None = None, per_point_limit: int | None = None):
        self.series: dict[str, list[Sample]] = {k: sorted(v, key=lambda s: s[0]) for k, v in (series or {}).items()}
        self.per_point_limit = per_point_limit

    def add(self, point_uri: str, ts: datetime, value) -> None:
        self.series.setdefault(point_uri, []).append((ts, value))
        self.series[point_uri].sort(key=lambda s: s[0])

    def _upto(self, uri: str, before: datetime | None) -> list[Sample]:
        return [s for s in self.series.get(uri, []) if before is None or s[0] <= before]

    def latest(self, point_uris: list[str], before: datetime | None = None) -> dict[str, Sample]:
        out = {}
        for u in point_uris:
            rows = self._upto(u, before)
            floor = (before or (rows[-1][0] if rows else None))
            valued = [s for s in rows if s[1] is not None and (floor is None or s[0] >= floor - VALUE_LOOKBACK)]
            if valued:
                out[u] = valued[-1]
        return out

    def latest_raw(self, point_uris: list[str], before: datetime | None = None) -> dict[str, Sample]:
        out = {}
        for u in point_uris:
            rows = self._upto(u, before)
            if rows:
                out[u] = rows[-1]
        return out

    def availability(self, point_uris: list[str], start: datetime, end: datetime,
                     max_gap: timedelta) -> dict[str, tuple[int, float]]:
        from baler.commissioning.procedures import availability_pct

        out = {}
        for u in point_uris:
            times = [t for t, v in self.series.get(u, []) if v is not None and start <= t < end]
            out[u] = (len(times), availability_pct(times, start, end, max_gap))
        return out

    def window(self, point_uris: list[str], start: datetime, end: datetime,
               truncated: set[str] | None = None) -> dict[str, list[Sample]]:
        out = {}
        for u in point_uris:
            rows = [(t, v) for t, v in self.series.get(u, []) if start <= t < end]
            if self.per_point_limit is not None and len(rows) > self.per_point_limit:
                rows = rows[:self.per_point_limit]
                if truncated is not None:
                    truncated.add(u)
            out[u] = rows
        return out

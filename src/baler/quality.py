"""
Per-sample quality flags (Baler#183): flag a bad value, never rewrite it.

Each point_history row has a `quality` column. NULL means ok, so a healthy
site's history costs nothing extra. Otherwise it holds one of FLAGS:

  fault           the device says the value is unreliable (BACnet
                  status-flags FAULT, or a `reliability` other than
                  no-fault-detected)
  overridden      the device says someone overrode it at the panel
  out_of_service  the device says the object is out of service
  out_of_range    outside the plausible range for the point's Brick class
                  and unit (RANGES)
  spike           an isolated jump: further than SPIKE_K scaled MADs from
                  the median of its neighbours on both sides
  unit_suspect    the values fit another unit than the one the point
                  claims (21 "°F" in a zone); a fix is offered as a
                  suggestion (quality_suggestions), never applied

Stale and stuck stay derived (his_checks), not stored: they are facts
about a series, not about one sample.

Two sources fill the flags:
  * the reading envelope: `quality` on an MQTT reading or POST /ingest,
    either a flag name or BACnet's own {"status_flags": [...],
    "reliability": "..."} (from_envelope). Windrow will send it once
    Windrow#20 lands; an envelope without it is ok, as before.
  * Baler's own checks: range, spike and unit suspicion, run by
    fault_detector's quality thread (Sweeper, #268) over what is new since
    its watermark, plus late rows it finds in the reading feed. A pass
    only fills a NULL - a device's own flag always wins - and never
    touches a value.

Consumers skip flagged samples (fault rules, Cx) or show the flag (the his
APIs). `GOOD_SQL` is the filter they share.
"""

from __future__ import annotations

import functools
import json
import os
import statistics
import time
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from baler import reading_feed, schema, timeseries

FLAGS = ("ok", "fault", "overridden", "out_of_service", "out_of_range", "spike", "unit_suspect")
DEVICE_FLAGS = ("fault", "overridden", "out_of_service")

# A sample that consumers treat as good. NULL is the stored form of ok.
GOOD_SQL = "(quality IS NULL OR quality = 'ok')"

# Schema (quality_suggestions): baler/migrations/, from 0001 (#194). Change it
# with a new migration there, not here.


def is_good(flag: str | None) -> bool:
    return flag is None or flag == "ok"


# --- the envelope -----------------------------------------------------------


def _norm(word) -> str:
    return str(word).strip().lower().replace("-", "_").replace(" ", "_")


def from_envelope(quality) -> str | None:
    """A reading's `quality` field -> the stored flag (None for ok, or for
    anything unrecognised: an unknown field must never make good data look
    bad, nor bad data look checked).

    Accepts a flag name, or BACnet's status: {"status_flags": [...] or
    {"fault": true, ...}, "reliability": "no-fault-detected"}. in-alarm
    alone is not bad data - the value is real, the alarm is about it."""
    if quality is None:
        return None
    if isinstance(quality, str):
        flag = _norm(quality)
        return flag if flag in FLAGS and flag != "ok" else None
    if not isinstance(quality, dict):
        return None
    raw = quality.get("status_flags", quality.get("statusFlags"))
    if isinstance(raw, dict):
        flags = {_norm(k) for k, v in raw.items() if v}
    elif isinstance(raw, (list, tuple, set)):
        flags = {_norm(f) for f in raw}
    else:
        flags = set()
    reliability = quality.get("reliability")
    if "out_of_service" in flags:
        return "out_of_service"
    if "fault" in flags or (reliability is not None and _norm(reliability) not in ("no_fault_detected", "")):
        return "fault"
    if "overridden" in flags:
        return "overridden"
    return None


# --- range ------------------------------------------------------------------

# (words the Brick class name must contain, unit) -> plausible (low, high).
# First match wins, so the specific rows come first. Wide on purpose: this
# catches a failed sensor (-40 or 180 in a zone, 140% open), not a comfort
# complaint - that's what fault rules are for.
RANGES: list[tuple[frozenset[str], str, tuple[float, float]]] = [
    (frozenset({"Zone", "Air", "Temperature"}), "°F", (32.0, 120.0)),
    (frozenset({"Zone", "Air", "Temperature"}), "°C", (0.0, 50.0)),
    (frozenset({"Outside", "Air", "Temperature"}), "°F", (-60.0, 140.0)),
    (frozenset({"Outside", "Air", "Temperature"}), "°C", (-50.0, 60.0)),
    (frozenset({"Air", "Temperature"}), "°F", (-40.0, 200.0)),
    (frozenset({"Air", "Temperature"}), "°C", (-40.0, 95.0)),
    (frozenset({"Water", "Temperature"}), "°F", (32.0, 260.0)),
    (frozenset({"Water", "Temperature"}), "°C", (0.0, 127.0)),
    (frozenset({"Temperature"}), "°F", (-60.0, 300.0)),
    (frozenset({"Temperature"}), "°C", (-50.0, 150.0)),
    (frozenset({"Humidity"}), "%RH", (0.0, 100.0)),
    (frozenset({"Humidity"}), "%", (0.0, 100.0)),
    (frozenset({"Position"}), "%", (0.0, 100.0)),
    (frozenset({"Damper"}), "%", (0.0, 100.0)),
    (frozenset({"Valve"}), "%", (0.0, 100.0)),
    (frozenset({"Speed"}), "%", (0.0, 100.0)),
    (frozenset({"CO2"}), "ppm", (0.0, 10000.0)),
]


def plausible_range(brick_class: str | None, unit: str | None) -> tuple[float, float] | None:
    if not brick_class or not unit:
        return None
    words = set(brick_class.split("_"))
    for need, u, rng in RANGES:
        if u == unit and need <= words:
            return rng
    return None


def range_flag(brick_class: str | None, unit: str | None, value) -> str | None:
    if not _numeric(value):
        return None
    rng = plausible_range(brick_class, unit)
    if rng is None:
        return None
    return "out_of_range" if not rng[0] <= value <= rng[1] else None


def _numeric(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# --- spike ------------------------------------------------------------------

SPIKE_HALF_WINDOW = 5  # neighbours on each side
SPIKE_K = 6.0
_MAD_SCALE = 1.4826  # MAD -> standard deviation for normal data


def spike_indexes(values: list[float], half: int = SPIKE_HALF_WINDOW, k: float = SPIKE_K, start: int = 0) -> list[int]:
    """Indexes of isolated jumps: a sample further than k scaled MADs from
    the median of the `half` samples before and after it. Centred, so a
    real step change (half the window on each level) isn't a spike, and
    only samples with a full window on both sides are judged - the newest
    `half` wait for the next sweep. The MAD gets a floor (0.1% of the
    median, at least 1e-6) so a perfectly flat series doesn't call every
    rounding wiggle a spike. Indexes below `start` are neighbours only
    (the incremental sweep's context, #268).

    The per-sample cost is most of a sweep's CPU (#268), so: two sorts of
    2*half floats (statistics.median's own work, without its overhead),
    and the MAD skipped when the jump is within k scaled floors - it can't
    be a spike whatever the MAD is."""
    out = []
    kk = k * _MAD_SCALE
    hi_mid = half  # the window has 2*half neighbours: an even count
    for i in range(max(half, start), len(values) - half):
        neighbours = values[i - half:i] + values[i + 1:i + 1 + half]
        neighbours.sort()
        med = (neighbours[hi_mid - 1] + neighbours[hi_mid]) / 2
        dev = abs(values[i] - med)
        floor = max(abs(med) * 0.001, 1e-6)
        if dev <= kk * floor:
            continue
        spread = sorted([abs(v - med) for v in neighbours])
        mad = (spread[hi_mid - 1] + spread[hi_mid]) / 2
        if dev > kk * max(mad, floor):
            out.append(i)
    return out


# --- unit -------------------------------------------------------------------

_OTHER_UNIT = {"°F": "°C", "°C": "°F"}


def unit_suspicion(brick_class: str | None, unit: str | None, values: Iterable) -> dict | None:
    """A suggested unit when the values fit the other temperature unit's
    plausible range and not their own: a zone reading 21-23 "°F". None
    when the values fit their unit, or fit neither."""
    nums = [v for v in values if _numeric(v)]
    other = _OTHER_UNIT.get(unit or "")
    if len(nums) < 10 or other is None:
        return None
    own, alt = plausible_range(brick_class, unit), plausible_range(brick_class, other)
    if own is None or alt is None:
        return None
    med = statistics.median(nums)
    fits_own = own[0] <= med <= own[1] and sum(own[0] <= v <= own[1] for v in nums) / len(nums) > 0.9
    fits_alt = all(alt[0] <= v <= alt[1] for v in nums)
    if fits_alt and not fits_own:
        return {"suggested": other, "median": round(med, 2), "samples": len(nums), "own_range": own, "suggested_range": alt}
    return None


# --- the sweep --------------------------------------------------------------

LOOKBACK = timedelta(hours=1)

# The incremental sweep (#268). Each pass judges (judged_to - OVERLAP, now]
# and moves judged_to to now, so a row is judged once (twice inside the
# overlap), not on every pass for an hour. The overlap is for rows that
# were in flight when the previous pass read their point - committed a few
# seconds after their ts. Anything later than that is a late row: the
# sweeper's feed watcher records it in quality_late and the next pass
# judges it on its own (Sweeper.watch).
OVERLAP = timedelta(seconds=float(os.environ.get("BALER_QUALITY_OVERLAP_S", "") or 30))
# Late rows judged per pass at most; the rest wait for the next one.
LATE_PER_SWEEP = 200_000
# Good samples fetched from before a pass's window, per point: the first
# half are the spike window's left neighbours, the last half are the
# samples the previous pass couldn't judge yet (no full window then).
CONTEXT = 2 * SPIKE_HALF_WINDOW


def ensure_schema(conn) -> None:
    schema.ensure(conn)


def point_meta(store) -> dict[str, tuple[str, str]]:
    """{point_uri: (Brick class, unit)} for every point that has both -
    the only points range and unit checks can judge."""
    rows = store.query("""
        PREFIX haystack: <urn:baler:haystack#>
        SELECT ?p ?type ?unit WHERE {
            ?p a ?type ; haystack:unit ?unit .
            FILTER(STRSTARTS(STR(?type), "https://brickschema.org/schema/Brick#"))
        }""")
    out: dict[str, tuple[str, str]] = {}
    for r in rows:
        out[str(r.p)] = (str(r.type).rsplit("#", 1)[1], str(r.unit))
    return out


def judge(brick_class: str | None, unit: str | None, samples: list[tuple[datetime, object, str | None]],
          context: list[tuple[datetime, float]] | None = None,
          only: set[datetime] | None = None) -> tuple[dict[datetime, str], dict | None]:
    """Flags for one point's samples ({ts: flag}, only for samples with no
    flag yet), and a unit suggestion if any. Pure: sweep feeds it rows.

    `context` (#268): up to CONTEXT good numeric samples just before
    `samples`, oldest first. They are evidence for the unit check and the
    spike window, and the newest SPIKE_HALF_WINDOW of them - the ones the
    previous pass left unjudged for want of right-hand neighbours - are
    judged for spikes now. `only`: judge just these samples (late rows);
    the rest are neighbours."""
    ctx = context or []
    suggestion = unit_suspicion(brick_class, unit, [v for _, v in ctx] + [v for _, v, q in samples if is_good(q)])
    rng = plausible_range(brick_class, unit)
    flags: dict[datetime, str] = {}
    for ts, value, q in samples:
        if q is not None or (only is not None and ts not in only) or not _numeric(value):
            continue
        if suggestion is not None:
            flags[ts] = "unit_suspect"
        elif rng is not None and not rng[0] <= value <= rng[1]:  # type: ignore[operator]
            flags[ts] = "out_of_range"
    # Spikes over the samples that are still good, numeric, in order.
    good = ctx + [(ts, float(v)) for ts, v, q in samples if q is None and ts not in flags and _numeric(v)]  # type: ignore[arg-type]
    for i in spike_indexes([v for _, v in good], start=len(ctx) - SPIKE_HALF_WINDOW):
        ts = good[i][0]
        if only is None or ts in only:
            flags[ts] = "spike"
    return flags, suggestion


def sweep(ts_conn, store, now: datetime | None = None, lookback: timedelta = LOOKBACK, meta: dict | None = None) -> dict:
    """Judge (now - lookback, now] of every point Baler can judge (a Brick
    class and a unit), write any unit suggestion, and return counts. Only
    fills NULL flags; never changes a value. Stateless: the daemon runs a
    Sweeper, which calls this shape of pass over just what is new."""
    now = now or datetime.now(timezone.utc)
    meta = point_meta(store) if meta is None else meta
    return judge_window(ts_conn, meta, sorted(meta), now - lookback, now)


def _counts() -> dict:
    return {"points": 0, "rows": 0, "context_rows": 0, "flagged": 0, "suggestions": 0}


# Rows per window query: SWEEP_ROWS / (expected rows per point) points at a
# time, never every point's window in one fetchall: at 25k points every 5 s
# an hour was 3.7M rows held at once, and the OOM kill took fault_detector's
# reading-feed thread down with it (#243).
SWEEP_ROWS = 100_000
SWEEP_CHUNK = 150  # points per query for an hour of 5 s data; more for a shorter window
_ASSUMED_PERIOD_S = 5.0

_CONTEXT_SQL = f"""
SELECT q.uri, h.ts, h.value
FROM unnest(%s::text[]) AS q(uri)
CROSS JOIN LATERAL (
    SELECT ts, value FROM point_history
    WHERE point_uri = q.uri AND ts <= %s AND ts > %s AND quality IS NULL AND value IS NOT NULL
    ORDER BY ts DESC LIMIT {CONTEXT}
) h"""

_WINDOW_SQL = """
SELECT point_uri, ts, value, value_text, value_bool, quality FROM point_history
WHERE point_uri = ANY(%s) AND ts > %s AND ts <= %s ORDER BY point_uri, ts"""


def _value(v, vt, vb):
    return vb if vb is not None else (vt if vt is not None else v)


def judge_window(ts_conn, meta: dict, uris: list[str], since: datetime, until: datetime) -> dict:
    """One pass over (since, until] of `uris`, each with its CONTEXT good
    samples from before `since` (looked for no further back than
    LOOKBACK: the hot, uncompressed end of history). Counts: points with
    rows, rows judged, context rows, flags written, suggestions."""
    counts = _counts()
    span_s = max((until - since).total_seconds(), 1.0)
    per_query = max(SWEEP_CHUNK, int(SWEEP_ROWS / (span_s / _ASSUMED_PERIOD_S + CONTEXT)))
    for i in range(0, len(uris), per_query):
        chunk = uris[i:i + per_query]
        ctx: dict[str, list] = {}
        for uri, ts, v in ts_conn.execute(_CONTEXT_SQL, (chunk, since, since - LOOKBACK)).fetchall():
            ctx.setdefault(uri, []).append((ts, float(v)))
        rows = ts_conn.execute(_WINDOW_SQL, (chunk, since, until)).fetchall()
        by_point: dict[str, list] = {}
        for uri, ts, v, vt, vb, q in rows:
            by_point.setdefault(uri, []).append((ts, _value(v, vt, vb), q))
        counts["rows"] += len(rows)
        del rows
        flags: list[tuple[str, datetime, str]] = []
        for uri, samples in by_point.items():
            counts["points"] += 1
            context = ctx.get(uri, [])
            context.reverse()  # fetched newest first
            counts["context_rows"] += len(context)
            brick_class, unit = meta[uri]
            got, suggestion = judge(brick_class, unit, samples, context)
            flags.extend((uri, ts, f) for ts, f in got.items())
            if suggestion is not None:
                suggest_unit(ts_conn, uri, unit, suggestion, until)
                counts["suggestions"] += 1
        counts["flagged"] += write_flags(ts_conn, flags)
    return counts


_FLAG_SQL = "UPDATE point_history SET quality = %s WHERE point_uri = %s AND ts = %s AND quality IS NULL"


def write_flags(ts_conn, flags: list[tuple[str, datetime, str]]) -> int:
    """Fill the NULL quality of each (point, ts) with its flag: one
    statement per flagged row, pipelined (psycopg's executemany), each a
    primary-key probe with its ts a constant, so TimescaleDB plans it
    against one chunk. Only flagged rows are written: a pass over clean
    data writes nothing, and a late row in a compressed chunk costs a
    decompression only when it is actually flagged (one segment batch,
    far under max_tuples_decompressed_per_dml_transaction)."""
    if not flags:
        return 0
    with ts_conn.cursor() as cur:
        cur.executemany(_FLAG_SQL, [(f, uri, ts) for uri, ts, f in flags])
    return len(flags)


# --- late rows (#268) -------------------------------------------------------

_LATE_SQL = f"""
SELECT q.uri, h.ts, h.value, h.value_text, h.value_bool, h.quality, h.side
FROM unnest(%s::text[], %s::timestamptz[], %s::timestamptz[]) AS q(uri, lo, hi)
CROSS JOIN LATERAL (
    (SELECT ts, value, value_text, value_bool, quality, -1 AS side FROM point_history
     WHERE point_uri = q.uri AND ts < q.lo AND ts > q.lo - interval '{int(LOOKBACK.total_seconds())} seconds'
       AND quality IS NULL AND value IS NOT NULL
     ORDER BY ts DESC LIMIT {CONTEXT})
    UNION ALL
    (SELECT ts, value, value_text, value_bool, quality, 0 FROM point_history
     WHERE point_uri = q.uri AND ts >= q.lo AND ts <= q.hi)
    UNION ALL
    (SELECT ts, value, value_text, value_bool, quality, 1 FROM point_history
     WHERE point_uri = q.uri AND ts > q.hi AND ts < q.hi + interval '{int(LOOKBACK.total_seconds())} seconds'
       AND quality IS NULL AND value IS NOT NULL
     ORDER BY ts LIMIT {SPIKE_HALF_WINDOW})
) h"""


def judge_late(ts_conn, meta: dict, late: list[tuple[str, datetime]]) -> dict:
    """Judge rows that arrived after the pass that covered their ts:
    each point's late rows with what surrounds them (CONTEXT good samples
    before, every row between its first and last late one, SPIKE_HALF_WINDOW
    good samples after) as neighbours, flags only for the late rows
    themselves. Rows of points Baler can't judge are just dropped."""
    counts = {"late_rows": len(late), "late_points": 0, "late_flagged": 0}
    by_point: dict[str, set[datetime]] = {}
    for uri, ts in late:
        if uri in meta:
            by_point.setdefault(uri, set()).add(ts)
    uris = sorted(by_point)
    for i in range(0, len(uris), SWEEP_CHUNK):
        chunk = uris[i:i + SWEEP_CHUNK]
        rows = ts_conn.execute(_LATE_SQL, (chunk, [min(by_point[u]) for u in chunk],
                                           [max(by_point[u]) for u in chunk])).fetchall()
        got: dict[str, tuple[list, list]] = {}
        for uri, ts, v, vt, vb, q, side in rows:
            context, samples = got.setdefault(uri, ([], []))
            if side < 0:
                context.append((ts, float(v)))
            else:
                samples.append((ts, _value(v, vt, vb), q))
        flags: list[tuple[str, datetime, str]] = []
        for uri, (context, samples) in got.items():
            counts["late_points"] += 1
            context.sort()
            samples.sort(key=lambda s: s[0])
            brick_class, unit = meta[uri]
            flagged, _ = judge(brick_class, unit, samples, context, only=by_point[uri])
            flags.extend((uri, ts, f) for ts, f in flagged.items())
        counts["late_flagged"] += write_flags(ts_conn, flags)
    return counts


# The watcher's one statement: the feed batches after its offset, every
# reading in them whose ts the sweep has already passed (by more than the
# overlap) into quality_late, and the offset moved - together, so a late
# row is either recorded or still ahead of the offset, never lost between.
_WATCH_SQL = """
WITH b AS (
    SELECT seq, n, point_uri, ts FROM reading_feed WHERE seq > %(seq)s ORDER BY seq LIMIT %(limit)s
), late AS (
    INSERT INTO quality_late (point_uri, ts)
    SELECT u.point_uri, u.ts FROM b CROSS JOIN LATERAL unnest(b.point_uri, b.ts) AS u(point_uri, ts)
    WHERE u.ts <= %(below)s
    ON CONFLICT (point_uri, ts) DO NOTHING
    RETURNING 1
), moved AS (
    UPDATE reading_feed_consumers SET seq = greatest(seq, (SELECT max(seq) FROM b)), updated_at = now()
    WHERE consumer = %(name)s AND EXISTS (SELECT 1 FROM b)
    RETURNING seq, missed_readings, missed_batches, last_missed_at
)
SELECT (SELECT count(*) FROM b), (SELECT COALESCE(sum(n), 0) FROM b), (SELECT count(*) FROM late),
       m.seq, m.missed_readings, m.missed_batches, m.last_missed_at
FROM (SELECT 1) one LEFT JOIN moved m ON true"""


class Sweeper:
    """fault_detector's quality sweep, incremental (#268), on its own
    thread and connection so His-mode rules never wait for it.

    - A persisted watermark (quality_watermark.judged_to): each pass
      judges (judged_to - overlap, now] and moves it to now. A new
      sweeper starts an hour back, where the old full sweep looked. A
      pass covers at most `max_span` of ts, so catching up after a stop
      is a series of bounded passes, run back to back, not one huge one;
      a gap longer than LOOKBACK is skipped (and reported), as the old
      sweep never looked further back either.
    - Late rows from the reading feed (watch()): every committed batch is
      a feed row, in the same transaction as its history write, so a row
      committed after the pass that covered its ts is seen there and
      recorded for the next pass. Its feed offset is a normal consumer
      (`name` in reading_feed_consumers): lag and misses show in
      /ops/status like any rule daemon's.

    Not thread-safe: one thread owns it and its connection."""

    def __init__(self, conn, store, name: str = "quality", interval: float = 300.0,
                 overlap: timedelta = OVERLAP, max_span: timedelta | None = None, log=None):
        self.conn, self.store, self.name = conn, store, name
        self.interval, self.overlap = interval, overlap
        self.max_span = max_span or timedelta(seconds=max(2 * interval, 60.0))
        self.log = log or functools.partial(print, flush=True)
        self.consumer = reading_feed.FeedConsumer(conn, name, log=self.log)
        self.judged_to: datetime | None = None
        self.last: dict = {}
        self.sweeps = 0
        self.watched_readings = 0
        self.late_seen = 0

    def start(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self.consumer.start()
        self.conn.execute(
            "INSERT INTO quality_watermark (sweeper, judged_to) VALUES (%s, %s) ON CONFLICT (sweeper) DO NOTHING",
            (self.name, now - LOOKBACK))
        self.judged_to = self.conn.execute(
            "SELECT judged_to FROM quality_watermark WHERE sweeper = %s", (self.name,)).fetchone()[0]

    def lag_s(self, now: datetime | None = None) -> float:
        """How far behind now the watermark is: about the interval between
        passes when keeping up, growing when a pass can't."""
        if self.judged_to is None:
            return 0.0
        return max(0.0, ((now or datetime.now(timezone.utc)) - self.judged_to).total_seconds())

    def behind(self, now: datetime | None = None) -> bool:
        """More than one pass's span to judge: run the next pass now."""
        return self.lag_s(now) > self.max_span.total_seconds()

    def sweep(self, now: datetime | None = None, meta: dict | None = None) -> dict:
        """One pass: the window since the watermark, then up to
        LATE_PER_SWEEP late rows. Returns its figures (the heartbeat's
        `quality`)."""
        t0 = time.monotonic()
        now = now or datetime.now(timezone.utc)
        if self.judged_to is None:
            self.start(now)
        assert self.judged_to is not None
        meta = point_meta(self.store) if meta is None else meta
        meta_s = time.monotonic() - t0
        since = self.judged_to - self.overlap
        skipped_s = 0.0
        oldest = now - LOOKBACK - self.overlap
        if (oldest - since).total_seconds() > 1.0:  # not the first pass's own rounding
            skipped_s = (oldest - since).total_seconds()
            self.log(f"quality: watermark {self.judged_to.isoformat()} is more than {LOOKBACK} behind; "
                     f"{skipped_s:.0f} s of history left unjudged")
            since = oldest
        until = min(now, since + self.max_span)
        # Only points that reported since `since`: point_registry's coarse
        # last_ts is at most REGISTRY_GRANULARITY_S older than the truth.
        recent = {u for (u,) in self.conn.execute(
            "SELECT point_uri FROM point_registry WHERE last_ts > %s",
            (since - timedelta(seconds=timeseries.REGISTRY_GRANULARITY_S),)).fetchall()}
        uris = sorted(recent.intersection(meta))
        counts = judge_window(self.conn, meta, uris, since, until)
        window_s = time.monotonic() - t0 - meta_s
        self.conn.execute("UPDATE quality_watermark SET judged_to = %s, updated_at = now() WHERE sweeper = %s",
                          (until, self.name))
        self.judged_to = until
        t1 = time.monotonic()
        late = self.conn.execute("SELECT point_uri, ts FROM quality_late ORDER BY point_uri, ts LIMIT %s",
                                 (LATE_PER_SWEEP,)).fetchall()
        lc = judge_late(self.conn, meta, late) if late else {"late_rows": 0, "late_points": 0, "late_flagged": 0}
        if late:
            self.conn.execute(
                "DELETE FROM quality_late l USING unnest(%s::text[], %s::timestamptz[]) AS d(uri, ts) "
                "WHERE l.point_uri = d.uri AND l.ts = d.ts", ([u for u, _ in late], [t for _, t in late]))
        self.sweeps += 1
        self.last = {**counts, **lc, "candidates": len(uris), "classified": len(meta),
                     "since": since.isoformat(), "until": until.isoformat(),
                     "span_s": round((until - since).total_seconds(), 1), "skipped_s": round(skipped_s, 1),
                     "meta_s": round(meta_s, 3), "window_s": round(window_s, 3),
                     "late_s": round(time.monotonic() - t1, 3), "sweep_s": round(time.monotonic() - t0, 3),
                     "at": now.isoformat()}
        return self.last

    def watch(self, limit: int = 200) -> int:
        """Drain the reading feed past this sweeper's offset, recording
        late rows (ts at or before judged_to - overlap). Cheap: the
        filter runs in Postgres over the batches' ts arrays and returns
        counts only. Returns the late rows recorded."""
        if self.judged_to is None:
            return 0
        below = self.judged_to - self.overlap
        found = 0
        while True:
            n, readings, late, seq, mr, mb, last = self.conn.execute(
                _WATCH_SQL, {"seq": self.consumer.seq, "limit": limit, "below": below, "name": self.name}).fetchone()
            if not n:
                return found
            self.consumer.seq = seq
            self.watched_readings += int(readings)
            found += int(late)
            self.late_seen += int(late)
            st = self.consumer.stats
            if mr > st.missed_readings or mb > st.missed_batches:
                self.log(f"quality: reading feed: MISSED {mr - st.missed_readings} reading(s) pruned before the "
                         "late-row watcher read them (more than the retention window behind): rows in them that "
                         "arrived late are not judged")
            st.missed_readings, st.missed_batches, st.last_missed_at = mr, mb, last
            if n < limit:
                return found

    def status(self) -> dict:
        """The heartbeat's `quality`: the last pass, the watermark and its
        lag, and the watcher's feed position."""
        feed = self.consumer.refresh_lag()
        return {**self.last, "interval_s": self.interval, "overlap_s": self.overlap.total_seconds(),
                "sweeps": self.sweeps,
                "watermark": self.judged_to.isoformat() if self.judged_to else None,
                "watermark_lag_s": round(self.lag_s(), 1),
                "late_seen": self.late_seen,
                "feed": {"lag_s": round(feed.lag_s, 1), "lag_readings": feed.lag_readings,
                         "readings": self.watched_readings, "missed_readings": feed.missed_readings}}


def suggest_unit(conn, point_uri: str, current: str | None, suggestion: dict, now: datetime) -> None:
    """One open suggestion per point and kind, refreshed on each sweep;
    a decided one (accepted or dismissed) is left alone."""
    conn.execute(
        """INSERT INTO quality_suggestions (point_uri, kind, current, suggested, evidence, status, created_at, updated_at)
           VALUES (%s, 'unit', %s, %s, %s, 'open', %s, %s)
           ON CONFLICT (point_uri, kind) WHERE status = 'open' DO UPDATE SET
             suggested = EXCLUDED.suggested, evidence = EXCLUDED.evidence, updated_at = EXCLUDED.updated_at""",
        (point_uri, current, suggestion["suggested"], json.dumps(suggestion), now, now),
    )


def open_suggestions(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT point_uri, kind, current, suggested, evidence, updated_at FROM quality_suggestions WHERE status = 'open' ORDER BY point_uri"
    ).fetchall()
    return [dict(zip(("point_uri", "kind", "current", "suggested", "evidence", "updated_at"), r)) for r in rows]

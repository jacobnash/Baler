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
  * Baler's own checks (sweep), run by fault_detector on an interval over
    the last hour: range, spike and unit suspicion. A sweep only fills a
    NULL - a device's own flag always wins - and never touches a value.

Consumers skip flagged samples (fault rules, Cx) or show the flag (the his
APIs). `GOOD_SQL` is the filter they share.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from baler import schema

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


def spike_indexes(values: list[float], half: int = SPIKE_HALF_WINDOW, k: float = SPIKE_K) -> list[int]:
    """Indexes of isolated jumps: a sample further than k scaled MADs from
    the median of the `half` samples before and after it. Centred, so a
    real step change (half the window on each level) isn't a spike, and
    only samples with a full window on both sides are judged - the newest
    `half` wait for the next sweep. The MAD gets a floor (0.1% of the
    median, at least 1e-6) so a perfectly flat series doesn't call every
    rounding wiggle a spike."""
    out = []
    for i in range(half, len(values) - half):
        neighbours = values[i - half:i] + values[i + 1:i + 1 + half]
        med = statistics.median(neighbours)
        mad = statistics.median(abs(v - med) for v in neighbours)
        floor = max(abs(med) * 0.001, 1e-6)
        if abs(values[i] - med) > k * _MAD_SCALE * max(mad, floor):
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


def judge(brick_class: str | None, unit: str | None, samples: list[tuple[datetime, object, str | None]]) -> tuple[dict[datetime, str], dict | None]:
    """Flags for one point's samples ({ts: flag}, only for samples with no
    flag yet), and a unit suggestion if any. Pure: sweep feeds it rows."""
    suggestion = unit_suspicion(brick_class, unit, (v for _, v, q in samples if is_good(q)))
    flags: dict[datetime, str] = {}
    for ts, value, q in samples:
        if q is not None:
            continue
        if suggestion is not None and _numeric(value):
            flags[ts] = "unit_suspect"
            continue
        f = range_flag(brick_class, unit, value)
        if f:
            flags[ts] = f
    # Spikes over the samples that are still good, numeric, in order.
    good = [(ts, float(v)) for ts, v, q in samples if q is None and ts not in flags and _numeric(v)]  # type: ignore[arg-type]
    for i in spike_indexes([v for _, v in good]):
        flags[good[i][0]] = "spike"
    return flags, suggestion


def sweep(ts_conn, store, now: datetime | None = None, lookback: timedelta = LOOKBACK, meta: dict | None = None) -> dict:
    """Flag the last `lookback` of every point Baler can judge (a Brick
    class and a unit), write any unit suggestion, and return counts. Only
    fills NULL flags; never changes a value."""
    now = now or datetime.now(timezone.utc)
    meta = point_meta(store) if meta is None else meta
    counts = {"points": 0, "flagged": 0, "suggestions": 0}
    if not meta:
        return counts
    uris = sorted(meta)
    counts["rows"] = 0
    # SWEEP_CHUNK points per query, not every point's hour in one fetchall:
    # at 25k points every 5 s that was 3.7M rows held at once, and the OOM
    # kill took fault_detector's reading-feed thread down with it (#243).
    for i in range(0, len(uris), SWEEP_CHUNK):
        _sweep_points(ts_conn, uris[i:i + SWEEP_CHUNK], meta, now, lookback, counts)
    return counts


# Points per query in sweep: ~100k rows at 5 s data over the hour.
SWEEP_CHUNK = 150


def _sweep_points(ts_conn, uris: list[str], meta: dict, now: datetime, lookback: timedelta, counts: dict) -> None:
    rows = ts_conn.execute(
        """SELECT point_uri, ts, value, value_text, value_bool, quality FROM point_history
           WHERE point_uri = ANY(%s) AND ts >= %s AND ts <= %s ORDER BY point_uri, ts""",
        (uris, now - lookback, now),
    ).fetchall()
    counts["rows"] += len(rows)
    by_point: dict[str, list] = {}
    for uri, ts, v, vt, vb, q in rows:
        value = vb if vb is not None else (vt if vt is not None else v)
        by_point.setdefault(uri, []).append((ts, value, q))
    del rows
    for uri, samples in by_point.items():
        counts["points"] += 1
        brick_class, unit = meta[uri]
        flags, suggestion = judge(brick_class, unit, samples)
        for ts, flag in flags.items():
            ts_conn.execute(
                "UPDATE point_history SET quality = %s WHERE point_uri = %s AND ts = %s AND quality IS NULL",
                (flag, uri, ts),
            )
        counts["flagged"] += len(flags)
        if suggestion is not None:
            suggest_unit(ts_conn, uri, unit, suggestion, now)
            counts["suggestions"] += 1


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

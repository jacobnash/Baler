"""
History predicates shared by fault_detector's His-mode rules and the
commissioning ladder (Baler#38), so "stale" and "stuck" mean one thing
in Baler, edge cases included:

  - a point that has never reported is NOT stale (no time to measure
    staleness from) - commissioning reports it as `inconclusive`
    instead, which is how it closes the documented `stale` gap for Cx;
  - a window with zero samples is NOT stuck - that's silence, not a
    frozen value.
"""

from __future__ import annotations

from datetime import datetime


def is_stale(last_ts: datetime | None, now: datetime, max_age_seconds: float) -> bool:
    return last_ts is not None and (now - last_ts).total_seconds() >= max_age_seconds


def stuck_verdict(n_samples: int, n_distinct: int) -> bool:
    """For callers that count in SQL (fault_detector) rather than fetch."""
    return n_samples > 0 and n_distinct == 1


def _key(value) -> str:
    # Same identity fault_detector's SQL uses: COALESCE(value::text,
    # value_text, value_bool::text) - one value, one key, whatever its type.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def is_stuck(values) -> bool:
    values = list(values)
    return stuck_verdict(len(values), len({_key(v) for v in values}))

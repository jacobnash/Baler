"""
One commissioning run: inventory -> checks -> summary, deterministic
given the same graph, history and clock. No I/O of its own beyond what
it's handed (a store, a History, scopes), so it runs the same against
Oxigraph+Postgres and against rdflib+InMemoryHistory in tests.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from baler.commissioning import checks, inventory, ladder
from baler.commissioning import templates as tpl
from baler.commissioning.history import History
from baler.scope import Scope

DEFAULT_WINDOW_HOURS = 24.0
MAX_WINDOW_HOURS = 24.0 * 31


def window_hours(value) -> float:
    if value is None:
        return DEFAULT_WINDOW_HOURS
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= MAX_WINDOW_HOURS:
        raise ValueError(f"window_hours must be a number in (0, {MAX_WINDOW_HOURS:g}]")
    return float(value)


def freshness_statement(now: datetime, window_h: float, n: int, fresh: int, stale: int, never: int) -> str:
    """What was actually observed - never "all good" when what is meant
    is "nothing new came in" (#5)."""
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    if n == 0:
        return f"as of {stamp}: no points in scope - nothing could be verified"
    return (f"as of {stamp}: {fresh} of {n} points reported in the last {window_h:g} h, "
            f"{stale} stale, {never} never reported")


def run(store, history: History, scopes: list[Scope], now: datetime, window_h: float = DEFAULT_WINDOW_HOURS,
        templates=None, ladder_config: dict | None = None,
        progress: Callable[[dict], None] | None = None) -> tuple[dict, list[dict]]:
    """(summary, results). `progress`, if given, is told the stage and how
    many equipment are done; it must be cheap (it's called per equipment)."""
    def report(stage: str, done: int = 0, total: int | None = None, points: int | None = None) -> None:
        if progress is not None:
            progress({"stage": stage, "equipment_done": done, "equipment_total": total, "points": points})

    templates = templates if templates is not None else tpl.load()
    report("inventory")
    equips = inventory.build(store, scopes)
    point_ids = sorted({p["id"] for e in equips for p in e["points"]})
    report("history", 0, len(equips), len(point_ids))
    latest = history.latest(point_ids) if point_ids else {}
    window = timedelta(hours=window_h)
    # window() is half-open on the end (point_history reads are); a sample
    # stamped exactly `now` is still evidence, so nudge the bound past it.
    series = history.window(point_ids, now - window, now + timedelta(seconds=1)) if point_ids else {}

    results: list[dict] = []
    without_template = []
    for i, e in enumerate(equips):
        report("checks", i, len(equips), len(point_ids))
        exists = checks.existence(e, latest, now, window)
        results.extend(exists)
        results.extend(ladder.climb(e, exists, series, now, window, ladder_config))
        if tpl.templates_for(e["brickClasses"], templates):
            results.extend(checks.template_completeness(e, templates))
        else:
            without_template.append({"id": e["id"], "brickClass": e["brickClass"]})

    by_status = {s: 0 for s in checks.STATUSES}
    by_check: dict[str, dict[str, int]] = {}
    for r in results:
        by_status[r["status"]] += 1
        family = r["check_id"].split(":", 1)[0]
        by_check.setdefault(family, {s: 0 for s in checks.STATUSES})[r["status"]] += 1
    exists = [r for r in results if r["check_id"] == "exists"]
    fresh = sum(1 for r in exists if r["status"] == "pass")
    stale = sum(1 for r in exists if r["status"] == "fail")
    never = sum(1 for r in exists if r["status"] == "inconclusive")
    histogram = {str(n): 0 for n in range(4)}
    for r in results:
        if r["check_id"] == "ladder":
            histogram[str(r["evidence"]["highest_rung_passed"])] += 1
    summary = {
        "ladder": {"highest_rung_passed": histogram, "rungs": {"1": "existence", "2": "liveness", "3": "responsiveness"}},
        "equipment": len(equips),
        "points": len(point_ids),
        "equipment_with_template": len(equips) - len(without_template),
        "equipment_without_template": without_template[:100],
        "unclassified_points": sum(1 for e in equips for p in e["points"] if not p["brickClass"]),
        "by_status": by_status,
        "by_check": by_check,
        "required_missing": sum(1 for r in results if r["check_id"].startswith("template:")
                                and r["status"] == "fail" and r["severity"] == "required"),
        "freshness": freshness_statement(now, window_h, len(point_ids), fresh, stale, never),
    }
    return summary, results

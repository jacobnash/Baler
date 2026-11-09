"""
GET /cx/projects/{id}/hunting (Baler#214): the per-loop hunting report for
the project's equipment the caller can see, against each equipment's
start-up baseline.
"""

from __future__ import annotations

import re

from baler.commissioning import hunting, inventory, runner
from baler.commissioning.routes import Route, query
from baler.http_handler_base import respond_json


def _num(q: dict, key: str, default: float, lo: float, hi: float) -> float:
    v = float(q.get(key, [default])[0])
    if not lo <= v <= hi:
        raise ValueError(f"{key} must be in [{lo:g}, {hi:g}]")
    return v


def _report(h, span, claims, pid) -> None:
    p = h._project(pid, claims)
    if p is None:
        return
    q = query(h)
    hours = _num(q, "hours", 24, 1, 24 * 7)
    days = int(_num(q, "days", 7, 1, 60))
    cfg = {"ratio": _num(q, "ratio", hunting.DEFAULTS["ratio"], 1, 100),
           "min_reversals_per_hour": _num(q, "min_reversals_per_hour", hunting.DEFAULTS["min_reversals_per_hour"], 1, 3600),
           "command_deadband": _num(q, "command_deadband", hunting.DEFAULTS["command_deadband"], 0, 50),
           "error_deadband": _num(q, "error_deadband", hunting.DEFAULTS["error_deadband"], 0, 100)}
    scopes, _ = runner.project_scopes(h.REPO, p, claims)
    equips = inventory.build(h.REPO.store, scopes)
    if q.get("equip"):
        equips = [e for e in equips if e["id"] in q["equip"]]
    now = h.NOW()
    with h.REPO.history() as hist:
        rows = hunting.analyse(equips, hist, now, hours, days, hunting.baseline_windows(h.REPO, pid), cfg)
    span.set_attribute("loops", len(rows))
    respond_json(h, 200, {"generated_at": now.isoformat(), "hours": hours, "settings": cfg,
                          "hunting": sum(1 for r in rows if r["verdict"] == "hunting"), "loops": rows})


ROUTES: list[Route] = [
    ("GET", re.compile(r"/projects/([^/]+)/hunting"), "hunting_report", _report),
]

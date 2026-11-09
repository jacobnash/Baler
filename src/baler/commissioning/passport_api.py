"""
GET /cx/projects/{id}/passport (Baler#219): the building passport, as JSON
or a printable page (?format=html, the Cx reports' CSP). Read in the
project's own scope, like the Cx report, so the same passport names the
same equipment whoever generates it.
"""

from __future__ import annotations

import re

from baler.commissioning import inventory, passport, runner
from baler.commissioning.routes import Route, query
from baler.http_handler_base import respond_json


def _passport(h, span, claims, pid) -> None:
    p = h._project(pid, claims)
    if p is None:
        return
    fmt = query(h).get("format", ["json"])[0]
    if fmt not in ("json", "html"):
        raise ValueError("format must be json or html")
    scopes, _ = runner.project_scopes(h.REPO, p, {"org_id": p["org_id"]})
    caller = h.REPO.scope_for(claims)
    equips = [e for e in inventory.build(h.REPO.store, scopes) if caller.allows(e["id"])] if h.REPO.store is not None else []
    rep = passport.build(h.REPO, p, equips, h.NOW(), h.REPO.user_email(claims.get("sub")))
    span.set_attribute("positions", len(rep["positions"]))
    h._event(pid, "passport.generate", claims, {"format": fmt, "positions": len(rep["positions"])})
    if fmt == "html":
        h._send_html(passport.html(rep).encode("utf-8"))
    else:
        respond_json(h, 200, rep)


ROUTES: list[Route] = [
    ("GET", re.compile(r"/projects/([^/]+)/passport"), "building_passport", _passport),
]

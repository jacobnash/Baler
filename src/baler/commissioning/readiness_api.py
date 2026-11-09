"""
GET /cx/spaces/{id}/readiness (Baler#210): the readiness report for a room,
or for every room under a suite, zone or floor, as JSON or a printable page
(?format=html, the same CSP as the Cx reports: nothing runs in it).
"""

from __future__ import annotations

import re

from baler.commissioning import readiness, spaces_api
from baler.commissioning.routes import Route, owned_row, query
from baler.http_handler_base import respond_json


def _readiness(h, span, claims, sid) -> None:
    hit = owned_row(h, spaces_api.SPACES, sid, claims, "space")
    if not hit:
        return
    space, project = hit
    fmt = query(h).get("format", ["json"])[0]
    if fmt not in ("json", "html"):
        raise ValueError("format must be json or html")
    rep = readiness.build(h.REPO, project, space, spaces_api.project_spaces(h.REPO, project["id"]), h.NOW(),
                          h.REPO.scope_for(claims).allows)
    span.set_attribute("rooms", len(rep["rooms"]))
    if fmt == "html":
        h._send_html(readiness.html(rep).encode("utf-8"))
    else:
        respond_json(h, 200, rep)


ROUTES: list[Route] = [
    ("GET", re.compile(r"/spaces/([^/]+)/readiness"), "space_readiness", _readiness),
]

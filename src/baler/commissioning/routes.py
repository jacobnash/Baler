"""
Route tables for the building-record features (UBIS, epic Baler#221) that
commissioning_api serves beside Cx.

Each feature module (spaces_api, readiness_api, ...) keeps its handlers
and a ROUTES list of (method, compiled path regex, span name, fn) and is
listed once in commissioning_api.EXTENSIONS; nothing else in the API
changes per feature. A handler is fn(h, span, claims, *path_groups), with
`h` the request handler: h.REPO, h.NOW(), h._project(), h._event(),
h.read_json_body(), h._send_html(); query(h) for the query string.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

from baler.http_handler_base import respond_error

Route = tuple[str, "re.Pattern[str]", str, Callable[..., None]]


def match(routes: list[Route], method: str, path: str) -> tuple[str, Callable[..., None], tuple[str, ...]] | None:
    """(span name, fn, path groups) of the first route that fits, or None."""
    for want, rx, name, fn in routes:
        if want == method:
            m = rx.fullmatch(path)
            if m:
                return name, fn, m.groups()
    return None


def query(h: Any) -> dict[str, list[str]]:
    """The request's query string, parsed."""
    return parse_qs(urlparse(h.path).query)


def owned_row(h: Any, table: str, row_id: str, claims: dict, what: str) -> tuple[dict, dict] | None:
    """(row, project) when the row exists and its project is visible to the
    caller; otherwise answers 404 (never says which) and returns None."""
    row = h.REPO.get_row(table, row_id)
    project = h.REPO.get_project(row["project_id"]) if row else None
    if row is None or project is None or not h._visible(project, claims):
        respond_error(h, 404, f"no such {what}")
        return None
    return row, project

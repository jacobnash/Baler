"""
The lifecycle record's HTTP routes, as plain functions over a Request, so
every route is unit-testable without a server (tests build a Request with
MemoryRows and call dispatch). `lifecycle.http` adapts ops_api's handler
to this.

A route function takes the Request plus the path's groups, and returns a
JSON-ready payload (200), a (status, payload) pair, or a Raw body (CSV,
a document's bytes). It raises HTTPError for a 4xx, or ValueError/TypeError
for a 400 with the message.

Paths are as ops_api sees them (no /ops prefix). Every path here also
needs a row in gateway/njs/policy.js, with the /ops prefix.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Raw:
    body: bytes
    content_type: str
    filename: str | None = None
    status: int = 200


def _allow_all(_uri: str) -> bool:
    return True


@dataclass
class Request:
    method: str
    path: str
    rows: Any  # rows.MemoryRows | rows.PgRows
    org_id: str
    who: dict = field(default_factory=dict)  # {"sub", "email"}
    query: dict[str, str] = field(default_factory=dict)
    body: Any = None  # parsed JSON, when the request sent JSON
    raw: bytes | None = None  # the body's bytes, when it didn't
    content_type: str = ""
    allows: Callable[[str], bool] = _allow_all  # Scope.allows of the caller
    role: str = "viewer"
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    services: dict = field(default_factory=dict)  # optional seams: "history", "store", ...

    def json(self) -> dict:
        if self.body is None and self.raw:
            raise HTTPError(415, "send JSON (Content-Type: application/json)")
        if self.body is None:
            return {}
        if not isinstance(self.body, dict):
            raise HTTPError(400, "the body must be a JSON object")
        return self.body

    def actor(self) -> str | None:
        return self.who.get("email") or self.who.get("sub")


Handler = Callable[..., Any]
ROUTES: list[tuple[str, re.Pattern, Handler]] = []


def route(method: str, pattern: str):
    rx = re.compile(pattern)

    def deco(fn: Handler) -> Handler:
        ROUTES.append((method, rx, fn))
        return fn

    return deco


_loaded = False


def load() -> None:
    """Import every module in baler.lifecycle, so each registers its routes."""
    global _loaded
    if _loaded:
        return
    import baler.lifecycle as pkg

    for m in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
        if m.name not in ("routes", "rows", "http"):
            importlib.import_module(f"baler.lifecycle.{m.name}")
    _loaded = True


def match(method: str, path: str) -> tuple[Handler, tuple[str, ...]] | None:
    load()
    for m, rx, fn in ROUTES:
        if m == method:
            hit = rx.fullmatch(path)
            if hit:
                return fn, hit.groups()
    return None


def is_lifecycle_path(path: str) -> bool:
    load()
    return any(rx.fullmatch(path) for _, rx, _ in ROUTES)


def dispatch(req: Request) -> tuple[int, Any]:
    """(status, payload or Raw)."""
    hit = match(req.method, req.path)
    if hit is None:
        return (405 if is_lifecycle_path(req.path) else 404), {"error": "no such route"}
    fn, groups = hit
    try:
        out = fn(req, *groups)
    except HTTPError as exc:
        return exc.status, {"error": exc.message}
    except (ValueError, TypeError) as exc:
        return 400, {"error": str(exc)}
    if isinstance(out, Raw):
        return out.status, out
    if isinstance(out, tuple) and len(out) == 2 and isinstance(out[0], int):
        return out
    return 200, out


# ---------------------------------------------------------------- helpers


def need(body: dict, key: str, kind: type | tuple = str) -> Any:
    v = body.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        raise ValueError(f"{key} is required")
    if not isinstance(v, kind):
        raise TypeError(f"{key} has the wrong type")
    return v.strip() if isinstance(v, str) else v


def text(body: dict, key: str, limit: int = 500) -> str | None:
    v = body.get(key)
    if v is None:
        return None
    if not isinstance(v, (str, int, float)):
        raise TypeError(f"{key} must be text")
    s = str(v).strip()
    if len(s) > limit:
        raise ValueError(f"{key} is longer than {limit} characters")
    return s or None


def number(body: dict, key: str) -> float | None:
    v = body.get(key)
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise TypeError(f"{key} must be a number")
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number") from None


def when(v: Any, key: str) -> datetime | None:
    """An ISO date or datetime -> an aware datetime (UTC when it has no zone)."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if not isinstance(v, str):
        raise TypeError(f"{key} must be an ISO date or datetime")
    try:
        d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{key} must be an ISO date or datetime, not {v!r}") from None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def as_dt(v: Any) -> datetime | None:
    """A stored timestamp (datetime from Postgres, or an ISO string) -> datetime."""
    if v is None or isinstance(v, datetime):
        return v
    return when(v, "timestamp")


def require_role(req: Request, role: str) -> None:
    """The gateway enforces each route's role; this is for a choice inside a
    route (e.g. a flag only an admin may set)."""
    rank = {"viewer": 1, "operator": 2, "service": 2, "admin": 3}
    if rank.get(req.role, 0) < rank[role]:
        raise HTTPError(403, f"needs the {role} role")

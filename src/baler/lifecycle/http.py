"""
ops_api's adapter for lifecycle.routes: builds a Request from the HTTP
handler (the caller's org, identity and site scope from its verified JWT),
dispatches, and writes the response. Bodies are JSON, except where a
route takes a file (Content-Type other than JSON: the bytes, up to
MAX_BODY_BYTES).
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from baler import auth
from baler.http_handler_base import respond_error, respond_json
from baler.lifecycle import routes
from baler.lifecycle.rows import MemoryRows, PgRows
from baler.scope import claims_from_header, scope_for

MAX_BODY_BYTES = 100 * 1024 * 1024


def _read_body(handler) -> tuple[object, bytes | None, str]:
    ctype = (handler.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    length = int(handler.headers.get("Content-Length") or 0)
    if length > MAX_BODY_BYTES:
        raise routes.HTTPError(413, f"body is over {MAX_BODY_BYTES // (1024 * 1024)} MB")
    data = handler.rfile.read(length) if length else b""
    if not data:
        return None, None, ctype
    if ctype in ("", "application/json"):
        try:
            return json.loads(data.decode("utf-8")), None, ctype
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise routes.HTTPError(400, "the body isn't valid JSON") from None
    return None, data, ctype


def serve(handler, method: str, ts_pool, rows=None, services: dict | None = None) -> None:
    """`rows` (tests): a MemoryRows to use instead of Postgres; the caller's
    scope is then everything."""
    url = urlparse(handler.path)
    claims = claims_from_header(handler.headers.get("Authorization")) or {}
    org_id = claims.get("org_id")
    if not org_id:
        respond_error(handler, 403, "this route needs a signed-in user of an org")
        return
    try:
        body, raw, ctype = _read_body(handler)
    except routes.HTTPError as exc:
        respond_error(handler, exc.status, exc.message)
        return
    sub = claims.get("sub")
    if isinstance(rows, MemoryRows) or ts_pool is None:
        allows, email = (lambda _u: True), sub
        rows = rows or MemoryRows()
    else:
        with ts_pool.connection() as conn:
            allows = scope_for(conn, claims).allows
            user = auth.get_user(conn, sub) if sub and claims.get("kind") == "session" else None
            email = user["email"] if user else sub
        rows = PgRows(ts_pool)
    req = routes.Request(
        method=method, path=url.path, rows=rows, org_id=org_id, who={"sub": sub, "email": email},
        query={k: v[-1] for k, v in parse_qs(url.query).items()}, body=body, raw=raw, content_type=ctype,
        allows=allows, role=str(claims.get("role") or "viewer"), services=dict(services or {}, pool=ts_pool),
    )
    status, payload = routes.dispatch(req)
    if isinstance(payload, routes.Raw):
        handler.send_response(status)
        handler.send_header("Content-Type", payload.content_type)
        handler.send_header("Content-Length", str(len(payload.body)))
        if payload.filename:
            safe = payload.filename.replace('"', "").replace("\r", "").replace("\n", "")
            handler.send_header("Content-Disposition", f'attachment; filename="{safe}"')
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.end_headers()
        handler.wfile.write(payload.body)
        return
    respond_json(handler, status, payload, default=str)

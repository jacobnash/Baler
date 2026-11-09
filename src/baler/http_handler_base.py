"""
Shared plumbing for the stdlib-http.server-based APIs (auth_api.py,
derivation_api.py, fault_api.py, ingest_api.py, validate_api.py) - see
AUDIT.md Theme A / PLAN.md Stage 2.1. jscpd found ~6% of the Python
codebase was this exact boilerplate (JSON responses, span-wrapped
request handling, CORS/OPTIONS, quiet request logging) reimplemented
independently in each of the 5 files.

Each service still runs as its own ThreadingHTTPServer/process/
container, built via its own make_handler() - this only removes the
duplicated glue between them, not the deliberate one-file-per-service,
no-framework architecture (a framework was already considered and
rejected for that, see docs-site/pages/architecture.mdx).
"""

import json
from http.server import BaseHTTPRequestHandler
from typing import Any


def respond_json(handler: BaseHTTPRequestHandler, status: int, payload, default=None, headers: dict | None = None) -> None:
    # default is json.dumps's own `default=` serializer hook - derivation_api.py
    # passes `str` since derivation traces can carry non-JSON-native values
    # (e.g. datetimes); every other caller leaves it None (json.dumps's own
    # default), unchanged from before this was shared.
    body = json.dumps(payload, default=default).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)


# Records per json.dumps call in json_pieces: ~2000 history samples is
# about a millisecond of encoding.
_JSON_CHUNK = 2000
_SCALARS = (str, int, float, bool, type(None))


def _flat(v) -> bool:
    """A scalar, or a list of scalars / scalar-valued dicts no longer than a
    chunk - cheap enough to encode in one call. Judged by the first item:
    a wrong guess only changes where the output is cut, never the output."""
    if isinstance(v, _SCALARS):
        return True
    if isinstance(v, list):
        if not v:
            return True
        first = v[0]
        return len(v) <= _JSON_CHUNK and (
            isinstance(first, _SCALARS) or (isinstance(first, dict) and all(isinstance(x, _SCALARS) for x in first.values()))
        )
    if isinstance(v, dict):
        return all(isinstance(x, _SCALARS) for x in v.values())
    return False


def json_pieces(obj):
    """json.dumps(obj), byte for byte, as a stream of strings, so that no
    single encode call runs long. json.dumps is one C call that holds the
    GIL from start to finish: 3.7 s for a 240 MB /his body, during which no
    other thread in the process - the health probe, POST /ingest - could
    run (#51). Containers that aren't small are split into their items,
    long lists of records into chunks of _JSON_CHUNK. Default separators,
    string keys (anything else is encoded whole)."""
    if isinstance(obj, dict) and not _flat(obj) and all(isinstance(k, str) for k in obj):
        yield "{"
        for i, (key, value) in enumerate(obj.items()):
            yield (", " if i else "") + json.dumps(key) + ": "
            yield from json_pieces(value)
        yield "}"
    elif isinstance(obj, list) and not _flat(obj):
        yield "["
        first = obj[0]
        if isinstance(first, _SCALARS) or (isinstance(first, dict) and _flat(first)):
            for i in range(0, len(obj), _JSON_CHUNK):
                yield (", " if i else "") + json.dumps(obj[i:i + _JSON_CHUNK])[1:-1]
        else:
            for i, item in enumerate(obj):
                if i:
                    yield ", "
                yield from json_pieces(item)
        yield "]"
    else:
        yield json.dumps(obj)


# Response headers that repeat a short page's paging meta (#280).
PAGING_HEADERS = "X-Baler-Incomplete, X-Baler-After, X-Baler-Limit, Link"


def respond_json_streamed(handler: BaseHTTPRequestHandler, status: int, payload, headers: dict | None = None) -> None:
    """respond_json for big payloads: the same bytes, encoded piecewise
    (json_pieces) and written in ~1 MB slices, so a large response never
    holds the GIL for more than a few milliseconds at a time."""
    pieces = [p.encode("utf-8") for p in json_pieces(payload)]
    handler.send_response(status)
    for k, v in (headers or {}).items():
        handler.send_header(k, v)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(sum(len(p) for p in pieces)))
    handler.end_headers()
    batch: list[bytes] = []
    size = 0
    for piece in pieces:
        batch.append(piece)
        size += len(piece)
        if size >= 1 << 20:
            handler.wfile.write(b"".join(batch))
            batch, size = [], 0
    if batch:
        handler.wfile.write(b"".join(batch))


def respond_error(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
    respond_json(handler, status, {"error": message})


class BaseAPIHandler(BaseHTTPRequestHandler):
    """Mix into each service's own handler class (in place of a bare
    BaseHTTPRequestHandler). Subclasses set TRACER to their module's own
    tracing.get_tracer(__name__) (kept separate per service so spans are
    still tagged by the service that emitted them), and may override
    ALLOWED_METHODS/ALLOWED_HEADERS (for do_OPTIONS) or extend
    ERROR_TYPES (for handle_traced's validation-error-to-400 mapping -
    see derivation_api.py's sandbox.SandboxError addition)."""

    TRACER: Any = None
    ALLOWED_METHODS = "GET, POST, OPTIONS"
    ALLOWED_HEADERS = "Content-Type"
    ERROR_TYPES: tuple = (KeyError, TypeError, ValueError, json.JSONDecodeError)

    def read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def handle_traced(self, span_name: str, fn) -> None:
        with self.TRACER.start_as_current_span(span_name) as span:
            try:
                fn(span)
            except self.ERROR_TYPES as exc:
                span.set_attribute("error", str(exc))
                respond_error(self, 400, str(exc))

    def serve_openapi_spec(self, span_name: str, spec_path: str) -> None:
        with self.TRACER.start_as_current_span(span_name):
            with open(spec_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/yaml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", self.ALLOWED_METHODS)
        self.send_header("Access-Control-Allow-Headers", self.ALLOWED_HEADERS)
        self.end_headers()

    def end_headers(self):
        # Lets the docs site's live try-it playground (served from a
        # different origin/port) call these APIs directly from the browser.
        self.send_header("Access-Control-Allow-Origin", "*")
        # A browser client on another origin may read the paging headers
        # (haystack_api.paging_headers, ingest_api's /his; #280).
        self.send_header("Access-Control-Expose-Headers", PAGING_HEADERS)
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # quiet by default; tracing carries the real signal

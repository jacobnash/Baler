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


def respond_json(handler: BaseHTTPRequestHandler, status: int, payload, default=None) -> None:
    # default is json.dumps's own `default=` serializer hook - derivation_api.py
    # passes `str` since derivation traces can carry non-JSON-native values
    # (e.g. datetimes); every other caller leaves it None (json.dumps's own
    # default), unchanged from before this was shared.
    body = json.dumps(payload, default=default).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


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
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # quiet by default; tracing carries the real signal

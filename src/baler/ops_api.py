"""
GET /status — building-engineer Ops status (Slice A).

Same shape as validate_api.py: stdlib http.server, own process/port,
OTel span per request, OpenAPI served off disk. Aggregates platform /
job probes; no start/stop yet (that's Slice C).
"""

import argparse
import os
import re
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

from baler import docs_ui, gateway_auth, sources, tracing
from baler.http_handler_base import BaseAPIHandler, respond_error, respond_json
from baler.ops_status import collect_status
from baler.timeseries import connect_pool

tracer = tracing.get_tracer(__name__)

OPENAPI_SPEC_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "ops-api-openapi.yaml")


def make_handler(ts_pool=None):
    class OpsHandler(BaseAPIHandler):
        TRACER = tracer
        ALLOWED_METHODS = "GET, POST, PATCH, DELETE, OPTIONS"

        def _gateway(self) -> bool:
            if gateway_auth.request_came_through_gateway(self):
                return True
            self.send_response(401)
            self.end_headers()
            return False

        # --- source registry (Baler#20); admin-only in gateway/njs/policy.js

        def _sources_get(self, span) -> None:
            path = urlparse(self.path).path
            if path == "/sources/kinds":
                respond_json(self, 200, sources.KINDS)
                return
            with ts_pool.connection() as conn:
                respond_json(self, 200, [sources.public(src) for src in sources.list_sources(conn)])

        def _sources_post(self, span) -> None:
            body = self.read_json_body()
            with ts_pool.connection() as conn:
                src = sources.create_source(conn, body.get("kind"), body.get("name"), body.get("settings") or {},
                                            bool(body.get("enabled", False)))
            respond_json(self, 201, sources.public(src))

        def _sources_patch(self, source_id: str, span) -> None:
            body = self.read_json_body()
            with ts_pool.connection() as conn:
                src = sources.update_source(conn, source_id, body.get("name"), body.get("settings"), body.get("enabled"))
            if src is None:
                respond_error(self, 404, "no such source")
                return
            respond_json(self, 200, sources.public(src))

        def _sources_delete(self, source_id: str, span) -> None:
            with ts_pool.connection() as conn:
                found = sources.delete_source(conn, source_id)
            self.send_response(204 if found else 404)
            self.end_headers()

        def do_POST(self):
            if not self._gateway():
                return
            if urlparse(self.path).path == "/sources":
                self.handle_traced("ops_api.post_source", self._sources_post)
                return
            self.send_response(404)
            self.end_headers()

        def do_PATCH(self):
            if not self._gateway():
                return
            m = re.fullmatch(r"/sources/([^/]+)", urlparse(self.path).path)
            if m:
                self.handle_traced("ops_api.patch_source", lambda span: self._sources_patch(m.group(1), span))
                return
            self.send_response(404)
            self.end_headers()

        def do_DELETE(self):
            if not self._gateway():
                return
            m = re.fullmatch(r"/sources/([^/]+)", urlparse(self.path).path)
            if m:
                self.handle_traced("ops_api.delete_source", lambda span: self._sources_delete(m.group(1), span))
                return
            self.send_response(404)
            self.end_headers()

        def do_GET(self):
            if self.path == "/openapi.yaml":
                self.serve_openapi_spec("ops_api.get_openapi_spec", OPENAPI_SPEC_PATH)
                return
            if self.path == "/docs":
                docs_ui.serve(self)
                return
            if urlparse(self.path).path in ("/sources", "/sources/kinds"):
                if not self._gateway():
                    return
                self.handle_traced("ops_api.get_sources", self._sources_get)
                return
            if self.path == "/status":
                if not gateway_auth.request_came_through_gateway(self):
                    self.send_response(401)
                    self.end_headers()
                    return
                self.handle_traced("ops_api.get_status", self._get_status)
                return
            self.send_response(404)
            self.end_headers()

        def _get_status(self, span) -> None:
            payload = collect_status()
            span.set_attribute("summary", payload["summary"])
            span.set_attribute("attention_count", len(payload["attention"]))
            respond_json(self, 200, payload)

    return OpsHandler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8007)
    args = parser.parse_args()

    tracing.init_tracing("baler-ops-api")

    ts_pool = connect_pool(statement_timeout="55s")
    with ts_pool.connection() as conn:
        sources.ensure_schema(conn)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(ts_pool))
    print(f"ops API listening on {args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

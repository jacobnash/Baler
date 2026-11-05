"""
GET /status — building-engineer Ops status (Slice A).

Same shape as validate_api.py: stdlib http.server, own process/port,
OTel span per request, OpenAPI served off disk. Aggregates platform /
job probes; no start/stop yet (that's Slice C).
"""

import argparse
import os
from http.server import ThreadingHTTPServer

from baler import docs_ui, gateway_auth, tracing
from baler.http_handler_base import BaseAPIHandler, respond_json
from baler.ops_status import collect_status

tracer = tracing.get_tracer(__name__)

OPENAPI_SPEC_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "ops-api-openapi.yaml")


def make_handler():
    class OpsHandler(BaseAPIHandler):
        TRACER = tracer
        ALLOWED_METHODS = "GET, OPTIONS"

        def do_GET(self):
            if self.path == "/openapi.yaml":
                self.serve_openapi_spec("ops_api.get_openapi_spec", OPENAPI_SPEC_PATH)
                return
            if self.path == "/docs":
                docs_ui.serve(self)
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

    server = ThreadingHTTPServer((args.host, args.port), make_handler())
    print(f"ops API listening on {args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

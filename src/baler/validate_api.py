"""
POST /validate - checks the live entity graph against Brick's own SHACL
shapes, on demand. Same shape as ingest_api.py/fault_api.py/
derivation_api.py: stdlib http.server, own process/port, OTel span per
request, spec served straight off disk. Always validates *current live*
Oxigraph state, no request body - keeps v1 simple (see
shacl_validate.py / todo/shacl-validation-and-service.md).
"""

import argparse
import os
from http.server import ThreadingHTTPServer

from baler import auth, docs_ui, gateway_auth, tracing
from baler.http_handler_base import BaseAPIHandler, respond_json
from baler.remote_store import RemoteStore
from baler.scope import request_scope
from baler.shacl_validate import (
    graph_from_remote_store,
    load_shapes,
    validate_graph,
)
from baler.timeseries import connect_pool

tracer = tracing.get_tracer(__name__)

OPENAPI_SPEC_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "validate-api-openapi.yaml")


def make_handler(store, ts_pool=None):
    class ValidateHandler(BaseAPIHandler):
        TRACER = tracer
        # ALLOWED_METHODS/ALLOWED_HEADERS match BaseAPIHandler's own
        # defaults ("GET, POST, OPTIONS" / "Content-Type") - no override
        # needed here.

        def do_POST(self):
            if self.path == "/validate":
                if not gateway_auth.request_came_through_gateway(self):
                    self.send_response(401)
                    self.end_headers()
                    return
                self._post_validate()
                return
            self.send_response(404)
            self.end_headers()

        def _post_validate(self) -> None:
            with tracer.start_as_current_span("validate_api.post_validate") as span:
                data_graph = graph_from_remote_store(store)
                span.set_attribute("triple_count", len(data_graph))
                result = validate_graph(data_graph)
                if ts_pool is not None:
                    with ts_pool.connection() as conn:
                        scope = request_scope(conn, self.headers)
                    if not scope.unrestricted:
                        # Only this caller's sites' violations, and whether
                        # *those* conform - not a hint about anyone else's.
                        mine = [v for v in result["violations"] if scope.allows(v["focus_node"])]
                        result = {**result, "violations": mine, "conforms": not mine}
                span.set_attribute("conforms", result["conforms"])
                span.set_attribute("violation_count", len(result["violations"]))
                # Conformance is data in the response, not an error status -
                # matching derivation_api.py's /dry-run: 200 either way.
                respond_json(self, 200, result)

        def do_GET(self):
            if self.path == "/openapi.yaml":
                self.serve_openapi_spec("validate_api.get_openapi_spec", OPENAPI_SPEC_PATH)
                return
            if self.path == "/docs":
                docs_ui.serve(self)
                return
            self.send_response(404)
            self.end_headers()

    return ValidateHandler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8005)
    args = parser.parse_args()

    tracing.init_tracing("baler-validate-api")

    store = RemoteStore()
    load_shapes()  # parse once at startup, not on the first request
    ts_pool = connect_pool()
    with ts_pool.connection() as conn:
        auth.ensure_schema(conn)  # sites.topic_prefixes, read by the scope check

    server = ThreadingHTTPServer((args.host, args.port), make_handler(store, ts_pool))
    print(f"validate API listening on {args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

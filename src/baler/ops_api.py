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

from baler import (
    auth,
    docs_ui,
    edges,
    gateway_auth,
    orphans,
    record_edit,
    sources,
    tracing,
)
from baler.http_handler_base import BaseAPIHandler, respond_error, respond_json
from baler.ops_status import collect_status, start_background_probes
from baler.scope import claims_from_header, scope_for
from baler.timeseries import connect_pool

tracer = tracing.get_tracer(__name__)

OPENAPI_SPEC_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "ops-api-openapi.yaml")


_EDGE_ACTION = re.compile(r"/edges/([^/]+)/([^/]+)/actions/([a-z_]+)")


def make_handler(ts_pool=None, store=None, edge_api: edges.Edges | None = None):
    edge_holder = [edge_api]

    def _edges() -> edges.Edges:
        if edge_holder[0] is None:
            edge_holder[0] = edges.Edges()
        return edge_holder[0]

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

        # --- orphaned points; admin-only in gateway/njs/policy.js

        def _store(self):
            nonlocal store
            if store is None:
                from baler.remote_store import RemoteStore

                store = RemoteStore()
            return store

        def _orphans_get(self, span) -> None:
            with ts_pool.connection() as conn:
                scope = scope_for(conn, claims_from_header(self.headers.get("Authorization")))
                items = orphans.find(self._store(), conn, scope)
            span.set_attribute("count", len(items))
            respond_json(self, 200, items)

        def _orphans_remove(self, span) -> None:
            body = self.read_json_body()
            claims = claims_from_header(self.headers.get("Authorization")) or {}
            with ts_pool.connection() as conn:
                scope = scope_for(conn, claims)
                sub = claims.get("sub")
                user = auth.get_user(conn, sub) if sub and claims.get("kind") == "session" else None
                result = orphans.remove(self._store(), conn, body.get("ids"), bool(body.get("purge_history")),
                                        (sub, user["email"] if user else sub), scope)
            span.set_attribute("removed", len(result["removed"]))
            respond_json(self, 200, result)

        # --- edge nodes and connectors (Connectors page); policy.js rows

        def _edges_get(self, span) -> None:
            try:
                nodes = _edges().nodes()
            except edges.EdgeError as exc:
                respond_error(self, exc.status, str(exc))
                return
            span.set_attribute("count", len(nodes))
            respond_json(self, 200, nodes)

        def _edge_action(self, site: str, instance: str, action: str, span) -> None:
            span.set_attribute("edge", f"{site}/{instance}")
            span.set_attribute("action", action)
            body = self.read_json_body() if int(self.headers.get("Content-Length") or 0) else {}
            if not isinstance(body, dict):
                respond_error(self, 400, "body must be a JSON object")
                return
            try:
                result = _edges().call(site, instance, action, body)
            except edges.EdgeError as exc:
                respond_error(self, exc.status, str(exc))
                return
            respond_json(self, 200, {"result": result})

        def do_POST(self):
            if not self._gateway():
                return
            m = _EDGE_ACTION.fullmatch(urlparse(self.path).path)
            if m:
                self.handle_traced("ops_api.edge_action", lambda span: self._edge_action(m.group(1), m.group(2), m.group(3), span))
                return
            if urlparse(self.path).path == "/orphans/remove":
                self.handle_traced("ops_api.remove_orphans", self._orphans_remove)
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
            if urlparse(self.path).path == "/orphans":
                if not self._gateway():
                    return
                self.handle_traced("ops_api.get_orphans", self._orphans_get)
                return
            if urlparse(self.path).path == "/edges":
                if not self._gateway():
                    return
                self.handle_traced("ops_api.get_edges", self._edges_get)
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
            scope = None
            if ts_pool is not None:
                with ts_pool.connection() as conn:
                    scope = scope_for(conn, claims_from_header(self.headers.get("Authorization")))
            payload = collect_status(scope)
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
        record_edit.ensure_schema(conn)
    start_background_probes()  # /status answers from the latest round (Baler#106)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(ts_pool))
    print(f"ops API listening on {args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

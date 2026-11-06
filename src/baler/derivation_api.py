"""
CRUD for derivations, plus test/dry-run validation and per-target health
management - mirrors fault_api.py's exact shape (stdlib http.server, one
Postgres connection per request from a pool, OTel span per request, spec
served straight off disk). Own process, own port - same reasoning as
fault_api.py getting its own OpenAPI spec rather than extending
ingest_api.py's or fault_api.py's.

POST /derivations and POST /derivations/test both execute submitted Python
(via sandbox.compile_fn/run_test_cases) as part of validation - see
derivation-api-openapi.yaml's security note. This must never be exposed
past a fully trusted, localhost-only boundary.
"""

import argparse
import json
import os
import re
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from typing import cast
from urllib.parse import unquote

from baler import (
    auth,
    derivation_engine,
    derivation_health,
    docs_ui,
    gateway_auth,
    json_store,
    sandbox,
    tracing,
)
from baler.http_handler_base import BaseAPIHandler, respond_json
from baler.remote_store import RemoteStore
from baler.schemas import Derivation
from baler.scope import request_scope
from baler.timeseries import connect_pool

tracer = tracing.get_tracer(__name__)

OPENAPI_SPEC_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "derivation-api-openapi.yaml")

# Guards read-modify-write on derivations.json against concurrent requests
# within this one process - same documented, accepted gap as fault_api.py's
# rules.json/webhooks.json (json_store.py's own docstring): not a
# cross-process concurrent-writer solution.
_file_lock = threading.Lock()

_TARGETS_RE = re.compile(r"^/derivations/([^/]+)/targets$")
_ENABLE_TARGET_RE = re.compile(r"^/derivations/([^/]+)/targets/([^/]+)/enable$")

# Derivation traces can carry non-JSON-native values (datetimes), unlike
# every other service's plain JSON payloads - respond_json's default=str
# keeps this file's json.dumps(payload, default=str) behavior unchanged.
_respond_json = partial(respond_json, default=str)


def _validate_fn_and_test_cases(fn_source: str, test_cases: list[dict]) -> None:
    if not test_cases:
        raise ValueError("test_cases must be a non-empty list - submitted code that will run unattended needs test coverage")
    fn = sandbox.compile_fn(fn_source)
    results = sandbox.run_test_cases(fn, test_cases)
    failed = [r for r in results if not r["passed"]]
    if failed:
        raise ValueError(f"test_cases failed: {json.dumps(failed, default=str)}")


def make_handler(derivations_path: str, store, ts_pool):
    class DerivationHandler(BaseAPIHandler):
        TRACER = tracer
        ALLOWED_METHODS = "GET, POST, DELETE, OPTIONS"
        ERROR_TYPES = (*BaseAPIHandler.ERROR_TYPES, sandbox.SandboxError)

        def _require_gateway(self) -> bool:
            if gateway_auth.request_came_through_gateway(self):
                return True
            self.send_response(401)
            self.end_headers()
            return False

        def do_POST(self):
            if not self._require_gateway():
                return
            if self.path == "/derivations":
                self.handle_traced("api.post_derivations", self._create_derivation)
                return
            if self.path == "/derivations/test":
                self.handle_traced("api.test_derivation", self._test_derivation)
                return
            if self.path == "/derivations/dry-run":
                self.handle_traced("api.dry_run_derivation", self._dry_run)
                return
            match = _ENABLE_TARGET_RE.match(self.path)
            if match:
                derivation_id, target_uri = match.group(1), unquote(match.group(2))
                self.handle_traced("api.enable_target", lambda span: self._enable_target(derivation_id, target_uri, span))
                return
            self.send_response(404)
            self.end_headers()

        def do_GET(self):
            if self.path == "/openapi.yaml":
                self.serve_openapi_spec("api.get_openapi_spec", OPENAPI_SPEC_PATH)
                return
            if self.path == "/docs":
                docs_ui.serve(self)
                return
            if not self._require_gateway():
                return
            if self.path == "/derivations":
                self.handle_traced("api.get_derivations", lambda span: _respond_json(self, 200, json_store.load(derivations_path)))
                return
            match = _TARGETS_RE.match(self.path)
            if match:
                self.handle_traced("api.get_targets", lambda span: self._list_targets(match.group(1), span))
                return
            self.send_response(404)
            self.end_headers()

        def do_DELETE(self):
            if not self._require_gateway():
                return
            if self.path.startswith("/derivations/"):
                derivation_id = self.path[len("/derivations/"):]
                self.handle_traced("api.delete_derivation", lambda span: self._delete(derivation_id, span))
                return
            self.send_response(404)
            self.end_headers()

        def _create_derivation(self, span) -> None:
            body = self.read_json_body()
            fn_source = body["fn_source"]
            test_cases = body["test_cases"]
            _validate_fn_and_test_cases(fn_source, test_cases)

            reserved = {"name", "kind", "fn_source", "test_cases", "window_seconds", "output", "depends_on"}
            derivation: Derivation = {
                "id": json_store.new_id(),
                "name": body["name"],
                "kind": body.get("kind", "formula"),
                "fn_source": fn_source,
                "test_cases": test_cases,
                "window_seconds": body.get("window_seconds", 3600),
                "output": body.get("output", {}),
                "depends_on": body.get("depends_on", []),
                **{k: v for k, v in body.items() if k not in reserved},
            }
            with _file_lock:
                derivations = cast(list[Derivation], json_store.load(derivations_path))
                derivations.append(derivation)
                json_store.save(derivations_path, derivations)
            span.set_attribute("derivation_id", derivation["id"])
            _respond_json(self, 201, derivation)

        def _test_derivation(self, span) -> None:
            body = self.read_json_body()
            fn = sandbox.compile_fn(body["fn_source"])
            results = sandbox.run_test_cases(fn, body.get("test_cases", []))
            span.set_attribute("passed", all(r["passed"] for r in results))
            _respond_json(self, 200, results)

        def _dry_run(self, span) -> None:
            body = self.read_json_body()
            derivation: Derivation
            if "id" in body and set(body.keys()) <= {"id"}:
                derivations = cast(list[Derivation], json_store.load(derivations_path))
                found_derivation = next((d for d in derivations if d["id"] == body["id"]), None)
                if found_derivation is None:
                    raise ValueError(f"no derivation with id {body['id']!r}")
                derivation = found_derivation
            else:
                # A draft dry-run body may omit fields a persisted Derivation
                # requires (e.g. name) - dry-run only reads what the engine
                # actually needs (fn_source, select/root_select, etc.), so
                # this is a deliberately lenient cast, not a real guarantee.
                derivation = cast(Derivation, {**body, "id": body.get("id", "draft")})
                sandbox.compile_fn(derivation["fn_source"])  # fail fast with a clear 400 instead of a silently-skipped trace

            span.set_attribute("derivation_id", derivation["id"])
            with ts_pool.connection() as conn:
                trace = derivation_engine.evaluate_derivations(store, conn, conn, conn, [derivation], dry_run=True)
                scope = request_scope(conn, self.headers)
            if not scope.unrestricted:
                # Outputs are urn:point:computed/..., outside every site; a
                # row belongs to the site of the equipment it attaches to.
                trace = [t for t in trace if scope.allows(t.get("would_attach_to", ""))]
            span.set_attribute("target_count", len(trace))
            _respond_json(self, 200, trace)

        def _list_targets(self, derivation_id: str, span) -> None:
            with ts_pool.connection() as conn:
                targets = derivation_health.list_targets(conn, derivation_id)
                scope = request_scope(conn, self.headers)
            targets = [t for t in targets if scope.allows(t["target_uri"])]
            span.set_attribute("count", len(targets))
            _respond_json(self, 200, targets)

        def _enable_target(self, derivation_id: str, target_uri: str, span) -> None:
            span.set_attribute("derivation_id", derivation_id)
            span.set_attribute("target", target_uri)
            with ts_pool.connection() as conn:
                allowed = request_scope(conn, self.headers).allows(target_uri)
                target = derivation_health.enable_target(conn, derivation_id, target_uri) if allowed else None
            if target is None:
                self.send_response(404)
                self.end_headers()
                return
            _respond_json(self, 200, target)

        def _delete(self, derivation_id: str, span) -> None:
            span.set_attribute("id", derivation_id)
            with _file_lock:
                derivations = cast(list[Derivation], json_store.load(derivations_path))
                remaining = [d for d in derivations if d["id"] != derivation_id]
                found = len(remaining) != len(derivations)
                if found:
                    json_store.save(derivations_path, remaining)
            span.set_attribute("found", found)
            self.send_response(204 if found else 404)
            self.end_headers()

    return DerivationHandler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8003)
    parser.add_argument("--derivations-file", default=json_store.data_file("derivations.json"))
    parser.add_argument("--oxigraph-url", default=os.environ.get("OXIGRAPH_URL", "http://localhost:7878"))
    args = parser.parse_args()

    tracing.init_tracing("baler-derivation-api")

    store = RemoteStore(args.oxigraph_url)
    ts_pool = connect_pool()
    with ts_pool.connection() as conn:
        derivation_health.ensure_schema(conn)
        auth.ensure_schema(conn)  # sites.topic_prefixes, read by the scope checks

    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.derivations_file, store, ts_pool))
    print(f"derivation API listening on {args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

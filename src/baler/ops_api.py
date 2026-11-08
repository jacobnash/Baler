"""
GET /status — building-engineer Ops status (Slice A). Also the read-only
post-deploy verification signals (GET /freshness, GET /gateway-errors,
Baler#178; see ops_freshness.py).

Same shape as validate_api.py: stdlib http.server, own process/port,
OTel span per request, OpenAPI served off disk. Aggregates platform /
job probes; no start/stop yet (that's Slice C).
"""

import argparse
import os
import re
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from baler import (
    auth,
    docs_ui,
    edges,
    gateway_auth,
    json_store,
    mapping_proposals,
    mapping_review,
    ops_freshness,
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

        # --- mapping review queue (Baler#184); policy.js rows

        def _mapping_context(self, conn):
            claims = claims_from_header(self.headers.get("Authorization")) or {}
            scope = scope_for(conn, claims)
            props = mapping_proposals.PgProposals(conn)
            props.ensure_schema()
            return claims, scope, props

        def _mapping_proposals_get(self, span) -> None:
            from baler import brick_vocab

            with ts_pool.connection() as conn:
                _claims, scope, props = self._mapping_context(conn)
                rows = props.list(mapping_proposals.HELD, limit=20000)
                rules = json_store.load(json_store.data_file("rules.json"))
                needs = mapping_review.fault_rule_needs(rules, brick_vocab.load())
                result = mapping_review.groups(rows, needs, mapping_review.active_cx_prefixes(conn), scope.allows)
            span.set_attribute("groups", len(result))
            respond_json(self, 200, result)

        def _mapping_summary_get(self, span) -> None:
            with ts_pool.connection() as conn:
                _claims, scope, props = self._mapping_context(conn)
                rows = props.list(mapping_proposals.HELD, limit=100000)
                payload = mapping_review.summary(self._store(), rows, mapping_review.person_classified(conn), scope.allows)
            respond_json(self, 200, payload)

        def _mapping_decide(self, span) -> None:
            from baler.commissioning import db as cx_db

            body = self.read_json_body()
            ids = body.get("ids")
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                respond_error(self, 400, "ids is a list of proposal ids")
                return
            with ts_pool.connection() as conn:
                claims, scope, props = self._mapping_context(conn)
                sub = claims.get("sub")
                user = auth.get_user(conn, sub) if sub and claims.get("kind") == "session" else None
                who = {"sub": sub, "email": user["email"] if user else sub}
                try:
                    result = mapping_review.decide(
                        props, self._store(), conn, ids, str(body.get("action") or ""), who,
                        brick_class=body.get("brick_class"), assignee=body.get("assignee"), note=body.get("note"),
                        save_rule=bool(body.get("save_rule")), allows=scope.allows,
                        webhooks_path=json_store.data_file("webhooks.json"),
                        cx_repo=cx_db.PgRepo(ts_pool, self._store()),
                    )
                except mapping_review.DecisionError as exc:
                    status = 404 if str(exc).startswith("no such proposal") else 409 if str(exc).startswith("already decided") else 400
                    respond_error(self, status, str(exc))
                    return
            span.set_attribute("action", result["action"])
            span.set_attribute("decided", result["decided"])
            respond_json(self, 200, result)

        # --- edge nodes and connectors (Connectors page); policy.js rows

        def _edges_get(self, span) -> None:
            try:
                nodes = _edges().nodes()
            except edges.EdgeError as exc:
                respond_error(self, exc.status, str(exc))
                return
            self._attach_site_prefixes(nodes)
            span.set_attribute("count", len(nodes))
            respond_json(self, 200, nodes)

        def _site_prefixes(self, usernames: list[str]) -> dict[str, tuple[str, list[str]]] | None:
            """{broker username: (site name, topic_prefixes)} for the
            caller's org. A remote node's site_id is its broker account's
            username, not a Baler site id: the account (broker_accounts)
            names the site whose prefixes the broker holds it to. Read here,
            for any role - the page used to call /auth/sites, which is
            admin-only, so other roles silently got no check (Baler#147).
            None when it can't be read (no auth schema yet)."""
            if ts_pool is None or not usernames:
                return {}
            claims = claims_from_header(self.headers.get("Authorization")) or {}
            try:
                with ts_pool.connection() as conn:
                    rows = conn.execute(
                        "SELECT b.username, b.org_id, s.name, s.topic_prefixes FROM broker_accounts b "
                        "JOIN sites s ON s.id = b.site_id WHERE b.username = ANY(%s)",
                        (usernames,),
                    ).fetchall()
            except Exception as exc:  # noqa: BLE001 - the page says it couldn't check; the broker still enforces
                print(f"edge site prefixes unavailable: {exc!r}")
                return None
            org = claims.get("org_id")
            return {r[0]: (r[2], list(r[3] or [])) for r in rows if not org or r[1] == org}

        def _attach_site_prefixes(self, nodes: list[dict]) -> None:
            """Each remote node's allowed_prefixes and site_name, for the
            Connectors page (Baler#111)."""
            remote = [n for n in nodes if n.get("kind") == "remote"]
            found = self._site_prefixes([n["site_id"] for n in remote]) or {}
            for n in remote:
                if n["site_id"] in found:
                    n["site_name"], n["allowed_prefixes"] = found[n["site_id"]]

        def _allowed_prefixes(self, site: str, instance: str) -> list[str] | None:
            if (site, instance) == edges.LOCAL_ID:
                return list(edges.LOCAL_TOPIC_ROOTS)
            found = self._site_prefixes([site]) or {}
            return found[site][1] if site in found else None

        def _edge_action(self, site: str, instance: str, action: str, span) -> None:
            span.set_attribute("edge", f"{site}/{instance}")
            span.set_attribute("action", action)
            body = self.read_json_body() if int(self.headers.get("Content-Length") or 0) else {}
            if not isinstance(body, dict):
                respond_error(self, 400, "body must be a JSON object")
                return
            if action in edges.PREFIX_ACTIONS and (action.endswith("create_connection") or "topic_prefix" in body):
                # Checked here as well as on the page, so no client can make a
                # connector whose data the broker or the listener drops.
                problem = edges.prefix_problem(body.get("topic_prefix"), self._allowed_prefixes(site, instance))
                if problem:
                    respond_error(self, 400, problem)
                    return
            try:
                result = _edges().call(site, instance, action, body)
            except edges.EdgeError as exc:
                respond_error(self, exc.status, str(exc))
                return
            respond_json(self, 200, {"result": result})

        # --- post-deploy verification (Baler#178); read-only, policy.js rows

        def _freshness_get(self, span) -> None:
            query = parse_qs(urlparse(self.path).query)
            try:
                prefix = ops_freshness.check_prefix(query.get("prefix", [""])[0])
            except ValueError as exc:
                respond_error(self, 400, str(exc))
                return
            span.set_attribute("prefix", prefix)
            with ts_pool.connection() as conn:
                scope = scope_for(conn, claims_from_header(self.headers.get("Authorization")))
                payload = ops_freshness.freshness(conn, prefix, scope)
            span.set_attribute("count", len(payload["points"]))
            respond_json(self, 200, payload)

        def _gateway_errors_get(self, span) -> None:
            query = parse_qs(urlparse(self.path).query)
            try:
                since = ops_freshness.parse_time(query.get("since", [None])[0], "since")
                until = ops_freshness.parse_time(query.get("until", [None])[0], "until")
            except ValueError as exc:
                respond_error(self, 400, str(exc))
                return
            payload = ops_freshness.gateway_errors(since, until)
            span.set_attribute("count", payload["count"])
            respond_json(self, 200, payload)

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
            if urlparse(self.path).path == "/mapping/decide":
                self.handle_traced("ops_api.mapping_decide", self._mapping_decide)
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
            if urlparse(self.path).path == "/mapping/proposals":
                if not self._gateway():
                    return
                self.handle_traced("ops_api.mapping_proposals", self._mapping_proposals_get)
                return
            if urlparse(self.path).path == "/mapping/summary":
                if not self._gateway():
                    return
                self.handle_traced("ops_api.mapping_summary", self._mapping_summary_get)
                return
            if urlparse(self.path).path == "/freshness":
                if not self._gateway():
                    return
                self.handle_traced("ops_api.get_freshness", self._freshness_get)
                return
            if urlparse(self.path).path == "/gateway-errors":
                if not self._gateway():
                    return
                self.handle_traced("ops_api.get_gateway_errors", self._gateway_errors_get)
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
        mapping_proposals.PgProposals(conn).ensure_schema()
    start_background_probes()  # /status answers from the latest round (Baler#106)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(ts_pool))
    print(f"ops API listening on {args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

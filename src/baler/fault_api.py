"""
CRUD for fault-detection rules and webhook subscriptions, plus GET /faults
- mirrors ingest_api.py's exact shape: stdlib http.server, one Postgres
connection per request from a pool, OTel span per request, spec served
straight off disk. Own process, own port - a second, independently-run
API, which is exactly why it gets its own OpenAPI spec file rather than
extending openapi.yaml (see fault-api-openapi.yaml's own note on this).
"""

import argparse
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from typing import cast
from urllib.parse import parse_qs, urlparse

from baler import auth, docs_ui, gateway_auth, json_store, tracing
from baler.faults import (
    ack_fault,
    enable_webhook,
    ensure_schema,
    fault_point_uri,
    list_faults,
    list_webhook_health,
    snooze_fault,
)
from baler.http_handler_base import BaseAPIHandler
from baler.http_handler_base import respond_json as _respond_json
from baler.schemas import CreateWebhookRequest, Webhook, WebhookHealth
from baler.scope import request_scope
from baler.timeseries import connect_pool
from baler.webhooks import validate_url

tracer = tracing.get_tracer(__name__)

OPENAPI_SPEC_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "fault-api-openapi.yaml")

_ACK_RE = re.compile(r"^/faults/(\d+)/ack$")
_SNOOZE_RE = re.compile(r"^/faults/(\d+)/snooze$")

# Guards read-modify-write on rules.json/webhooks.json against concurrent
# requests within this one process (ThreadingHTTPServer = one thread per
# request). Does NOT solve cross-process concurrent writers - that's a
# documented, accepted gap (json_store.py's own docstring), unchanged here.
_file_lock = threading.Lock()


def _redact_webhook(webhook: Webhook) -> Webhook:
    return {**webhook, "secret": None}


def make_handler(rules_path: str, webhooks_path: str, ts_pool):
    class FaultHandler(BaseAPIHandler):
        TRACER = tracer
        ALLOWED_METHODS = "GET, POST, DELETE, OPTIONS"

        def _require_gateway(self) -> bool:
            if gateway_auth.request_came_through_gateway(self):
                return True
            self.send_response(401)
            self.end_headers()
            return False

        def do_POST(self):
            if not self._require_gateway():
                return
            if self.path == "/rules":
                self.handle_traced("api.post_rules", self._create_rule)
                return
            if self.path == "/webhooks":
                self.handle_traced("api.post_webhooks", self._create_webhook)
                return
            if self.path.startswith("/webhooks/") and self.path.endswith("/enable"):
                webhook_id = self.path[len("/webhooks/"):-len("/enable")]
                self.handle_traced("api.post_webhook_enable", lambda span: self._enable_webhook(webhook_id, span))
                return
            ack_match = _ACK_RE.match(self.path)
            if ack_match:
                self.handle_traced("api.ack_fault", lambda span: self._ack_fault(int(ack_match.group(1)), span))
                return
            snooze_match = _SNOOZE_RE.match(self.path)
            if snooze_match:
                self.handle_traced("api.snooze_fault", lambda span: self._snooze_fault(int(snooze_match.group(1)), span))
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
            if self.path == "/rules":
                self.handle_traced("api.get_rules", lambda span: _respond_json(self, 200, json_store.load(rules_path)))
                return
            if self.path == "/webhooks":
                def handle(span):
                    with ts_pool.connection() as conn:
                        health = list_webhook_health(conn)
                    default_health: WebhookHealth = {"disabled": False, "consecutive_failures": 0, "last_error": None}
                    webhooks = [
                        {**_redact_webhook(w), **health.get(w["id"], default_health)}
                        for w in cast(list[Webhook], json_store.load(webhooks_path))
                    ]
                    _respond_json(self, 200, webhooks)

                self.handle_traced("api.get_webhooks", handle)
                return
            if self.path.startswith("/faults"):
                self.handle_traced("api.get_faults", self._list_faults)
                return
            self.send_response(404)
            self.end_headers()

        def do_DELETE(self):
            if not self._require_gateway():
                return
            if self.path.startswith("/rules/"):
                rule_id = self.path[len("/rules/"):]
                self.handle_traced("api.delete_rule", lambda span: self._delete(rules_path, rule_id, span))
                return
            if self.path.startswith("/webhooks/"):
                webhook_id = self.path[len("/webhooks/"):]
                self.handle_traced("api.delete_webhook", lambda span: self._delete(webhooks_path, webhook_id, span))
                return
            self.send_response(404)
            self.end_headers()

        def _create_rule(self, span) -> None:
            body = self.read_json_body()
            rule = {
                "id": json_store.new_id(),
                "name": body["name"],
                "mode": body["mode"],  # "cur" | "his"
                "type": body["type"],  # "range" | "stuck" | "stale"
                "applies_to": body["applies_to"],
                **{k: v for k, v in body.items() if k not in ("name", "mode", "type", "applies_to")},
            }
            with _file_lock:
                rules = json_store.load(rules_path)
                rules.append(rule)
                json_store.save(rules_path, rules)
            span.set_attribute("rule_id", rule["id"])
            _respond_json(self, 201, rule)

        def _create_webhook(self, span) -> None:
            body = cast(CreateWebhookRequest, self.read_json_body())
            # Same deployment-level escape hatch as fault_detector.py's
            # delivery-time check - never a per-request/per-webhook toggle.
            allow_private = os.environ.get("BALER_ALLOW_PRIVATE_WEBHOOKS") == "1"
            validate_url(body["url"], allow_private=allow_private)  # raises ValueError -> 400, via handle_traced's except clause
            webhook: Webhook = {
                "id": json_store.new_id(),
                "url": body["url"],
                "secret": body["secret"],
                "filter": body.get("filter"),
            }
            with _file_lock:
                webhooks = cast(list[Webhook], json_store.load(webhooks_path))
                webhooks.append(webhook)
                json_store.save(webhooks_path, webhooks)
            span.set_attribute("webhook_id", webhook["id"])
            _respond_json(self, 201, webhook)  # secret shown once, at creation - redacted on every later GET

        def _delete(self, path: str, record_id: str, span) -> None:
            span.set_attribute("id", record_id)
            with _file_lock:
                records = json_store.load(path)
                remaining = [r for r in records if r["id"] != record_id]
                found = len(remaining) != len(records)
                if found:
                    json_store.save(path, remaining)
            span.set_attribute("found", found)
            self.send_response(204 if found else 404)
            self.end_headers()

        def _enable_webhook(self, webhook_id: str, span) -> None:
            span.set_attribute("webhook_id", webhook_id)
            webhooks = json_store.load(webhooks_path)
            if not any(w["id"] == webhook_id for w in webhooks):
                self.send_response(404)
                self.end_headers()
                return
            with ts_pool.connection() as conn:
                enable_webhook(conn, webhook_id)
            self.send_response(204)
            self.end_headers()

        def _in_scope(self, conn, fault_id: int) -> bool:
            """A fault on another site's point is a 404, same as a missing
            one, so its existence doesn't leak."""
            uri = fault_point_uri(conn, fault_id)
            return uri is not None and request_scope(conn, self.headers).allows(uri)

        def _ack_fault(self, fault_id: int, span) -> None:
            span.set_attribute("fault_id", fault_id)
            # X-User is gateway-set ("<sub>:<role>", see gateway/njs/main.js)
            # and documented there as logging/tracing only, not an authz
            # input - fine for "who acked this" attribution, same trust
            # level as any other audit-trail field this API already writes.
            acked_by = self.headers.get("X-User", "unknown")
            with ts_pool.connection() as conn:
                found = self._in_scope(conn, fault_id) and ack_fault(conn, fault_id, acked_by, datetime.now(timezone.utc))
            if not found:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(204)
            self.end_headers()

        def _snooze_fault(self, fault_id: int, span) -> None:
            span.set_attribute("fault_id", fault_id)
            body = self.read_json_body()
            minutes = body["minutes"]
            if not isinstance(minutes, (int, float)) or minutes <= 0:
                raise ValueError("minutes must be a positive number")
            until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
            with ts_pool.connection() as conn:
                found = self._in_scope(conn, fault_id) and snooze_fault(conn, fault_id, until)
            if not found:
                self.send_response(404)
                self.end_headers()
                return
            _respond_json(self, 200, {"id": fault_id, "snoozed_until": until.isoformat()})

        def _list_faults(self, span) -> None:
            query = parse_qs(urlparse(self.path).query)
            status = query.get("status", [None])[0]
            point_uri = query.get("point_uri", [None])[0]
            rule_id = query.get("rule_id", [None])[0]
            with ts_pool.connection() as conn:
                faults = list_faults(conn, status=status, point_uri=point_uri, rule_id=rule_id)
                scope = request_scope(conn, self.headers)
            faults = [f for f in faults if scope.allows(f["point_uri"])]
            span.set_attribute("count", len(faults))
            _respond_json(self, 200, faults)

    return FaultHandler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--rules-file", default=json_store.data_file("rules.json"))
    parser.add_argument("--webhooks-file", default=json_store.data_file("webhooks.json"))
    args = parser.parse_args()

    tracing.init_tracing("baler-fault-api")

    ts_pool = connect_pool()
    with ts_pool.connection() as conn:
        ensure_schema(conn)
        auth.ensure_schema(conn)  # sites.topic_prefixes, read by the scope checks

    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.rules_file, args.webhooks_file, ts_pool))
    print(f"fault API listening on {args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

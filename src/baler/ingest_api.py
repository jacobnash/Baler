"""
POST /ingest for push-style sources that would rather push directly than
run their own MQTT loop. Same envelope as mqtt_listener, same
ingest_reading underneath - two front doors, one code path. Stdlib
http.server rather than a new web framework dependency for one route.
"""

import argparse
import json
import os
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from rdflib import URIRef

from baler import docs_ui, gateway_auth, hisquery, tracing
from baler.http_handler_base import BaseAPIHandler
from baler.http_handler_base import respond_error as _respond_error
from baler.http_handler_base import respond_json as _respond_json
from baler.ingest import (
    ingest_reading,
    ingest_tags,
    link_equip_ref,
    link_part_of,
    merge_equip,
    topic_to_point_uri,
)
from baler.mapping import reclassify
from baler.remote_store import RemoteStore
from baler.scope import claims_from_header, scope_for
from baler.timeseries import connect_pool, delete_point_value, read_range

tracer = tracing.get_tracer(__name__)

OPENAPI_SPEC_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "openapi.yaml")

# read_range() takes inclusive bounds, not an optional span - "whole
# history" (IHisExt.read's span==null case) is just the widest span this
# store could ever contain, not a separate no-bounds query path.
_EPOCH_START = datetime.fromtimestamp(0, tz=timezone.utc)

# Zone that `hisRead(today)` etc. resolve "today" in when a request doesn't
# say (?tz=...). UTC, not the container's local zone, so the answer doesn't
# silently change between a bare-metal run and docker compose.
DEFAULT_TZ = os.environ.get("BALER_TZ") or "UTC"


def make_handler(store, ts_pool):
    class IngestHandler(BaseAPIHandler):
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
            if self.path == "/ingest":
                self._post_ingest()
                return
            if self.path == "/tags":
                self._post_tags()
                return
            if self.path == "/equip/merge":
                self._post_equip_merge()
                return
            if self.path == "/part":
                self._post_part()
                return
            self.send_response(404)
            self.end_headers()

        def _post_ingest(self) -> None:
            with tracer.start_as_current_span("ingest_api.post_ingest") as span:
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    span.set_attribute("error", str(exc))
                    _respond_error(self, 400, str(exc))
                    return

                readings = body if isinstance(body, list) else [body]
                try:
                    # One connection per request, from the pool - not one
                    # shared connection for every concurrent request.
                    # psycopg.Connection isn't safe for concurrent use from
                    # multiple threads, and sharing one is exactly what made
                    # this process fragile before (it died silently once
                    # this session from a stale connection with no recovery
                    # short of a restart).
                    with ts_pool.connection() as conn:
                        for reading in readings:
                            topic = reading["point"]
                            ingest_reading(store, conn, topic, reading.get("value"), reading.get("ts"))
                except (KeyError, TypeError) as exc:
                    span.set_attribute("error", str(exc))
                    _respond_error(self, 400, f"reading missing required field: {exc}")
                    return

                self.send_response(204)
                self.end_headers()

        def _post_tags(self) -> None:
            with tracer.start_as_current_span("ingest_api.post_tags") as span:
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    span.set_attribute("error", str(exc))
                    _respond_error(self, 400, str(exc))
                    return

                try:
                    topic = body["point"]
                    tags = body["tags"]
                except (KeyError, TypeError) as exc:
                    span.set_attribute("error", str(exc))
                    _respond_error(self, 400, f"missing required field: {exc}")
                    return

                point_uri = ingest_tags(store, topic, tags, body.get("ts"))
                # No-op unless this point carries an equipRef and the
                # matching equip's own tags have already landed - same
                # wiring as mqtt_listener's plain /tags branch, safe to
                # call on every POST, not just the first.
                link_equip_ref(store, point_uri)
                # HTTP equivalent of mqtt_listener's <topic>/tags handling,
                # plus classification right away - a caller pushing tags
                # over HTTP gets the same direct/fallback/miss outcome
                # mapping.py already computes for the MQTT path, instead of
                # having to separately query for it. reclassify, not
                # classify_point directly - a second POST for a point
                # that's already classified (tags changed, human correction)
                # must replace the prior Brick/PROJ type, not double-assert.
                outcome, brick_class = reclassify(store, point_uri)
                span.set_attribute("point_uri", str(point_uri))
                span.set_attribute("outcome", outcome)
                _respond_json(self, 200, {"point": str(point_uri), "outcome": outcome, "brickClass": brick_class})

        def _post_equip_merge(self) -> None:
            with tracer.start_as_current_span("ingest_api.post_equip_merge") as span:
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    uri_a, uri_b = body["a"], body["b"]
                except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError) as exc:
                    span.set_attribute("error", str(exc))
                    _respond_error(self, 400, f"expected {{'a': <equip uri>, 'b': <equip uri>}}: {exc}")
                    return

                merge_equip(store, URIRef(uri_a), URIRef(uri_b))
                span.set_attribute("a", uri_a)
                span.set_attribute("b", uri_b)
                self.send_response(204)
                self.end_headers()

        def _post_part(self) -> None:
            with tracer.start_as_current_span("ingest_api.post_part") as span:
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    child, parent = body["child"], body["parent"]
                except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError) as exc:
                    span.set_attribute("error", str(exc))
                    _respond_error(self, 400, f"expected {{'child': <uri>, 'parent': <uri>}}: {exc}")
                    return

                link_part_of(store, URIRef(child), URIRef(parent))
                span.set_attribute("child", child)
                span.set_attribute("parent", parent)
                self.send_response(204)
                self.end_headers()

        def do_GET(self):
            if self.path == "/docs":
                docs_ui.serve(self)
                return

            if self.path == "/openapi.yaml":
                self.serve_openapi_spec("ingest_api.get_openapi_spec", OPENAPI_SPEC_PATH)
                return

            if not self._require_gateway():
                return

            if self.path.startswith("/history"):
                self._get_history()
                return
            if self.path == "/his" or self.path.startswith("/his?"):
                self._get_his()
                return

            self.send_response(404)
            self.end_headers()

        def _get_his(self) -> None:
            """Axon-style `readAll(filter).hisRead(span)` over the live
            graph + history - see hisquery.py for the grammar, ui/his.html
            for the browser surface over this route."""
            with tracer.start_as_current_span("ingest_api.get_his") as span:
                query = parse_qs(urlparse(self.path).query)
                expr = query.get("expr", [None])[0]
                if not expr:
                    _respond_error(self, 400, "missing required query param: expr (e.g. readAll(ahu).hisRead(today))")
                    return
                tz = query.get("tz", [DEFAULT_TZ])[0]
                try:
                    limit = int(query.get("limit", [hisquery.DEFAULT_LIMIT])[0])
                except ValueError:
                    _respond_error(self, 400, "limit must be an integer")
                    return
                if limit < 1 or limit > hisquery.MAX_LIMIT:
                    _respond_error(self, 400, f"limit must be between 1 and {hisquery.MAX_LIMIT}")
                    return
                span.set_attribute("expr", expr)
                span.set_attribute("tz", tz)

                try:
                    parsed = hisquery.parse_query(expr)
                    with ts_pool.connection() as conn:
                        caller_scope = scope_for(conn, claims_from_header(self.headers.get("Authorization")))
                        result = hisquery.run_query(
                            store, conn, parsed, now=datetime.now(timezone.utc), tz=tz, limit=limit, scope=caller_scope
                        )
                except hisquery.NoMatchError as exc:
                    span.set_attribute("error", str(exc))
                    _respond_error(self, 404, str(exc))
                    return
                except hisquery.HisQueryError as exc:
                    span.set_attribute("error", str(exc))
                    _respond_error(self, 400, str(exc))
                    return
                span.set_attribute("matched", result["matchedCount"])
                span.set_attribute("series", len(result["series"]))
                _respond_json(self, 200, result)

        def do_DELETE(self):
            if not self._require_gateway():
                return
            if self.path.startswith("/history"):
                self._delete_history()
                return
            self.send_response(404)
            self.end_headers()

        def _get_history(self) -> None:
            with tracer.start_as_current_span("ingest_api.get_history") as span:
                query = parse_qs(urlparse(self.path).query)
                point = query.get("point", [None])[0]
                if not point:
                    _respond_error(self, 400, "missing required query param: point")
                    return
                point_uri = str(topic_to_point_uri(point))

                try:
                    start = _EPOCH_START if "start" not in query else datetime.fromtimestamp(float(query["start"][0]), tz=timezone.utc)
                    end = datetime.now(timezone.utc) if "end" not in query else datetime.fromtimestamp(float(query["end"][0]), tz=timezone.utc)
                except ValueError as exc:
                    _respond_error(self, 400, f"start/end must be unix epoch seconds: {exc}")
                    return
                span.set_attribute("point_uri", point_uri)

                with ts_pool.connection() as conn:
                    if not scope_for(conn, claims_from_header(self.headers.get("Authorization"))).allows(point_uri):
                        # Same answer as a point with no history: never
                        # reveal that an out-of-scope point exists.
                        _respond_json(self, 200, [])
                        return
                    rows = read_range(conn, point_uri, start, end)
                span.set_attribute("count", len(rows))
                _respond_json(self, 200, [{"ts": ts.timestamp(), "value": value} for ts, value in rows])

        def _delete_history(self) -> None:
            with tracer.start_as_current_span("ingest_api.delete_history") as span:
                query = parse_qs(urlparse(self.path).query)
                point = query.get("point", [None])[0]
                ts_param = query.get("ts", [None])[0]
                if not point or ts_param is None:
                    _respond_error(self, 400, "missing required query params: point, ts")
                    return
                point_uri = str(topic_to_point_uri(point))

                try:
                    ts = datetime.fromtimestamp(float(ts_param), tz=timezone.utc)
                except ValueError as exc:
                    _respond_error(self, 400, f"ts must be unix epoch seconds: {exc}")
                    return
                span.set_attribute("point_uri", point_uri)
                with ts_pool.connection() as conn:
                    found = delete_point_value(conn, point_uri, ts)
                span.set_attribute("found", found)
                self.send_response(204 if found else 404)
                self.end_headers()

    return IngestHandler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    tracing.init_tracing("baler-ingest-api")

    store = RemoteStore()
    ts_pool = connect_pool()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(store, ts_pool))
    print(f"POST /ingest listening on {args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

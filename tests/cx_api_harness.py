"""
A commissioning_api on MemoryRepo for the building-record feature tests
(epic #221): the same handler, gateway secret and JWTs as
test_commissioning_openapi.py, no services.
"""

from __future__ import annotations

from http.server import ThreadingHTTPServer
from threading import Thread

import cx_fixtures as fx
import jwt as pyjwt
import requests
from conftest import assert_matches_schema, load_spec

from baler.commissioning import db
from baler.commissioning_api import OPENAPI_SPEC_PATH, make_handler
from baler.scope import Scope

GATEWAY = "test-gateway-secret"
JWT = "j" * 32
SITES = {
    "site-a": {"id": "site-a", "org_id": "org-a", "name": "A", "topic_prefixes": ["fbf/a"]},
    "site-b": {"id": "site-b", "org_id": "org-b", "name": "B", "topic_prefixes": ["fbf/b"]},
}


def scope(claims):
    if not claims:
        return Scope.nothing()
    mine = [s for s in SITES.values() if s["org_id"] == claims["org_id"]]
    return Scope(tuple(sorted(p for s in mine for p in s["topic_prefixes"])))


def headers(org="org-a", sub="u-a"):
    tok = pyjwt.encode({"sub": sub, "org_id": org, "site_ids": [], "role": "operator", "kind": "session"}, JWT, algorithm="HS256")
    return {"X-Gateway-Secret": GATEWAY, "Authorization": f"Bearer {tok}"}


A = headers()
B = headers(org="org-b", sub="u-b")


def repo(store=None, history=None):
    return db.MemoryRepo(store=store if store is not None else fx.store(), history=history or fx.history(),
                         sites=SITES, scope_fn=scope, users={"u-a": "a@example.com"})


def serve(monkeypatch, r, clock=None, **kw):
    """(base url, server); call server.shutdown() when done."""
    monkeypatch.setenv("BALER_GATEWAY_SECRET", GATEWAY)
    monkeypatch.setenv("BALER_JWT_SECRET", JWT)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(r, clock=clock or (lambda: fx.NOW), **kw))
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}", server


def project(base, h=A, **body):
    body = {"name": "Building A", "site_id": "site-a", "phase": "startup"} | body
    r = requests.post(f"{base}/projects", json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


SPEC = None


def check(resp, path: str, method: str, status: int | None = None):
    """Assert the status and that the body matches the spec's schema."""
    global SPEC
    SPEC = SPEC or load_spec(OPENAPI_SPEC_PATH)
    status = status or resp.status_code
    assert resp.status_code == status, resp.text
    schema = SPEC["paths"][path][method]["responses"][str(status)]["content"]["application/json"]["schema"]
    assert schema
    esc = path.replace("~", "~0").replace("/", "~1")
    assert_matches_schema(resp.json(), {"$ref": f"#/paths/{esc}/{method}/responses/{status}/content/application~1json/schema"}, SPEC)
    return resp.json()

"""deploy/nash-srv/seed-demo-account.sh against a scripted fake gateway
(Baler#173): an auth_api that's restarting (5xx, no answer) must never lead
to POST /auth/orgs, and a missing account still gets created."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "nash-srv" / "seed-demo-account.sh"
EMAIL, PASSWORD = "demo@example.invalid", 'p"w\\d'  # quote and backslash: the body must stay valid JSON


class FakeGateway:
    """Answers each path from its own list of statuses (the last repeats)
    and records every request."""

    def __init__(self, script: dict[str, list[int]], body_401: bytes = b'{"error": "invalid email or password"}'):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, dict]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls.append((self.path, body))
                codes = fake.script.get(self.path, [404])
                code = codes.pop(0) if len(codes) > 1 else codes[0]
                out = body_401 if code == 401 else b"{}"
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"

    def paths(self) -> list[str]:
        return [p for p, _ in self.calls]


@pytest.fixture
def gateway():
    made = []

    def make(script, **kw):
        g = FakeGateway(script, **kw)
        made.append(g)
        return g

    yield make
    for g in made:
        g.srv.shutdown()


def run(url: str, budget: str = "3") -> subprocess.CompletedProcess:
    env = {**os.environ, "BALER_URL": url, "DEMO_ADMIN_EMAIL": EMAIL, "DEMO_ADMIN_PASSWORD": PASSWORD,
           "SEED_RETRY_SECONDS": budget, "SEED_RETRY_DELAY": "0.1"}
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60, check=False)


def test_502_then_200_retries_and_never_creates(gateway):
    g = gateway({"/auth/login": [502, 502, 200]})
    r = run(g.url)
    assert r.returncode == 0, r.stderr
    assert g.paths() == ["/auth/login"] * 3
    assert g.calls[0][1] == {"email": EMAIL, "password": PASSWORD}


def test_auth_api_never_back_fails_without_creating(gateway):
    g = gateway({"/auth/login": [503]})
    r = run(g.url, budget="1")
    assert r.returncode != 0
    assert "/auth/orgs" not in g.paths() and len(g.paths()) >= 2
    assert "not creating" in r.stderr


def test_nothing_listening_fails_without_creating():
    with socket.socket() as s:  # a port that was free a moment ago
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    r = run(f"http://127.0.0.1:{port}", budget="1")
    assert r.returncode != 0 and "login still 000" in r.stderr


def test_missing_account_is_created(gateway):
    g = gateway({"/auth/login": [401], "/auth/orgs": [201]})
    r = run(g.url)
    assert r.returncode == 0, r.stderr
    assert g.paths() == ["/auth/login", "/auth/orgs"]
    assert g.calls[1][1] == {"name": "Demo", "admin_email": EMAIL, "admin_password": PASSWORD}


def test_taken_email_means_it_exists(gateway):
    """401 with the account present (another password): the server's 409
    on the taken admin email ends it; no second org."""
    g = gateway({"/auth/login": [401], "/auth/orgs": [409]})
    r = run(g.url)
    assert r.returncode == 0, r.stderr
    assert "already exists" in r.stdout


def test_create_retries_through_a_restart(gateway):
    """Retrying POST /auth/orgs is safe: a repeat for the same admin email
    is refused (409), never a second org."""
    g = gateway({"/auth/login": [401], "/auth/orgs": [502, 201]})
    r = run(g.url)
    assert r.returncode == 0, r.stderr
    assert g.paths() == ["/auth/login", "/auth/orgs", "/auth/orgs"]


def test_other_create_error_fails(gateway):
    g = gateway({"/auth/login": [401], "/auth/orgs": [400]})
    assert run(g.url).returncode != 0


def test_a_401_that_isnt_auth_apis_never_creates(gateway):
    """Only auth_api's own bad-credentials 401 may lead to a create; any other
    401 (a proxy, a changed gateway) is unknown."""
    g = gateway({"/auth/login": [401]}, body_401=b"unauthorized")
    r = run(g.url, budget="1")
    assert r.returncode != 0 and "/auth/orgs" not in g.paths()


def test_403_means_it_exists(gateway):
    g = gateway({"/auth/login": [403]})
    assert run(g.url).returncode == 0 and g.paths() == ["/auth/login"]

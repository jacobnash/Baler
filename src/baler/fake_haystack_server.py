"""
A fake Haystack server: implements just enough of the server side of
Haystack's SCRAM handshake (https://project-haystack.org/doc/Auth) to
authenticate one known user/password and hand back canned Haystack-JSON
grids. Not a real Haystack server - it derives its own SCRAM math
independently from the same stdlib primitives HaystackClient uses, so a
passing round-trip proves real crypto interop, not just "doesn't crash".

Two callers share this: tests/test_haystack_client.py (proving
HaystackClient's handshake/parsing against a correct peer) and
scripts/mock_haystack_server.py (a standalone process for trying
haystack_puller without a real Haxall/SkySpark instance - see
haystack-puller.mdx).
"""

import base64
import hashlib
import hmac
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from baler.haystack_client import (
    _b64url_decode,
    _b64url_nopad,
    _parse_auth_params,
)

TEST_USER = "tduser"
TEST_PASSWORD = "tdpass"
TEST_SALT = b"fixed-test-salt-"
TEST_ITERATIONS = 4096


def make_fake_haxall_server(canned_grids: dict | None = None):
    grids: dict = canned_grids or {}
    handshakes = {}
    bearer_tokens: set[str] = set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _grid_for(self, op: str) -> dict:
            """grids[op] is either a flat {"rows": [...]} grid (same
            response regardless of query params - what every op besides
            read/hisRead needs), or - only for "read"/"hisRead" - a dict
            keyed by the request's filter/id query param, so a caller can
            hand back different rows for an equip read vs a point read, or
            different history per point id."""
            grid = grids.get(op, {"rows": []})
            if "rows" in grid or op not in ("read", "hisRead"):
                return grid
            params = parse_qs(urlsplit(self.path).query)
            key = params.get("filter" if op == "read" else "id", [""])[0]
            return grid.get(key, {"rows": []})

        def do_GET(self):
            op = self.path.partition("?")[0].strip("/")
            auth = self.headers.get("Authorization", "")

            if auth.startswith("BEARER "):
                token = _parse_auth_params(auth).get("authToken")
                if token not in bearer_tokens:
                    self.send_response(401)
                    self.end_headers()
                    return
                self._send_json(200, self._grid_for(op))
                return

            if auth.startswith("HELLO "):
                username = _b64url_decode(_parse_auth_params(auth)["username"]).decode()
                token = secrets.token_hex(8)
                handshakes[token] = {"username": username}
                self.send_response(401)
                self.send_header("WWW-Authenticate", f"SCRAM hash=SHA-256, handshakeToken={token}")
                self.end_headers()
                return

            if auth.startswith("SCRAM "):
                self._handle_scram_step(auth, handshakes, bearer_tokens)
                return

            self.send_response(401)
            self.send_header("WWW-Authenticate", "SCRAM hash=SHA-256")
            self.end_headers()

        def _handle_scram_step(self, auth, handshakes, bearer_tokens):
            params = _parse_auth_params(auth)
            state = handshakes.get(params.get("handshakeToken"))
            if state is None:
                self.send_response(401)
                self.end_headers()
                return
            message = _b64url_decode(params["data"]).decode()

            if "p=" not in message:
                # client-first message: "n,,n=<user>,r=<client_nonce>" -
                # split on the double comma, not the first comma, or the
                # gs2 header's own trailing comma leaks into client_first_bare
                client_first_bare = message.split(",,", 1)[1]
                client_nonce = dict(p.split("=", 1) for p in client_first_bare.split(","))["r"]
                combined_nonce = client_nonce + secrets.token_hex(8)
                server_first = f"r={combined_nonce},s={base64.b64encode(TEST_SALT).decode()},i={TEST_ITERATIONS}"
                new_token = secrets.token_hex(8)
                handshakes[new_token] = {
                    "username": state["username"],
                    "client_first_bare": client_first_bare,
                    "server_first": server_first,
                }
                self.send_response(401)
                self.send_header(
                    "WWW-Authenticate",
                    f"SCRAM handshakeToken={new_token}, hash=SHA-256, data={_b64url_nopad(server_first.encode())}",
                )
                self.end_headers()
                return

            # client-final message: "c=biws,r=<combined_nonce>,p=<proof>"
            fields = dict(p.split("=", 1) for p in message.split(","))
            client_final_no_proof = f"c={fields['c']},r={fields['r']}"
            auth_message = f"{state['client_first_bare']},{state['server_first']},{client_final_no_proof}"

            if state["username"] != TEST_USER:
                self.send_response(401)
                self.end_headers()
                return
            salted_password = hashlib.pbkdf2_hmac("sha256", TEST_PASSWORD.encode(), TEST_SALT, TEST_ITERATIONS, dklen=32)
            client_key = hmac.new(salted_password, b"Client Key", hashlib.sha256).digest()
            stored_key = hashlib.sha256(client_key).digest()
            client_signature = hmac.new(stored_key, auth_message.encode(), hashlib.sha256).digest()
            expected_proof = bytes(a ^ b for a, b in zip(client_key, client_signature))

            if not hmac.compare_digest(expected_proof, base64.b64decode(fields["p"])):
                self.send_response(401)
                self.end_headers()
                return
            bearer_token = secrets.token_hex(8)
            bearer_tokens.add(bearer_token)
            self.send_response(200)
            self.send_header("Authentication-Info", f"authToken={bearer_token}, hash=SHA-256")
            self.end_headers()

        def _send_json(self, status, body):
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, bearer_tokens

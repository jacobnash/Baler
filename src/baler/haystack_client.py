"""
Read-only client for a Haystack REST API server (Haxall, SkySpark, or any
other Project Haystack-compliant server) - about/read/hisRead, authenticated
via Haystack's SCRAM handshake (https://project-haystack.org/doc/Auth). Lets
Baler run passively alongside an existing Haxall/SkySpark deployment:
pull its already-tagged data using nothing but a read-only API user, with no
pod install and no change to the target system - contrast fantom-his-ext,
which installs into the target Haxall/SkySpark process itself.

This implements the current, standardized project-haystack.org spec (full
RFC 5802 SCRAM-SHA-256: gs2 header "n,,", client/server nonce exchange,
PBKDF2-derived salted password) - not the older SkySpark-v3-era
"SCRAM over SASL" draft some reference clients (e.g. pyhaystack) still
implement, which predates the finalized spec and uses a different, simpler
wire format that modern Haxall/SkySpark servers no longer speak.
"""

import base64
import hashlib
import hmac
import secrets
from datetime import datetime

import requests


class HaystackAuthError(RuntimeError):
    """Raised when the SCRAM handshake or bearer-token request fails."""


def _b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _parse_auth_params(header_value: str) -> dict:
    """Parses 'key=value, key2=value2' auth-params, optionally prefixed by a
    scheme word - WWW-Authenticate sends one ('SCRAM hash=..., ...'),
    Authentication-Info doesn't (RFC 7615: no scheme, just params). Every
    value here is base64/token chars - no quoted-string handling needed."""
    if not header_value:
        return {}
    first_word = header_value.split(None, 1)[0]
    rest = header_value.split(None, 1)[1] if "=" not in first_word and " " in header_value else header_value
    params = {}
    for item in rest.split(","):
        item = item.strip()
        if "=" in item:
            key, _, value = item.partition("=")
            params[key.strip()] = value.strip()
    return params


def _decode_haystack_scalar(value):
    """Minimal decoder for classic Haystack JSON scalar encoding - only the
    kinds read()/hisRead() responses actually carry (marker, ref, number,
    dateTime, bool/str pass through as native JSON). Not a full Haystack
    JSON codec.
    ponytail: add more kinds (coord, uri, xstr, date, time, ...) if a real
    response needs one that isn't handled here yet."""
    if not isinstance(value, str):
        return value
    if value == "m:":
        return True
    if value.startswith("n:"):
        return float(value[2:].split(" ", 1)[0])
    if value.startswith("r:"):
        return value[2:].split(" ", 1)[0]
    if value.startswith("t:"):
        # "t:<isoInstant> <tzName>" - only the ISO instant matters for a
        # numeric ts; hisRead's whole point is feeding it straight into
        # ingest_reading(..., ts: float), so decode to epoch seconds here
        # rather than handing every caller a string to re-parse.
        iso = value[2:].split(" ", 1)[0]
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    return value


class HaystackClient:
    """One authenticated session against a Haystack REST API server. Mirrors
    remote_store.RemoteStore's shape (Session reuse, base_url handling,
    raise_for_status, public methods delegating to a private transport
    helper) - same kind of long-lived client making many calls."""

    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._session = requests.Session()
        self._authenticated = False

    def _authenticate(self) -> None:
        hello_resp = self._session.get(
            f"{self.base_url}/about",
            headers={"Authorization": f"HELLO username={_b64url_nopad(self._username.encode())}"},
        )
        if hello_resp.status_code != 401:
            raise HaystackAuthError(f"expected 401 HELLO challenge, got {hello_resp.status_code}")
        handshake_token = _parse_auth_params(hello_resp.headers.get("WWW-Authenticate", "")).get("handshakeToken")
        if not handshake_token:
            raise HaystackAuthError("server did not return a handshakeToken")

        client_nonce = _b64url_nopad(secrets.token_bytes(24))
        client_first_bare = f"n={self._username},r={client_nonce}"
        first_resp = self._session.get(
            f"{self.base_url}/about",
            headers={
                "Authorization": f"SCRAM handshakeToken={handshake_token}, "
                f"data={_b64url_nopad(f'n,,{client_first_bare}'.encode())}"
            },
        )
        if first_resp.status_code != 401:
            raise HaystackAuthError(f"expected 401 SCRAM challenge, got {first_resp.status_code}")
        first_params = _parse_auth_params(first_resp.headers.get("WWW-Authenticate", ""))
        handshake_token = first_params.get("handshakeToken", handshake_token)
        server_first_message = _b64url_decode(first_params["data"]).decode()
        server_params = dict(p.split("=", 1) for p in server_first_message.split(","))
        combined_nonce = server_params["r"]
        if not combined_nonce.startswith(client_nonce):
            # RFC 5802's whole point of echoing the client nonce back: proves
            # this server-first-message is a live response to *our* request,
            # not a replayed/substituted one.
            raise HaystackAuthError("server nonce does not extend client nonce - possible tampering")
        salt = base64.b64decode(server_params["s"])
        iterations = int(server_params["i"])

        salted_password = hashlib.pbkdf2_hmac("sha256", self._password.encode(), salt, iterations, dklen=32)
        client_key = hmac.new(salted_password, b"Client Key", hashlib.sha256).digest()
        stored_key = hashlib.sha256(client_key).digest()
        client_final_no_proof = f"c=biws,r={combined_nonce}"  # biws = base64("n,,"), i.e. no channel binding
        auth_message = f"{client_first_bare},{server_first_message},{client_final_no_proof}"
        client_signature = hmac.new(stored_key, auth_message.encode(), hashlib.sha256).digest()
        client_proof = bytes(a ^ b for a, b in zip(client_key, client_signature))
        client_final_message = f"{client_final_no_proof},p={base64.b64encode(client_proof).decode()}"

        final_resp = self._session.get(
            f"{self.base_url}/about",
            headers={
                "Authorization": f"SCRAM handshakeToken={handshake_token}, "
                f"data={_b64url_nopad(client_final_message.encode())}"
            },
        )
        if final_resp.status_code != 200:
            raise HaystackAuthError(f"SCRAM auth rejected (status {final_resp.status_code}) - check username/password")
        auth_token = _parse_auth_params(final_resp.headers.get("Authentication-Info", "")).get("authToken")
        if not auth_token:
            raise HaystackAuthError("server accepted auth but returned no authToken")

        self._session.headers["Authorization"] = f"BEARER authToken={auth_token}"
        self._authenticated = True

    def _get(self, op: str, params: dict | None = None, _retry_on_401: bool = True) -> dict:
        if not self._authenticated:
            self._authenticate()
        resp = self._session.get(f"{self.base_url}/{op}", params=params or {}, headers={"Accept": "application/json"})
        if resp.status_code == 401 and _retry_on_401:
            # Bearer token expired/revoked mid-session - re-auth once and
            # retry, not a loop (a second 401 is a real failure, not a
            # transient one).
            self._authenticated = False
            return self._get(op, params, _retry_on_401=False)
        resp.raise_for_status()
        return resp.json()

    def about(self) -> dict:
        row = self._get("about")["rows"][0]
        return {k: _decode_haystack_scalar(v) for k, v in row.items()}

    def read(self, filter: str, limit: int | None = None) -> list[dict]:
        params: dict[str, str | int] = {"filter": filter}
        if limit is not None:
            params["limit"] = limit
        rows = self._get("read", params)["rows"]
        return [{k: _decode_haystack_scalar(v) for k, v in row.items()} for row in rows]

    def his_read(self, id: str, range: str) -> list[dict]:
        rows = self._get("hisRead", {"id": id, "range": range})["rows"]
        return [{k: _decode_haystack_scalar(v) for k, v in row.items()} for row in rows]

"""
Client for a Haystack REST API server (Haxall, SkySpark, or any other
Project Haystack-compliant server) - about/read/hisRead (plus hisWrite, for
notebooks), authenticated via Haystack's SCRAM handshake
(https://project-haystack.org/doc/Auth) or a bearer token such as a Baler
API key (POST /auth/api-keys). Lets
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
import math
import secrets
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

import requests


class HaystackAuthError(RuntimeError):
    """Raised when the SCRAM handshake or bearer-token request fails."""


class HaystackError(RuntimeError):
    """Raised when the server answers an op with a Haystack error grid."""


def _encode_haystack_scalar(value: Any) -> Any:
    """Inverse of _decode_haystack_scalar for what hisWrite sends: Haystack
    3 JSON (`ver: 3.0`). Numbers, bools, strings and datetimes only."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        f = float(value)
        if math.isnan(f):
            return "n:NaN"
        if math.isinf(f):
            return "n:INF" if f > 0 else "n:-INF"
        return f"n:{f!r}"
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"naive datetime {value!r}: hisWrite timestamps need a timezone")
        iso = value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return f"t:{iso} UTC"
    if isinstance(value, str):
        return f"s:{value}"
    raise TypeError(f"can't encode {type(value).__name__} for hisWrite")


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
    if isinstance(value, dict) and "_kind" in value:
        return _decode_hayson(value)
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


def _decode_hayson(value: dict):
    """Haystack 4 JSON (Hayson) kinds, decoded to the same native shapes as
    the classic encoding above: Baler's own /api answers a GET with Hayson
    (it has no request body whose flavor it could mirror)."""
    kind = value["_kind"]
    if kind == "marker":
        return True
    if kind == "number":
        return float(value["val"])
    if kind in ("ref", "uri"):
        return value["val"]
    if kind == "dateTime":
        return datetime.fromisoformat(value["val"].replace("Z", "+00:00")).timestamp()
    return value


class HaystackClient:
    """One authenticated session against a Haystack REST API server. Mirrors
    remote_store.RemoteStore's shape (Session reuse, base_url handling,
    raise_for_status, public methods delegating to a private transport
    helper) - same kind of long-lived client making many calls."""

    def __init__(self, base_url: str, username: str = "", password: str = "", *, token: str | None = None):
        """SCRAM with username/password, or skip the handshake and send
        `token` as `Authorization: Bearer` - what a Baler API key is for
        (baler_fn.connect, #91). A rejected token is a hard failure: there
        is no password to re-authenticate with."""
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token = token
        self._session = requests.Session()
        self._authenticated = False
        if token:
            self._session.headers["Authorization"] = f"Bearer {token}"
            self._authenticated = True

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
        return self._check(resp, lambda: self._get(op, params, _retry_on_401=False), _retry_on_401)

    def _post(self, op: str, grid: dict, _retry_on_401: bool = True) -> dict:
        if not self._authenticated:
            self._authenticate()
        resp = self._session.post(
            f"{self.base_url}/{op}", json=grid,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        return self._check(resp, lambda: self._post(op, grid, _retry_on_401=False), _retry_on_401)

    def _check(self, resp, retry, retry_on_401: bool) -> dict:
        if resp.status_code == 401 and self._token:
            raise HaystackAuthError("bearer token rejected (401) - check the API key, or whether it was revoked")
        if resp.status_code == 401 and retry_on_401:
            # Bearer token expired/revoked mid-session - re-auth once and
            # retry, not a loop (a second 401 is a real failure, not a
            # transient one).
            self._authenticated = False
            return retry()
        resp.raise_for_status()
        return resp.json()

    def about(self) -> dict:
        row = self._get("about")["rows"][0]
        return {k: _decode_haystack_scalar(v) for k, v in row.items()}

    def read(self, filter: str, limit: int | None = None) -> list[dict]:
        """Every record the filter matches, or the first `limit`. A server
        that pages (Baler, #244) answers a page at a time and names the last
        id it covered in grid meta `after`; without a limit this follows it
        to the end. Servers that don't page never set it: one request."""
        params: dict[str, str | int] = {"filter": filter}
        if limit is not None:
            params["limit"] = limit
        rows: list[dict] = []
        while True:
            grid = self._get("read", params)
            rows += [r for r in grid.get("rows", []) if r]
            after = (grid.get("meta") or {}).get("after")
            if isinstance(after, dict) and after.get("_kind") == "ref":  # Hayson
                after = str(after["val"])
            elif isinstance(after, str) and after.startswith("r:"):  # JSON v3
                after = after[2:].split(" ", 1)[0]
            else:
                after = None
            if limit is not None or after is None:
                break
            params["after"] = "@" + after
        return [{k: _decode_haystack_scalar(v) for k, v in row.items()} for row in rows]

    def his_read(self, id: str, range: str) -> list[dict]:
        rows = self._get("hisRead", {"id": id, "range": range})["rows"]
        return [{k: _decode_haystack_scalar(v) for k, v in row.items()} for row in rows]

    def his_write(self, id: str, items: Iterable[tuple[datetime, Any]]) -> None:
        """Writes (timestamp, value) pairs to one point's history. `id` is
        the point's ref, with or without the leading `@`; timestamps must
        be timezone-aware. Needs a role that may hisWrite (Baler: operator)."""
        rows = [{"ts": _encode_haystack_scalar(ts), "val": _encode_haystack_scalar(val)} for ts, val in items]
        if not rows:
            return
        grid = {
            "meta": {"ver": "3.0", "id": f"r:{id.lstrip('@')}"},
            "cols": [{"name": "ts"}, {"name": "val"}],
            "rows": rows,
        }
        body = self._post("hisWrite", grid)
        if "err" in body.get("meta", {}):
            raise HaystackError(str(body["meta"].get("dis", "hisWrite failed")))

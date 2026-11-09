"""
Identity service: login, API-key issuance/revocation, org/site directory
CRUD, and GET /internal/revoked-jtis (consumed by the gateway's
js_periodic poller - container-internal only, no public gateway route).
Same shape as fault_api.py: stdlib http.server, one Postgres connection
per request from a pool, OTel span per request, spec served straight off
disk.

Role/site/rate-limit enforcement happens in the gateway (gateway/njs/),
not here - every handler below just does the one thing it's for. The
exception is gateway_auth.request_came_through_gateway(), checked on
every route except /auth/login (which can't have gone through the
gateway's js_access yet - it's the request that gets you a token in the
first place) and /internal/revoked-jtis (never reachable through the
public gateway at all, so the header is meaningless there).
"""

import argparse
import logging
import os
import re
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

from baler import (
    auth,
    broker_accounts,
    docs_ui,
    gateway_auth,
    mfa,
    password_reset,
    tracing,
)
from baler.http_handler_base import BaseAPIHandler
from baler.http_handler_base import respond_error as _respond_error
from baler.http_handler_base import respond_json as _respond_json
from baler.timeseries import connect_pool

tracer = tracing.get_tracer(__name__)

OPENAPI_SPEC_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "auth-api-openapi.yaml")


def make_handler(ts_pool, broker=None):
    # Edge-node MQTT accounts (Baler#36); tests pass a fake control channel.
    broker = broker or broker_accounts.DynsecClient()

    class AuthHandler(BaseAPIHandler):
        TRACER = tracer
        ALLOWED_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        ALLOWED_HEADERS = "Content-Type, Authorization"

        def _require_gateway(self) -> bool:
            if gateway_auth.request_came_through_gateway(self):
                return True
            _respond_error(self, 401, "request did not come through the gateway")
            return False

        def _require_caller(self, span, enroll_ok: bool = False) -> dict | None:
            claims = auth.decode_own_token(self.headers.get("Authorization"))
            if not claims:
                _respond_error(self, 401, "missing or invalid bearer token")
                return None
            span.set_attribute("caller_sub", claims["sub"])
            # The org requires MFA and this user hasn't set it up: the
            # session reaches only enrolment (also enforced in the gateway,
            # policy.MFA_ENROLL_ROUTES).
            if claims.get("mfa_enroll") and not enroll_ok:
                _respond_error(self, 403, "your org requires multi-factor login: set up an authenticator first")
                return None
            return claims

        def _ip(self) -> str | None:
            return self.headers.get("X-Real-IP") or (self.client_address[0] if self.client_address else None)

        def _session_user(self, span) -> tuple[dict, dict] | None:
            """(claims, user) for a logged-in person (not an API key)."""
            caller = self._require_caller(span, enroll_ok=True)
            if not caller:
                return None
            if caller.get("kind") != "session":
                _respond_error(self, 400, "only a logged-in user (not an API key) has multi-factor login")
                return None
            with ts_pool.connection() as conn:
                user = auth.get_user(conn, caller["sub"])
            if not user:
                _respond_error(self, 401, "no such user")
                return None
            return caller, user

        def do_POST(self):
            # Bare paths, matching every other service's convention - the
            # gateway's `location /auth/ { proxy_pass http://auth_api:8006/; }`
            # strips the /auth prefix before it reaches here.
            path = urlparse(self.path).path
            if path == "/login":
                self.handle_traced("auth_api.post_login", self._login)
                return
            if path == "/login/mfa":
                # Public like /login: the second step of the same sign-in.
                self.handle_traced("auth_api.post_login_mfa", self._login_mfa)
                return
            if path == "/password-reset":
                self.handle_traced("auth_api.post_password_reset", self._password_reset_request)
                return
            if path == "/password-reset/confirm":
                self.handle_traced("auth_api.post_password_reset_confirm", self._password_reset_confirm)
                return
            if path == "/orgs":
                # Public, like /login - creating a brand-new org has to
                # be reachable with zero prior credentials (its own
                # payload creates the first admin user in the same
                # call), same bootstrap reasoning as create_org() in
                # auth.py. gateway/njs/policy.js marks this route public
                # to match.
                self.handle_traced("auth_api.post_orgs", self._create_org)
                return
            if not self._require_gateway():
                return
            if path == "/logout":
                self.handle_traced("auth_api.post_logout", self._logout)
                return
            if path == "/api-keys":
                self.handle_traced("auth_api.post_api_keys", self._create_api_key)
                return
            if path == "/sites":
                self.handle_traced("auth_api.post_sites", self._create_site)
                return
            if path == "/users":
                self.handle_traced("auth_api.post_users", self._create_user)
                return
            if path == "/me/password":
                self.handle_traced("auth_api.post_me_password", self._change_own_password)
                return
            if path == "/me/mfa/totp/setup":
                self.handle_traced("auth_api.post_mfa_setup", self._mfa_setup)
                return
            if path == "/me/mfa/totp/enable":
                self.handle_traced("auth_api.post_mfa_enable", self._mfa_enable)
                return
            if path == "/me/mfa/disable":
                self.handle_traced("auth_api.post_mfa_disable", self._mfa_disable)
                return
            if path == "/me/mfa/recovery-codes":
                self.handle_traced("auth_api.post_mfa_recovery_codes", self._mfa_recovery_codes)
                return
            if path == "/broker-accounts":
                self.handle_traced("auth_api.post_broker_accounts", self._create_broker_account)
                return
            m = re.fullmatch(r"/broker-accounts/([^/]+)/rotate", path)
            if m:
                account_id = m.group(1)
                self.handle_traced("auth_api.rotate_broker_account",
                                   lambda span: self._rotate_broker_account(account_id, span))
                return
            self.send_response(404)
            self.end_headers()

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/internal/revoked-jtis":
                # Never routed through the public gateway (no /auth/
                # prefix, and no gateway location proxies this path at
                # all) - reachable only container-to-container, so no
                # gateway check here either.
                self.handle_traced("auth_api.get_revoked_jtis", self._get_revoked_jtis)
                return
            if path == "/openapi.yaml":
                self.serve_openapi_spec("auth_api.get_openapi_spec", OPENAPI_SPEC_PATH)
                return
            if path == "/docs":
                docs_ui.serve(self)
                return
            if path == "/password-reset/config":
                # Public: the login page asks whether to offer "forgot password".
                _respond_json(self, 200, {"self_service": password_reset.enabled()})
                return
            if not self._require_gateway():
                return
            if path == "/me/mfa":
                self.handle_traced("auth_api.get_mfa", self._mfa_get)
                return
            if path == "/org":
                self.handle_traced("auth_api.get_org", self._org_get)
                return
            if path == "/events":
                self.handle_traced("auth_api.get_events", self._events_get)
                return
            if path == "/api-keys":
                self.handle_traced("auth_api.get_api_keys", self._list_api_keys)
                return
            if path == "/me":
                self.handle_traced("auth_api.get_me", self._get_me)
                return
            if path == "/users":
                self.handle_traced("auth_api.get_users", self._list_users)
                return
            if path == "/sites":
                self.handle_traced("auth_api.get_sites", self._list_sites)
                return
            if path == "/broker-accounts":
                self.handle_traced("auth_api.get_broker_accounts", self._list_broker_accounts)
                return
            self.send_response(404)
            self.end_headers()

        def do_PUT(self):
            if not self._require_gateway():
                return
            path = urlparse(self.path).path
            m = re.fullmatch(r"/sites/([^/]+)/topic-prefixes", path)
            if m:
                site_id = m.group(1)
                self.handle_traced("auth_api.put_site_prefixes", lambda span: self._set_site_prefixes(site_id, span))
                return
            m = re.fullmatch(r"/users/([^/]+)/password", path)
            if m:
                user_id = m.group(1)
                self.handle_traced("auth_api.put_user_password", lambda span: self._set_user_password(user_id, span))
                return
            self.send_response(404)
            self.end_headers()

        def do_PATCH(self):
            if not self._require_gateway():
                return
            path = urlparse(self.path).path
            m = re.fullmatch(r"/users/([^/]+)", path)
            if m:
                user_id = m.group(1)
                self.handle_traced("auth_api.patch_user", lambda span: self._update_user(user_id, span))
                return
            if path == "/org":
                self.handle_traced("auth_api.patch_org", self._org_patch)
                return
            self.send_response(404)
            self.end_headers()

        def do_DELETE(self):
            if not self._require_gateway():
                return
            path = urlparse(self.path).path
            if path.startswith("/api-keys/"):
                key_id = path[len("/api-keys/"):]
                self.handle_traced("auth_api.delete_api_key", lambda span: self._delete_api_key(key_id, span))
                return
            m = re.fullmatch(r"/users/([^/]+)/mfa", path)
            if m:
                user_id = m.group(1)
                self.handle_traced("auth_api.delete_user_mfa", lambda span: self._mfa_admin_reset(user_id, span))
                return
            m = re.fullmatch(r"/users/([^/]+)", path)
            if m:
                user_id = m.group(1)
                self.handle_traced("auth_api.delete_user", lambda span: self._delete_user(user_id, span))
                return
            m = re.fullmatch(r"/broker-accounts/([^/]+)", path)
            if m:
                account_id = m.group(1)
                self.handle_traced("auth_api.delete_broker_account",
                                   lambda span: self._revoke_broker_account(account_id, span))
                return
            self.send_response(404)
            self.end_headers()

        def _login(self, span) -> None:
            body = self.read_json_body()
            email = body["email"]
            password = body["password"]
            with ts_pool.connection() as conn:
                user = auth.get_user_by_email(conn, email)
                if not user or not auth.verify_password(password, user["password_hash"]):
                    span.set_attribute("outcome", "invalid_credentials")
                    _respond_error(self, 401, "invalid email or password")
                    return
                step = mfa.login_step(conn, user["id"])
                if step == "mfa":
                    # Password right, second factor still to come: no session yet.
                    span.set_attribute("outcome", "mfa_required")
                    _respond_json(self, 200, {"mfa_required": True, "mfa_token": mfa.issue_mfa_token(user["id"]),
                                              "expires_in": mfa.MFA_TOKEN_TTL_SECONDS})
                    return
                site_ids = auth.user_site_ids(conn, user["id"])
                token = auth.issue_session_token(user, site_ids, mfa="enroll" if step == "enroll" else None)
            span.set_attribute("outcome", "ok" if step == "session" else "mfa_enrollment_required")
            span.set_attribute("user_id", user["id"])
            payload = {
                "token": token,
                "user": {"id": user["id"], "org_id": user["org_id"], "role": user["role"], "site_ids": site_ids},
            }
            if step == "enroll":
                payload["mfa_enrollment_required"] = True
            _respond_json(self, 200, payload)

        def _login_mfa(self, span) -> None:
            body = self.read_json_body()
            try:
                with ts_pool.connection() as conn:
                    user = auth.get_user(conn, mfa.read_mfa_token(str(body["mfa_token"])))
                    if not user or not auth.get_user_by_email(conn, user["email"]):  # deleted or disabled since
                        raise mfa.MFAError("the sign-in step expired; enter your password again")
                    how = mfa.verify(conn, user, str(body.get("code") or body.get("recovery_code") or ""), ip=self._ip())
                    site_ids = auth.user_site_ids(conn, user["id"])
                    token = auth.issue_session_token(user, site_ids, mfa="otp")
                    left = mfa.state(conn, user["id"])["recovery_codes_left"]
            except mfa.Locked as exc:
                span.set_attribute("outcome", "locked")
                _respond_error(self, 429, str(exc))
                return
            except mfa.MFAError as exc:
                span.set_attribute("outcome", "invalid_code")
                _respond_error(self, 401, str(exc))
                return
            span.set_attribute("outcome", "ok")
            span.set_attribute("factor", how)
            _respond_json(self, 200, {
                "token": token,
                "user": {"id": user["id"], "org_id": user["org_id"], "role": user["role"], "site_ids": site_ids},
                "recovery_codes_left": left,
            })

        # --- password reset (#278): public, rate-limited in nginx and per account

        def _password_reset_request(self, span) -> None:
            body = self.read_json_body()
            try:
                with ts_pool.connection() as conn:
                    message = password_reset.request(conn, body.get("email"), ip=self._ip())
            except password_reset.ResetError as exc:
                _respond_error(self, 503, str(exc))
                return
            _respond_json(self, 202, {"status": message})

        def _password_reset_confirm(self, span) -> None:
            body = self.read_json_body()
            try:
                with ts_pool.connection() as conn:
                    password_reset.confirm(conn, body.get("token"), body.get("new_password"), ip=self._ip())
            except password_reset.ResetError as exc:
                _respond_error(self, 400, str(exc))
                return
            self.send_response(204)
            self.end_headers()

        # --- MFA (#278): the caller's own account

        def _mfa_get(self, span) -> None:
            got = self._session_user(span)
            if not got:
                return
            with ts_pool.connection() as conn:
                _respond_json(self, 200, mfa.state(conn, got[1]["id"]))

        def _mfa_setup(self, span) -> None:
            got = self._session_user(span)
            if not got:
                return
            try:
                with ts_pool.connection() as conn:
                    result = mfa.start_enrolment(conn, got[1])
            except mfa.MFAError as exc:
                _respond_error(self, 409, str(exc))
                return
            _respond_json(self, 200, result)

        def _mfa_enable(self, span) -> None:
            got = self._session_user(span)
            if not got:
                return
            _claims, user = got
            body = self.read_json_body()
            try:
                with ts_pool.connection() as conn:
                    codes = mfa.confirm_enrolment(conn, user, str(body["code"]), ip=self._ip())
                    # Other sessions (signed in without the second factor) end;
                    # this one is replaced by a full, MFA-verified session.
                    auth.end_user_sessions(conn, user["id"])
                    token = auth.issue_session_token(user, auth.user_site_ids(conn, user["id"]), mfa="otp")
            except mfa.MFAError as exc:
                _respond_error(self, 400, str(exc))
                return
            _respond_json(self, 200, {"recovery_codes": codes, "token": token})

        def _mfa_disable(self, span) -> None:
            got = self._session_user(span)
            if not got:
                return
            _claims, user = got
            body = self.read_json_body()
            if not auth.verify_password(str(body.get("password") or ""), user["password_hash"]):
                _respond_error(self, 401, "password is wrong")
                return
            try:
                with ts_pool.connection() as conn:
                    mfa.disable(conn, user, str(body.get("code") or ""), ip=self._ip())
            except mfa.Locked as exc:
                _respond_error(self, 429, str(exc))
                return
            except mfa.MFAError as exc:
                _respond_error(self, 400, str(exc))
                return
            self.send_response(204)
            self.end_headers()

        def _mfa_recovery_codes(self, span) -> None:
            got = self._session_user(span)
            if not got:
                return
            body = self.read_json_body()
            try:
                with ts_pool.connection() as conn:
                    codes = mfa.regenerate_recovery_codes(conn, got[1], str(body.get("code") or ""), ip=self._ip())
            except mfa.Locked as exc:
                _respond_error(self, 429, str(exc))
                return
            except mfa.MFAError as exc:
                _respond_error(self, 400, str(exc))
                return
            _respond_json(self, 200, {"recovery_codes": codes})

        # --- MFA (#278): admin, the caller's own org

        def _org_get(self, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            with ts_pool.connection() as conn:
                _respond_json(self, 200, mfa.get_org(conn, caller["org_id"]))

        def _org_patch(self, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            body = self.read_json_body()
            if "mfa_required" not in body:
                raise ValueError("nothing to change: send mfa_required")
            if caller.get("kind") != "session":
                _respond_error(self, 400, "change the MFA requirement as a logged-in admin, not with an API key")
                return
            try:
                with ts_pool.connection() as conn:
                    org = mfa.set_org_required(conn, caller["org_id"], body["mfa_required"], {"id": caller["sub"]},
                                               ip=self._ip())
            except mfa.MFAError as exc:
                _respond_error(self, 409, str(exc))
                return
            _respond_json(self, 200, org)

        def _mfa_admin_reset(self, user_id: str, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            span.set_attribute("target_user_id", user_id)
            with ts_pool.connection() as conn:
                found = mfa.admin_reset(conn, caller["org_id"], user_id, caller["sub"], ip=self._ip())
            if not found:
                _respond_error(self, 404, "no such user in your org")
                return
            self.send_response(204)
            self.end_headers()

        def _events_get(self, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            with ts_pool.connection() as conn:
                _respond_json(self, 200, mfa.list_events(conn, caller["org_id"]))

        def _logout(self, span) -> None:
            claims = auth.decode_own_token(self.headers.get("Authorization"))
            if claims and claims.get("kind") == "api_key":
                with ts_pool.connection() as conn:
                    conn.execute(
                        "INSERT INTO revoked_tokens (jti, revoked_at) VALUES (%s, now()) ON CONFLICT (jti) DO NOTHING",
                        (claims["jti"],),
                    )
            # Session tokens have nothing to do here - their own short
            # expiry is the guard, matching the gateway's revocation
            # design. Always 204: logout is idempotent either way.
            self.send_response(204)
            self.end_headers()

        def _create_api_key(self, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            body = self.read_json_body()
            role = body["role"]
            site_ids = body.get("site_ids")
            expires_in_seconds = body.get("expires_in_seconds")
            with ts_pool.connection() as conn:
                key = auth.create_api_key(conn, caller["org_id"], role, site_ids, expires_in_seconds)
                token = auth.issue_api_key_token(key)
            span.set_attribute("api_key_id", key["id"])
            _respond_json(self, 201, {"id": key["id"], "role": key["role"], "site_ids": key["site_ids"], "token": token})

        def _list_api_keys(self, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            with ts_pool.connection() as conn:
                keys = auth.list_api_keys(conn, caller["org_id"])
            _respond_json(self, 200, keys)

        def _delete_api_key(self, key_id: str, span) -> None:
            span.set_attribute("api_key_id", key_id)
            with ts_pool.connection() as conn:
                found = auth.revoke_api_key(conn, key_id)
            span.set_attribute("found", found)
            self.send_response(204 if found else 404)
            self.end_headers()

        def _get_me(self, span) -> None:
            caller = self._require_caller(span, enroll_ok=True)
            if not caller:
                return
            email = name = mfa_state = None
            if caller.get("kind") == "session":
                with ts_pool.connection() as conn:
                    user = auth.get_user(conn, caller["sub"])
                    name = auth.get_display_name(conn, caller["sub"]) if user else None
                    mfa_state = mfa.state(conn, caller["sub"]) if user else None
                email = user["email"] if user else None
            _respond_json(self, 200, {
                "sub": caller["sub"], "org_id": caller["org_id"], "role": caller["role"],
                "site_ids": caller.get("site_ids", []), "kind": caller.get("kind"), "email": email,
                "display_name": name, "mfa": mfa_state, "mfa_enrollment_required": bool(caller.get("mfa_enroll")),
            })

        def _create_org(self, span) -> None:
            body = self.read_json_body()
            auth.check_new_password(body["admin_password"])
            try:
                with ts_pool.connection() as conn:
                    org = auth.create_org(conn, body["name"], body["admin_email"], body["admin_password"])
            except auth.EmailTaken as exc:
                _respond_error(self, 409, str(exc))
                return
            span.set_attribute("org_id", org["id"])
            _respond_json(self, 201, org)

        def _create_site(self, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            body = self.read_json_body()
            with ts_pool.connection() as conn:
                site = auth.create_site(conn, caller["org_id"], body["name"], body.get("topic_prefixes"))
            span.set_attribute("site_id", site["id"])
            _respond_json(self, 201, site)

        def _set_site_prefixes(self, site_id: str, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            prefixes = self.read_json_body()["topic_prefixes"]
            if not isinstance(prefixes, list) or not all(isinstance(p, str) for p in prefixes):
                raise TypeError("topic_prefixes must be a list of strings")
            with ts_pool.connection() as conn:
                site = auth.set_site_prefixes(conn, caller["org_id"], site_id, prefixes)
            if site is None:
                _respond_error(self, 404, "no such site in your org")
                return
            span.set_attribute("site_id", site_id)
            # The site's edge-node accounts may publish only under its
            # prefixes: re-apply them. The prefixes are saved either way; a
            # broker that's down is reported, and the next change retries.
            try:
                with ts_pool.connection() as conn:
                    site["broker_accounts_updated"] = broker_accounts.reapply_site(
                        conn, broker, caller["org_id"], site_id)
            except broker_accounts.BrokerError as exc:
                span.set_attribute("broker_error", str(exc))
                site["broker_error"] = f"saved, but the broker's account permissions weren't updated: {exc}"
            _respond_json(self, 200, site)

        def _list_broker_accounts(self, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            with ts_pool.connection() as conn:
                accounts = broker_accounts.list_accounts(conn, caller["org_id"])
            _respond_json(self, 200, {"accounts": accounts, "connect": broker_accounts.connect_info()})

        def _broker_call(self, span, fn):
            """Run fn(conn); broker failures -> 502, unknown site -> 404.
            Either way the transaction rolls back."""
            try:
                with ts_pool.connection() as conn:
                    return fn(conn)
            except broker_accounts.BrokerError as exc:
                span.set_attribute("broker_error", str(exc))
                _respond_error(self, 502, f"MQTT broker: {exc}")
            except LookupError as exc:
                if isinstance(exc, KeyError):
                    raise
                _respond_error(self, 404, str(exc.args[0]) if exc.args else "not found")
            return None

        def _create_broker_account(self, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            body = self.read_json_body()
            site_id, username = body["site_id"], body["username"]
            description = body.get("description")
            if description is not None and not isinstance(description, str):
                raise TypeError("description must be a string")
            result = self._broker_call(span, lambda conn: broker_accounts.create_account(
                conn, broker, caller["org_id"], site_id, username, description))
            if result is None:
                return
            account, password = result
            span.set_attribute("username", account["username"])
            _respond_json(self, 201, {"account": account, "password": password,
                                      "connect": broker_accounts.connect_info()})

        def _rotate_broker_account(self, account_id: str, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            result = self._broker_call(span, lambda conn: broker_accounts.rotate_password(
                conn, broker, caller["org_id"], account_id))
            if result is None:
                return
            account, password = result
            _respond_json(self, 200, {"account": account, "password": password,
                                      "connect": broker_accounts.connect_info()})

        def _revoke_broker_account(self, account_id: str, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            done = self._broker_call(span, lambda conn: broker_accounts.revoke(
                conn, broker, caller["org_id"], account_id) or True)
            if not done:
                return
            self.send_response(204)
            self.end_headers()

        def _create_user(self, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            body = self.read_json_body()
            auth.check_new_password(body["password"])
            try:
                with ts_pool.connection() as conn:
                    user = auth.create_user(conn, caller["org_id"], body["email"], body["password"], body["role"],
                                            body.get("display_name"))
                    if body.get("site_ids"):
                        auth.update_user(conn, caller["org_id"], user["id"], site_ids=body["site_ids"])
                        user["site_ids"] = auth.user_site_ids(conn, user["id"])
            except auth.EmailTaken as exc:
                _respond_error(self, 409, str(exc))
                return
            span.set_attribute("new_user_id", user["id"])
            _respond_json(self, 201, user)

        def _list_users(self, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            with ts_pool.connection() as conn:
                users = auth.list_users(conn, caller["org_id"])
                has_mfa = mfa.mfa_by_user(conn, caller["org_id"])
            for u in users:
                u["mfa_enabled"] = has_mfa.get(u["id"], False)
            _respond_json(self, 200, users)

        def _delete_user(self, user_id: str, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            span.set_attribute("target_user_id", user_id)
            if user_id == caller["sub"]:
                # an org's only admin deleting themselves locks everyone out
                _respond_error(self, 400, "you can't delete your own account")
                return
            try:
                with ts_pool.connection() as conn:
                    found = auth.delete_user(conn, caller["org_id"], user_id)
            except auth.LastAdmin as exc:
                _respond_error(self, 409, str(exc))
                return
            if not found:
                _respond_error(self, 404, "no such user in your org")
                return
            self.send_response(204)
            self.end_headers()

        def _set_user_password(self, user_id: str, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            span.set_attribute("target_user_id", user_id)
            password = self.read_json_body()["password"]
            auth.check_new_password(password)
            with ts_pool.connection() as conn:
                found = auth.set_password(conn, caller["org_id"], user_id, password)
            if not found:
                _respond_error(self, 404, "no such user in your org")
                return
            self.send_response(204)
            self.end_headers()

        def _change_own_password(self, span) -> None:
            caller = self._require_caller(span)  # not for an enrolment-only session
            if not caller:
                return
            if caller.get("kind") != "session":
                _respond_error(self, 400, "only a logged-in user (not an API key) has a password to change")
                return
            body = self.read_json_body()
            auth.check_new_password(body["new_password"])
            with ts_pool.connection() as conn:
                user = auth.get_user(conn, caller["sub"])
                if not user or not auth.verify_password(body["current_password"], user["password_hash"]):
                    _respond_error(self, 401, "current password is wrong")
                    return
                auth.set_password(conn, user["org_id"], user["id"], body["new_password"])
                # every session ended, this one included: hand back a fresh one
                token = auth.issue_session_token(user, auth.user_site_ids(conn, user["id"]))
            _respond_json(self, 200, {"token": token})

        def _list_sites(self, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            with ts_pool.connection() as conn:
                sites = auth.list_sites(conn, caller["org_id"])
            _respond_json(self, 200, sites)

        def _update_user(self, user_id: str, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            span.set_attribute("target_user_id", user_id)
            body = self.read_json_body()
            if not {"role", "site_ids", "disabled", "display_name"} & body.keys():
                raise ValueError("nothing to change: send role, site_ids, disabled and/or display_name")
            if user_id == caller["sub"] and body.get("role", caller["role"]) != caller["role"]:
                _respond_error(self, 400, "you can't change your own role")
                return
            if user_id == caller["sub"] and body.get("disabled"):
                _respond_error(self, 400, "you can't disable your own account")
                return
            try:
                with ts_pool.connection() as conn:
                    extra = {"display_name": body["display_name"]} if "display_name" in body else {}
                    user = auth.update_user(conn, caller["org_id"], user_id, body.get("role"), body.get("site_ids"),
                                            body.get("disabled"), **extra)
            except auth.LastAdmin as exc:
                _respond_error(self, 409, str(exc))
                return
            if user is None:
                _respond_error(self, 404, "no such user in your org")
                return
            _respond_json(self, 200, user)

        def _get_revoked_jtis(self, span) -> None:
            with ts_pool.connection() as conn:
                jtis = auth.list_revoked_jtis(conn)
            span.set_attribute("count", len(jtis))
            _respond_json(self, 200, jtis)

    return AuthHandler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8006)
    args = parser.parse_args()

    if not os.environ.get("BALER_JWT_SECRET"):
        raise SystemExit(
            "BALER_JWT_SECRET is not set - auth_api can't issue tokens without it. "
            "Set it in .env (see .env.example) before starting this service."
        )

    problem = mfa.key_problem()
    if problem:
        raise SystemExit(problem + " - auth_api can't seal two-step sign-in secrets with it.")

    tracing.init_tracing("baler-auth-api")
    if not os.environ.get("BALER_MFA_KEY"):
        logging.getLogger("baler.auth_api").warning(
            "BALER_MFA_KEY is not set: two-step sign-in (TOTP) secrets are stored unsealed in the users "
            "table. Set it (see .env.example) before anyone enrols.")

    ts_pool = connect_pool()
    with ts_pool.connection() as conn:
        auth.ensure_schema(conn)
        broker_accounts.ensure_schema(conn)

    server = ThreadingHTTPServer((args.host, args.port), make_handler(ts_pool))
    print(f"auth API listening on {args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

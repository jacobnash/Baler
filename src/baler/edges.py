"""
Edge nodes and their connectors, as one API for the Connectors page.

A Windrow instance is an edge node. Baler reaches it one of two ways, and
the page doesn't care which:

  local   the windrow_api container beside Baler (WINDROW_API_URL), over
          HTTP with the gateway secret - the same routes the old Devices
          page used through /windrow.
  remote  a Windrow on a site's own network (a Raspberry Pi gateway),
          over the MQTT command channel cmd/<site>/<instance>/... -
          Baler never needs an inbound path into the building. Remote
          nodes are found from their retained status heartbeat.

Both take the same action names (Windrow's command-channel vocabulary);
LOCAL_ROUTES maps each to its HTTP route. READ_ACTIONS are safe for
viewers; the rest change the edge (admin, gateway/njs/policy.js).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from baler import point_write

READ_ACTIONS = ("list_devices", "list_connections", "modbus_list_connections")
WRITE_ACTIONS = (
    "add_device", "set_device_status", "recheck_credentials", "set_credential",
    "create_connection", "update_connection", "delete_connection",
    "modbus_create_connection", "modbus_update_connection", "modbus_delete_connection",
    "discover", "learn", "modbus_discover", "modbus_test_read",
    # Background jobs: only admins start scans, so only admins follow them.
    "start_job", "get_job", "list_jobs",
)
ACTIONS = READ_ACTIONS + WRITE_ACTIONS
# Scans and learns walk a network: give them longer than a CRUD call. Windrow
# itself gives up on a synchronous one after 15 s, though, so the page runs
# them as jobs instead (start_job, then get_job until done - Baler#139).
SLOW_ACTIONS = {"discover": 60.0, "learn": 120.0, "modbus_discover": 120.0, "modbus_test_read": 20.0}
# A remote node whose last heartbeat is older than this is shown offline
# even if its last will never fired (a broker restart can lose it).
STALE_AFTER_S = 180.0
# Asked for synchronously, these run as a job on the node and are waited for
# here (Baler#139), up to SYNC_JOB_WAIT - under the gateway's 60 s proxy
# read timeout. Past that the caller gets the job id to follow with get_job.
JOB_ACTIONS = ("discover", "learn", "modbus_discover")
SYNC_JOB_WAIT = 55.0
JOB_POLL_S = 1.0
LOCAL_ID = ("local", "local")
# The Windrow beside Baler publishes as the trusted `baler` broker user, so
# the broker doesn't restrict its topics - but mqtt_listener only ingests
# fbf/# (its --topic-pattern default), so anything else never reaches Baler.
LOCAL_TOPIC_ROOTS = ("fbf",)


class EdgeError(RuntimeError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _local_route(action: str, body: dict) -> tuple[str, str, dict | None]:
    """(method, path, json body) for an action on the local windrow_api."""
    b = {k: v for k, v in body.items() if k not in ("id", "target")}
    oid = str(body.get("id", ""))
    if action == "list_devices":
        return "GET", "/devices", None
    if action == "add_device":
        return "POST", "/devices", b
    if action == "set_device_status":
        return "PATCH", f"/devices/{oid}", {"status": body.get("status")}
    if action == "recheck_credentials":
        return "POST", f"/devices/{oid}/recheck-credentials", {}
    if action == "set_credential":
        prefix = {"device": "/devices", "connection": "/connections", "modbus_connection": "/modbus/connections"}.get(
            str(body.get("target")))
        if prefix is None:
            raise EdgeError("set_credential target is device, connection or modbus_connection", 400)
        return "PATCH", f"{prefix}/{oid}/credential", body.get("credential") or {}
    if action in ("list_connections", "modbus_list_connections"):
        return "GET", "/modbus/connections" if action.startswith("modbus") else "/connections", None
    if action in ("create_connection", "modbus_create_connection"):
        return "POST", "/modbus/connections" if action.startswith("modbus") else "/connections", b
    if action in ("update_connection", "modbus_update_connection"):
        base = "/modbus/connections" if action.startswith("modbus") else "/connections"
        return "PATCH", f"{base}/{oid}", b
    if action in ("delete_connection", "modbus_delete_connection"):
        base = "/modbus/connections" if action.startswith("modbus") else "/connections"
        return "DELETE", f"{base}/{oid}", None
    if action == "discover":
        return "POST", "/discover", b
    if action == "learn":
        return "POST", "/learn", b
    if action == "modbus_discover":
        return "POST", "/modbus/discover", b
    if action == "modbus_test_read":
        return "POST", "/modbus/test-read", b
    if action == "start_job":
        return "POST", "/jobs", b
    if action == "get_job":
        return "GET", f"/jobs/{oid}", None
    if action == "list_jobs":
        return "GET", "/jobs", None
    raise EdgeError(f"unknown action {action!r}", 400)


def _iso(ts: float | None) -> str | None:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None
    except (OverflowError, OSError, ValueError, TypeError):
        return None


class Edges:
    def __init__(self, commander: point_write.Commander | None = None, local_url: str | None = None,
                 secret: str | None = None, opener=None, now=time.time, sleep=time.sleep):
        self.commander = commander
        self.local_url = (local_url if local_url is not None else os.environ.get("WINDROW_API_URL", "")).rstrip("/")
        self.secret = secret if secret is not None else os.environ.get(
            "BALER_GATEWAY_SECRET", os.environ.get("TIMBERDOODLE_GATEWAY_SECRET", ""))
        self._open = opener or urllib.request.urlopen
        self._now = now
        self._sleep = sleep

    def _commander(self) -> point_write.Commander:
        if self.commander is None:
            self.commander = point_write.Commander()
        return self.commander

    # ------------------------------------------------------------ nodes

    def nodes(self) -> list[dict]:
        out = []
        if self.local_url:
            node = {"site_id": "local", "instance_id": "local", "kind": "local", "label": "Windrow beside Baler",
                    "online": False, "last_seen": None, "error": None,
                    "allowed_prefixes": list(LOCAL_TOPIC_ROOTS), "networks": []}
            try:
                devices = self._local("list_devices", {})
                node.update(online=True, last_seen=_iso(self._now()), devices=len(devices),
                            pending_devices=sum(1 for d in devices if d.get("status") == "pending"))
            except EdgeError as exc:
                node["error"] = str(exc)
            if node["online"]:
                # Windrow's GET /status (version, subnets); an older Windrow
                # has no such route, and the node is still usable without it.
                try:
                    st = self._request("GET", "/status", None, 5.0)
                except EdgeError:
                    st = None
                if isinstance(st, dict):
                    node.update(version=st.get("version"), started_at=st.get("started_at"),
                                networks=list(st.get("networks") or []))
            out.append(node)
        try:
            statuses = self._commander().statuses()
        except Exception as exc:
            statuses = {}
            if not out:
                raise EdgeError(f"can't reach the MQTT broker to find edge nodes: {exc}") from exc
        for (site, inst), st in sorted(statuses.items()):
            fresh = self._now() - st.get("received_at", 0) < STALE_AFTER_S
            out.append({"site_id": site, "instance_id": inst, "kind": "remote", "label": f"{site} / {inst}",
                        "online": bool(st.get("online")) and fresh, "last_seen": st.get("ts") or _iso(st.get("received_at")),
                        "version": st.get("version"), "started_at": st.get("started_at"),
                        "devices": st.get("devices"), "pending_devices": st.get("pending_devices"),
                        "bacnet_connections": st.get("bacnet_connections"),
                        "modbus_connections": st.get("modbus_connections"),
                        "networks": list(st.get("networks") or []), "error": None})
        return out

    # ------------------------------------------------------------ actions

    def call(self, site: str, instance: str, action: str, body: dict | None = None):
        if action not in ACTIONS:
            raise EdgeError(f"unknown action {action!r}; one of {', '.join(ACTIONS)}", 400)
        body = body or {}
        if action in JOB_ACTIONS:
            done, result = self._via_job(site, instance, action, body)
            if done:
                return result
        return self._dispatch(site, instance, action, body)

    def _via_job(self, site: str, instance: str, action: str, body: dict) -> tuple[bool, object]:
        """(True, result) once the job finishes; (False, None) for a node
        whose Windrow predates jobs, which then gets the one-shot action."""
        try:
            job = self._dispatch(site, instance, "start_job", {**body, "job": action})
        except EdgeError as exc:
            if "unknown action" in str(exc) or "answered 404" in str(exc):
                return False, None
            raise
        deadline = time.monotonic() + SYNC_JOB_WAIT
        while isinstance(job, dict) and job.get("state") == "running":
            if time.monotonic() > deadline:
                raise EdgeError(f"{action} is still running on the node as job {job.get('id')}; "
                                "follow it with the get_job action", 504)
            self._sleep(JOB_POLL_S)
            job = self._dispatch(site, instance, "get_job", {"id": job.get("id")})
        if not isinstance(job, dict) or job.get("state") != "done":
            raise EdgeError(f"{action} failed on the node: {(job or {}).get('error') if isinstance(job, dict) else job}")
        return True, job.get("result")

    def _dispatch(self, site: str, instance: str, action: str, body: dict):
        if (site, instance) == LOCAL_ID:
            if not self.local_url:
                raise EdgeError("no local Windrow is configured (WINDROW_API_URL)", 404)
            return self._local(action, body)
        try:
            return self._commander().command(site, instance, action, body, timeout=SLOW_ACTIONS.get(action))
        except point_write.CommandError as exc:
            raise EdgeError(str(exc), 504 if "didn't answer" in str(exc) else 502) from exc

    def _local(self, action: str, body: dict):
        method, path, payload = _local_route(action, body)
        return self._request(method, path, payload, SLOW_ACTIONS.get(action, 15.0))

    def _request(self, method: str, path: str, payload: dict | None, timeout: float):
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.local_url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "X-Gateway-Secret": self.secret})
        try:
            with self._open(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("error", detail)
            except (ValueError, AttributeError):
                pass
            raise EdgeError(f"Windrow answered {exc.code}: {detail}", 400 if exc.code < 500 else 502) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise EdgeError(f"can't reach the local Windrow at {self.local_url}: {exc}") from exc
        if not raw:
            return {"ok": True}
        return json.loads(raw)

"""Edge nodes and connectors for the Connectors page (baler.edges and
ops_api's /edges routes): the local windrow_api over HTTP, remote Windrows
over the MQTT command channel, one action vocabulary. No services."""

from __future__ import annotations

import io
import json
import urllib.error
from http.server import ThreadingHTTPServer
from threading import Thread

import pytest
import requests

from baler import edges, point_write
from baler.ops_api import make_handler

SECRET = "test-gateway-secret"


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, answers=None, fail=None):
        self.answers = answers or {}
        self.fail = fail
        self.calls = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), req.full_url, body, req.headers.get("X-gateway-secret")))
        if self.fail:
            raise self.fail
        return FakeResp(json.dumps(self.answers.get((req.get_method(), req.full_url.split("8001", 1)[1]), [])).encode())


class FakeCommander:
    def __init__(self, statuses=None, results=None, error=None):
        self._statuses = statuses or {}
        self.results = results or {}
        self.error = error
        self.calls = []

    def statuses(self):
        return self._statuses

    def command(self, site, instance, action, body=None, timeout=None):
        self.calls.append((site, instance, action, body, timeout))
        if self.error:
            raise self.error
        return self.results.get(action, {"ok": True})


@pytest.mark.parametrize("action,body,expect", [
    ("list_devices", {}, ("GET", "/devices", None)),
    ("add_device", {"protocol": "bacnet", "device_address": "10.0.0.5", "device_instance": 7},
     ("POST", "/devices", {"protocol": "bacnet", "device_address": "10.0.0.5", "device_instance": 7})),
    ("set_device_status", {"id": "d1", "status": "ignored"}, ("PATCH", "/devices/d1", {"status": "ignored"})),
    ("update_connection", {"id": "c1", "poll_interval": 10}, ("PATCH", "/connections/c1", {"poll_interval": 10})),
    ("modbus_delete_connection", {"id": "m1"}, ("DELETE", "/modbus/connections/m1", None)),
    ("set_credential", {"target": "modbus_connection", "id": "m1", "credential": {"username": "u"}},
     ("PATCH", "/modbus/connections/m1/credential", {"username": "u"})),
    ("modbus_discover", {"cidr": "10.0.0.0/24"}, ("POST", "/modbus/discover", {"cidr": "10.0.0.0/24"})),
])
def test_local_routes(action, body, expect):
    assert edges._local_route(action, body) == expect


def test_local_call_carries_the_gateway_secret():
    op = Opener({("GET", "/devices"): [{"id": "d1", "status": "pending"}]})
    e = edges.Edges(commander=FakeCommander(), local_url="http://windrow_api:8001", secret=SECRET, opener=op)
    assert e.call("local", "local", "list_devices") == [{"id": "d1", "status": "pending"}]
    assert op.calls[0][3] == SECRET


def test_local_errors_are_readable():
    err = urllib.error.HTTPError("http://x", 400, "bad", {}, io.BytesIO(b'{"error": "device_address is required"}'))
    e = edges.Edges(commander=FakeCommander(), local_url="http://windrow_api:8001", secret=SECRET, opener=Opener(fail=err))
    with pytest.raises(edges.EdgeError, match="device_address is required") as exc:
        e.call("local", "local", "add_device", {})
    assert exc.value.status == 400


def test_remote_calls_go_over_the_command_channel_with_longer_timeouts_for_scans():
    c = FakeCommander(results={"list_connections": [{"id": "c1"}]})
    e = edges.Edges(commander=c, local_url="", secret=SECRET)
    assert e.call("home", "pi-1", "list_connections") == [{"id": "c1"}]
    e.call("home", "pi-1", "discover", {})
    assert c.calls[0][:3] == ("home", "pi-1", "list_connections") and c.calls[0][4] is None
    assert c.calls[1][4] == edges.SLOW_ACTIONS["discover"]


def test_unknown_action_and_unconfigured_local_are_refused():
    e = edges.Edges(commander=FakeCommander(), local_url="", secret=SECRET)
    with pytest.raises(edges.EdgeError) as exc:
        e.call("home", "pi-1", "rm_rf")
    assert exc.value.status == 400
    with pytest.raises(edges.EdgeError) as exc:
        e.call("local", "local", "list_devices")
    assert exc.value.status == 404


def test_a_site_that_doesnt_answer_is_a_gateway_timeout():
    c = FakeCommander(error=point_write.CommandError("site home/pi-1 didn't answer within 10s"))
    e = edges.Edges(commander=c, local_url="", secret=SECRET)
    with pytest.raises(edges.EdgeError) as exc:
        e.call("home", "pi-1", "list_devices")
    assert exc.value.status == 504


def test_nodes_list_local_and_remote_with_freshness():
    now = 1_000_000.0
    statuses = {("home", "pi-1"): {"online": True, "ts": "2026-11-07T12:00:00+00:00", "devices": 2, "pending_devices": 1,
                                   "bacnet_connections": 1, "modbus_connections": 0, "received_at": now - 30},
                ("barn", "pi-2"): {"online": True, "received_at": now - 600},  # heartbeat long gone
                ("shed", "pi-3"): {"online": False, "received_at": now - 5}}  # last will fired
    op = Opener({("GET", "/devices"): [{"status": "pending"}, {"status": "provisioned"}]})
    e = edges.Edges(commander=FakeCommander(statuses), local_url="http://windrow_api:8001", secret=SECRET, opener=op,
                    now=lambda: now)
    nodes = {(n["site_id"], n["instance_id"]): n for n in e.nodes()}
    assert nodes[("local", "local")]["online"] and nodes[("local", "local")]["pending_devices"] == 1
    assert nodes[("home", "pi-1")]["online"] and nodes[("home", "pi-1")]["devices"] == 2
    assert not nodes[("barn", "pi-2")]["online"] and not nodes[("shed", "pi-3")]["online"]


def test_local_node_that_is_down_still_lists_with_its_error():
    e = edges.Edges(commander=FakeCommander(), local_url="http://windrow_api:8001", secret=SECRET,
                    opener=Opener(fail=urllib.error.URLError("connection refused")))
    [node] = e.nodes()
    assert node["kind"] == "local" and not node["online"] and "connection refused" in node["error"]


# ---------------------------------------------------------------- ops_api


@pytest.fixture
def ops(monkeypatch):
    monkeypatch.setenv("BALER_GATEWAY_SECRET", SECRET)
    c = FakeCommander({("home", "pi-1"): {"online": True, "received_at": 990.0}},
                      results={"list_devices": [{"id": "d1"}], "create_connection": {"id": "c9"}})
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(edge_api=edges.Edges(commander=c, local_url="", secret=SECRET,
                                                                                    now=lambda: 1000.0)))
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", c
    server.shutdown()


H = {"X-Gateway-Secret": SECRET}


def test_ops_edges_routes(ops):
    base, c = ops
    assert requests.get(f"{base}/edges").status_code == 401
    nodes = requests.get(f"{base}/edges", headers=H).json()
    assert nodes[0]["site_id"] == "home" and nodes[0]["kind"] == "remote"
    r = requests.post(f"{base}/edges/home/pi-1/actions/list_devices", headers=H)
    assert r.status_code == 200 and r.json() == {"result": [{"id": "d1"}]}
    r = requests.post(f"{base}/edges/home/pi-1/actions/create_connection", headers=H,
                      json={"device_address": "10.0.0.5", "device_instance": 7, "topic_prefix": "fbf/home/x", "points": []})
    assert r.status_code == 200 and r.json()["result"] == {"id": "c9"}
    assert c.calls[-1][3]["topic_prefix"] == "fbf/home/x"
    assert requests.post(f"{base}/edges/home/pi-1/actions/nope", headers=H).status_code == 400
    assert requests.post(f"{base}/edges/local/local/actions/list_devices", headers=H).status_code == 404

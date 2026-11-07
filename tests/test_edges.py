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
    def __init__(self, statuses=None, results=None, error=None, error_for=None):
        self._statuses = statuses or {}
        self.results = results or {}
        self.error = error
        self.error_for = error_for or {}
        self.result_fn = {}
        self.calls = []

    def statuses(self):
        return self._statuses

    def command(self, site, instance, action, body=None, timeout=None):
        self.calls.append((site, instance, action, body, timeout))
        if self.error:
            raise self.error
        if action in self.error_for:
            raise self.error_for[action]
        if action in self.result_fn:
            return self.result_fn[action](body)
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
    ("start_job", {"job": "modbus_discover", "cidr": "10.0.0.0/24"},
     ("POST", "/jobs", {"job": "modbus_discover", "cidr": "10.0.0.0/24"})),
    ("get_job", {"id": "j1"}, ("GET", "/jobs/j1", None)),
    ("list_announced", {}, ("GET", "/announced", None)),
    ("list_jobs", {}, ("GET", "/jobs", None)),
    ("modbus_test_read", {"host": "10.0.0.6", "register": 3},
     ("POST", "/modbus/test-read", {"host": "10.0.0.6", "register": 3})),
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
    c = FakeCommander(results={"list_connections": [{"id": "c1"}]},
                      error_for={"start_job": point_write.CommandError("site refused start_job: unknown action 'start_job'")})
    e = edges.Edges(commander=c, local_url="", secret=SECRET)
    assert e.call("home", "pi-1", "list_connections") == [{"id": "c1"}]
    e.call("home", "pi-1", "discover", {})  # a Windrow without jobs: the one-shot action
    assert c.calls[0][:3] == ("home", "pi-1", "list_connections") and c.calls[0][4] is None
    assert [x[2] for x in c.calls[1:]] == ["start_job", "discover"]
    assert c.calls[2][4] == edges.SLOW_ACTIONS["discover"]


def test_slow_actions_run_as_a_job_and_are_waited_for():
    """Baler#139: a /24 sweep outlasted Windrow's 15 s per-call limit."""
    states = iter([{"id": "j1", "state": "running", "progress": {"done": 10, "total": 254}},
                   {"id": "j1", "state": "done", "result": [{"host": "10.0.0.30", "port": 502}]}])
    c = FakeCommander(results={"start_job": {"id": "j1", "state": "running"}})
    c.result_fn = {"get_job": lambda body: next(states)}
    e = edges.Edges(commander=c, local_url="", secret=SECRET, sleep=lambda s: None)
    assert e.call("home", "pi-1", "modbus_discover", {"cidr": "10.0.0.0/24"}) == [{"host": "10.0.0.30", "port": 502}]
    assert c.calls[0][2:4] == ("start_job", {"cidr": "10.0.0.0/24", "job": "modbus_discover"})
    assert [x[2] for x in c.calls[1:]] == ["get_job", "get_job"] and c.calls[1][3] == {"id": "j1"}


def test_a_failed_or_overlong_job_is_an_error(monkeypatch):
    c = FakeCommander(results={"start_job": {"id": "j1", "state": "running"}, "get_job": {"id": "j1", "state": "failed", "error": "boom"}})
    e = edges.Edges(commander=c, local_url="", secret=SECRET, sleep=lambda s: None)
    with pytest.raises(edges.EdgeError, match="boom"):
        e.call("home", "pi-1", "learn", {"device_address": "a", "device_instance": 1})
    monkeypatch.setattr(edges, "SYNC_JOB_WAIT", -1)
    c = FakeCommander(results={"start_job": {"id": "j2", "state": "running"}})
    e = edges.Edges(commander=c, local_url="", secret=SECRET, sleep=lambda s: None)
    with pytest.raises(edges.EdgeError, match="job j2") as exc:
        e.call("home", "pi-1", "discover", {})
    assert exc.value.status == 504


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


def test_local_node_carries_its_topic_root_and_windrow_status():
    op = Opener({("GET", "/devices"): [], ("GET", "/status"): {"version": "0.2", "networks": ["172.30.0.0/24"]}})
    e = edges.Edges(commander=FakeCommander(), local_url="http://windrow_api:8001", secret=SECRET, opener=op)
    [node] = e.nodes()
    assert node["allowed_prefixes"] == ["fbf"]
    assert node["networks"] == ["172.30.0.0/24"] and node["version"] == "0.2"


def test_local_node_without_a_status_route_still_lists():
    err = urllib.error.HTTPError("http://x", 404, "nf", {}, io.BytesIO(b""))

    class Op(Opener):
        def __call__(self, req, timeout=None):
            if req.full_url.endswith("/status"):
                raise err
            return super().__call__(req, timeout)

    e = edges.Edges(commander=FakeCommander(), local_url="http://windrow_api:8001", secret=SECRET, opener=Op())
    [node] = e.nodes()
    assert node["online"] and node["networks"] == []


def test_remote_node_reports_its_heartbeat_networks():
    statuses = {("home", "pi-1"): {"online": True, "received_at": 999.0, "networks": ["192.168.1.0/24"]}}
    e = edges.Edges(commander=FakeCommander(statuses), local_url="", secret=SECRET, now=lambda: 1000.0)
    assert e.nodes()[0]["networks"] == ["192.168.1.0/24"]


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


class FakeConn:
    def __init__(self, rows):
        self.rows, self.sql = rows, []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        return self

    def fetchall(self):
        return self.rows


class FakePool:
    def __init__(self, rows):
        self.conn = FakeConn(rows)

    def connection(self):
        pool = self

        class Ctx:
            def __enter__(self):
                return pool.conn

            def __exit__(self, *a):
                return False

        return Ctx()


def test_ops_edges_attach_the_broker_accounts_site_prefixes(monkeypatch):
    monkeypatch.setenv("BALER_GATEWAY_SECRET", SECRET)
    c = FakeCommander({("home", "pi-1"): {"online": True, "received_at": 990.0},
                       ("other", "pi-9"): {"online": True, "received_at": 990.0}})
    pool = FakePool([("home", "org-1", "Home", ["fbf/home"]), ("other", "org-2", "Elsewhere", ["fbf/x"])])
    handler = make_handler(ts_pool=pool, edge_api=edges.Edges(commander=c, local_url="", secret=SECRET, now=lambda: 1000.0))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        nodes = {n["site_id"]: n for n in requests.get(f"{base}/edges", headers=H).json()}
        assert nodes["home"]["allowed_prefixes"] == ["fbf/home"] and nodes["home"]["site_name"] == "Home"
        # no login claims here, so no org filter; with one, another org's site stays hidden
        assert nodes["other"]["allowed_prefixes"] == ["fbf/x"]
    finally:
        server.shutdown()


@pytest.mark.parametrize("prefix,allowed,ok", [
    ("fbf/home/ahu-1", ["fbf/home"], True),
    ("fbf/home", ["fbf/home"], False),          # the site itself isn't a piece of equipment
    ("fbf/homeward/x", ["fbf/home"], False),    # a lookalike isn't under it
    ("site/ahu-1", ["fbf"], False),
    ("anything/goes", None, True),              # unknown: the page says it couldn't check
    ("", ["fbf"], False),
])
def test_prefix_problem(prefix, allowed, ok):
    assert (edges.prefix_problem(prefix, allowed) is None) is ok


def test_ops_refuses_a_prefix_outside_the_nodes_own(monkeypatch):
    """#147: the check doesn't depend on the admin-only /auth/sites - ops_api
    reads the broker account's site prefixes itself and refuses server-side."""
    monkeypatch.setenv("BALER_GATEWAY_SECRET", SECRET)
    c = FakeCommander({("home", "pi-1"): {"online": True, "received_at": 990.0}}, results={"create_connection": {"id": "c1"}})
    pool = FakePool([("home", "org-1", "Home", ["fbf/home"])])
    handler = make_handler(ts_pool=pool, edge_api=edges.Edges(commander=c, local_url="", secret=SECRET, now=lambda: 1000.0))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        body = {"device_address": "10.0.0.5", "device_instance": 7, "points": [], "topic_prefix": "fbf/elsewhere/x"}
        r = requests.post(f"{base}/edges/home/pi-1/actions/create_connection", headers=H, json=body)
        assert r.status_code == 400 and "fbf/home/" in r.json()["error"] and c.calls == []
        r = requests.post(f"{base}/edges/home/pi-1/actions/create_connection", headers=H, json={**body, "topic_prefix": "fbf/home/x"})
        assert r.status_code == 200
        r = requests.post(f"{base}/edges/home/pi-1/actions/update_connection", headers=H, json={"id": "c1", "poll_interval": 9})
        assert r.status_code == 200  # an update that doesn't move the prefix isn't checked
        r = requests.post(f"{base}/edges/local/local/actions/modbus_create_connection", headers=H,
                          json={"host": "h", "points": {"a": 0}, "topic_prefix": "site/x"})
        assert r.status_code == 400 and "fbf/" in r.json()["error"]
    finally:
        server.shutdown()


def test_nodes_carry_announce_counts_from_status_and_heartbeat():
    op = Opener({("GET", "/devices"): [], ("GET", "/status"): {"announced": 2, "announced_online": 1, "announce_rejected": 3}})
    statuses = {("home", "pi-1"): {"online": True, "received_at": 999.0, "announced": 4, "announced_online": 4, "announce_rejected": 0}}
    e = edges.Edges(commander=FakeCommander(statuses), local_url="http://windrow_api:8001", secret=SECRET, opener=op,
                    now=lambda: 1000.0)
    nodes = {n["site_id"]: n for n in e.nodes()}
    assert (nodes["local"]["announced"], nodes["local"]["announced_online"], nodes["local"]["announce_rejected"]) == (2, 1, 3)
    assert nodes["home"]["announced_online"] == 4
    # a Windrow without announce reports none of them
    bare = edges.Edges(commander=FakeCommander({("x", "y"): {"online": True, "received_at": 999.0}}), local_url="",
                       secret=SECRET, now=lambda: 1000.0)
    assert "announced" not in bare.nodes()[0]


def test_list_announced_is_a_read_action():
    assert "list_announced" in edges.READ_ACTIONS

"""Positions vs installed assets (#197), with no services: the routes over
lifecycle.rows.MemoryRows, and once through ops_api over HTTP."""

from datetime import datetime, timedelta, timezone

import pytest

from baler.lifecycle import assets, routes, rows

T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
POS = "urn:equip:fbf/hosp/ahu-1/sf-motor"
POS2 = "urn:equip:fbf/hosp/ahu-2/sf-motor"


class Client:
    def __init__(self, org="org1", allows=lambda _u: True):
        self.rows = rows.MemoryRows()
        self.org = org
        self.allows = allows
        self.now = datetime(2036, 1, 1, tzinfo=timezone.utc)

    def __call__(self, method, path, body=None, **query):
        req = routes.Request(method=method, path=path, rows=self.rows, org_id=self.org, body=body,
                             query={k: str(v) for k, v in query.items()}, who={"sub": "u1", "email": "eng@example.com"},
                             allows=self.allows, now=self.now)
        return routes.dispatch(req)

    def ok(self, method, path, body=None, **query):
        status, out = self(method, path, body, **query)
        assert status in (200, 201), out
        return out


@pytest.fixture
def c():
    return Client()


def _asset(c, **f):
    return c.ok("POST", "/assets", {"asset_type": "motor", **f})


def test_replacement_appends_history_and_answers_the_brand_question(c):
    a = _asset(c, manufacturer="Baldor", model="EM3615T", serial="B-1", cost=1850)
    c.ok("POST", f"/assets/{a['id']}/install", {"position_uri": POS, "at": T0.isoformat(), "reason": "new construction"})
    out = c.ok("POST", "/positions/replace", {
        "position_uri": POS, "at": "2031-07-14T10:00:00Z", "remove_reason": "bearing failure",
        "asset": {"asset_type": "motor", "manufacturer": "WEG", "model": "W22", "serial": "W-9"}})
    assert out["removed"]["asset_id"] == a["id"] and out["installed"]["asset_id"] == out["asset"]["id"]

    hist = c.ok("GET", "/positions/history", uri=POS)
    assert [h["asset"]["manufacturer"] for h in hist["history"]] == ["Baldor", "WEG"]
    assert hist["history"][0]["removed_at"].startswith("2031-07-14") and hist["history"][0]["remove_reason"] == "bearing failure"
    assert hist["current"]["asset"]["manufacturer"] == "WEG"

    # "motors changed in 2031, to WEG" is a query
    ch = c.ok("GET", "/assets/changes", since="2031-01-01", until="2032-01-01", type="motor", manufacturer="weg")
    assert len(ch) == 1 and ch[0]["from"]["manufacturer"] == "Baldor" and ch[0]["brand_changed"]
    assert c.ok("GET", "/assets/changes", since="2032-01-01") == []

    # which asset was there when: a baseline taken in 2026 belongs to the Baldor
    at = c.ok("GET", "/positions/asset-at", uri=POS, at="2027-06-01")
    assert at["asset"]["manufacturer"] == "Baldor"
    assert c.ok("GET", "/positions/asset-at", uri=POS, at="2033-01-01")["asset"]["manufacturer"] == "WEG"
    assert c.ok("GET", "/positions/asset-at", uri=POS, at="2020-01-01")["asset"] is None

    old = c.ok("GET", f"/assets/{a['id']}")
    assert old["status"] == "in_stock" and len(old["history"]) == 1
    actions = [e["action"] for e in c.ok("GET", f"/assets/{a['id']}/audit")]
    assert actions == ["asset.remove", "asset.install", "asset.create"]


def test_one_asset_per_position_and_one_position_per_asset(c):
    a, b = _asset(c, serial="A"), _asset(c, serial="B")
    c.ok("POST", f"/assets/{a['id']}/install", {"position_uri": POS, "at": T0.isoformat()})
    st, out = c("POST", f"/assets/{b['id']}/install", {"position_uri": POS, "at": T0.isoformat()})
    assert st == 409 and "replace" in out["error"]
    st, out = c("POST", f"/assets/{a['id']}/install", {"position_uri": POS2, "at": T0.isoformat()})
    assert st == 409 and "already installed" in out["error"]


def test_events_only_go_forward_and_never_into_the_future(c):
    a = _asset(c, serial="A")
    c.ok("POST", f"/assets/{a['id']}/install", {"position_uri": POS, "at": T0.isoformat()})
    st, out = c("POST", f"/assets/{a['id']}/remove", {"at": (T0 - timedelta(days=1)).isoformat(), "reason": "x"})
    assert st == 400 and "back in time" in out["error"]
    st, out = c("POST", f"/assets/{a['id']}/remove", {"at": "2026-04-01"})
    assert st == 400 and "reason" in out["error"]
    st, out = c("POST", f"/assets/{a['id']}/remove", {"at": "2099-01-01", "reason": "x"})
    assert st == 400 and "future" in out["error"]
    c.ok("POST", f"/assets/{a['id']}/remove", {"at": "2026-04-01", "reason": "warranty swap"})
    assert c("POST", f"/assets/{a['id']}/remove", {"reason": "again"})[0] == 409


def test_edits_are_audited_and_history_is_not_editable(c):
    a = _asset(c, serial="B-l")
    c.ok("PATCH", f"/assets/{a['id']}", {"serial": "B-1"})
    assert c.rows.audit_log(rows.entity_uri("asset", a["id"]))[0]["detail"] == {"serial": {"from": "B-l", "to": "B-1"}}
    st, out = c("PATCH", f"/assets/{a['id']}", {"installed_at": "2020-01-01"})
    assert st == 400 and "append-only" in out["error"]
    assert c("POST", "/assets", {"attributes": {}})[0] == 400


def test_retire_needs_removal_first(c):
    a = _asset(c, serial="A")
    c.ok("POST", f"/assets/{a['id']}/install", {"position_uri": POS, "at": T0.isoformat()})
    assert c("POST", f"/assets/{a['id']}/retire", {"reason": "scrapped"})[0] == 409
    c.ok("POST", f"/assets/{a['id']}/remove", {"reason": "failed"})
    assert c.ok("POST", f"/assets/{a['id']}/retire", {"reason": "scrapped"})["retired_reason"] == "scrapped"
    assert c("POST", f"/assets/{a['id']}/install", {"position_uri": POS})[0] == 409
    assert c.ok("GET", "/assets", status="retired")[0]["id"] == a["id"]


def test_orgs_and_sites_are_kept_apart():
    c = Client(allows=lambda u: "/ahu-1/" in u)
    a = _asset(c, serial="A")
    c.ok("POST", f"/assets/{a['id']}/install", {"position_uri": POS, "at": T0.isoformat()})
    assert c("POST", f"/assets/{_asset(c)['id']}/install", {"position_uri": POS2})[0] == 404
    other = Client()
    other.rows = c.rows
    other.org = "org2"
    assert other("GET", f"/assets/{a['id']}")[0] == 404
    assert other.ok("GET", "/assets") == []
    narrow = Client(allows=lambda u: "/ahu-2/" in u)
    narrow.rows = c.rows
    assert a["id"] not in [x["id"] for x in narrow.ok("GET", "/assets")]
    assert narrow("GET", f"/assets/{a['id']}")[0] == 404


def test_intervals_pairs_events_in_time_order():
    ev = [
        {"id": "3", "asset_id": "a", "position_uri": "p", "kind": "install", "at": T0 + timedelta(days=2)},
        {"id": "1", "asset_id": "a", "position_uri": "p", "kind": "install", "at": T0},
        {"id": "2", "asset_id": "a", "position_uri": "p", "kind": "remove", "at": T0 + timedelta(days=1), "reason": "r"},
    ]
    ivs = assets.intervals(ev)
    assert [(i["install_event"], i["remove_event"]) for i in ivs] == [("1", "2"), ("3", None)]


def test_through_ops_api(monkeypatch):
    import json
    from http.server import ThreadingHTTPServer
    from threading import Thread

    import jwt
    import requests

    from baler.ops_api import make_handler

    monkeypatch.setenv("BALER_GATEWAY_SECRET", "gw")
    monkeypatch.setenv("BALER_JWT_SECRET", "jwt-secret-for-tests-0123456789abcdef")
    token = jwt.encode({"sub": "u1", "org_id": "org1", "role": "operator"}, "jwt-secret-for-tests-0123456789abcdef",
                       algorithm="HS256")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(lifecycle_rows=rows.MemoryRows()))
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    h = {"X-Gateway-Secret": "gw", "Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        assert requests.get(f"{base}/assets").status_code == 401
        r = requests.post(f"{base}/assets", data=json.dumps({"manufacturer": "Baldor"}), headers=h)
        assert r.status_code == 201, r.text
        assert requests.get(f"{base}/assets", headers=h).json()[0]["manufacturer"] == "Baldor"
        assert requests.delete(f"{base}/assets", headers=h).status_code == 405
        no_org = jwt.encode({"sub": "svc"}, "jwt-secret-for-tests-0123456789abcdef", algorithm="HS256")
        assert requests.get(f"{base}/assets", headers={**h, "Authorization": f"Bearer {no_org}"}).status_code == 403
    finally:
        server.shutdown()

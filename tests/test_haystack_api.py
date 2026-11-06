"""
haystack_api.py without live services: an in-memory rdflib Store shaped the
way ingest.py writes it, a fake timeseries layer, and the real HTTP handler
on a loopback port. SCRAM is exercised against a hash produced by
auth.hash_password, with the client side computed independently here.
"""

import base64
import hashlib
import hmac
import threading
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import pytest

oh = pytest.importorskip("openhay")

from rdflib import RDF, Literal, URIRef

from baler import auth
from baler import haystack_api as ha
from baler.store import BRICK, HAYSTACK, Store

AHU = "urn:equip:fbf/ahu-1"
DAT = "urn:point:fbf/ahu-1/analogInput,1"
FAN = "urn:point:fbf/ahu-1/binaryValue,3"
T0 = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store():
    s = Store()
    s.add_many([
        (URIRef(AHU), RDF.type, BRICK.Air_Handling_Unit),
        (URIRef(AHU), HAYSTACK.hasTag, Literal("ahu")),
        (URIRef(AHU), HAYSTACK.hasTag, Literal("equip")),
        (URIRef(AHU), HAYSTACK.dis, Literal("AHU-1")),
        (URIRef(AHU), BRICK.hasPoint, URIRef(DAT)),
        (URIRef(AHU), BRICK.hasPoint, URIRef(FAN)),
        (URIRef(DAT), RDF.type, BRICK.Supply_Air_Temperature_Sensor),
        (URIRef(DAT), BRICK.isPointOf, URIRef(AHU)),
        (URIRef(DAT), HAYSTACK.hasTag, Literal("temp")),
        (URIRef(DAT), HAYSTACK.hasTag, Literal("sensor")),
        (URIRef(DAT), HAYSTACK.dis, Literal("Discharge Air Temp")),
        (URIRef(DAT), HAYSTACK.unit, Literal("°F")),
        (URIRef(DAT), HAYSTACK.equipRef, Literal("source-server-id")),  # recomputed, never echoed
        (URIRef(FAN), BRICK.isPointOf, URIRef(AHU)),
        (URIRef(FAN), HAYSTACK.hasTag, Literal("fan")),
    ])
    return s


class FakeTS:
    """Stands in for point_history: {uri: [(ts, value)]}."""

    def __init__(self):
        self.rows = {DAT: [(T0, 55.0), (T0 + timedelta(minutes=5), 55.5)], FAN: [(T0, True)]}
        self.sites: list[tuple] = []  # (id, org_id, topic_prefixes) - empty = scoping off

    def execute(self, sql, params=None):
        # Just enough SQL for scope.scope_for and point_write's level table.
        levels = self.__dict__.setdefault("levels", {})
        if "FROM sites" in sql:
            rows = self.sites
        elif sql.lstrip().startswith("DELETE FROM point_write_levels"):
            levels.pop((params[0], params[1]), None)
            rows = []
        elif "INSERT INTO point_write_levels" in sql:
            uri, level, value_json, who, _user_sub, ts = params
            levels[(uri, level)] = (level, value_json, who, ts)
            rows = []
        elif "FROM point_write_levels" in sql:
            rows = [v for (u, _), v in levels.items() if u == params[0]]
        else:
            raise AssertionError(sql)

        class _Cur:
            def fetchall(self):
                return rows

        return _Cur()

    @contextmanager
    def connection(self):
        yield self

    def latest(self, conn, uris):
        return {u: self.rows[u][-1] for u in uris if self.rows.get(u)}

    def labels(self, conn, uris):
        return {}

    def range_many(self, conn, uris, start, end, limit):
        return {u: ([r for r in self.rows.get(u, []) if start <= r[0] < end], False) for u in uris}

    def write(self, conn, uri, val, ts, unit=None, label=None):
        self.rows.setdefault(uri, []).append((ts, val))
        self.rows[uri].sort(key=lambda r: r[0])


@pytest.fixture
def backend(store, monkeypatch):
    ts = FakeTS()
    monkeypatch.setattr(ha, "read_latest_many", ts.latest)
    monkeypatch.setattr(ha.timeseries, "read_point_labels", ts.labels)
    monkeypatch.setattr(ha.timeseries, "read_range_many", ts.range_many)
    monkeypatch.setattr(ha.timeseries, "write_point_value", ts.write)
    return ha.Backend(store, ts)


# --- refs -----------------------------------------------------------------


@pytest.mark.parametrize("uri", [DAT, AHU, "urn:point:x~y z/ü", "urn:point:plain"])
def test_ref_encoding_roundtrips_and_is_haystack_safe(uri):
    ref = ha.uri_to_ref(uri)
    assert all(c.isalnum() or c in "_:-.~" for c in ref), ref
    assert ha.ref_to_uri(ref) == uri
    assert ha.ref_to_uri("@" + ref) == uri


# --- records / read -------------------------------------------------------


def test_read_filter_builds_haystack_records(backend):
    recs = backend.read_filter("temp and sensor", None)
    assert len(recs) == 1
    r = recs[0]
    assert r["id"] == oh.Ref(ha.uri_to_ref(DAT)) and r["id"].dis == "Discharge Air Temp"
    assert r["point"] is oh.MARKER and r["his"] is oh.MARKER and r["temp"] is oh.MARKER
    assert r["equipRef"] == oh.Ref(ha.uri_to_ref(AHU))  # from the graph, not the stale source id
    assert r["curVal"] == oh.Number(55.5, "°F") and r["kind"] == "Number"
    assert r["brickClass"] == "Supply_Air_Temperature_Sensor"


def test_read_filter_matches_brick_classes_too(backend):
    assert [r["id"].id for r in backend.read_filter("Air_Handling_Unit", None)] == [ha.uri_to_ref(AHU)]


def test_read_filter_follows_tag_paths_and_takes_its_own_ref_ids(backend):
    dis = lambda f: sorted(r["dis"] for r in backend.read_filter(f, None) if "dis" in r)
    assert dis('equipRef->dis == "AHU-1"') == ["Discharge Air Temp"]
    assert len(backend.read_filter("point and equipRef->ahu", None)) == 2
    assert backend.read_filter("point and not equipRef->ahu", None) == []
    # The ids records carry (~XX-escaped) work in filters, not just raw URIs.
    ahu_id = backend.read_filter("ahu", None)[0]["id"].id
    assert "~" in ahu_id
    assert len(backend.read_filter(f"equipRef == @{ahu_id}", None)) == 2
    assert len(backend.read_filter(f"equipRef->id == @{ahu_id}", None)) == 2


def test_read_limit_and_bool_curval(backend):
    pts = backend.read_filter("point or fan", None)
    fan = next(r for r in pts if "fan" in r)
    assert fan["curVal"] is True and fan["kind"] == "Bool"
    assert len(backend.read_filter("point or fan or ahu", 1)) == 1


def test_read_rejects_hisread_expression(backend):
    with pytest.raises(ha.OpError):
        backend.read_filter("ahu.hisRead(today)", None)


def test_read_ids_missing_is_none(backend):
    got = backend.read_ids([oh.Ref(ha.uri_to_ref(DAT)), oh.Ref("urn:point:nope")])
    assert got[0]["id"].id == ha.uri_to_ref(DAT) and got[1] is None


def test_nav_equip_then_points(backend):
    top = backend.nav(None)
    assert [r["navId"] for r in top] == [ha.uri_to_ref(AHU)]
    kids = backend.nav(top[0]["navId"])
    assert sorted(r["id"].id for r in kids) == sorted([ha.uri_to_ref(DAT), ha.uri_to_ref(FAN)])


# --- history --------------------------------------------------------------


def test_his_read_and_write(backend):
    ref = oh.Ref(ha.uri_to_ref(DAT))
    g = backend.his_read(ref, "2026-09-08")
    assert g.col_names == ["ts", "val"]
    assert [r["val"] for r in g.rows] == [oh.Number(55.0, "°F"), oh.Number(55.5, "°F")]
    backend.his_write(ref, [{"ts": T0 + timedelta(hours=1), "val": oh.Number(60.0, "°F")}])
    assert len(backend.his_read(ref, "2026-09-08,2026-09-08").rows) == 3


def test_his_write_rejects_non_point(backend):
    with pytest.raises(ha.OpError):
        backend.his_write(oh.Ref(ha.uri_to_ref(AHU)), [{"ts": T0, "val": 1}])


# --- watches --------------------------------------------------------------


def test_watch_reports_only_changes(backend):
    req = oh.Grid.from_rows([{"id": oh.Ref(ha.uri_to_ref(DAT))}], meta={"watchDis": "t"})
    sub = ha.dispatch(backend, "watchSub", req)
    wid = sub.meta["watchId"]
    assert len(sub.rows) == 1
    poll = oh.Grid(meta={"watchId": wid})
    assert ha.dispatch(backend, "watchPoll", poll).rows == []
    backend.his_write(oh.Ref(ha.uri_to_ref(DAT)), [{"ts": T0 + timedelta(hours=2), "val": 61.0}])
    changed = ha.dispatch(backend, "watchPoll", poll).rows
    assert changed[0]["curVal"].val == 61.0
    ha.dispatch(backend, "watchUnsub", oh.Grid(meta={"watchId": wid, "close": oh.MARKER}))
    with pytest.raises(ha.OpError):
        ha.dispatch(backend, "watchPoll", poll)


class FakeCommander:
    def __init__(self, fail: str | None = None):
        self.sent, self.fail = [], fail

    def point_write(self, site, instance, topic, value, priority):
        if self.fail:
            raise ha.pw.CommandError(self.fail)
        self.sent.append((site, instance, topic, value, priority))
        return {"present_value": value}


SP = "urn:point:fbf/ahu-1/analogValue,9"


@pytest.fixture
def writable(store, backend):
    store.add_many([
        (URIRef(AHU), HAYSTACK.windrowSite, Literal("site-a")),
        (URIRef(AHU), HAYSTACK.windrowInstance, Literal("i1")),
        (URIRef(SP), BRICK.isPointOf, URIRef(AHU)),
        (URIRef(SP), HAYSTACK.hasTag, Literal("sp")),
        (URIRef(SP), HAYSTACK.hasTag, Literal("writable")),
    ])
    backend.commander = FakeCommander()
    return backend


def _pw(backend, level=None, val=None, who=None, caller=None, scope=ha.EVERYTHING):
    row = {"id": oh.Ref(ha.uri_to_ref(SP))}
    if level is not None:
        row.update(level=oh.Number(level), val=val)
    if who:
        row["who"] = who
    return ha.dispatch(backend, "pointWrite", oh.Grid.from_rows([row]), scope, caller)


def test_point_write_routes_to_owning_windrow_and_records_level(writable):
    _pw(writable, 8, oh.Number(72.5, "°F"), caller={"sub": "user-1"})
    assert writable.commander.sent == [("site-a", "i1", "fbf/ahu-1/analogValue,9", 72.5, 8)]
    arr = _pw(writable)
    assert len(arr.rows) == 16
    eight = arr.rows[7]
    assert eight["level"] == oh.Number(8) and eight["val"] == oh.Number(72.5) and eight["who"] == "user-1"
    assert "val" not in arr.rows[15]


def test_relinquish_clears_level(writable):
    _pw(writable, 8, oh.Number(72.5), who="operator console")
    assert _pw(writable).rows[7]["who"] == "operator console"
    _pw(writable, 8, None)
    assert writable.commander.sent[-1][3] is None
    assert "val" not in _pw(writable).rows[7]


def test_point_write_refusals(writable, store):
    with pytest.raises(ha.OpError, match="not writable"):
        ha.dispatch(writable, "pointWrite", oh.Grid.from_rows(
            [{"id": oh.Ref(ha.uri_to_ref(DAT)), "level": oh.Number(8), "val": oh.Number(1)}]))
    with pytest.raises(ha.OpError, match="1-16"):
        _pw(writable, 17, oh.Number(1))
    with pytest.raises(ha.OpError, match="unknown rec"):
        _pw(writable, 8, oh.Number(1), scope=ha.Scope(("somewhere-else",)))
    writable.commander = FakeCommander(fail="site refused the write: not commandable")
    with pytest.raises(ha.OpError, match="refused"):
        _pw(writable, 8, oh.Number(1))
    assert "val" not in _pw(writable).rows[7]  # a refused write is never recorded


def test_point_write_without_route_is_explained(writable, store):
    store.graph.remove((URIRef(AHU), HAYSTACK.windrowSite, None))
    with pytest.raises(ha.OpError, match="no command route"):
        _pw(writable, 8, oh.Number(1))
    assert writable.commander.sent == []


# --- HTTP -----------------------------------------------------------------

SECRET = "test-gateway-secret"


@pytest.fixture
def http(backend, monkeypatch):
    monkeypatch.setenv("BALER_GATEWAY_SECRET", SECRET)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ha.make_handler(backend, None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _req(url, body=None, headers=None, secret=SECRET):
    h = {"X-Gateway-Secret": secret, **(headers or {})}
    r = urllib.request.Request(url, data=body.encode() if body else None, headers=h, method="POST" if body else "GET")
    with urllib.request.urlopen(r) as resp:
        return resp.status, resp.headers, resp.read().decode()


def test_http_get_read_zinc(http):
    status, headers, body = _req(f"{http}/api/demo/read?filter=temp")
    assert status == 200 and headers["Content-Type"].startswith("text/zinc")
    g = oh.decode_grid(body)
    assert g.rows[0]["curVal"] == oh.Number(55.5, "°F")


def test_http_post_json3_answers_json3(http):
    req = oh.encode_grid(oh.Grid.from_rows([{"filter": "ahu"}]), "json3")
    _, _headers, body = _req(f"{http}/api/demo/read", req,
                            {"Content-Type": "application/json", "Accept": "application/json"})
    assert '"m:"' in body and oh.sniff(body) == "json3"


def test_http_defs_libs_filetypes(http):
    libs = oh.decode_grid(_req(f"{http}/api/demo/libs")[2])
    assert sorted(r["def"].val for r in libs) == ["lib:ph", "lib:phIct", "lib:phIoT", "lib:phScience"]
    site = oh.decode_grid(_req(f"{http}/api/defs?filter=def%3D%3D%5Esite")[2])
    assert [r["def"] for r in site] == [oh.Symbol("site")]
    assert len(oh.decode_grid(_req(f"{http}/api/demo/filetypes?limit=2")[2])) == 2
    assert oh.decode_grid(_req(f"{http}/api/defs?filter=def%20%3D%3D")[2]).is_error
    ops = {r["name"] for r in oh.decode_grid(_req(f"{http}/api/ops")[2])}
    assert {"defs", "libs", "filetypes"} <= ops


def test_http_flat_layout_and_error_grid(http):
    _, _, body = _req(f"{http}/api/read?filter=point%20and%20(")
    assert oh.decode_grid(body).is_error


def test_http_requires_gateway(http):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _req(f"{http}/api/demo/about", secret="wrong")
    assert exc.value.code == 401


# --- SCRAM against the existing password hash -----------------------------


def test_scram_credentials_from_baler_hash_match_direct_derivation():
    stored = auth.hash_password("pencil")
    salt_hex, _ = stored.split("$")
    creds = ha.ScramLogin.credentials_from_hash(stored)
    direct = oh.ScramCredentials.derive("pencil", bytes.fromhex(salt_hex), auth._PBKDF2_ITERATIONS)
    assert creds.stored_key == direct.stored_key and creds.server_key == direct.server_key


def test_scram_login_issues_session_jwt(monkeypatch):
    monkeypatch.setenv("BALER_JWT_SECRET", "x" * 32)
    monkeypatch.setattr(auth, "_PBKDF2_ITERATIONS", 4096)  # keep the test fast; same math
    user = {"id": "u1", "org_id": "o1", "email": "admin@acme.example",
            "password_hash": auth.hash_password("pencil"), "role": "admin"}
    monkeypatch.setattr(auth, "get_user_by_email", lambda conn, e: user if e == user["email"] else None)
    monkeypatch.setattr(auth, "user_site_ids", lambda conn, uid: ["s1"])
    login = ha.ScramLogin(FakeTS())

    def run(email, password):
        status, h, _ = login.handle("HELLO", {"username": oh.b64url(email.encode())})
        assert status == 401
        token = oh.parse_auth_header(h["WWW-Authenticate"])[1]["handshakeToken"]
        bare = f"n={email},r=abc123"
        status, h, _ = login.handle("SCRAM", {"handshakeToken": token, "data": oh.b64url(f"n,,{bare}".encode())})
        assert status == 401
        sf = oh.b64url_decode(oh.parse_auth_header(h["WWW-Authenticate"])[1]["data"]).decode()
        a = dict(kv.split("=", 1) for kv in sf.split(","))
        salted = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(a["s"]), int(a["i"]))
        ck = hmac.new(salted, b"Client Key", "sha256").digest()
        wp = f"c=biws,r={a['r']}"
        sig = hmac.new(hashlib.sha256(ck).digest(), f"{bare},{sf},{wp}".encode(), "sha256").digest()
        proof = base64.b64encode(bytes(x ^ y for x, y in zip(ck, sig))).decode()
        return login.handle("SCRAM", {"handshakeToken": token, "data": oh.b64url(f"{wp},p={proof}".encode())})

    status, headers, who = run("admin@acme.example", "pencil")
    assert status == 200 and who["id"] == "u1"
    jwt = oh.parse_auth_header(headers["Authentication-Info"])[1]["authToken"]
    claims = auth.decode_own_token("Bearer " + jwt)
    assert claims["sub"] == "u1" and claims["site_ids"] == ["s1"] and claims["kind"] == "session"

    assert run("admin@acme.example", "wrong")[0] == 403
    assert run("nobody@acme.example", "pencil")[0] == 403


# --- site scoping ---------------------------------------------------------

from baler.scope import Scope


def test_scope_hides_other_sites_everywhere(backend):
    other = Scope(("fbf/somewhere-else",))
    mine = Scope(("fbf/ahu-1",))
    assert backend.read_filter("point or fan or ahu", None, other) == []
    assert len(backend.read_filter("point or fan or ahu", None, mine)) == 3
    assert backend.read_ids([oh.Ref(ha.uri_to_ref(DAT))], other) == [None]
    assert backend.nav(None, other) == [] and len(backend.nav(None, mine)) == 1
    with pytest.raises(ha.OpError, match="unknown rec"):
        backend.his_read(oh.Ref(ha.uri_to_ref(DAT)), "2026-09-08", other)
    with pytest.raises(ha.OpError, match="unknown rec"):
        backend.his_write(oh.Ref(ha.uri_to_ref(DAT)), [{"ts": T0, "val": 1.0}], other)


def test_scope_applies_through_dispatch_and_watches(backend):
    other = Scope(("nope",))
    req = oh.Grid.from_rows([{"id": oh.Ref(ha.uri_to_ref(DAT))}], meta={"watchDis": "t"})
    assert ha.dispatch(backend, "watchSub", req, other).rows == []
    assert ha.dispatch(backend, "read", oh.Grid.from_rows([{"filter": "temp"}]), other).rows == []

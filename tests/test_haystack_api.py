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

from baler import auth, hisquery
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
            uri, level, value_json, who, _user_sub, ts, expires_at = params
            levels[(uri, level)] = (level, value_json, who, ts, expires_at)
            rows = []
        elif "expires_at <= %s" in sql:
            rows = [(u, v[0], v[2]) for (u, _), v in levels.items() if v[4] is not None and v[4] <= params[0]]
        elif "FROM point_write_levels" in sql:
            rows = [v for (u, _), v in levels.items() if u == params[0]]
        elif "INSERT INTO revoked_tokens" in sql:
            self.__dict__.setdefault("revoked", set()).add(params[0])
            rows = []
        elif sql.lstrip().startswith("DELETE FROM revoked_tokens"):
            rows = []
        elif "FROM users WHERE id" in sql:
            rows = [(params[0], "org-1", f"{params[0]}@acme.example", "x", "operator")] if params[0] == "user-1" else []
        elif "FROM revoked_tokens" in sql:
            rows = [(1,)] if params[0] in self.__dict__.get("revoked", set()) else []
        else:
            raise AssertionError(sql)

        class _Cur:
            def fetchall(self):
                return rows

            def fetchone(self):
                return rows[0] if rows else None

        return _Cur()

    @contextmanager
    def connection(self):
        yield self

    def latest(self, conn, uris):
        return {u: self.rows[u][-1] for u in uris if self.rows.get(u)}

    def labels(self, conn, uris):
        return {}

    def first(self, conn, uris):
        return {u: self.rows[u][0][0] for u in uris if self.rows.get(u)}

    def range_many(self, conn, uris, start, end, limit):
        return {u: ([r for r in self.rows.get(u, []) if start <= r[0] < end], False) for u in uris}

    def write(self, conn, uri, val, ts, unit=None, label=None):
        self.rows.setdefault(uri, []).append((ts, val))
        self.rows[uri].sort(key=lambda r: r[0])

    def write_many(self, conn, rows):
        for row in rows:  # (uri, val, ts[, unit[, label]]), as timeseries.write_point_values
            self.write(conn, row[0], row[1], row[2], *row[3:])
        return len(rows)


@pytest.fixture
def backend(store, monkeypatch):
    ts = FakeTS()
    monkeypatch.setattr(ha, "read_latest_many", ts.latest)
    monkeypatch.setattr(ha, "read_first_ts_many", ts.first)
    monkeypatch.setattr(ha.timeseries, "read_point_labels", ts.labels)
    monkeypatch.setattr(ha.timeseries, "read_range_many", ts.range_many)
    monkeypatch.setattr(ha.timeseries, "write_point_value", ts.write)
    monkeypatch.setattr(ha.timeseries, "write_point_values", ts.write_many)
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
    assert r["hisStart"] == T0 and r["hisEnd"] == T0 + timedelta(minutes=5)  # history extent, like Haxall
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
    # navId is a Str in the spec; a client sending it back as a Ref works too
    for nav_id in (top[0]["navId"], oh.Ref(top[0]["navId"])):
        g = ha.dispatch(backend, "nav", oh.Grid.from_rows([{"navId": nav_id}]))
        assert sorted(r["id"].id for r in g.rows) == sorted([ha.uri_to_ref(DAT), ha.uri_to_ref(FAN)])


# --- history --------------------------------------------------------------


def test_his_read_and_write(backend):
    ref = oh.Ref(ha.uri_to_ref(DAT))
    g = backend.his_read(ref, "2026-09-08")
    assert g.col_names == ["ts", "val"]
    assert [r["val"] for r in g.rows] == [oh.Number(55.0, "°F"), oh.Number(55.5, "°F")]
    backend.his_write(ref, [{"ts": T0 + timedelta(hours=1), "val": oh.Number(60.0, "°F")}])
    assert len(backend.his_read(ref, "2026-09-08,2026-09-08").rows) == 3


def test_his_read_datetime_ranges(backend):
    ref = oh.Ref(ha.uri_to_ref(DAT))
    # {dateTime},{dateTime}: start inclusive, end exclusive
    g = backend.his_read(ref, "2026-09-08T12:00:00Z UTC,2026-09-08T12:05:00Z UTC")
    assert [r["val"] for r in g.rows] == [oh.Number(55.0, "°F")]
    # {dateTime} alone: from then until now
    g = backend.his_read(ref, "2026-09-08T12:01:00Z UTC")
    assert [r["val"] for r in g.rows] == [oh.Number(55.5, "°F")]


def test_his_read_batch_and_his_write_batch(backend):
    dat, fan = oh.Ref(ha.uri_to_ref(DAT)), oh.Ref(ha.uri_to_ref(FAN))
    req = oh.Grid(meta={"range": "2026-09-08"}, cols=[oh.Col("id")], rows=[{"id": dat}, {"id": fan}])
    g = ha.dispatch(backend, "hisRead", req)
    assert g.col_names == ["ts", "v0", "v1"]
    assert g.cols[1].meta["id"] == dat and g.cols[2].meta["id"] == fan
    assert g.rows[0]["v0"] == oh.Number(55.0, "°F") and g.rows[0]["v1"] is True
    assert "v1" not in g.rows[1]  # FAN has no sample at 12:05

    t = T0 + timedelta(hours=3)
    write = oh.Grid(cols=[oh.Col("ts"), oh.Col("v0", {"id": dat}), oh.Col("v1", {"id": fan})],
                    rows=[{"ts": t, "v0": oh.Number(70.0, "°F"), "v1": False}])
    ha.dispatch(backend, "hisWrite", write)
    assert backend.his_read(dat, "2026-09-08").rows[-1]["val"] == oh.Number(70.0, "°F")
    assert backend.his_read(fan, "2026-09-08").rows[-1]["val"] is False


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
    assert len(arr.rows) == 17  # 1-16 plus 17, the default
    assert arr.col_names == ["level", "levelDis", "val", "valDis", "who", "expires"]
    eight = arr.rows[7]
    assert eight["level"] == oh.Number(8) and eight["val"] == oh.Number(72.5) and eight["who"] == "user-1@acme.example"
    assert eight["levelDis"] == "8 (manual)" and arr.rows[16]["levelDis"] == "def"
    assert "val" not in arr.rows[15] and "expires" not in eight  # no duration = permanent


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
    with pytest.raises(ha.OpError, match="1-17"):
        _pw(writable, 18, oh.Number(1))
    with pytest.raises(ha.OpError, match="not relinquished"):
        _pw(writable, 17, None)
    with pytest.raises(ha.OpError, match="unknown rec"):
        _pw(writable, 8, oh.Number(1), scope=ha.Scope(("somewhere-else",)))
    writable.commander = FakeCommander(fail="site refused the write: not commandable")
    with pytest.raises(ha.OpError, match="refused"):
        _pw(writable, 8, oh.Number(1))
    assert "val" not in _pw(writable).rows[7]  # a refused write is never recorded


def test_level_17_and_timed_level_8_override(writable):
    _pw(writable, 17, oh.Number(68.0))
    assert writable.commander.sent[-1][4] == 17  # Windrow writes relinquish-default
    assert _pw(writable).rows[16]["val"] == oh.Number(68.0)

    ha.dispatch(writable, "pointWrite", oh.Grid.from_rows([{
        "id": oh.Ref(ha.uri_to_ref(SP)), "level": oh.Number(8), "val": oh.Number(70.0),
        "duration": oh.Number(30, "min")}]))
    left = _pw(writable).rows[7]["expires"]
    assert left.unit == "s" and 1790 < left.val <= 1800
    assert writable.expire_overrides() == 0  # not due yet

    # time passes: the override is relinquished at level 8, nothing else
    later = datetime.now(timezone.utc) + timedelta(minutes=31)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(ha, "datetime", type("dt", (datetime,), {"now": staticmethod(lambda tz=None: later)}))
    try:
        assert writable.expire_overrides() == 1
    finally:
        monkey.undo()
    assert writable.commander.sent[-1][3:] == (None, 8)
    arr = _pw(writable)
    assert "val" not in arr.rows[7] and arr.rows[16]["val"] == oh.Number(68.0)
    with pytest.raises(ha.OpError, match="only applies to level 8"):
        ha.dispatch(writable, "pointWrite", oh.Grid.from_rows([{
            "id": oh.Ref(ha.uri_to_ref(SP)), "level": oh.Number(10), "val": oh.Number(1),
            "duration": oh.Number(1, "min")}]))


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
    ops = {r["def"].val for r in oh.decode_grid(_req(f"{http}/api/ops")[2])}
    assert {"op:defs", "op:libs", "op:filetypes"} <= ops


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


def test_records_keep_haystack_types_from_an_import(tmp_path, monkeypatch):
    """Values stored by haystack_import come back out of /api as the same
    Haystack types: Numbers with units, Refs and Ref lists, dates."""
    from baler import haystack_import as hi

    store = Store()
    ts = FakeTS()
    monkeypatch.setattr(ha, "read_latest_many", ts.latest)
    monkeypatch.setattr(ha, "read_first_ts_many", ts.first)
    monkeypatch.setattr(ha.timeseries, "read_point_labels", ts.labels)
    (tmp_path / "r.zinc").write_text(
        'ver:"3.0"\nid,dis,equip,ahu,area,since,feeds\n'
        '@e1,"AHU-1",M,M,1200ft²,2021-05-04,\n'
        '@e2,"AHU-2",M,M,,,[@e1]\n'
    )
    hi.import_file(store, None, str(tmp_path / "r.zinc"), "src")
    backend = ha.Backend(store, ts)
    recs = {r["dis"]: r for r in backend.read_filter("ahu", None)}
    assert recs["AHU-1"]["area"] == oh.Number(1200.0, "ft²")
    assert str(recs["AHU-1"]["since"]) == "2021-05-04"
    feeds = recs["AHU-2"]["feeds"]
    assert isinstance(feeds, oh.Ref) and ha.ref_to_uri(feeds.id) == "urn:equip:haystack:src:e1"
    assert [r["dis"] for r in backend.read_filter('feeds->dis == "AHU-1"', None)] == ["AHU-2"]
    assert [r["dis"] for r in backend.read_filter("area > 1000ft²", None)] == ["AHU-1"]
    assert backend.read_filter("area > 1000", None) == []  # units must match


def test_close_revokes_the_session_token(backend):
    from baler import auth

    caller = {"sub": "u1", "jti": "session-123", "exp": int(T0.timestamp()) + 3600}
    assert not auth.is_revoked(backend.ts_pool, "session-123")
    assert len(ha.dispatch(backend, "close", oh.Grid(), caller=caller)) == 0
    assert auth.is_revoked(backend.ts_pool, "session-123")
    assert not auth.is_revoked(backend.ts_pool, "another-session")


def test_ops_are_op_defs_for_served_ops(backend):
    g = ha.dispatch(backend, "ops", oh.Grid())
    names = {r["def"].val for r in g.rows}
    assert {"op:about", "op:read", "op:hisRead", "op:pointWrite", "op:close"} <= names
    assert all(r["def"].val.startswith("op:") and oh.Symbol("op") in r["is"] for r in g.rows)
    assert all(n[len("op:"):] in {o for o, _ in ha.OPS} | {"pointWrite"} for n in names)
    assert "op:invokeAction" not in names  # deliberately unsupported (Baler#134)
    about = ha.dispatch(backend, "about", oh.Grid()).rows[0]
    assert about["moduleName"] and about["moduleVersion"]


def test_his_write_refuses_a_different_unit(backend):
    ref = oh.Ref(ha.uri_to_ref(DAT))  # DAT's unit is °F
    with pytest.raises(ha.OpError, match="doesn't match"):
        backend.his_write(ref, [{"ts": T0 + timedelta(hours=4), "val": oh.Number(20.0, "°C")}])
    backend.his_write(ref, [{"ts": T0 + timedelta(hours=4), "val": oh.Number(70.0, "°F")}])


def test_defs_limit_marks_incomplete_and_filetypes_are_what_we_serve(backend):
    g = ha.dispatch(backend, "defs", oh.Grid.from_rows([{"limit": oh.Number(3)}]))
    assert len(g.rows) == 3 and g.meta["limit"] == oh.Number(3) and "incomplete" in g.meta
    names = {r["def"].val for r in ha.dispatch(backend, "filetypes", oh.Grid()).rows}
    assert names == {"filetype:zinc", "filetype:json", "filetype:trio", "filetype:csv"}


def test_http_unsupported_accept_is_406(http):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _req(f"{http}/api/demo/about", headers={"Accept": "text/turtle"})
    assert exc.value.code == 406


def test_brick_location_links_come_back_as_ref_lists():
    """The UI tree (ui/app.js) walks Site > Building > Floor > Zone and
    feeds from these, through /api instead of the graph (Baler#12)."""
    store = Store()
    g = store.graph
    site, bld = URIRef("urn:location:site-1"), URIRef("urn:location:site/b1")
    ahu = URIRef("urn:equip:windrow/ahu-1")
    g.add((site, RDF.type, BRICK.Site))
    g.add((site, BRICK.hasPart, bld))
    g.add((ahu, BRICK.feeds, bld))
    recs = ha.build_records(store, None, [str(site), str(ahu)])
    assert recs[str(site)]["brickClass"] == "Site"
    assert recs[str(site)]["brickHasPart"] == [oh.Ref(ha.uri_to_ref(str(bld)))]
    assert recs[str(ahu)]["brickFeeds"] == [oh.Ref(ha.uri_to_ref(str(bld)))]


def test_live_tags_filter_on_point_history(monkeypatch):
    """`his`, `curVal`, `curStatus` come from point_history, not the graph;
    read must still filter on them (it returned nothing for `point and his`
    on nash-srv while every record showed `his`)."""
    store = Store()
    g = store.graph
    ahu = URIRef("urn:equip:w/ahu-1")
    hot, cold, idle = (URIRef(f"urn:point:w/ahu-1/{n}") for n in ("hot", "cold", "idle"))
    g.add((ahu, RDF.type, BRICK.AHU))
    for p in (hot, cold, idle):
        g.add((ahu, BRICK.hasPoint, p))
        g.add((p, BRICK.isPointOf, ahu))
        g.add((p, HAYSTACK.hasTag, Literal("sensor")))
    now = datetime.now(timezone.utc)
    latest = {str(hot): (now, 80.0), str(cold): (now, 55.0)}  # idle: no history
    monkeypatch.setattr(ha, "read_latest_many", lambda conn, uris: {u: latest[u] for u in uris if u in latest})
    monkeypatch.setattr(ha, "read_first_ts_many", lambda conn, uris: {u: latest[u][0] for u in uris if u in latest})
    monkeypatch.setattr(ha.timeseries, "read_point_labels", lambda conn, uris: {u: ("°F", None) for u in uris})

    class Pool:
        @contextmanager
        def connection(self):
            yield object()

    be = ha.Backend(store, Pool())

    def names(flt, limit=None):
        return sorted(str(r["id"].dis) for r in be.read_filter(flt, limit))

    assert names("point and his") == ["cold", "hot"]
    assert names("his") == ["cold", "hot"]
    assert names("point and not his") == ["idle"]
    assert names("curVal > 70°F") == ["hot"]
    assert names("curVal > 70") == []  # Haystack: no comparison across units
    assert names("sensor and curVal < 60°F") == ["cold"]
    assert names("curStatus == \"ok\"") == ["cold", "hot"]
    assert names("not (his and curVal > 70°F) and point") == ["cold", "idle"]
    assert names("equip or curVal > 70°F") == ["ahu-1", "hot"]
    assert len(names("his", limit=1)) == 1


def test_site_ref_from_brick_containment():
    """siteRef comes from Brick structure (Site > Building > Floor, an AHU
    feeding the floor, its points); nav goes site -> equip -> point, like
    Haxall's."""
    store = Store()
    g = store.graph
    site, bld, fl = URIRef("urn:location:site-1"), URIRef("urn:location:site/b1"), URIRef("urn:location:f1")
    ahu, orphan, p = URIRef("urn:equip:w/ahu-1"), URIRef("urn:equip:w/orphan"), URIRef("urn:point:w/ahu-1/sat")
    for u, c in [(site, BRICK.Site), (bld, BRICK.Building), (fl, BRICK.Floor)]:
        g.add((u, RDF.type, c))
    g.add((site, BRICK.hasPart, bld))
    g.add((bld, BRICK.hasPart, fl))
    g.add((ahu, BRICK.feeds, fl))
    g.add((ahu, BRICK.hasPoint, p))
    g.add((p, BRICK.isPointOf, ahu))
    g.add((orphan, BRICK.hasPoint, URIRef("urn:point:w/o/x")))

    class Pool:
        @contextmanager
        def connection(self):
            yield None

    be = ha.Backend(store, Pool())
    recs = ha.build_records(store, None, [str(site), str(ahu), str(p), str(orphan)])
    assert recs[str(site)]["site"] == oh.MARKER
    assert recs[str(ahu)]["siteRef"].id == ha.uri_to_ref(str(site))
    assert recs[str(p)]["siteRef"].id == ha.uri_to_ref(str(site))
    assert "siteRef" not in recs[str(orphan)]

    def ids(flt):
        return sorted(ha.ref_to_uri(r["id"].id) for r in be.read_filter(flt, None))

    assert ids("site") == [str(site)]
    assert ids(f"point and siteRef==@{ha.uri_to_ref(str(site))}") == [str(p)]
    assert ids("point and siteRef->site") == [str(p)]
    assert ids("equip and not siteRef") == [str(orphan)]

    assert [ha.ref_to_uri(r["navId"]) for r in be.nav(None)] == [str(orphan), str(site)]
    assert [ha.ref_to_uri(r["navId"]) for r in be.nav(ha.uri_to_ref(str(site)))] == [str(ahu)]
    assert [ha.ref_to_uri(r["id"].id) for r in be.nav(ha.uri_to_ref(str(ahu)))] == [str(p)]


def test_uri_literals_compare_only_with_uri_values():
    """`link == `http://x/a`` (Baler#9): a Uri tag is stored typed, compared
    only against a Uri literal (never a Str with the same text), and comes
    back out of /api as a Uri."""
    from baler.haystack_import import _row
    from baler.ingest import ingest_haystack_equip_tags

    store = Store()
    for rid, link in [("a", oh.Uri("http://x/a")), ("b", "http://x/a"), ("c", oh.Uri("http://x/c"))]:
        ingest_haystack_equip_tags(store, rid, _row({"id": oh.Ref(rid), "site": oh.MARKER, "link": link}))

    def ids(flt):
        return sorted(u.rsplit(":", 1)[1] for u in hisquery.match_entities(store, hisquery.parse_query(flt).filter))

    assert ids("link == `http://x/a`") == ["a"]
    assert ids('link == "http://x/a"') == ["b"]
    assert ids("link > `http://x/b`") == ["c"]
    recs = ha.build_records(store, None, [str(u) for u in hisquery.match_entities(store, hisquery.parse_query("site").filter)])
    links = {u.rsplit(":", 1)[1]: r["link"] for u, r in recs.items()}
    assert links["a"] == oh.Uri("http://x/a") and links["b"] == "http://x/a"


def test_nested_dicts_and_datetime_zones_round_trip():
    """Baler#10/#9: a nested Dict is stored as its own node (paths step into
    it, /api rebuilds it typed), and a DateTime keeps its Haystack zone."""
    from baler.haystack_import import _row
    from baler.ingest import ingest_haystack_equip_tags

    store = Store()
    rec = oh.from_hayson({
        "id": {"_kind": "ref", "val": "e"}, "site": {"_kind": "marker"},
        "ts": {"_kind": "dateTime", "val": "2020-09-01T01:00:00-04:00", "tz": "New_York"},
        "ref": {"thru": "e", "n": {"_kind": "number", "val": 3, "unit": "kW"}, "m": {"_kind": "marker"}, "deep": {"x": 1}},
    })
    uri = str(ingest_haystack_equip_tags(store, "e", _row(rec)))
    out = ha.build_records(store, None, [uri])[uri]
    assert out["ref"] == {"thru": "e", "n": oh.Number(3, "kW"), "m": oh.MARKER, "deep": {"x": oh.Number(1)}}
    assert out["ts"].tzinfo.key == "America/New_York"

    def ids(flt):
        return hisquery.match_entities(store, hisquery.parse_query(flt).filter)

    assert ids('ref->thru == "e"') == [uri] and ids("ref->m") == [uri] and ids("ref->n > 2kW") == [uri]
    assert ids('ts->tz == "New_York"') == [uri]
    assert ids("ts == 2020-09-01T01:00:00-04:00 New_York") == [uri]
    assert ids("ts == 2020-09-01T05:00:00Z UTC") == []  # same instant, other zone: not equal
    assert ids("ts < 2020-09-01T06:00:00Z UTC // comments are fine") == [uri]
    assert not any(u.startswith("urn:baler:dict:") for u in ids("thru or m"))  # dict nodes aren't records


def test_records_advertise_the_xeto_spec_they_fit():
    """Baler#22: the most specific ph spec, from the record's markers plus
    the markers its Brick class implies (aliases and subclasses included)."""
    m = oh.MARKER
    assert ha.best_spec({"equip": m, "ahu": m}) == "ph::Ahu"
    assert ha.best_spec({"equip": m, "brickClass": "AHU"}) == "ph::Ahu"  # Brick alias, no ahu marker
    assert ha.best_spec({"point": m, "brickClass": "Zone_Air_Temperature_Sensor"}) == "ph.points::ZoneAirTempSensor"
    assert ha.best_spec({"point": m}) == "ph::Point"  # not BoolPoint: same markers, base spec wins
    assert ha.best_spec({"dis": "x"}) is None


# --- Baler#134: the gaps once listed in todo/haystack-spec-comparison.md ---


def test_invoke_action_is_a_clear_error_grid_not_a_404(http, backend):
    """invokeAction is deliberately unsupported: an error grid that says so
    and points at pointWrite, over HTTP as a normal 200 Haystack error."""
    req = oh.Grid.from_rows([{"id": oh.Ref(ha.uri_to_ref(DAT)), "action": "on"}])
    with pytest.raises(ha.OpError, match="not supported.*pointWrite"):
        ha.dispatch(backend, "invokeAction", req)
    status, _, body = _req(f"{http}/api/demo/invokeAction", oh.encode_grid(req, "zinc"),
                           {"Content-Type": "text/zinc"})
    g = oh.decode_grid(body)
    assert status == 200 and g.is_error and "pointWrite" in g.meta["dis"]


def test_close_then_the_same_token_is_refused(http, backend, monkeypatch):
    """close revokes the session's jti; haystack_api refuses it at once (the
    gateway refuses it everywhere else once its denylist refreshes)."""
    import jwt as pyjwt

    monkeypatch.setenv("BALER_JWT_SECRET", "test-jwt-secret-of-at-least-32-bytes")
    exp = int(datetime.now(timezone.utc).timestamp()) + 3600
    claims = {"sub": "u1", "jti": "sess-close-1", "exp": exp, "kind": "session"}
    token = pyjwt.encode(claims, "test-jwt-secret-of-at-least-32-bytes", algorithm="HS256")
    auth_h = {"Authorization": f"Bearer {token}"}
    assert _req(f"{http}/api/demo/about", headers=auth_h)[0] == 200
    assert _req(f"{http}/api/demo/close", headers=auth_h)[0] == 200
    with pytest.raises(urllib.error.HTTPError) as exc:
        _req(f"{http}/api/demo/about", headers=auth_h)
    assert exc.value.code == 401


def test_tag_paths_units_and_literals_in_filters():
    """-> paths, units and date/time/uri/^symbol literals all parse (each is
    also matched end to end in the tests above); a Symbol as a comparison
    value isn't Haystack filter grammar and is a parse error, not a guess."""
    parse = lambda f: hisquery.parse_query(f).filter
    assert isinstance(parse("point and equipRef->ahu").right, hisquery.Path)
    assert parse('equipRef->siteRef->dis == "HQ"').hops == ("equipRef", "siteRef")
    assert parse("area > 1000ft²").value == hisquery.Num(1000.0, "ft²")
    assert isinstance(parse("occ == 08:30").value, hisquery.TimeLit)
    assert isinstance(parse("d == 2024-01-01").value, hisquery.DateLit)
    assert isinstance(parse("link == `http://x/`").value, hisquery.UriLit)
    assert parse("^hot-water") == parse("hot and water")
    with pytest.raises(hisquery.HisQueryError):
        parse("kind == ^elec")

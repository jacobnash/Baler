"""
Clients follow read pages (#280). Baler answers a filter read a page at a
time (BALER_READ_LIMIT, 1000); these run 2,500 records through the real
haystack_api handler on a loopback port (in-memory graph, fake historian)
and check that Baler's clients come back with all of them, that a caller
asking for one page gets `after`, and that the page's paging meta is in the
HTTP headers too, for clients that only hand back rows.
"""

import threading
import urllib.parse
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import pytest

from baler.haystack_client import HaystackClient, HaystackError, Records
from baler.ingest_api import his_paging_headers

N = 2500
SECRET = "test-gateway-secret"


# --- without openhay: the client against a fake paging server ------------


class _PagedClient(HaystackClient):
    """HaystackClient whose transport is a canned list of grids."""

    def __init__(self, grids):
        super().__init__("http://unused", token="t")
        self.grids, self.calls = list(grids), []

    def _get(self, op, params=None, _retry_on_401=True):
        self.calls.append(dict(params or {}))
        return self.grids.pop(0)


def test_read_page_surfaces_json3_paging_meta():
    c = _PagedClient([{"meta": {"incomplete": "s:limit of 1 reached", "limit": "n:1", "after": "r:p1 Zone"},
                       "rows": [{"id": "r:p1"}]}])
    page = c.read_page("point", 1)
    assert page == [{"id": "p1"}] and page.after == "p1" and page.limit == 1
    assert page.incomplete == "limit of 1 reached" and not page.complete
    assert c.calls == [{"filter": "point", "limit": 1}]


def test_a_cut_short_read_without_after_is_flagged_not_passed_off_as_complete(caplog):
    c = _PagedClient([{"meta": {"incomplete": "row limit"}, "rows": [{"id": "r:p1"}]}])
    recs = c.read("point")
    assert recs == [{"id": "p1"}] and recs.incomplete == "row limit" and not recs.complete
    assert "cut the read short" in caplog.text


def test_a_server_repeating_after_is_an_error_not_a_loop():
    page = {"meta": {"after": {"_kind": "ref", "val": "p1"}}, "rows": [{"id": "r:p1"}]}
    with pytest.raises(HaystackError, match="same `after`"):
        _PagedClient([page, page, page]).read("point")


def test_a_complete_read_says_so():
    recs = _PagedClient([{"meta": {}, "rows": [{"id": "r:p1"}]}]).read("point")
    assert recs.complete and recs.after is None and recs.pages == 1 and isinstance(recs, Records)


def test_his_paging_headers_repeat_after_and_link_the_next_page():
    result = {"after": "urn:point:x/p-0999", "incomplete": "seriesLimit of 1000 reached; pass after to read on"}
    h = his_paging_headers("/his?expr=readAll(point).hisRead(today)&after=urn:point:x/p-0001", result)
    assert h["X-Baler-After"] == result["after"] and h["X-Baler-Incomplete"] == result["incomplete"]
    query = urllib.parse.parse_qs(h["Link"].split(">")[0].lstrip("<?"))
    assert query == {"expr": ["readAll(point).hisRead(today)"], "after": [result["after"]]}
    assert h["Link"].endswith('rel="next"')
    assert his_paging_headers("/his?expr=x", {"after": None, "incomplete": None}) == {}
    assert "X-Baler-After" not in his_paging_headers("/his?expr=x", {**result, "after": "urn:point:ü"})


# --- with openhay: 2,500 records through the real server -----------------

try:
    import openhay as oh
except ImportError:  # the HTTP half needs haystack_api, so openhay
    oh = None
else:
    from rdflib import Literal, URIRef

    from baler import haystack_api as ha
    from baler import openhay_paging
    from baler.store import HAYSTACK, Store
    from baler_fn import BalerClient

needs_openhay = pytest.mark.skipif(oh is None, reason="openhay not installed (pip install -e '.[haystack]')")


class _NoHistory:
    """The historian, empty: scoping off, nothing revoked, no samples."""

    def execute(self, sql, params=None):
        class _Cur:
            def fetchall(self):
                return []

            def fetchone(self):
                return None

        return _Cur()

    @contextmanager
    def connection(self):
        yield self


@pytest.fixture(scope="module")
def big_backend():
    store = Store()
    triples = []
    for i in range(N):
        uri = URIRef(f"urn:point:site/p-{i:04d}")
        triples += [(uri, HAYSTACK.hasTag, Literal("point")), (uri, HAYSTACK.hasTag, Literal("sensor")),
                    (uri, HAYSTACK.dis, Literal(f"P{i}"))]
    store.add_many(triples)
    mp = pytest.MonkeyPatch()
    mp.setattr(ha, "read_latest_many", lambda conn, uris: {})
    mp.setattr(ha, "read_first_ts_many", lambda conn, uris: {})
    mp.setattr(ha.timeseries, "read_point_labels", lambda conn, uris: {})
    yield ha.Backend(store, _NoHistory())
    mp.undo()


@contextmanager
def _serving(handler, monkeypatch):
    monkeypatch.setenv("BALER_GATEWAY_SECRET", SECRET)
    monkeypatch.delenv("BALER_JWT_SECRET", raising=False)
    monkeypatch.setattr(ha, "READ_LIMIT", 1000)  # the shipped default
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()


@pytest.fixture
def server(big_backend, monkeypatch):
    with _serving(ha.make_handler(big_backend, None), monkeypatch) as base:
        yield base


@pytest.fixture
def gateway(big_backend, monkeypatch):
    """The server as openhay's native (Rust) client reaches it: behind a
    stand-in for the gateway, which adds its secret header to every request
    it passes on (the native client has no way to add one itself)."""
    backend_handler = ha.make_handler(big_backend, None)

    class Gatewayed(backend_handler):
        def parse_request(self):
            ok = super().parse_request()
            if ok:
                del self.headers["X-Gateway-Secret"]
                self.headers["X-Gateway-Secret"] = SECRET
            return ok

    with _serving(Gatewayed, monkeypatch) as base:
        yield base


def _client(base):
    c = HaystackClient(f"{base}/api/demo", token="t")
    c._session.headers["X-Gateway-Secret"] = SECRET  # what the gateway adds
    return c


@needs_openhay
def test_2500_records_come_back_whole_through_the_client(server):
    recs = _client(server).read("point")
    assert len(recs) == N and len({r["id"] for r in recs}) == N
    assert recs.complete and recs.pages == 3


@needs_openhay
def test_2500_records_come_back_whole_through_baler_fn(server):
    bc = BalerClient(server, "t")
    bc.haystack._session.headers["X-Gateway-Secret"] = SECRET
    assert len(bc.read("point")) == N


@needs_openhay
def test_one_page_on_request_with_after_to_read_on(server):
    c = _client(server)
    first = c.read("point", limit=1000)
    assert len(first) == 1000 and first.after == first[-1]["id"] and first.limit == 1000 and first.pages == 1
    assert "limit of 1000 reached" in first.incomplete
    rest, after = [], first.after
    while after is not None:
        page = c.read_page("point", 1000, after)
        rest += page
        after = page.after
    assert len(first) + len(rest) == N
    default_page = c.read_page("point")  # no limit: the server's page size
    assert len(default_page) == 1000 and default_page.after is not None


def _get(url):
    r = urllib.request.Request(url, headers={"X-Gateway-Secret": SECRET, "Accept": "application/json"})
    with urllib.request.urlopen(r) as resp:
        return resp.headers, resp.read().decode()


@needs_openhay
def test_a_short_page_says_so_in_headers_and_links_the_next_page(server):
    url = f"{server}/api/demo/read?filter=point"
    headers, body = _get(url)
    grid = oh.decode_grid(body)
    assert headers["X-Baler-Incomplete"] == str(grid.meta["incomplete"])
    assert headers["X-Baler-After"] == "@" + grid.meta["after"].id and headers["X-Baler-Limit"] == "1000"
    assert "X-Baler-After" in headers["Access-Control-Expose-Headers"]
    # Follow Link the way a generic HTTP client would: relative to the URL.
    seen = len(grid.rows)
    while "Link" in headers:
        target = headers["Link"].split(">")[0].lstrip("<")
        assert headers["Link"].endswith('rel="next"')
        headers, body = _get(urllib.parse.urljoin(url, target))
        seen += len(oh.decode_grid(body).rows)
    assert seen == N and "X-Baler-Incomplete" not in headers


@needs_openhay
def test_link_from_a_post_read_carries_its_filter_and_limit(big_backend):
    req = oh.Grid.from_rows([{"filter": "point or sensor", "limit": oh.Number(10)}])
    h = ha.paging_headers("read", req, ha.dispatch(big_backend, "read", req))
    assert urllib.parse.parse_qs(h["Link"].split(">")[0].lstrip("<?")) == {
        "filter": ["point or sensor"], "limit": ["10"], "after": [h["X-Baler-After"]]}
    whole = ha.dispatch(big_backend, "read", oh.Grid.from_rows([{"filter": "point", "limit": oh.Number(N)}]))
    assert ha.paging_headers("read", req, whole) == {}


class _InProcess:
    """openhay.Client's `call`, answered by Baler's own dispatch (the
    native client can't add the gateway's secret header)."""

    def __init__(self, backend, max_page=None):
        self.backend, self.max_page = backend, max_page

    def call(self, op, req=None):
        if self.max_page is not None and req is not None:  # a server that caps the page size
            row = dict(req.rows[0])
            asked = int(row["limit"].val) if "limit" in row else self.max_page
            row["limit"] = oh.Number(min(asked, self.max_page))
            req = oh.Grid.from_rows([row])
        return ha.dispatch(self.backend, op, req if req is not None else oh.Grid())


@needs_openhay
def test_openhays_native_read_follows_after(gateway):
    """Baler's openhay pin pages natively (openhay #9): its own Client.read
    comes back whole from Baler, and read_page hands back the cursor."""
    assert openhay_paging.NATIVE_PAGING
    c = oh.Client(f"{gateway}/api/demo")
    recs = c.read("point")
    assert len(recs) == N and len({r["id"].id for r in recs}) == N
    assert len(c.read("point", 1500)) == 1500  # past one page, stops at limit
    page = c.read_page("point", 1000)
    assert len(page.rows) == 1000 and page.meta["after"].id == page.rows[-1]["id"].id


@needs_openhay
def test_protocol_level_read_follows_after_with_any_openhay(big_backend, monkeypatch):
    """The module-level read/read_page page through `call("read")` alone,
    so they hold for an openhay that predates native paging too."""
    monkeypatch.setattr(ha, "READ_LIMIT", 1000)
    c = _InProcess(big_backend)
    assert len(c.call("read", oh.Grid.from_rows([{"filter": "point"}])).rows) == 1000  # one request: one page
    recs = openhay_paging.read(c, "point")
    assert len(recs) == N and recs.complete and recs.pages == 3
    assert all(isinstance(r["id"], oh.Ref) for r in recs)
    page = openhay_paging.read_page(c, "point", 500)
    assert len(page) == 500 and page.after == page[-1]["id"].id and page.limit == 500
    nxt = openhay_paging.read_page(c, "point", 500, oh.Ref(page.after))
    assert nxt[0]["id"].id > page.after
    assert len(openhay_paging.read(c, "point", limit=7)) == 7
    # Against a server whose pages are smaller than the limit asked for, it
    # reads on until it has them, as openhay's own read does, and `after`
    # continues from there.
    capped = _InProcess(big_backend, max_page=400)
    some = openhay_paging.read(capped, "point", limit=1000)
    assert len(some) == 1000 and some.pages == 3 and some.after == some[-1]["id"].id
    assert [r["id"] for r in some] == [r["id"] for r in recs[:1000]]
    assert len(openhay_paging.read(capped, "point", limit=N + 10)) == N


@needs_openhay
@pytest.mark.parametrize("native", [True, False], ids=["native-paging", "own-loop"])
def test_openhay_paging_client_is_a_drop_in(gateway, monkeypatch, native):
    """openhay_paging.Client over HTTP returns the full set across pages and
    exposes `after` on one page, whether openhay pages (the pin) or the
    wrapper's own loop does (an older openhay)."""
    monkeypatch.setattr(openhay_paging, "NATIVE_PAGING", native)
    c = openhay_paging.Client(f"{gateway}/api/demo")
    recs = c.read("point")
    assert isinstance(recs, Records) and recs.after is None
    assert len(recs) == N and len({r["id"].id for r in recs}) == N
    assert len(c.read("point", 1500)) == 1500
    page = c.read_page("point", 3)
    assert isinstance(page, Records) and len(page) == 3 and page.after == page[-1]["id"].id
    assert page.limit == 3 and not page.complete
    seen, after = list(page), page.after
    while after is not None:
        nxt = c.read_page("point", 1000, after)
        assert nxt[0]["id"].id > after
        seen += nxt
        after = nxt.after
    assert len(seen) == N and len({r["id"].id for r in seen}) == N


@needs_openhay
def test_openhay_paging_client_hands_over_to_native_paging(monkeypatch):
    """Once Baler's openhay pin pages natively (Client.read_page exists),
    the wrapper delegates to it instead of running its own loop."""
    page = oh.Grid(meta={"after": oh.Ref("p9"), "incomplete": "limit of 1 reached", "limit": oh.Number(1)},
                   cols=[oh.Col("id")], rows=[{"id": oh.Ref("p9")}])
    monkeypatch.setattr(openhay_paging, "NATIVE_PAGING", True)
    monkeypatch.setattr(oh.Client, "read_page", lambda self, f, limit=None, after=None: page, raising=False)
    monkeypatch.setattr(oh.Client, "read", lambda self, f, limit=None: [{"id": oh.Ref("a")}, {"id": oh.Ref("b")}])
    monkeypatch.setattr(openhay_paging.Client, "call", lambda self, op, req=None: pytest.fail("own loop ran"))
    c = openhay_paging.Client("http://unused")
    assert [r["id"].id for r in c.read("point")] == ["a", "b"]
    got = c.read_page("point", 1)
    assert got.after == "p9" and got.limit == 1 and got.incomplete == "limit of 1 reached"


class _FakePhable:
    """phable.HaystackClient's `call`, over canned pages keyed by `after`."""

    def __init__(self, pages):
        self.pages, self.sent = pages, []

    def call(self, path, data):
        import phable

        row = data.rows[0]
        after = row.get("after")
        self.sent.append(after.val if isinstance(after, phable.Ref) else None)
        return self.pages[self.sent[-1]]

    def read_all(self, filter, limit=None):
        return self.pages[None]


def test_phable_read_all_follows_after_where_phables_stops():
    import phable

    from baler_fn import phable_read_all

    def grid(ids, after=None, extra=()):
        meta = {"ver": "3.0"} | ({"after": phable.Ref(after), "incomplete": "limit of 2 reached"} if after else {})
        return phable.Grid(meta=meta, cols=[phable.GridCol("id"), *map(phable.GridCol, extra)],
                           rows=[{"id": phable.Ref(i)} for i in ids])

    c = _FakePhable({None: grid(["a", "b"], "b"), "b": grid(["c", "d"], "d", ["dis"]), "d": grid(["e"])})
    assert len(c.read_all("point").rows) == 2  # phable's own: the first page
    g = phable_read_all(c, "point")
    assert [r["id"].val for r in g.rows] == ["a", "b", "c", "d", "e"] and c.sent == [None, "b", "d"]
    assert [col.name for col in g.cols] == ["id", "dis"] and "after" not in g.meta
    assert len(phable_read_all(c, "point", limit=2).rows) == 2
    with pytest.raises(RuntimeError, match="same `after`"):
        phable_read_all(_FakePhable({None: grid(["a"], "a"), "a": grid(["a"], "a")}), "point")

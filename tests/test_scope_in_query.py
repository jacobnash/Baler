"""Site scope applied inside the graph query (Baler#16): to the result
and to every record a path hop reaches, per org, with locations visible
when they contain something visible. Checked on rdflib and Oxigraph."""

from __future__ import annotations

import pytest
from rdflib import RDF, Literal, URIRef

from baler import hisquery
from baler.scope import Scope, scope_for
from baler.store import BRICK, HAYSTACK, Store

A_PT = "urn:point:fbf/a/ahu-1/sat"
A_EQ = "urn:equip:fbf/a/ahu-1"
B_EQ = "urn:equip:fbf/b/vault"
SITE_A = "urn:location:site-a"
SITE_B = "urn:location:site-b"


@pytest.fixture(scope="module")
def store():
    s = Store()
    g = s.graph
    g.add((URIRef(A_EQ), BRICK.hasPoint, URIRef(A_PT)))
    g.add((URIRef(A_PT), BRICK.isPointOf, URIRef(A_EQ)))
    g.add((URIRef(A_PT), HAYSTACK.hasTag, Literal("sensor")))
    # a raw ref from A's point to B's equip: following it must not reveal B's tags
    g.add((URIRef(A_PT), HAYSTACK.spaceRef, Literal(B_EQ)))
    g.add((URIRef(B_EQ), HAYSTACK.dis, Literal("vault")))
    g.add((URIRef(B_EQ), HAYSTACK.hasTag, Literal("equip")))
    for site, eq in ((SITE_A, A_EQ), (SITE_B, B_EQ)):
        g.add((URIRef(site), RDF.type, BRICK.Site))
        g.add((URIRef(site), BRICK.hasPart, URIRef(eq)))
    return s


def _match(store, flt, scope):
    q = hisquery.parse_query(flt).filter
    got = hisquery.match_entities(store, q, scope=scope)
    try:
        import pyoxigraph as ox
    except ImportError:
        return got
    oxs = ox.Store()
    oxs.load(store.graph.serialize(format="nt").encode(), ox.RdfFormat.N_TRIPLES)
    on_ox = sorted(r["s"].value for r in oxs.query(hisquery.compile_filter(q, hisquery.graph_vocab(store), scope)))
    assert on_ox == got, "Oxigraph disagrees with rdflib"
    return got


A_ONLY = Scope(("fbf/a",))


def test_path_hop_cannot_reach_out_of_scope_records(store):
    assert _match(store, 'spaceRef->dis == "vault"', Scope.everything()) == [A_PT]
    assert _match(store, 'spaceRef->dis == "vault"', A_ONLY) == []  # was [A_PT]: a leak
    assert _match(store, "spaceRef->equip", A_ONLY) == []


def test_result_scoped_in_query_and_locations_follow_contents(store):
    assert _match(store, "equip or point", A_ONLY) == [A_EQ, A_PT]
    assert _match(store, "site", A_ONLY) == [SITE_A]  # contains A's equip
    other_org = Scope(None, ("fbf/a",))  # an org with no prefixes of its own
    assert _match(store, "equip", other_org) == [B_EQ]
    assert _match(store, "site", other_org) == [SITE_B]  # site-a holds a claimed equip
    assert hisquery.visible_uris(store, [SITE_B, A_EQ, SITE_A], A_ONLY) == [A_EQ, SITE_A]


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        rows = self.rows

        class _C:
            def fetchall(self):
                return rows

        return _C()


def test_scope_is_per_org():
    rows = [("s1", "org-a", ["fbf/a"]), ("s2", "org-a", ["fbf/a2"]), ("s3", "org-b", [])]
    conn = _Conn(rows)
    assert scope_for(conn, {"org_id": "org-a"}) == Scope(("fbf/a", "fbf/a2"))
    assert scope_for(conn, {"org_id": "org-a", "site_ids": ["s2"]}) == Scope(("fbf/a2",))
    # org-b claimed nothing: it isn't blinded by org-a partitioning, just kept out of org-a's data
    assert scope_for(conn, {"org_id": "org-b"}) == Scope(None, ("fbf/a", "fbf/a2"))
    assert scope_for(conn, None) == Scope.nothing()
    assert scope_for(_Conn([("s1", "org-a", [])]), {"org_id": "org-a"}).unrestricted

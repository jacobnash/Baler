"""Baler's filter compiler against haxall's own FilterTest cases.

The record sets and expected matches come from openhay.vectors (generated
by running haxall's FilterTest through the reference runtime). Records are
stored exactly the way a Haystack import stores them
(haystack_import._row -> ingest_haystack_equip_tags), then matched by
hisquery's filter -> SPARQL compiler: the same path /api read and
/ingest/his use. So a failure here is a real difference between what a
Haystack client asks Baler and what Haxall would answer.

Differences we know about are in KNOWN_GAPS as strict xfails: fixing one
forces removing it from the list.
"""

from __future__ import annotations

import pytest

oh = pytest.importorskip("openhay")
vectors = pytest.importorskip("openhay.vectors")

from baler import hisquery
from baler.haystack_import import _row
from baler.ingest import haystack_ref_to_equip_uri, ingest_haystack_equip_tags
from baler.store import Store

V = vectors.load("haxall/testHaystack-4.0.6")

# case id -> why Baler differs from the reference today
KNOWN_GAPS: dict[str, str] = {}


def _store_for(records):
    store = Store()
    by_uri = {}
    for hay in records:
        rec = oh.from_hayson(hay)
        uri = ingest_haystack_equip_tags(store, rec["id"].id, _row(rec))
        by_uri[str(uri)] = rec["dis"]
    return store, by_uri


def _cases():
    for s in V["filter_eval"]:
        for c in s["cases"]:
            key = f"{s['src']}:{c['filter']}"
            yield pytest.param(s["records"], c, key, id=key)


_STORES: dict[int, tuple] = {}


@pytest.mark.parametrize("records, case, key", list(_cases()))
def test_filter_matches_reference(records, case, key, request):
    why = KNOWN_GAPS.get(key)  # not callspec.id: pytest escapes non-ASCII (°) there
    if why:
        request.applymarker(pytest.mark.xfail(reason=why, strict=True))
    key = id(records)
    if key not in _STORES:
        _STORES[key] = _store_for(records)
    store, by_uri = _STORES[key]
    q = hisquery.parse_query(case["filter"])
    got = sorted(by_uri[u] for u in hisquery.match_entities(store, q.filter) if u in by_uri)
    assert got == sorted(case["expected"])
    # Production runs Oxigraph, not rdflib: same answer there too.
    oxs = _oxigraph(store)
    if oxs is not None:
        sparql = hisquery.compile_filter(q.filter, hisquery.graph_vocab(store))
        on_ox = sorted(by_uri[r["s"].value] for r in oxs.query(sparql) if r["s"].value in by_uri)
        assert on_ox == got, "Oxigraph disagrees with rdflib"


_OX: dict[int, object] = {}


def _oxigraph(store):
    try:
        import pyoxigraph as ox
    except ImportError:
        return None
    if id(store) not in _OX:
        oxs = ox.Store()
        oxs.load(store.graph.serialize(format="nt").encode(), ox.RdfFormat.N_TRIPLES)
        _OX[id(store)] = oxs
    return _OX[id(store)]


def test_known_gaps_name_real_cases():
    ids = {f"{s['src']}:{c['filter']}" for s in V["filter_eval"] for c in s["cases"]}
    assert set(KNOWN_GAPS) <= ids, set(KNOWN_GAPS) - ids


def test_records_land_as_haystack_equip():
    _store, by_uri = _store_for(V["filter_eval"][0]["records"])
    assert str(haystack_ref_to_equip_uri("a")) in by_uri

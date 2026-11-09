"""
baler.migrate's Timberdoodle -> Baler graph rewrite, run against rdflib
(in-memory Store for the default graph, a Dataset for the named-graph
move) - the same SPARQL that runs against Oxigraph in the compose
`migrate` service.
"""

from rdflib import BNode, Dataset, Literal, Namespace, URIRef

from baler import migrate
from baler.store import BRICK, Store

OLD_TD = Namespace("urn:timberdoodle:td#")
OLD_HS = Namespace("urn:timberdoodle:haystack#")
OLD_PROJ = Namespace("urn:timberdoodle:proj#")
NEW_TD = Namespace("urn:baler:td#")
NEW_HS = Namespace("urn:baler:haystack#")
NEW_PROJ = Namespace("urn:baler:proj#")
RDF_TYPE = URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")
P = URIRef("urn:point:fbf/ahu-1/analogInput,1")


def _seed(store):
    b = BNode()
    store.add_many([
        (P, OLD_HS.hasTag, Literal("temp")),              # old predicate
        (P, RDF_TYPE, OLD_TD.RawPoint),                    # old object
        (OLD_PROJ.Fancy_Sensor, RDF_TYPE, BRICK.Class),    # old subject
        (b, OLD_HS.dis, Literal("urn:timberdoodle:not-an-iri")),  # bnode subject + literal that only looks like one
        (P, BRICK.isPointOf, URIRef("urn:equip:fbf/ahu-1")),     # untouched
    ])
    return b


def test_rewrites_every_position_and_leaves_ids_and_literals():
    store = Store()
    b = _seed(store)
    result = migrate.migrate_graph(store)
    assert result["rewritten"] == 4
    g = store.graph
    assert (P, NEW_HS.hasTag, Literal("temp")) in g
    assert (P, RDF_TYPE, NEW_TD.RawPoint) in g
    assert (NEW_PROJ.Fancy_Sensor, RDF_TYPE, BRICK.Class) in g
    # bnode subject kept (not dropped by an erroring BIND), literal kept verbatim
    assert (b, NEW_HS.dis, Literal("urn:timberdoodle:not-an-iri")) in g
    assert (P, BRICK.isPointOf, URIRef("urn:equip:fbf/ahu-1")) in g
    assert not any(str(t).startswith("urn:timberdoodle:") for triple in g for t in triple
                   if isinstance(t, URIRef))
    assert len(g) == 5  # nothing lost, nothing duplicated


def test_idempotent():
    store = Store()
    _seed(store)
    migrate.migrate_graph(store)
    before = set(store.graph)
    assert migrate.migrate_graph(store)["rewritten"] == 0
    assert set(store.graph) == before


class DatasetStore:
    """Named-graph-aware stand-in for Oxigraph."""

    def __init__(self):
        self.ds = Dataset()

    def query(self, sparql):
        return self.ds.query(sparql)

    def update(self, sparql):
        self.ds.update(sparql)


def test_ontology_graph_moved_when_new_is_empty():
    s = DatasetStore()
    s.ds.graph(URIRef(migrate.OLD_ONTOLOGY_GRAPH)).add((BRICK.AHU, RDF_TYPE, BRICK.Class))
    assert migrate.migrate_graph(s)["ontology"] == "moved"
    assert (BRICK.AHU, RDF_TYPE, BRICK.Class) in s.ds.graph(URIRef(migrate.NEW_ONTOLOGY_GRAPH))
    assert len(s.ds.graph(URIRef(migrate.OLD_ONTOLOGY_GRAPH))) == 0
    assert migrate.migrate_graph(s)["ontology"] == "absent"


def test_ontology_graph_never_merged_into_existing_new_graph():
    s = DatasetStore()
    s.ds.graph(URIRef(migrate.OLD_ONTOLOGY_GRAPH)).add((BRICK.AHU, RDF_TYPE, BRICK.Class))
    s.ds.graph(URIRef(migrate.NEW_ONTOLOGY_GRAPH)).add((BRICK.VAV, RDF_TYPE, BRICK.Class))
    assert migrate.migrate_graph(s)["ontology"] == "kept-both"
    assert len(s.ds.graph(URIRef(migrate.OLD_ONTOLOGY_GRAPH))) == 1
    assert len(s.ds.graph(URIRef(migrate.NEW_ONTOLOGY_GRAPH))) == 1


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        return None


def test_wait_for_postgres_retries_refused_connections(monkeypatch):
    # pg_isready can pass during the image's first-start init, which then
    # restarts the server: the first connections are refused.
    import psycopg

    monkeypatch.setattr(migrate.time, "sleep", lambda s: None)
    calls = []

    def connect(dsn, **kw):
        calls.append(kw)
        if len(calls) < 3:
            raise psycopg.OperationalError("connection refused")
        return _Conn()

    migrate._wait_for_postgres("postgresql://x", connect=connect)
    assert len(calls) == 3
    assert calls[0]["connect_timeout"] == 5


def test_wait_for_postgres_gives_up_after_attempts(monkeypatch):
    import psycopg
    import pytest

    monkeypatch.setattr(migrate.time, "sleep", lambda s: None)

    def connect(dsn, **kw):
        raise psycopg.OperationalError("connection refused")

    with pytest.raises(psycopg.OperationalError):
        migrate._wait_for_postgres("postgresql://x", attempts=3, connect=connect)

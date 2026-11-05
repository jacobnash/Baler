"""
Same scenario as test_store.py, but against a live Oxigraph instance -
proves RemoteStore is a real drop-in replacement, not just an API-shape
match. Requires `docker-compose up -d` (oxigraph on localhost:7878).
"""

import pytest
from rdflib import Namespace

from baler.remote_store import RemoteStore
from baler.store import BRICK

BLDG = Namespace("urn:test-remote-store#")


@pytest.fixture
def store():
    """Scoped to this test's own namespace, not a blanket wipe - the
    previous version (`DELETE WHERE { ?s ?p ?o }`) nuked the *entire*
    default graph on every run, including live demo data from other
    processes sharing this same Oxigraph instance."""
    s = RemoteStore()
    # the "DELETE WHERE { ... }" shortcut form rejects FILTER (SPARQL 1.1
    # restricts it to a bare triple pattern) - need the full DELETE/WHERE form.
    s._update(f'DELETE {{ ?s ?p ?o }} WHERE {{ ?s ?p ?o . FILTER(STRSTARTS(STR(?s), "{BLDG}")) }}')
    return s


@pytest.mark.integration
def test_load_brick_and_query_feeds(store):
    store.load_ontology("tests/fixtures/brick_subset.ttl")
    assert len(store) > 200

    store.add_entity(BLDG.chiller_1, BRICK.Chiller)
    store.add_entity(BLDG.ahu_1, BRICK.AHU)
    store.add_relationship(BLDG.chiller_1, BRICK.feeds, BLDG.ahu_1)

    rows = store.query("""
        PREFIX brick: <https://brickschema.org/schema/Brick#>
        PREFIX bldg: <urn:test-remote-store#>
        SELECT ?fed WHERE { bldg:chiller_1 brick:feeds ?fed }
    """)
    assert len(rows) == 1
    assert rows[0].fed == str(BLDG.ahu_1)


@pytest.mark.integration
def test_remove_relationship_deletes_exactly_that_triple(store):
    store.add_entity(BLDG.ahu_2, BRICK.AHU)
    store.add_relationship(BLDG.ahu_2, BRICK.feeds, BLDG.vav_1)
    store.add_relationship(BLDG.ahu_2, BRICK.feeds, BLDG.vav_2)

    store.remove_relationship(BLDG.ahu_2, BRICK.feeds, BLDG.vav_1)

    rows = {r.fed for r in store.query("""
        PREFIX brick: <https://brickschema.org/schema/Brick#>
        PREFIX bldg: <urn:test-remote-store#>
        SELECT ?fed WHERE { bldg:ahu_2 brick:feeds ?fed }
    """)}
    assert rows == {str(BLDG.vav_2)}


@pytest.mark.integration
def test_derived_from_chain_is_walkable(store):
    store.add_entity(BLDG.point_a, BRICK.Point)
    store.add_entity(BLDG.point_b, BRICK.Point)
    store.add_entity(BLDG.point_c, BRICK.Point)
    store.add_derived_from(BLDG.point_b, BLDG.point_a)
    store.add_derived_from(BLDG.point_c, BLDG.point_b)

    rows = store.query("""
        PREFIX prov: <http://www.w3.org/ns/prov#>
        PREFIX bldg: <urn:test-remote-store#>
        SELECT ?ancestor WHERE { bldg:point_c prov:wasDerivedFrom+ ?ancestor }
    """)
    ancestors = {r.ancestor for r in rows}
    assert ancestors == {str(BLDG.point_a), str(BLDG.point_b)}

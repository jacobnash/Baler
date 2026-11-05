from rdflib import RDF, Namespace

from baler.store import BRICK, Store

BLDG = Namespace("urn:mybuilding#")


def test_load_brick_and_query_feeds():
    store = Store()
    store.load_ontology("tests/fixtures/brick_subset.ttl")
    assert len(store) > 200  # real excerpt of Brick, not a synthetic stub

    store.add_entity(BLDG.chiller_1, BRICK.Chiller)
    store.add_entity(BLDG.ahu_1, BRICK.AHU)
    store.add_relationship(BLDG.chiller_1, BRICK.feeds, BLDG.ahu_1)

    rows = list(store.query("""
        PREFIX brick: <https://brickschema.org/schema/Brick#>
        PREFIX bldg: <urn:mybuilding#>
        SELECT ?fed WHERE { bldg:chiller_1 brick:feeds ?fed }
    """))
    assert len(rows) == 1
    assert str(rows[0].fed) == str(BLDG.ahu_1)


def test_remove_relationship_deletes_exactly_that_triple():
    store = Store()
    store.add_entity(BLDG.ahu_2, BRICK.AHU)
    store.add_relationship(BLDG.ahu_2, BRICK.feeds, BLDG.vav_1)
    store.add_relationship(BLDG.ahu_2, BRICK.feeds, BLDG.vav_2)

    store.remove_relationship(BLDG.ahu_2, BRICK.feeds, BLDG.vav_1)

    rows = {str(r.fed) for r in store.query("""
        PREFIX brick: <https://brickschema.org/schema/Brick#>
        PREFIX bldg: <urn:mybuilding#>
        SELECT ?fed WHERE { bldg:ahu_2 brick:feeds ?fed }
    """)}
    assert rows == {str(BLDG.vav_2)}
    # the entity's own rdf:type triple is untouched - only the targeted triple was removed
    assert (BLDG.ahu_2, RDF.type, BRICK.AHU) in store.graph


def test_remove_relationship_on_nonexistent_triple_is_a_noop():
    store = Store()
    store.remove_relationship(BLDG.ahu_3, BRICK.feeds, BLDG.vav_9)  # must not raise
    assert len(store) == 0


def test_derived_from_chain_is_walkable():
    store = Store()
    store.add_entity(BLDG.point_a, BRICK.Point)
    store.add_entity(BLDG.point_b, BRICK.Point)
    store.add_entity(BLDG.point_c, BRICK.Point)
    store.add_derived_from(BLDG.point_b, BLDG.point_a)  # b computed from a
    store.add_derived_from(BLDG.point_c, BLDG.point_b)  # c computed from b

    # walk the transitive dependency chain in one query - this is the
    # "novel" capability from having a real graph underneath
    rows = list(store.query("""
        PREFIX prov: <http://www.w3.org/ns/prov#>
        PREFIX bldg: <urn:mybuilding#>
        SELECT ?ancestor WHERE { bldg:point_c prov:wasDerivedFrom+ ?ancestor }
    """))
    ancestors = {str(r.ancestor) for r in rows}
    assert ancestors == {str(BLDG.point_a), str(BLDG.point_b)}

"""
Pure in-memory rdflib + pyshacl - no live Postgres/Oxigraph needed, so
this runs in the fast default suite (no @pytest.mark.integration).
"""

from rdflib import RDF, Graph, Namespace

from baler.ingest import ingest_tags
from baler.mapping import classify_point, load_rules
from baler.shacl_validate import load_shapes, validate_graph
from baler.store import Store

EX = Namespace("urn:test-shacl#")

_CUSTOM_SHAPES_TTL = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <urn:test-shacl#> .
ex:WidgetShape a sh:NodeShape ;
    sh:targetClass ex:Widget ;
    sh:property [
        sh:path ex:hasSerial ;
        sh:minCount 1 ;
    ] .
"""


def _custom_shapes() -> Graph:
    graph = Graph()
    graph.parse(data=_CUSTOM_SHAPES_TTL, format="turtle")
    return graph


def test_load_shapes_parses_the_real_vendored_brick_ontology():
    shapes = load_shapes()
    assert len(shapes) > 0


def test_validate_graph_catches_a_deliberate_violation():
    """Proves the mechanism actually reports a violation, not just that it
    runs without crashing - a synthetic shapes graph with an easy
    sh:minCount constraint, independent of whether any particular real
    Brick class happens to be this strict for the classes this project's
    rules actually assign (see the next test for that real-shapes check)."""
    data = Graph()
    data.add((EX.widget1, RDF.type, EX.Widget))  # missing the required ex:hasSerial

    result = validate_graph(data, shapes_graph=_custom_shapes())

    assert result["conforms"] is False
    assert len(result["violations"]) == 1
    assert result["violations"][0]["focus_node"] == str(EX.widget1)


def test_validate_graph_conforms_when_the_constraint_is_satisfied():
    data = Graph()
    data.add((EX.widget1, RDF.type, EX.Widget))
    data.add((EX.widget1, EX.hasSerial, EX.some_serial))

    result = validate_graph(data, shapes_graph=_custom_shapes())

    assert result["conforms"] is True
    assert result["violations"] == []


def test_real_classification_rules_produce_structurally_valid_brick_instances():
    """The real regression value: build a graph via mapping.classify_point
    (the actual rule engine, same idiom test_mapping.py uses) for every
    rule in rules/haystack_to_brick.yaml, then validate the result against
    the real vendored Brick shapes - checking, via Brick's own independent
    authority rather than manual review, whether the hand-curated
    classification rules produce output Brick itself considers valid. If
    this ever goes red, that's a genuine finding about the rules, not a
    reason to weaken the test."""
    store = Store()
    rules = load_rules()
    for rule in rules:
        point_uri = ingest_tags(store, f"shacl-test:{rule['brick_class']}", {t: True for t in rule["tags"]})
        classify_point(store, point_uri, rules=rules)

    result = validate_graph(store.graph)

    assert result["conforms"] is True, result["violations"]

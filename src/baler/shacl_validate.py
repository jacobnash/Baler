"""
Structural validation of the entity graph against Brick's own SHACL
shapes (embedded in ontology/Brick-only.ttl) - a real, independent check
that the hand-curated rules/haystack_to_brick.yaml classification
actually produces structurally valid Brick instances, not just "the
class name exists" (which is all test_mapping.py checks today). See
todo/shacl-validation-and-service.md for the full design rationale.
"""

import os

from pyshacl import validate as pyshacl_validate
from rdflib import RDF, Graph, Namespace

DEFAULT_SHAPES_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "ontology", "Brick-only.ttl")
SH = Namespace("http://www.w3.org/ns/shacl#")

_shapes_cache: Graph | None = None


def load_shapes(path: str = DEFAULT_SHAPES_PATH) -> Graph:
    """Parses the vendored ontology once per process and caches it - it's
    ~1.5MB of Turtle, re-parsing on every /validate call would make the
    endpoint needlessly slow for something that never changes at runtime."""
    global _shapes_cache
    if _shapes_cache is None or path != DEFAULT_SHAPES_PATH:
        graph = Graph()
        graph.parse(path, format="turtle")
        if path == DEFAULT_SHAPES_PATH:
            _shapes_cache = graph
        return graph
    return _shapes_cache


def graph_from_remote_store(store) -> Graph:
    """Bulk-fetches the live Oxigraph default graph as Turtle and parses
    it into an in-memory rdflib.Graph - pyshacl.validate() needs a graph
    it can walk locally, not a live SPARQL endpoint to query per-shape.
    `?default` is required on the GET: this deployment runs Oxigraph with
    --union-default-graph, which makes a bare GET /store try to serialize
    the whole multi-graph dataset (and fail, since Turtle can't represent
    named graphs) instead of just the actual default graph entity writes
    land in. RemoteStore.load_ontology targets its own named graph, not
    the default graph, so this fetch already excludes it - the data graph
    validated here is entity data only, never the ontology itself."""
    resp = store._session.get(f"{store.base_url}/store", params={"default": ""}, headers={"Accept": "text/turtle"})
    resp.raise_for_status()
    graph = Graph()
    graph.parse(data=resp.text, format="turtle")
    return graph


def validate_graph(data_graph: Graph, shapes_graph: Graph | None = None) -> dict:
    """Only Brick-typed nodes are worth reporting on - the graph also
    carries raw HAYSTACK:hasTag facts and other non-Brick data that
    Brick's shapes were never meant to constrain, so we scope the data
    graph passed to pyshacl to just the (subject, predicate, object)
    triples reachable from Brick-typed subjects... but pyshacl already
    handles "shapes with no matching targets are simply skipped" for us,
    so no pre-filtering is actually needed here - passing the whole graph
    is both simpler and correct."""
    conforms, results_graph, _results_text = pyshacl_validate(
        data_graph,
        shacl_graph=shapes_graph if shapes_graph is not None else load_shapes(),
        data_graph_format="turtle",
        shacl_graph_format="turtle",
    )

    violations = []
    for report in results_graph.subjects(RDF.type, SH.ValidationResult):
        violations.append({
            "focus_node": str(next(results_graph.objects(report, SH.focusNode), "")),
            "message": str(next(results_graph.objects(report, SH.resultMessage), "")),
            "severity": str(next(results_graph.objects(report, SH.resultSeverity), "")),
        })

    return {"conforms": bool(conforms), "violations": violations}

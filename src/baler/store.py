"""
Ontology-agnostic building data store. The graph doesn't know or care which
ontology (Brick, Haystack, DBO) it's loaded with - that's a load-time choice,
not a schema baked into this code. See docs/ontology-agnostic-storage.md.
"""

from rdflib import RDF, Graph, Namespace, URIRef

BRICK = Namespace("https://brickschema.org/schema/Brick#")

# Brick has no native "derived from" relationship - PROV-O (the W3C standard
# provenance ontology) does, and it's the correct vocabulary for this, not
# something to invent or misattribute to Brick.
PROV = Namespace("http://www.w3.org/ns/prov#")

# Baler's own vocabulary for system-level concepts that aren't part
# of any building ontology: raw ingested points before mapping, unmapped
# points, computation rules. Kept deliberately separate from PROJ (the
# per-deployment extension namespace used when a Haystack concept has no
# Brick equivalent) - TD is for Baler itself, PROJ is for whoever's
# building on it.
TD = Namespace("urn:baler:td#")

# Raw Haystack tag facts, as published by Windrow's haystack_bridge over the
# /tags topic-suffix convention - lossless storage of whatever tags a
# Haystack source actually sent, independent of whether mapping.py can
# classify them into Brick yet.
HAYSTACK = Namespace("urn:baler:haystack#")
# The unit of a Number-valued tag: <entity> HUNIT:<tag> "°F", next to the
# HAYSTACK:<tag> number itself (a literal can't carry a unit).
HUNIT = Namespace("urn:baler:haystack-unit#")

# Per-deployment extension namespace for Haystack concepts that have no
# Brick equivalent - mapping.py falls back to this (subClassOf the nearest
# Brick ancestor) rather than dropping the extra information on the floor.
PROJ = Namespace("urn:baler:proj#")


class Store:
    # A single rdflib Graph: no named graphs (RemoteStore/Oxigraph has them).
    supports_named_graphs = False

    def __init__(self):
        self.graph = Graph()

    def load_ontology(self, path: str, fmt: str = "turtle") -> None:
        """Load an ontology file as data. Brick today; swap the file to
        change vocabulary without touching any other code here."""
        self.graph.parse(path, format=fmt)

    def add_entity(self, uri: URIRef, rdf_type: URIRef) -> None:
        self.graph.add((uri, RDF.type, rdf_type))

    def add_relationship(self, subj: URIRef, predicate: URIRef, obj: URIRef) -> None:
        self.graph.add((subj, predicate, obj))

    def remove_relationship(self, subj: URIRef, predicate: URIRef, obj: URIRef) -> None:
        self.graph.remove((subj, predicate, obj))

    def add_many(self, triples) -> None:
        """Same effect as calling add_relationship per triple, in one call -
        no network cost here to batch, but RemoteStore's version merges
        these into a single HTTP round trip, and callers should be able to
        write against either Store without caring which."""
        for subj, predicate, obj in triples:
            self.graph.add((subj, predicate, obj))

    def add_derived_from(self, computed_point: URIRef, source_point: URIRef) -> None:
        """A synthetic point or computed history is still just a Point -
        this is the one relationship that distinguishes it, enabling
        lineage tracing and dependency ordering via SPARQL property paths
        (e.g. `?p prov:wasDerivedFrom+ ?ancestor`) with no extra machinery."""
        self.graph.add((computed_point, PROV.wasDerivedFrom, source_point))

    def query(self, sparql: str):
        return self.graph.query(sparql)

    def update(self, sparql: str) -> None:
        self.graph.update(sparql)

    def __len__(self) -> int:
        return len(self.graph)

"""
Same method surface as store.Store, backed by a running `oxigraph serve`
instance over HTTP+SPARQL instead of in-memory rdflib. store.Store stays
untouched for fast unit tests; this is what the live daemon points at.

Verified against a real running Oxigraph instance: single-triple adds via
SPARQL 1.1 Update (`/update`, INSERT DATA) land in the true default graph;
queries via `/query` with a JSON results Accept header. Ontology files are
streamed straight to Oxigraph's bulk graph-store endpoint, into their own
named graph via PUT (replace), instead of being parsed with rdflib and
re-sent as INSERT DATA into the default graph - ontologies like Brick are
full of blank nodes from OWL restrictions, which INSERT DATA rejects, and
whose fresh identity on every parse would make a POST/merge reload
duplicate forever instead of converging (see load_ontology below).
"""

import os
import re

import requests
from rdflib.term import Literal, URIRef


def _to_sparql_term(value) -> str:
    """Correct escaping for arbitrary literals (quotes, newlines, language
    tags, datatypes) matters here - Brick's ontology has plenty of them in
    rdfs:label/skos:definition. Reuse rdflib's own N3 serializer instead of
    hand-rolling escaping logic."""
    if isinstance(value, (URIRef, Literal)):
        return value.n3()
    if isinstance(value, str):
        return Literal(value).n3()
    return f"<{value}>"


_XSD = "http://www.w3.org/2001/XMLSchema#"
_ESCAPES = {"t": "\t", "b": "\b", "n": "\n", "r": "\r", "f": "\f", '"': '"', "'": "'", "\\": "\\"}
_ESCAPE_RE = re.compile(r"\\(?:u([0-9A-Fa-f]{4})|U([0-9A-Fa-f]{8})|(.))")


def _unescape(m: re.Match) -> str:
    code = m.group(1) or m.group(2)
    return chr(int(code, 16)) if code else _ESCAPES.get(m.group(3), m.group(3))


def parse_term(tok: str) -> tuple[str, str, str | None] | None:
    """One SPARQL TSV field -> (value, type, datatype), exactly what the
    JSON results format says about the same term: type "uri", "literal" or
    "bnode", the lexical form as `value`, and a datatype only where JSON
    gives one (none for plain or language-tagged strings). None for an
    unbound variable. TSV writes terms in Turtle syntax, which abbreviates
    integers, decimals, doubles and booleans to bare tokens."""
    if not tok:
        return None
    c = tok[0]
    if c == "<":
        return tok[1:-1], "uri", None
    if c == '"':
        end = tok.rfind('"')  # a datatype IRI or language tag can't hold a quote
        lex = tok[1:end]
        if "\\" in lex:
            lex = _ESCAPE_RE.sub(_unescape, lex)
        rest = tok[end + 1:]
        if rest.startswith("^^<"):
            return lex, "literal", rest[3:-1]
        return lex, "literal", None  # plain, or @lang (JSON carries no datatype for either)
    if c == "_" and tok.startswith("_:"):
        return tok[2:], "bnode", None
    if tok in ("true", "false"):
        return tok, "literal", _XSD + "boolean"
    if "e" in tok or "E" in tok:
        return tok, "literal", _XSD + "double"
    if "." in tok:
        return tok, "literal", _XSD + "decimal"
    return tok, "literal", _XSD + "integer"


def typed_value(value: str, type_: str, datatype: str | None):
    """A term as SparqlRow hands it back: numbers as int/float, everything
    else its lexical form."""
    if type_ == "literal" and datatype:
        if datatype.endswith(("integer", "int")):
            return int(value)
        if datatype.endswith(("double", "float", "decimal")):
            return float(value)
    return value


class SparqlRow:
    """Attribute-style access over one SPARQL JSON result row, matching
    rdflib's `row.varname` style so callers don't care which Store they're
    talking to."""

    def __init__(self, bindings: dict):
        self._bindings = bindings

    def __getattr__(self, name: str):
        if name not in self._bindings:
            raise AttributeError(name)
        binding = self._bindings[name]
        value = binding["value"]
        if binding["type"] == "literal" and binding.get("datatype", "").endswith(("integer", "int")):
            return int(value)
        if binding["type"] == "literal" and binding.get("datatype", "").endswith(("double", "float", "decimal")):
            return float(value)
        return value


class RemoteStore:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or os.environ.get("OXIGRAPH_URL", "http://localhost:7878")).rstrip("/")
        # A bare requests.post() opens a fresh TCP connection every call -
        # a Session reuses one via HTTP keep-alive, cutting handshake cost
        # off every write/query this store ever does.
        self._session = requests.Session()

    def _update(self, sparql: str) -> None:
        resp = self._session.post(
            f"{self.base_url}/update",
            headers={"Content-Type": "application/sparql-update"},
            data=sparql.encode("utf-8"),
        )
        resp.raise_for_status()

    def update(self, sparql: str) -> None:
        """Run a SPARQL 1.1 Update (DELETE/INSERT ... WHERE, MOVE, ...)."""
        self._update(sparql)

    ONTOLOGY_GRAPH = "urn:baler:ontology"

    def load_ontology(self, path: str, fmt: str = "turtle") -> None:
        """Stream the file straight to Oxigraph's bulk graph-store endpoint
        rather than parsing with rdflib and re-serializing as batched INSERT
        DATA - ontologies like Brick are full of blank nodes (OWL
        restrictions), and SPARQL's INSERT DATA form rejects them; splitting
        across batches would also risk breaking blank node identity across
        requests. Letting Oxigraph parse the file directly sidesteps both.

        Targets a dedicated named graph (not the true default graph the rest
        of this class writes entities to) via PUT, not POST: PUT replaces a
        graph's contents outright, POST merges into it. That distinction is
        the whole point here - blank nodes get fresh identities on every
        parse, so a POST/merge reload can never dedupe against a previous
        load and just keeps appending the same ontology forever. PUT makes
        repeat calls idempotent instead. Queries still see this graph merged
        with entity data automatically (docker-compose runs Oxigraph with
        --union-default-graph); graph_from_remote_store's default-graph-only
        fetch stays correctly scoped to just entity data either way."""
        content_type = {"turtle": "text/turtle", "xml": "application/rdf+xml", "n3": "text/n3"}.get(fmt, "text/turtle")
        with open(path, "rb") as f:
            resp = self._session.put(
                f"{self.base_url}/store",
                params={"graph": self.ONTOLOGY_GRAPH},
                headers={"Content-Type": content_type},
                data=f,
            )
        resp.raise_for_status()

    def add_entity(self, uri: URIRef, rdf_type: URIRef) -> None:
        self._update(f"INSERT DATA {{ {_to_sparql_term(uri)} a {_to_sparql_term(rdf_type)} . }}")

    def add_relationship(self, subj: URIRef, predicate: URIRef, obj: URIRef) -> None:
        self._update(
            f"INSERT DATA {{ {_to_sparql_term(subj)} {_to_sparql_term(predicate)} {_to_sparql_term(obj)} . }}"
        )

    def remove_relationship(self, subj: URIRef, predicate: URIRef, obj: URIRef) -> None:
        self._update(
            f"DELETE DATA {{ {_to_sparql_term(subj)} {_to_sparql_term(predicate)} {_to_sparql_term(obj)} . }}"
        )

    def add_many(self, triples) -> None:
        """Every triple in one INSERT DATA block - one HTTP round trip
        instead of one per triple. Standard multi-triple SPARQL, not a
        multi-operation request - no need to verify Oxigraph accepts
        chained update operations, this is just several triple patterns
        inside a single INSERT DATA {}."""
        triples = list(triples)
        if not triples:
            return
        body = " ".join(
            f"{_to_sparql_term(subj)} {_to_sparql_term(predicate)} {_to_sparql_term(obj)} ."
            for subj, predicate, obj in triples
        )
        self._update(f"INSERT DATA {{ {body} }}")

    def add_derived_from(self, computed_point: URIRef, source_point: URIRef) -> None:
        from baler.store import PROV

        self.add_relationship(computed_point, PROV.wasDerivedFrom, source_point)

    def query(self, sparql: str):
        return [SparqlRow(b) for b in self.select(sparql)]

    def select(self, sparql: str) -> list[dict]:
        """query()'s rows as the raw SPARQL JSON bindings ({var: {"type",
        "value", "datatype"?}}), for callers reading thousands of rows: each
        SparqlRow attribute access is a Python call, and those were ~0.1 s
        of an 800-point /api/read (Baler#107). The bindings also carry each
        term's kind and datatype, which saves projecting them in SPARQL."""
        resp = self._session.post(
            f"{self.base_url}/query",
            headers={
                "Content-Type": "application/sparql-query",
                "Accept": "application/sparql-results+json",
            },
            data=sparql.encode("utf-8"),
        )
        resp.raise_for_status()
        return resp.json()["results"]["bindings"]

    def select_tsv(self, sparql: str) -> list[list[str]]:
        """A SELECT's rows as SPARQL TSV fields (Turtle-syntax terms, ""
        for unbound; decode one with parse_term), for the reads that pull
        every triple of thousands of records. Against JSON results that is
        about half the bytes and half of Oxigraph's own time (24.7 MB and
        0.95 s vs 13 MB and 0.50 s for 10k points' 136k triples, Baler#244),
        and a split per line instead of a dict per term to decode."""
        return list(self.iter_tsv(sparql))

    def iter_tsv(self, sparql: str):
        """select_tsv's rows as they arrive: Oxigraph streams a result while
        it evaluates it, so decoding row by row overlaps the caller's work
        with the server's instead of waiting for the last byte first."""
        resp = self._session.post(
            f"{self.base_url}/query",
            headers={"Content-Type": "application/sparql-query", "Accept": "text/tab-separated-values"},
            data=sparql.encode("utf-8"),
            stream=True,
        )
        try:
            resp.raise_for_status()
            rest = b""
            header = True
            for chunk in resp.iter_content(chunk_size=1 << 16):
                lines = (rest + chunk).split(b"\n")
                rest = lines.pop()
                if header and lines:  # the first line names the variables (?s ?p ?o)
                    lines = lines[1:]
                    header = False
                for line in lines:
                    if line:
                        yield line.decode("utf-8").split("\t")
            if rest and not header:
                yield rest.decode("utf-8").split("\t")
        finally:
            resp.close()

    def __len__(self) -> int:
        rows = self.query("SELECT (COUNT(*) as ?c) WHERE { ?s ?p ?o }")
        return rows[0].c

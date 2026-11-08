"""RemoteStore's SPARQL TSV reads (Baler#244): parse_term must say about
every term exactly what the JSON results format says, and iter_tsv must
reassemble rows split across network chunks."""

import pytest

from baler.remote_store import RemoteStore, parse_term, typed_value

XSD = "http://www.w3.org/2001/XMLSchema#"


@pytest.mark.parametrize("tok, want", [
    ("<urn:point:fbf/a/b>", ("urn:point:fbf/a/b", "uri", None)),
    ('"plain"', ("plain", "literal", None)),
    ('""', ("", "literal", None)),
    ('"x"@en-US', ("x", "literal", None)),
    ('"2026-01-01"^^<http://www.w3.org/2001/XMLSchema#date>', ("2026-01-01", "literal", XSD + "date")),
    ('"a\\tb\\nc\\"d\\\\e\\u00e9\\U0001F600"', ('a\tb\nc"d\\eé\U0001F600', "literal", None)),
    ('"q\\""', ('q"', "literal", None)),
    ("_:b12", ("b12", "bnode", None)),
    ("12", ("12", "literal", XSD + "integer")),
    ("-7", ("-7", "literal", XSD + "integer")),
    ("1.5", ("1.5", "literal", XSD + "decimal")),
    ("1.5E3", ("1.5E3", "literal", XSD + "double")),
    ("true", ("true", "literal", XSD + "boolean")),
    ("", None),
])
def test_parse_term(tok, want):
    assert parse_term(tok) == want


def test_typed_value_matches_sparqlrow():
    assert typed_value("12", "literal", XSD + "integer") == 12
    assert typed_value("1500", "literal", XSD + "double") == 1500.0
    assert typed_value("true", "literal", XSD + "boolean") == "true"
    assert typed_value("12", "literal", None) == "12"
    assert typed_value("urn:x", "uri", None) == "urn:x"


class _Resp:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield from self.chunks

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, resp):
        self.resp = resp

    def post(self, *a, **kw):
        assert kw["headers"]["Accept"] == "text/tab-separated-values"
        return self.resp


@pytest.mark.parametrize("chunks", [
    [b'?s\t?o\n<urn:a>\t"x"\n<urn:b>\t12\n'],
    [b"?s\t", b'?o\n<urn:a>\t"', b'x"\n<urn:b', b">\t12"],  # split mid-header, mid-term, no final newline
    [b"?s\t?o\n", b'<urn:a>\t"x"\n', b"<urn:b>\t12\n", b""],
])
def test_iter_tsv_reassembles_rows_across_chunks(chunks):
    store = RemoteStore("http://unused")
    resp = _Resp(chunks)
    store._session = _Session(resp)
    assert store.select_tsv("SELECT ?s ?o WHERE { ?s ?p ?o }") == [["<urn:a>", '"x"'], ["<urn:b>", "12"]]
    assert resp.closed


def test_iter_tsv_header_only_is_empty():
    store = RemoteStore("http://unused")
    store._session = _Session(_Resp([b"?s\t?o\n"]))
    assert store.select_tsv("SELECT ?s ?o WHERE { ?s ?p ?o }") == []


@pytest.mark.integration
def test_tsv_and_json_agree_on_live_oxigraph():
    """Every kind of term, as Oxigraph actually writes it."""
    store = RemoteStore()
    q = ('SELECT ?o WHERE { VALUES ?o { 1 -7 1.5 -0.25 1.5e3 2E-3 true false "a\\tb\\nc\\"d\\\\e\\u00e9" "x"@en-US '
         '"2026-01-01"^^<http://www.w3.org/2001/XMLSchema#date> "abc"^^<http://www.w3.org/2001/XMLSchema#integer> '
         '"12"^^<http://www.w3.org/2001/XMLSchema#string> <urn:x:a%20b> "q\\"" "NaN"^^<http://www.w3.org/2001/XMLSchema#double> '
         '"1.0"^^<http://www.w3.org/2001/XMLSchema#float> } }')
    js = sorted(((b["o"]["value"], b["o"]["type"], b["o"].get("datatype")) for b in store.select(q)), key=repr)
    ts = sorted((parse_term(row[0]) for row in store.select_tsv(q)), key=repr)
    assert js == ts

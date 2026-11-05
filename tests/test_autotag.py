"""
Against a live Oxigraph (see test_remote_store.py's rationale for why -
proves this works against the real store, not just the in-memory one).
The injected fake llm_classify never touches the network - classify_point
with_fallback's LLM step is a plain injected callable, so nothing here
needs ANTHROPIC_API_KEY or the `anthropic` package installed.

The run()-level tests below are plain unit tests (no store, no
@pytest.mark.integration) - they monkeypatch find_unclassified/
classify_point_with_fallback/load_rules/read_tags/propose_rule to drive
run()'s own retry-loop and rule-proposal control flow directly. This is
the code AUDIT.md flagged as 34% covered (CC 16) despite the module's
other functions being well-tested - these characterize run() itself,
not the classification logic it delegates to.
"""

from dataclasses import dataclass

import pytest
import requests
from rdflib import Namespace, URIRef

from baler import autotag, llm_classifier, mapping
from baler.autotag import find_unclassified, run
from baler.ingest import ingest_tags
from baler.mapping import classify_point_with_fallback
from baler.remote_store import RemoteStore
from baler.store import BRICK

BLDG = Namespace("urn:test-autotag#")


@dataclass
class _FakeResult:
    brick_class: str
    confidence: float
    reasoning: str


@pytest.fixture
def store():
    s = RemoteStore()
    s._update('DELETE { ?s ?p ?o } WHERE { ?s ?p ?o . FILTER(STRSTARTS(STR(?s), "urn:point:test-autotag")) }')
    return s


@pytest.mark.integration
def test_find_unclassified_includes_a_fresh_miss_point(store):
    point_uri = ingest_tags(store, "test-autotag:mystery-1", {"someRandomTag": True})

    assert point_uri in find_unclassified(store)


@pytest.mark.integration
def test_find_unclassified_excludes_directly_classified_points(store):
    point_uri = ingest_tags(store, "test-autotag:direct-1", {"zone": True, "air": True, "temp": True, "sensor": True})
    classify_point_with_fallback(store, point_uri)  # direct rule match, no LLM needed

    assert point_uri not in find_unclassified(store)


@pytest.mark.integration
def test_classify_point_with_fallback_applies_llm_guess_above_threshold(store):
    point_uri = ingest_tags(store, "test-autotag:chiller-temp", {"someRandomTag": True})

    calls = []

    def fake_llm_classify(tags, label, description, existing_brick_classes):
        calls.append((tags, label))
        return _FakeResult(brick_class="Chiller", confidence=0.9, reasoning="looks like a chiller point")

    outcome, brick_class = classify_point_with_fallback(store, point_uri, llm_classify=fake_llm_classify)

    assert len(calls) == 1
    assert (outcome, brick_class) == ("llm", "Chiller")
    assert list(store.query(f"SELECT ?o WHERE {{ <{point_uri}> a <{BRICK.Chiller}> }}"))


@pytest.mark.integration
def test_classify_point_with_fallback_ignores_low_confidence_llm_guess(store):
    point_uri = ingest_tags(store, "test-autotag:low-confidence", {"someRandomTag": True})

    def fake_llm_classify(tags, label, description, existing_brick_classes):
        return _FakeResult(brick_class="Chiller", confidence=0.2, reasoning="not sure")

    outcome, brick_class = classify_point_with_fallback(store, point_uri, llm_classify=fake_llm_classify)

    assert (outcome, brick_class) == ("miss", None)
    assert not list(store.query(f"SELECT ?o WHERE {{ <{point_uri}> a <{BRICK.Chiller}> }}"))


def test_run_skips_point_after_max_retries_on_connection_error(monkeypatch):
    monkeypatch.setattr(autotag, "find_unclassified", lambda store: [URIRef("urn:point:test-retry-1")])

    calls = []

    def flaky_classify(store, uri, rules, llm_classify):
        calls.append(uri)
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(mapping, "classify_point_with_fallback", flaky_classify)
    monkeypatch.setattr(autotag, "_RETRY_DELAY_SECONDS", 0)

    run(store=object(), llm_classify=None, propose_rules=False)  # must not raise

    assert len(calls) == autotag._MAX_ATTEMPTS


def test_run_skips_point_after_max_retries_on_unparseable_llm_response(monkeypatch):
    """LLMClassificationError (AUDIT.md Theme D) is retried and skipped the
    same as a network error, not left to crash the whole sweep."""
    monkeypatch.setattr(autotag, "find_unclassified", lambda store: [URIRef("urn:point:test-retry-2")])

    calls = []

    def unparseable_classify(store, uri, rules, llm_classify):
        calls.append(uri)
        raise llm_classifier.LLMClassificationError("model returned no parsed output")

    monkeypatch.setattr(mapping, "classify_point_with_fallback", unparseable_classify)
    monkeypatch.setattr(autotag, "_RETRY_DELAY_SECONDS", 0)

    run(store=object(), llm_classify=None, propose_rules=False)  # must not raise

    assert len(calls) == autotag._MAX_ATTEMPTS


def test_run_proposes_rule_when_llm_outcome_matches_a_known_class(monkeypatch):
    point_uri = URIRef("urn:point:test-propose-1")
    monkeypatch.setattr(autotag, "find_unclassified", lambda store: [point_uri])
    monkeypatch.setattr(mapping, "load_rules", lambda path: [{"brick_class": "Chiller"}])
    monkeypatch.setattr(mapping, "classify_point_with_fallback", lambda store, uri, rules, llm_classify: ("llm", "Chiller"))
    monkeypatch.setattr(mapping, "read_tags", lambda store, uri: {"someRandomTag"})

    proposed = []
    monkeypatch.setattr(
        llm_classifier, "propose_rule", lambda rules_path, tags, brick_class: proposed.append((rules_path, tags, brick_class)) or True
    )

    run(store=object(), llm_classify=None, propose_rules=True)

    assert proposed == [(autotag.POINT_RULES_PATH, {"someRandomTag"}, "Chiller")]

"""
autotag: the rules, then model_step on what they leave (#182).

The find_unclassified and model-step tests run against a live Oxigraph
(see test_remote_store.py's rationale), with a fake model - nothing here
needs a key or the `anthropic` package. The run()-level tests are plain
unit tests that monkeypatch around the store to drive run()'s own control
flow: retries, and what goes to the model step.
"""

import pytest
import requests
from rdflib import RDF, Literal, Namespace, URIRef

from baler import autotag, llm_classifier, mapping, mapping_proposals, model_step
from baler.autotag import find_unclassified, run
from baler.ingest import ingest_tags
from baler.mapping import classify_point_with_fallback
from baler.remote_store import RemoteStore
from baler.store import BRICK, TD

BLDG = Namespace("urn:test-autotag#")


class _AgreeingModel:
    name = "fake"

    def __init__(self, brick_class):
        self.brick_class = brick_class
        self.prompts = []

    def ask(self, prompt):
        self.prompts.append(prompt)
        return {"candidates": [{"brick_class": self.brick_class, "reason": "fake"}], "confidence": 0.9}


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
def test_find_unclassified_includes_a_point_with_a_reading_but_no_markers(store):
    """#182: `AI-17` with only a unit and readings still reaches the model step."""
    point_uri = ingest_tags(store, "test-autotag:ai-17", {"unit": "°F"})
    store.add_many([(point_uri, RDF.type, TD.RawPoint)])

    assert point_uri in find_unclassified(store)


@pytest.mark.integration
def test_model_step_against_oxigraph_writes_an_agreed_class(store):
    topic = "test-autotag:chiller-1/CHW-SUP-TEM-7"
    point_uri = ingest_tags(store, topic, {"unit": "°F"})
    store.add_many([(point_uri, TD.sourceTopic, Literal(topic))])
    model = _AgreeingModel("Chilled_Water_Supply_Temperature_Sensor")

    [d] = model_step.run(store, [(point_uri, "point")], model)

    assert d.accepted == "Chilled_Water_Supply_Temperature_Sensor"
    assert "unit=°F" in model.prompts[0]
    assert list(store.query(f"SELECT ?o WHERE {{ <{point_uri}> a <{BRICK.Chilled_Water_Supply_Temperature_Sensor}> }}"))


@pytest.mark.integration
def test_model_step_against_oxigraph_never_writes_an_invented_class(store):
    point_uri = ingest_tags(store, "test-autotag:invented", {"unit": "°F"})

    [d] = model_step.run(store, [(point_uri, "point")], _AgreeingModel("Chilled_Water_Supply_Temp_Sensor"))

    assert d.accepted is None
    assert not list(store.query(f"SELECT ?o WHERE {{ <{point_uri}> a <{BRICK.Chilled_Water_Supply_Temp_Sensor}> }}"))


def test_run_skips_an_entity_after_max_retries_on_connection_error(monkeypatch):
    monkeypatch.setattr(autotag, "find_unclassified", lambda store: [URIRef("urn:point:test-retry-1")])
    calls = []

    def flaky(store, uri, rules):
        calls.append(uri)
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(mapping, "classify_point", flaky)
    monkeypatch.setattr(autotag, "_RETRY_DELAY_SECONDS", 0)
    sent = []
    monkeypatch.setattr(model_step, "run", lambda store, entities, model, proposals=None, stats=None, site_rules=None: sent.append(list(entities)) or [])

    run(store=object())  # must not raise

    assert len(calls) == autotag._MAX_ATTEMPTS
    assert sent == [[]]  # a failed entity isn't sent on to the model step


def test_run_sends_only_what_the_rules_left_to_the_model_step(monkeypatch):
    direct, miss, equip = URIRef("urn:point:a"), URIRef("urn:point:b"), URIRef("urn:equip:fbf/x-1")
    monkeypatch.setattr(autotag, "find_unclassified", lambda store: [direct, miss, equip])
    outcomes = {direct: ("direct", "Fan_Status"), miss: ("miss", None), equip: ("miss", None)}
    monkeypatch.setattr(mapping, "classify_point", lambda store, uri, rules: outcomes[uri])
    sent = []
    monkeypatch.setattr(model_step, "run", lambda store, entities, model, proposals=None, stats=None, site_rules=None: sent.append(list(entities)) or [])

    run(store=object())

    assert sent == [[(miss, "point"), (equip, "equip")]]


def test_a_model_ask_that_keeps_failing_is_treated_as_no_answer(monkeypatch):
    monkeypatch.setattr(autotag, "_RETRY_DELAY_SECONDS", 0)

    class Broken:
        name = "broken"

        def ask(self, prompt):
            raise llm_classifier.LLMClassificationError("garbled")

    assert autotag._RetryingModel(Broken()).ask("q") == {}


def test_proposals_are_rows_not_rules_file_edits():
    """#182: nothing writes into rules/*.yaml any more."""
    assert not hasattr(llm_classifier, "propose_rule")
    assert hasattr(mapping_proposals, "PgProposals")

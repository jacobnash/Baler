"""
Mapping memory (#185): confirmed examples, nearest neighbour, few-shot.
No model is ever called; the Postgres store is checked against a live
database.
"""

import uuid

import pytest
from rdflib import RDF, SKOS, Literal

from baler import (
    brick_vocab,
    mapping_eval,
    mapping_proposals,
    mapping_review,
    memory,
    model_step,
)
from baler.ingest import ingest_tags
from baler.store import BRICK, TD, Store

VOCAB = brick_vocab.load()


def ev(label, unit=None, markers=(), kind="point"):
    return {"label": label, "kind": kind, "markers": list(markers), "values": {"unit": unit} if unit else {}}


def ex(label, cls, unit=None, org=None, site=None, kind="point"):
    return memory.Example(f"urn:point:{label}", kind, cls, memory.tokens(ev(label, unit, kind=kind)), org, site, label)


def test_tokens_drop_numbers_and_let_abbreviations_meet_words():
    cryptic = set(memory.tokens(ev("fbf/chiller-2/CHW-SUP-TEM-17", "°F")))
    camel = set(memory.tokens(ev("fbf/chiller-1/ChwSupTemp", "°F")))
    assert not any(t[2:].isdigit() for t in cryptic)
    assert {"p:chw", "p:sup", "p:tem", "e:chiller", "u:°F"} <= cryptic & camel


def _mem(*examples):
    return memory.Memory(examples, VOCAB)


def test_agreeing_neighbours_are_taken():
    m = _mem(ex("fbf/chiller-1/CHW-SUP-TEM-7", "Chilled_Water_Supply_Temperature_Sensor", "°F"),
             ex("fbf/chiller-2/ChwSupTemp", "Chilled_Water_Supply_Temperature_Sensor", "°F"),
             ex("fbf/vav-1/ZoneAirTemp", "Zone_Air_Temperature_Sensor", "°F"))
    v = m.vote(ev("fbf/chiller-3/chw_sup_temp", "°F"), "point")
    assert v.accepted == "Chilled_Water_Supply_Temperature_Sensor"
    assert len(v.neighbours) >= 2 and all(n["brick_class"] == v.accepted for n in v.neighbours)


def test_disagreeing_neighbours_only_suggest():
    m = _mem(ex("fbf/ahu-1/SA-TEMP", "Supply_Air_Temperature_Sensor", "°F"),
             ex("fbf/ahu-2/SA-TEMP", "Mixed_Air_Temperature_Sensor", "°F"))
    v = m.vote(ev("fbf/ahu-3/SA-TEMP", "°F"), "point")
    assert v.accepted is None and v.suggestion in ("Supply_Air_Temperature_Sensor", "Mixed_Air_Temperature_Sensor")


def test_one_neighbour_is_not_enough():
    m = _mem(ex("fbf/chiller-1/CHW-SUP-TEM-7", "Chilled_Water_Supply_Temperature_Sensor", "°F"))
    v = m.vote(ev("fbf/chiller-3/CHW-SUP-TEM-9", "°F"), "point")
    assert v.accepted is None and v.suggestion == "Chilled_Water_Supply_Temperature_Sensor"


def test_dissimilar_examples_say_nothing():
    m = _mem(ex("fbf/chiller-1/CHW-SUP-TEM-7", "Chilled_Water_Supply_Temperature_Sensor", "°F"),
             ex("fbf/chiller-2/CHW-SUP-TEM-8", "Chilled_Water_Supply_Temperature_Sensor", "°F"))
    v = m.vote(ev("fbf/meter-1/KWH-TOT-4", "kWh"), "point")
    assert v.accepted is None and v.suggestion is None and v.neighbours == []


def test_examples_never_cross_organisations():
    m = _mem(ex("a/chiller-1/CHW-SUP-TEM-7", "Chilled_Water_Supply_Temperature_Sensor", "°F", org="org-a"),
             ex("a/chiller-2/CHW-SUP-TEM-8", "Chilled_Water_Supply_Temperature_Sensor", "°F", org="org-a"))
    assert m.vote(ev("b/chiller-1/CHW-SUP-TEM-7", "°F"), "point", org_id="org-b").neighbours == []
    assert m.vote(ev("b/chiller-1/CHW-SUP-TEM-7", "°F"), "point", org_id=None).neighbours == []
    assert m.vote(ev("a/chiller-3/CHW-SUP-TEM-9", "°F"), "point", org_id="org-a").accepted


def test_the_sites_own_examples_win_over_the_orgs():
    m = _mem(ex("s1/ahu-1/SAT", "Supply_Air_Temperature_Sensor", "°F", org="o", site="s1"),
             ex("s1/ahu-2/SAT", "Supply_Air_Temperature_Sensor", "°F", org="o", site="s1"),
             ex("s2/ahu-1/SAT", "Mixed_Air_Temperature_Sensor", "°F", org="o", site="s2"),
             ex("s2/ahu-2/SAT", "Mixed_Air_Temperature_Sensor", "°F", org="o", site="s2"),
             ex("s2/ahu-3/SAT", "Mixed_Air_Temperature_Sensor", "°F", org="o", site="s2"))
    v = m.vote(ev("s1/ahu-3/SAT", "°F"), "point", org_id="o", site_id="s1")
    assert v.accepted == "Supply_Air_Temperature_Sensor" and v.neighbours[0]["scope"] == "site"
    # a site with no examples of its own falls back to the org, which disagrees here
    assert m.vote(ev("s3/ahu-1/SAT", "°F"), "point", org_id="o", site_id="s3").accepted is None


# --- in the model step ------------------------------------------------------


def _point(store, topic, unit):
    uri = ingest_tags(store, topic, {"unit": unit})
    store.add_many([(uri, RDF.type, TD.RawPoint), (uri, TD.sourceTopic, Literal(topic))])
    return uri


class FakeModel:
    name = "fake"

    def __init__(self, *classes):
        self.classes = list(classes)
        self.prompts = []

    def ask(self, prompt):
        self.prompts.append(prompt)
        cls = self.classes[min(len(self.prompts), len(self.classes)) - 1]
        return {"candidates": [{"brick_class": cls, "reason": "r"}], "confidence": 0.9}


CHW = _mem(ex("fbf/chiller-1/CHW-SUP-TEM-7", "Chilled_Water_Supply_Temperature_Sensor", "°F"),
           ex("fbf/chiller-2/ChwSupTemp", "Chilled_Water_Supply_Temperature_Sensor", "°F"))


def test_agreeing_memory_classifies_without_a_model_call():
    store = Store()
    uri = _point(store, "fbf/chiller-3/chw_sup_temp", "°F")
    model = FakeModel("Zone_Air_Temperature_Sensor")
    [d] = model_step.run(store, [(uri, "point")], model, vocab=VOCAB, recall=memory.recall_for(CHW))
    assert d.accepted == "Chilled_Water_Supply_Temperature_Sensor" and d.source == "memory" and model.prompts == []
    assert any(str(o).startswith("memory-classified") for o in store.graph.objects(uri, SKOS.definition))
    assert d.asks[0]["model"] == "memory" and len(d.asks[0]["neighbours"]) == 2


def test_the_model_agreeing_with_memorys_suggestion_needs_no_re_ask():
    store = Store()
    uri = _point(store, "fbf/chiller-3/CHW-SUP-TEM-9", "°F")
    one = _mem(ex("fbf/chiller-1/CHW-SUP-TEM-7", "Chilled_Water_Supply_Temperature_Sensor", "°F"))  # suggests, can't accept alone
    model = FakeModel("Chilled_Water_Supply_Temperature_Sensor")
    [d] = model_step.run(store, [(uri, "point")], model, vocab=VOCAB, recall=memory.recall_for(one))
    assert d.accepted == "Chilled_Water_Supply_Temperature_Sensor" and d.source == "model+memory"
    assert len(model.prompts) == 1
    assert "Confirmed points from this organisation" in model.prompts[0] and "CHW-SUP-TEM-7" in model.prompts[0]


def test_the_model_disagreeing_with_memory_is_asked_again_as_before():
    store = Store()
    uri = _point(store, "fbf/chiller-3/CHW-SUP-TEM-9", "°F")
    one = _mem(ex("fbf/chiller-1/CHW-SUP-TEM-7", "Chilled_Water_Supply_Temperature_Sensor", "°F"))
    model = FakeModel("Chilled_Water_Return_Temperature_Sensor", "Chilled_Water_Supply_Temperature_Sensor")
    [d] = model_step.run(store, [(uri, "point")], model, vocab=VOCAB, recall=memory.recall_for(one))
    assert len(model.prompts) == 2 and d.accepted is None and "asked twice" in d.refused_because
    assert "Chilled_Water_Supply_Temperature_Sensor" in [c["brick_class"] for c in d.candidates]


def test_memory_respects_the_veto():
    store = Store()
    uri = _point(store, "fbf/chiller-3/chw_sup_temp", "%RH")
    # a (mistaken) memory that agrees confidently, for a point whose unit says humidity
    wrong = _mem(ex("fbf/chiller-1/CHW-SUP-TEM-7", "Chilled_Water_Supply_Temperature_Sensor", "%RH"),
                 ex("fbf/chiller-2/ChwSupTemp", "Chilled_Water_Supply_Temperature_Sensor", "%RH"))
    [d] = model_step.run(store, [(uri, "point")], None, vocab=VOCAB, recall=memory.recall_for(wrong))
    assert d.accepted is None and d.refused_because.startswith("vetoed")


def test_summary_counts_memory_classified_points():
    store = Store()
    uri = _point(store, "fbf/chiller-3/chw_sup_temp", "°F")
    store.add_entity(uri, BRICK.Chilled_Water_Supply_Temperature_Sensor)
    store.add_relationship(uri, SKOS.definition, Literal("memory-classified: x"))
    assert mapping_review.summary(store, [], set())["states"]["memory"] == 1


# --- the harness ------------------------------------------------------------


@pytest.mark.skipif(not mapping_eval.windrow_available(), reason="windrow submodule not checked out")
def test_memory_from_a_reviewed_building_on_mock_hospital():
    mem, n = mapping_eval.reviewed_memory(mapping_eval.load_mock_hospital(seed=42), VOCAB)
    assert n > 100
    s = mapping_eval.summarize(mapping_eval.evaluate(mapping_eval.load_mock_hospital(seed=7), vocab=VOCAB, memory=mem), VOCAB)
    assert s["points"]["steps"]["memory"]["points"] > 0
    assert s["points"]["auto_accepted_wrong"] == 0 and s["equipment"]["auto_accepted_wrong"] == 0


# --- Postgres -----------------------------------------------------------------


@pytest.mark.integration
def test_an_accepted_proposal_becomes_an_example_in_its_own_org():
    from baler import auth, timeseries

    conn = timeseries.connect()
    auth.ensure_schema(conn)
    memory.ensure_schema(conn)
    tag = uuid.uuid4().hex[:8]
    org, site = f"org-{tag}", f"site-{tag}"
    uri = f"urn:point:test-mem-{tag}/chiller-1/CHW-SUP-TEM-7"
    try:
        conn.execute("INSERT INTO orgs (id, name, created_at) VALUES (%s, %s, now())", (org, org))
        conn.execute("INSERT INTO sites (id, org_id, name, created_at, topic_prefixes) VALUES (%s, %s, %s, now(), %s)",
                     (site, org, site, [f"test-mem-{tag}"]))
        props = mapping_proposals.MemoryProposals()
        props.propose(entity_uri=uri, pattern="p", candidates=[{"brick_class": "Chilled_Water_Supply_Temperature_Sensor", "reason": "r"}],
                      refused_because="x", evidence={"label": uri[len("urn:point:"):], "values": {"unit": "°F"}}, asks=[])
        store = Store()
        _point(store, uri[len("urn:point:"):], "°F")
        mapping_review.decide(props, store, conn, [props.list()[0]["id"]], "accept", {"sub": "u", "email": "e"},
                              brick_class="Chilled_Water_Supply_Temperature_Sensor")
        mem = memory.load(conn)
        [e] = [e for e in mem.examples if e.entity_uri == uri]
        assert (e.org_id, e.site_id, e.brick_class) == (org, site, "Chilled_Water_Supply_Temperature_Sensor")
        assert memory.pg_locator(conn)(f"urn:point:test-mem-{tag}/chiller-2/x") == (org, site)
    finally:
        conn.execute("DELETE FROM mapping_examples WHERE entity_uri = %s", (uri,))
        conn.execute("DELETE FROM record_edits WHERE entity_uri = %s", (uri,))
        conn.execute("DELETE FROM sites WHERE id = %s", (site,))
        conn.execute("DELETE FROM orgs WHERE id = %s", (org,))
        conn.close()

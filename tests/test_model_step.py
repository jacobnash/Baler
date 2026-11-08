"""
The tagging ladder's model step (#182). No services and no real model:
every model here is a fake answering from a script.
"""

from rdflib import RDF, SKOS, Literal

from baler import brick_vocab, mapping_proposals, model_step
from baler.ingest import (
    ingest_equip_tags,
    ingest_tags,
    link_point_to_equip,
    topic_to_point_uri,
)
from baler.store import BRICK, TD, Store

VOCAB = brick_vocab.load()


class FakeModel:
    """Answers each ask from `script` (a list of replies, or one reply for
    every ask) and keeps the prompts."""

    name = "fake"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def ask(self, prompt: str) -> dict:
        self.prompts.append(prompt)
        return self.replies[min(len(self.prompts), len(self.replies)) - 1]


def reply(*classes, confidence=0.9):
    return {"candidates": [{"brick_class": c, "reason": f"because {c}"} for c in classes], "confidence": confidence}


def _point(store, topic, tags):
    uri = ingest_tags(store, topic, tags)
    store.add_many([(uri, RDF.type, TD.RawPoint), (uri, TD.sourceTopic, Literal(topic))])
    return uri


def _run(store, uris, model, proposals=None, stats=None):
    return model_step.run(store, [(u, "point") for u in uris], model, proposals=proposals, stats=stats, vocab=VOCAB)


def test_agreement_on_re_ask_auto_accepts_and_writes_the_class():
    store = Store()
    uri = _point(store, "fbf/chiller-1/CHW-SUP-TEM-7", {"unit": "°F", "writable": True})
    model = FakeModel(reply("Chilled_Water_Supply_Temperature_Sensor", "Water_Temperature_Sensor"))
    proposals = mapping_proposals.MemoryProposals()

    [d] = _run(store, [uri], model, proposals)

    assert d.accepted == "Chilled_Water_Supply_Temperature_Sensor"
    assert len(model.prompts) == 2 and model.prompts[0] != model.prompts[1]
    assert (uri, RDF.type, BRICK.Chilled_Water_Supply_Temperature_Sensor) in store.graph
    assert any("agreed on re-ask" in str(o) for o in store.graph.objects(uri, SKOS.definition))
    assert proposals.list() == []


def test_disagreement_becomes_a_proposal_with_both_answers():
    store = Store()
    uri = _point(store, "fbf/ahu-1/SAT", {"unit": "°F"})
    model = FakeModel(reply("Supply_Air_Temperature_Sensor"), reply("Mixed_Air_Temperature_Sensor"))
    proposals = mapping_proposals.MemoryProposals()

    [d] = _run(store, [uri], model, proposals)

    assert d.accepted is None and "asked twice" in d.refused_because
    assert not [o for o in store.graph.objects(uri, RDF.type) if str(o).startswith(str(BRICK))]
    [row] = proposals.list()
    assert row["entity_uri"] == str(uri)
    assert [c["brick_class"] for c in row["candidates"]][:2] == ["Supply_Air_Temperature_Sensor", "Mixed_Air_Temperature_Sensor"]
    assert len(row["asks"]) == 2 and row["evidence"]["values"]["unit"] == "°F"


def test_brick_equivalents_count_as_agreement():
    store = Store()
    uri = _point(store, "fbf/ahu-1/DAT", {"unit": "°F"})
    model = FakeModel(reply("Discharge_Air_Temperature_Sensor"), reply("Supply_Air_Temperature_Sensor"))

    [d] = _run(store, [uri], model)

    assert d.accepted == "Discharge_Air_Temperature_Sensor"


def test_a_class_brick_does_not_have_is_never_written():
    """#181, carried over: an invented name is dropped even when the model
    repeats it confidently."""
    store = Store()
    uri = _point(store, "fbf/chiller-1/CHW-SUP-TEM-7", {"unit": "°F"})
    model = FakeModel(reply("Chilled_Water_Supply_Temp_Sensor", confidence=0.99))
    proposals = mapping_proposals.MemoryProposals()

    [d] = _run(store, [uri], model, proposals)

    assert d.accepted is None
    assert "Chilled_Water_Supply_Temp_Sensor" in d.refused_because
    assert (uri, RDF.type, BRICK.Chilled_Water_Supply_Temp_Sensor) not in store.graph
    assert d.asks[0]["rejected"] == ["Chilled_Water_Supply_Temp_Sensor"]
    assert all(VOCAB.canonical_class(c["brick_class"]) == c["brick_class"] for c in proposals.list()[0]["candidates"])


def test_a_real_class_is_written_in_brick_s_spelling():
    store = Store()
    uri = _point(store, "fbf/chiller-1/CHW-SUP-TEM-7", {"unit": "°F"})

    [d] = _run(store, [uri], FakeModel(reply("chilled_water_supply_temperature_sensor")))

    assert d.accepted == "Chilled_Water_Supply_Temperature_Sensor"


def test_low_stated_confidence_blocks_auto_accept_even_on_agreement():
    store = Store()
    uri = _point(store, "fbf/ahu-1/SAT", {"unit": "°F"})

    [d] = _run(store, [uri], FakeModel(reply("Supply_Air_Temperature_Sensor", confidence=0.2)))

    assert d.accepted is None and "unsure" in d.refused_because


def test_unit_veto_blocks_an_agreed_class():
    store = Store()
    uri = _point(store, "fbf/vav-1/ZN-RH", {"unit": "°F"})

    [d] = _run(store, [uri], FakeModel(reply("Zone_Air_Humidity_Sensor")))

    assert d.accepted is None and d.refused_because.startswith("vetoed: unit °F")


def test_value_veto_blocks_a_status_class_for_analog_values():
    store = Store()
    uri = _point(store, "fbf/pump-1/PMP-SPD", {"unit": "%"})

    def stats(uris):
        return {str(uri): {"samples": 10, "min": 12.0, "max": 88.0, "boolean": False}}

    [d] = _run(store, [uri], FakeModel(reply("Pump_Status")), stats=stats)

    assert d.accepted is None and "analog" in d.refused_because


def test_one_question_per_naming_pattern():
    store = Store()
    uris = []
    for i in (1, 2, 3):
        uri = _point(store, f"fbf/chiller-{i}/CHW-SUP-TEM-{10 + i}", {"unit": "°F"})
        uris.append(uri)
    other = _point(store, "fbf/chiller-1/KW-INP-9", {"unit": "kW"})
    model = FakeModel(reply("Chilled_Water_Supply_Temperature_Sensor"), reply("Chilled_Water_Supply_Temperature_Sensor"),
                      reply("Electric_Power_Sensor"))

    decisions = _run(store, uris + [other], model)

    assert len(model.prompts) == 4  # two patterns, asked twice each
    assert "(3 in all)" in model.prompts[0] or "(3 in all)" in model.prompts[2]
    assert sorted(d.accepted for d in decisions if d.accepted) == ["Chilled_Water_Supply_Temperature_Sensor"] * 3 + ["Electric_Power_Sensor"]


def test_prompt_carries_the_evidence():
    store = Store()
    equip = ingest_equip_tags(store, "fbf/chiller-1", {})
    uri = _point(store, "fbf/chiller-1/CHW-SUP-TEM-7", {"unit": "°F", "dis": "Chilled water supply"})
    sib = _point(store, "fbf/chiller-1/KW-INP-9", {"unit": "kW"})
    store.add_entity(sib, BRICK.Electric_Power_Sensor)
    link_point_to_equip(store, uri, equip)
    link_point_to_equip(store, topic_to_point_uri("fbf/chiller-1/KW-INP-9"), equip)
    model = FakeModel(reply("Chilled_Water_Supply_Temperature_Sensor"))

    _run(store, [uri], model, stats=lambda uris: {str(uri): {"samples": 5, "min": 43.1, "max": 45.0, "boolean": False}})

    p = model.prompts[0]
    assert "unit=°F" in p and "dis=Chilled water supply" in p
    assert "KW-INP-9 (Electric_Power_Sensor)" in p
    assert "min=43.1" in p
    assert "Candidate Brick classes:" in p


def test_no_model_still_proposes_ranked_candidates():
    store = Store()
    uri = _point(store, "fbf/vav-3/ZoneAirflow-12", {"unit": "cfm"})
    proposals = mapping_proposals.MemoryProposals()

    [d] = _run(store, [uri], None, proposals)

    assert d.accepted is None and d.refused_because == "no model configured"
    names = [c["brick_class"] for c in proposals.list()[0]["candidates"]]
    assert len(names) == 3 and any("Air_Flow" in n for n in names)


def test_re_running_refreshes_the_open_proposal_instead_of_duplicating():
    store = Store()
    uri = _point(store, "fbf/vav-3/ZoneAirflow-12", {"unit": "cfm"})
    proposals = mapping_proposals.MemoryProposals()

    _run(store, [uri], None, proposals)
    _run(store, [uri], FakeModel(reply("Supply_Air_Flow_Sensor"), reply("Air_Flow_Sensor")), proposals)

    [row] = proposals.list()
    assert row["refused_because"].startswith("asked twice")


def test_an_auto_accept_closes_an_open_proposal():
    store = Store()
    uri = _point(store, "fbf/vav-3/ZoneAirflow-12", {"unit": "cfm"})
    proposals = mapping_proposals.MemoryProposals()
    _run(store, [uri], None, proposals)

    _run(store, [uri], FakeModel(reply("Supply_Air_Flow_Sensor")), proposals)

    assert proposals.list() == []
    assert proposals.list("auto-accepted")[0]["decided_class"] == "Supply_Air_Flow_Sensor"


def test_a_malformed_reply_is_no_answer_not_a_crash():
    store = Store()
    uri = _point(store, "fbf/ahu-1/SAT", {"unit": "°F"})

    [d] = _run(store, [uri], FakeModel({"candidates": "nope"}))

    assert d.accepted is None and "no Brick class" in d.refused_because


def test_equipment_candidates_use_brick_aliases():
    store = Store()
    equip = ingest_equip_tags(store, "fbf/vav-12", {})
    ev = model_step.gather(store, equip, "equip")

    assert model_step.candidates(ev, VOCAB)[0] == "Variable_Air_Volume_Box"


def test_pattern_ignores_numbers_but_not_tags_or_unit():
    a = model_step.Evidence("u1", "point", "fbf/vav-1/DAM-POS-12", [], {"unit": "%"})
    b = model_step.Evidence("u2", "point", "fbf/vav-7/DAM-POS-88", [], {"unit": "%"})
    c = model_step.Evidence("u3", "point", "fbf/vav-7/DAM-POS-88", [], {"unit": "°F"})
    assert model_step.pattern(a) == model_step.pattern(b) != model_step.pattern(c)


def test_ranker_splits_run_together_words():
    ev = model_step.Evidence("u", "point", "fbf/vav-1/ZoneAirflow", [], {"unit": "cfm"})
    words = model_step.evidence_words(ev, VOCAB)
    assert {"air", "flow", "sensor"} <= set(words)

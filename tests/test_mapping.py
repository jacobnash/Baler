from dataclasses import dataclass

import pytest
from rdflib import RDF, RDFS, SKOS, Graph, Literal, Namespace

from baler.ingest import ingest_tags
from baler.mapping import (
    classify_point,
    classify_point_with_fallback,
    load_rules,
    reclassify,
)
from baler.store import BRICK, PROJ, TD, Store

BLDG = Namespace("urn:test-mapping#")

RULES = load_rules()

# The real Brick ontology, verified once per test run - every rule's
# brick_class must exist in it, or the rule is asserting a class that
# doesn't exist and would produce a broken graph on real data.
BRICK_ONTOLOGY = Graph()
BRICK_ONTOLOGY.parse("tests/fixtures/brick_subset.ttl", format="turtle")


def test_load_rules_returns_at_least_the_expected_real_rules():
    brick_classes = {r["brick_class"] for r in RULES}
    assert {"Zone_Air_Temperature_Sensor", "Fan_Status", "Zone_CO2_Level_Sensor"}.issubset(brick_classes)
    assert len(RULES) >= 10


@pytest.mark.parametrize("rule", RULES, ids=[r["brick_class"] for r in RULES])
def test_every_rule_brick_class_exists_in_the_real_ontology(rule):
    """Guards against a typo'd or invented class name - every rule here
    must resolve to something that's actually in Brick, not a guess."""
    cls = BRICK[rule["brick_class"]]
    assert any(BRICK_ONTOLOGY.triples((cls, None, None))), f"{rule['brick_class']} is not a real Brick class"


def test_no_two_rules_share_an_identical_tag_set():
    """An authoring bug, not a runtime one - two rules with the same exact
    tag set make the 'most specific' tie-break in classify_point
    nondeterministic between them."""
    tag_sets = [frozenset(r["tags"]) for r in RULES]
    assert len(tag_sets) == len(set(tag_sets))


@pytest.mark.parametrize("rule", RULES, ids=[r["brick_class"] for r in RULES])
def test_classify_point_direct_match_for_every_rule_exact_tags(rule):
    store = Store()
    point_uri = ingest_tags(store, f"test-mapping:{rule['brick_class']}", {t: True for t in rule["tags"]})

    outcome, matched = classify_point(store, point_uri, rules=RULES)

    assert (outcome, matched) == ("direct", rule["brick_class"])
    assert (point_uri, RDF.type, BRICK[rule["brick_class"]]) in store.graph


@pytest.mark.parametrize("rule", RULES, ids=[r["brick_class"] for r in RULES])
def test_classify_point_direct_match_survives_real_structural_extras(rule):
    """A real Haxall point always carries `point`/`cur`/`dis`/`kind`/`unit`
    alongside the domain markers a rule cares about - those extras must
    never demote a clean match to a fallback (see the generic tag export
    hardening this rule set was built to survive)."""
    store = Store()
    tags = {t: True for t in rule["tags"]}
    tags["point"] = True
    tags["cur"] = True
    point_uri = ingest_tags(store, f"test-mapping:{rule['brick_class']}-real", tags)

    outcome, matched = classify_point(store, point_uri, rules=RULES)

    assert (outcome, matched) == ("direct", rule["brick_class"])


def test_classify_point_defaults_to_load_rules_when_rules_omitted():
    """AUDIT.md/mutmut: every other test here passes rules=RULES explicitly,
    so classify_point's `if rules is None: rules = load_rules()` fallback
    (mapping.py:59-60) had 100% line coverage but zero mutation coverage -
    deleting the load_rules() call entirely still passed every test. This
    calls classify_point with rules genuinely omitted to actually exercise
    that default."""
    rule = RULES[0]
    store = Store()
    point_uri = ingest_tags(store, "test-mapping:default-rules-path", {t: True for t in rule["tags"]})

    outcome, matched = classify_point(store, point_uri)

    assert (outcome, matched) == ("direct", rule["brick_class"])


def test_classify_point_falls_back_to_proj_for_partial_overlap():
    """Shares zone/air/sensor with the temp-sensor rule but has `tvoc`
    (a real Haystack marker - phScience/lib/air.trio) instead of `temp` -
    a real concept with no direct rule, preserved as a PROJ extension of
    the nearest structurally-related Brick class rather than dropped."""
    store = Store()
    point_uri = ingest_tags(
        store, "test-mapping:zone-tvoc",
        {"zone": True, "air": True, "sensor": True, "tvoc": True},
    )

    outcome, matched = classify_point(store, point_uri, rules=RULES)

    assert outcome == "fallback"
    assert matched == "Zone_Air_Temperature_Sensor"
    proj_class = PROJ["Zone_Air_Temperature_Sensor_Tvoc"]
    assert (point_uri, RDF.type, proj_class) in store.graph
    assert (proj_class, RDFS.subClassOf, BRICK.Zone_Air_Temperature_Sensor) in store.graph
    assert (proj_class, SKOS.definition, Literal("air, sensor, tvoc, zone")) in store.graph


def test_classify_point_miss_becomes_unmapped():
    store = Store()
    point_uri = ingest_tags(store, "test-mapping:mystery", {"someRandomTag": True})

    outcome, matched = classify_point(store, point_uri, rules=RULES)

    assert (outcome, matched) == ("miss", None)
    assert (point_uri, RDF.type, TD.UnmappedPoint) in store.graph


def test_reclassify_replaces_prior_direct_class_when_rules_change():
    store = Store()
    point_uri = ingest_tags(store, "test-mapping:reclassify-1", {"zone": True, "air": True, "temp": True, "sensor": True})
    classify_point(store, point_uri, rules=RULES)  # -> Zone_Air_Temperature_Sensor

    # simulate a rules-file change: the exact same tag set now maps elsewhere
    new_rules = [{"tags": ["zone", "air", "temp", "sensor"], "brick_class": "Fan_Status"}]
    outcome, matched = reclassify(store, point_uri, rules=new_rules)

    assert (outcome, matched) == ("direct", "Fan_Status")
    assert (point_uri, RDF.type, BRICK.Fan_Status) in store.graph
    assert (point_uri, RDF.type, BRICK.Zone_Air_Temperature_Sensor) not in store.graph


def test_reclassify_from_direct_to_fallback_removes_old_direct_type():
    store = Store()
    point_uri = ingest_tags(store, "test-mapping:reclassify-2", {"zone": True, "air": True, "sensor": True, "tvoc": True})
    old_rules = [{"tags": ["zone", "air", "sensor", "tvoc"], "brick_class": "Zone_Air_Temperature_Sensor"}]
    classify_point(store, point_uri, rules=old_rules)
    assert (point_uri, RDF.type, BRICK.Zone_Air_Temperature_Sensor) in store.graph

    # reclassify against the real rules, where this tag combo is only a fallback
    outcome, _matched = reclassify(store, point_uri, rules=RULES)

    assert outcome == "fallback"
    assert (point_uri, RDF.type, BRICK.Zone_Air_Temperature_Sensor) not in store.graph
    proj_class = PROJ["Zone_Air_Temperature_Sensor_Tvoc"]
    assert (point_uri, RDF.type, proj_class) in store.graph


def test_reclassify_on_never_classified_point_behaves_like_classify_point():
    store = Store()
    point_uri = ingest_tags(store, "test-mapping:reclassify-3", {"fan": True, "run": True, "sensor": True})
    outcome, matched = reclassify(store, point_uri, rules=RULES)
    assert (outcome, matched) == ("direct", "Fan_Status")


def test_reclassify_does_not_touch_non_classification_type_triples():
    store = Store()
    point_uri = ingest_tags(store, "test-mapping:reclassify-4", {"zone": True, "air": True, "temp": True, "sensor": True})
    store.add_entity(point_uri, TD.RawPoint)  # unrelated type, asserted by ingest_reading in real usage
    classify_point(store, point_uri, rules=RULES)

    reclassify(store, point_uri, rules=RULES)  # same rules - re-asserts the same class

    assert (point_uri, RDF.type, TD.RawPoint) in store.graph  # untouched by reclassify
    assert (point_uri, RDF.type, BRICK.Zone_Air_Temperature_Sensor) in store.graph


def test_classify_point_prefers_most_specific_rule_when_multiple_subsets_match():
    """Synthetic rules where a shorter tag set is itself a subset of a
    longer one - both would qualify as a 'direct' match, and the longer
    (more specific) rule must win the tie-break."""
    store = Store()
    synthetic_rules = [
        {"tags": ["zone", "sensor"], "brick_class": "Sensor"},
        {"tags": ["zone", "air", "temp", "sensor"], "brick_class": "Zone_Air_Temperature_Sensor"},
    ]
    point_uri = ingest_tags(
        store, "test-mapping:specificity-tiebreak",
        {"zone": True, "air": True, "temp": True, "sensor": True},
    )

    outcome, matched = classify_point(store, point_uri, rules=synthetic_rules)

    assert (outcome, matched) == ("direct", "Zone_Air_Temperature_Sensor")


@dataclass
class _FakeLLMResult:
    brick_class: str
    confidence: float
    reasoning: str


def test_classify_point_with_fallback_never_calls_llm_on_a_direct_match():
    store = Store()
    point_uri = ingest_tags(store, "test-mapping:fallback-direct", {"zone": True, "air": True, "temp": True, "sensor": True})

    def explode(*args, **kwargs):
        raise AssertionError("llm_classify must not be called on a direct rule match")

    outcome, matched = classify_point_with_fallback(store, point_uri, rules=RULES, llm_classify=explode)

    assert (outcome, matched) == ("direct", "Zone_Air_Temperature_Sensor")


def test_classify_point_with_fallback_none_llm_classify_behaves_like_classify_point():
    store = Store()
    point_uri = ingest_tags(store, "test-mapping:fallback-none", {"someRandomTag": True})

    outcome, matched = classify_point_with_fallback(store, point_uri, rules=RULES, llm_classify=None)

    assert (outcome, matched) == ("miss", None)


def test_classify_point_with_fallback_applies_high_confidence_llm_guess_on_miss():
    store = Store()
    point_uri = ingest_tags(store, "test-mapping:fallback-llm-applied", {"someRandomTag": True})

    def fake_llm_classify(tags, label, description, existing_brick_classes):
        return _FakeLLMResult(brick_class="Chiller", confidence=0.9, reasoning="chw points nearby")

    outcome, matched = classify_point_with_fallback(store, point_uri, rules=RULES, llm_classify=fake_llm_classify)

    assert (outcome, matched) == ("llm", "Chiller")
    assert (point_uri, RDF.type, BRICK.Chiller) in store.graph
    assert (point_uri, SKOS.definition, Literal("llm-classified: chw points nearby")) in store.graph


def test_classify_point_with_fallback_discards_low_confidence_llm_guess():
    store = Store()
    point_uri = ingest_tags(store, "test-mapping:fallback-llm-discarded", {"someRandomTag": True})

    def fake_llm_classify(tags, label, description, existing_brick_classes):
        return _FakeLLMResult(brick_class="Chiller", confidence=0.3, reasoning="not sure")

    outcome, matched = classify_point_with_fallback(store, point_uri, rules=RULES, llm_classify=fake_llm_classify)

    assert (outcome, matched) == ("miss", None)
    assert (point_uri, RDF.type, BRICK.Chiller) not in store.graph
    assert (point_uri, RDF.type, TD.UnmappedPoint) in store.graph


def test_classify_point_with_fallback_applies_llm_guess_on_partial_overlap_fallback():
    store = Store()
    point_uri = ingest_tags(
        store, "test-mapping:fallback-llm-on-fallback",
        {"zone": True, "air": True, "sensor": True, "tvoc": True},
    )

    def fake_llm_classify(tags, label, description, existing_brick_classes):
        assert "tvoc" in tags
        return _FakeLLMResult(brick_class="Zone_Air_Quality_Sensor", confidence=0.8, reasoning="tvoc reading")

    outcome, matched = classify_point_with_fallback(store, point_uri, rules=RULES, llm_classify=fake_llm_classify)

    assert (outcome, matched) == ("llm", "Zone_Air_Quality_Sensor")
    assert (point_uri, RDF.type, BRICK.Zone_Air_Quality_Sensor) in store.graph

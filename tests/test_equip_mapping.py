"""
Equipment-level classification: the same classify_point engine as
test_mapping.py, applied to equip-rec tags via
rules/haystack_equip_to_brick.yaml, plus the end-to-end "these points
become one AHU" flow tying ingest_equip_tags + link_point_to_equip +
classify_point together.
"""

import pytest
from rdflib import RDF, Graph

from baler.ingest import (
    ingest_equip_tags,
    ingest_tags,
    link_point_to_equip,
    topic_prefix_to_equip_uri,
)
from baler.mapping import classify_point, load_rules
from baler.store import BRICK, Store

EQUIP_RULES_PATH = "rules/haystack_equip_to_brick.yaml"
EQUIP_RULES = load_rules(EQUIP_RULES_PATH)

BRICK_ONTOLOGY = Graph()
BRICK_ONTOLOGY.parse("tests/fixtures/brick_subset.ttl", format="turtle")


def test_load_equip_rules_returns_ahu_and_vav():
    brick_classes = {r["brick_class"] for r in EQUIP_RULES}
    assert brick_classes == {
        "Air_Handling_Unit",
        "Variable_Air_Volume_Box",
        "Electrical_Meter",
        "Chiller",
        "Boiler",
        "Pump",
        "Exhaust_Fan",
        "Weather_Station",
    }


@pytest.mark.parametrize("rule", EQUIP_RULES, ids=[r["brick_class"] for r in EQUIP_RULES])
def test_every_equip_rule_brick_class_exists_in_the_real_ontology(rule):
    cls = BRICK[rule["brick_class"]]
    assert any(BRICK_ONTOLOGY.triples((cls, None, None))), f"{rule['brick_class']} is not a real Brick class"


def test_classify_equip_direct_match_for_ahu():
    store = Store()
    equip_uri = ingest_equip_tags(store, "test-equip:ahu-1", {"equip": True, "ahu": True, "dis": "AHU-1"})

    outcome, matched = classify_point(store, equip_uri, rules=EQUIP_RULES)

    assert (outcome, matched) == ("direct", "Air_Handling_Unit")
    assert (equip_uri, RDF.type, BRICK.Air_Handling_Unit) in store.graph


def test_classify_equip_direct_match_for_vav():
    store = Store()
    equip_uri = ingest_equip_tags(store, "test-equip:vav-12", {"equip": True, "vav": True})

    outcome, matched = classify_point(store, equip_uri, rules=EQUIP_RULES)

    assert (outcome, matched) == ("direct", "Variable_Air_Volume_Box")


def test_bacnet_sourced_ahu_groups_untagged_points_with_zero_tags_required():
    """The core "somewhat unique AHU" story from a BACnet connection: the
    topic_prefix already gives hasPoint structure with zero tags on either
    end - equipment classification (ahu marker below) is an independent,
    optional layer on top, not a prerequisite for the points to be grouped."""
    store = Store()
    topic_prefix = "fbf/ahu-3"
    equip_uri = topic_prefix_to_equip_uri(topic_prefix)

    point_uris = [ingest_tags(store, f"{topic_prefix}/{label}", {}) for label in ("zone-temp", "fan-status", "damper-cmd")]
    for point_uri in point_uris:
        link_point_to_equip(store, point_uri, equip_uri)

    # Structural grouping works with no Brick class assigned to the equipment yet.
    linked = {str(row.p) for row in store.query(f"""
        PREFIX brick: <https://brickschema.org/schema/Brick#>
        SELECT ?p WHERE {{ <{equip_uri}> brick:hasPoint ?p }}
    """)}
    assert linked == {str(p) for p in point_uris}

    # Equipment classification is a separate, best-effort step on top.
    ingest_equip_tags(store, topic_prefix, {"ahu": True})
    outcome, matched = classify_point(store, equip_uri, rules=EQUIP_RULES)
    assert (outcome, matched) == ("direct", "Air_Handling_Unit")

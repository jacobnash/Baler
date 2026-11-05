"""
brick_vocab reads the vendored ontology/Brick-only.ttl with a regex over
its (rdflib-serialized, one-block-per-class) layout. These pin the facts
hisquery.py relies on, against the real file - so a Brick version bump
that changes the serialization fails here, loudly, instead of silently
turning `readAll(Temperature_Sensor)` into an exact-type match.
"""

import os

import pytest

from baler import brick_vocab

pytestmark = pytest.mark.skipif(not os.path.exists(brick_vocab.DEFAULT_ONTOLOGY_PATH), reason="vendored Brick not present")


@pytest.fixture(scope="module")
def vocab():
    return brick_vocab.load()


def test_parses_every_class_block_including_deprecated_shapeless_ones(vocab):
    # `grep -c "^brick:\w* a owl:Class" ontology/Brick-only.ttl` == 1428 for 1.4.4.
    assert len(vocab.classes) == 1428
    assert len(vocab.tags) == 531
    # Deprecated classes are `a owl:Class ;` on one line, no sh:NodeShape.
    assert "Zone_Air_Temperature_Setpoint" in vocab.classes


def test_class_names_resolve_case_insensitively(vocab):
    assert vocab.canonical_class("air_handling_unit") == "Air_Handling_Unit"
    assert vocab.canonical_class("Zone_air_temperature_SENSOR") == "Zone_Air_Temperature_Sensor"
    assert vocab.canonical_class("Not_A_Brick_Class") is None


def test_descendants_include_subclasses_and_aliases_both_ways(vocab):
    ts = vocab.descendants("Temperature_Sensor")
    assert {"Temperature_Sensor", "Air_Temperature_Sensor", "Zone_Air_Temperature_Sensor", "Discharge_Air_Temperature_Sensor"} <= ts
    assert "Fan_Status" not in ts
    # brick:AHU owl:equivalentClass brick:Air_Handling_Unit - either name reaches the other's subtree.
    assert "Air_Handling_Unit" in vocab.descendants("AHU")
    assert "AHU" in vocab.descendants("Air_Handling_Unit")
    assert "Rooftop_Unit" in vocab.descendants("Air_Handling_Unit")
    # brick:isReplacedBy links a deprecated class to its replacement, treated the same way.
    assert "Zone_Air_Temperature_Setpoint" in vocab.descendants("Target_Zone_Air_Temperature_Setpoint")


def test_effective_tags_inherit_through_equivalents_and_parents(vocab):
    assert vocab.effective_tags("Zone_Air_Temperature_Sensor") == {"Air", "Point", "Sensor", "Temperature", "Zone"}
    # No hasAssociatedTag of its own in 1.4.4 - inherits from Supply_Air_Temperature_Sensor via owl:equivalentClass.
    assert {"Temperature", "Sensor", "Discharge", "Supply"} <= vocab.effective_tags("Discharge_Air_Temperature_Sensor")
    # Alias with no tags of its own.
    assert "AHU" in vocab.effective_tags("AHU") and "Equipment" in vocab.effective_tags("AHU")
    # Deprecated + tagless: tags come from the replacement class.
    assert "Zone" in vocab.effective_tags("Zone_Air_Temperature_Setpoint")
    # Only Brick's abstract roots have nothing.
    untagged = {c for c in vocab.classes if not vocab.effective_tags(c)}
    assert untagged == {"Class", "Entity", "Quantity", "Substance", "EntityPropertyValue", "Tag"}


def test_tag_words_resolve_with_haystack_synonyms(vocab):
    assert vocab.canonical_tag("temperature") == "Temperature"
    assert vocab.canonical_tag("temp") == "Temperature"  # Haystack spelling
    assert vocab.canonical_tag("sp") == "Setpoint"
    assert vocab.canonical_tag("cmd") == "Command"
    assert vocab.canonical_tag("equip") == "Equipment"
    assert vocab.canonical_tag("co2") == "CO2"
    assert vocab.canonical_tag("ahu") == "AHU"
    assert vocab.canonical_tag("notaword") is None
    for haystack, brick in brick_vocab.HAYSTACK_TO_BRICK_TAG.items():
        assert brick in vocab.tags, f"{haystack} -> {brick}: not a Brick 1.4.4 tag"


def test_classes_with_tag_is_the_inverse_of_effective_tags(vocab):
    with_temp = vocab.classes_with_tag("Temperature")
    assert "Zone_Air_Temperature_Sensor" in with_temp and "Discharge_Air_Temperature_Sensor" in with_temp
    assert "Fan_Status" not in with_temp
    assert all("Temperature" in vocab.effective_tags(c) for c in with_temp)


def test_suggestions_for_near_misses(vocab):
    assert vocab.suggest_class("Air_Handeling_Unit")[0] == "Air_Handling_Unit"
    assert vocab.suggest_tag("temperture") == ["temperature"]
    assert vocab.suggest_class("Zzzz_Qqqq") == []


def test_missing_file_degrades_to_an_empty_vocab(tmp_path):
    v = brick_vocab.load(str(tmp_path / "nope.ttl"))
    assert v.classes == {} and v.tags == set()
    assert v.canonical_class("Air_Handling_Unit") is None
    assert v.descendants("Air_Handling_Unit") == frozenset()

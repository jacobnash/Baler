"""
Unit tests, no live services and no ANTHROPIC_API_KEY needed.
classify_with_llm does `import anthropic` lazily inside the function (see
llm_classifier.py's module docstring for why), so injecting a fake module
into sys.modules before the call is enough to stub the client - no need
for the real `anthropic` package to be installed to run this file.
"""

import sys
import types
from dataclasses import dataclass

import pytest

from baler import llm_classifier


@dataclass
class _FakeParsedOutput:
    brick_class: str
    confidence: float
    reasoning: str


def _install_fake_anthropic(monkeypatch, parsed_output):
    class _FakeResponse:
        def __init__(self, parsed):
            self.parsed_output = parsed

    class _FakeMessages:
        def parse(self, **kwargs):
            assert kwargs["model"] == llm_classifier.MODEL
            return _FakeResponse(parsed_output)

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            self.messages = _FakeMessages()

    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = _FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)


def test_classify_with_llm_returns_parsed_result(monkeypatch):
    _install_fake_anthropic(monkeypatch, _FakeParsedOutput(brick_class="Chiller", confidence=0.85, reasoning="chw supply/return pair"))

    result = llm_classifier.classify_with_llm(
        tags=set(), label="fbf/chiller-1/CHW-Sup-Temp", description=None, existing_brick_classes=["Air_Handling_Unit"]
    )

    assert result.brick_class == "Chiller"
    assert result.confidence == 0.85
    assert result.reasoning == "chw supply/return pair"


def test_classify_with_llm_raises_on_unparseable_response(monkeypatch):
    """AUDIT.md Theme D: response.parsed_output is None whenever the SDK
    can't parse the model's output into _ClassificationSchema - accessing
    .brick_class on it directly used to raise an unhandled AttributeError.
    Confirms it now raises a clear, catchable error instead."""
    _install_fake_anthropic(monkeypatch, parsed_output=None)

    with pytest.raises(llm_classifier.LLMClassificationError):
        llm_classifier.classify_with_llm(tags={"chw"}, label="fbf/chiller-1/CHW-Sup-Temp", description=None, existing_brick_classes=[])


def test_propose_rule_appends_a_new_proposal(tmp_path):
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text("rules:\n  - tags: [zone, air, temp, sensor]\n    brick_class: Zone_Air_Temperature_Sensor\n")

    added = llm_classifier.propose_rule(str(rules_path), {"chw", "sup", "temp"}, "Chilled_Water_Supply_Temperature_Sensor")

    assert added is True
    text = rules_path.read_text()
    assert llm_classifier._PROPOSED_MARKER in text
    assert "Chilled_Water_Supply_Temperature_Sensor" in text
    # the promoted rules list itself must be untouched - still exactly one real rule
    import yaml

    assert len(yaml.safe_load(text)["rules"]) == 1


def test_propose_rule_dedupes_identical_tag_sets(tmp_path):
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text("rules:\n  - tags: [zone, air, temp, sensor]\n    brick_class: Zone_Air_Temperature_Sensor\n")

    first = llm_classifier.propose_rule(str(rules_path), {"chw", "sup", "temp"}, "Chilled_Water_Supply_Temperature_Sensor")
    second = llm_classifier.propose_rule(str(rules_path), {"chw", "sup", "temp"}, "Chilled_Water_Supply_Temperature_Sensor")

    assert (first, second) == (True, False)
    assert rules_path.read_text().count("Chilled_Water_Supply_Temperature_Sensor") == 1


def test_propose_rule_skips_a_tag_set_already_promoted(tmp_path):
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text("rules:\n  - tags: [zone, air, temp, sensor]\n    brick_class: Zone_Air_Temperature_Sensor\n")

    added = llm_classifier.propose_rule(str(rules_path), {"zone", "air", "temp", "sensor"}, "Zone_Air_Temperature_Sensor")

    assert added is False
    assert llm_classifier._PROPOSED_MARKER not in rules_path.read_text()

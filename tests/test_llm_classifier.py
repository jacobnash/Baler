"""
Model clients for the tagging model step (#182). No network and no
`anthropic` package needed: a fake module stands in for it, and the
OpenAI-compatible client gets a fake chat object.
"""

import sys
import types

import pytest

from baler import llm_classifier
from baler.commissioning import llm


class _Parsed:
    def __init__(self, data):
        self.data = data

    def model_dump(self):
        return self.data


def _fake_anthropic(monkeypatch, parsed, seen):
    class _Messages:
        def parse(self, **kwargs):
            seen.append(kwargs)
            return types.SimpleNamespace(parsed_output=parsed)

    class _Client:
        def __init__(self, *a, **k):
            self.messages = _Messages()

    mod = types.ModuleType("anthropic")
    mod.Anthropic = _Client
    monkeypatch.setitem(sys.modules, "anthropic", mod)


def test_claude_tagger_returns_the_parsed_reply(monkeypatch):
    seen: list = []
    _fake_anthropic(monkeypatch, _Parsed({"candidates": [{"brick_class": "Chiller", "reason": "r"}], "confidence": 0.8}), seen)
    monkeypatch.delenv("BALER_TAG_MODEL", raising=False)

    out = llm_classifier.ClaudeTagger().ask("which class?")

    assert out["candidates"][0]["brick_class"] == "Chiller"
    assert seen[0]["model"] == llm_classifier.DEFAULT_ANTHROPIC_MODEL
    assert seen[0]["messages"][0]["content"] == "which class?"


def test_claude_tagger_raises_on_unparseable_reply(monkeypatch):
    _fake_anthropic(monkeypatch, None, [])
    with pytest.raises(llm_classifier.LLMClassificationError):
        llm_classifier.ClaudeTagger().ask("x")


def test_tag_model_env_overrides_the_default(monkeypatch):
    monkeypatch.setenv("BALER_TAG_MODEL", "claude-sonnet-x")
    assert llm_classifier.ClaudeTagger().model == "claude-sonnet-x"


class _FakeChat:
    def __init__(self, contents, reject_first=0):
        self.contents = list(contents)
        self.calls: list[dict] = []
        self.reject_first = reject_first

    def chat(self, messages, **params):
        self.calls.append(params)
        if len(self.calls) <= self.reject_first:
            err = llm.LLMError("unsupported response_format")
            err.status = 400  # type: ignore[attr-defined]
            raise err
        return {"choices": [{"message": {"content": self.contents.pop(0)}}]}


def test_openai_tagger_parses_json_even_inside_prose():
    chat = _FakeChat(['Sure: {"candidates": [{"brick_class": "Pump", "reason": "r"}], "confidence": 0.7}'])
    out = llm_classifier.OpenAITagger(chat=chat, model="m").ask("q")
    assert out["candidates"][0]["brick_class"] == "Pump"
    assert chat.calls[0]["response_format"]["type"] == "json_schema"


def test_openai_tagger_steps_down_response_formats_the_endpoint_rejects():
    chat = _FakeChat(['{"candidates": [], "confidence": 0}'], reject_first=2)
    llm_classifier.OpenAITagger(chat=chat, model="m").ask("q")
    assert "response_format" not in chat.calls[-1]


def test_openai_tagger_raises_on_a_non_json_reply():
    with pytest.raises(llm_classifier.LLMClassificationError):
        llm_classifier.OpenAITagger(chat=_FakeChat(["no idea"]), model="m").ask("q")


def test_model_from_env_follows_baler_llm_provider(monkeypatch):
    monkeypatch.setenv("BALER_LLM_PROVIDER", "none")
    assert llm_classifier.model_from_env() is None
    monkeypatch.setenv("BALER_LLM_PROVIDER", "anthropic")
    assert isinstance(llm_classifier.model_from_env(), llm_classifier.ClaudeTagger)
    monkeypatch.setenv("BALER_LLM_PROVIDER", "openai")
    monkeypatch.setenv("BALER_LLM_BASE_URL", "http://ollama:11434/v1")
    monkeypatch.setenv("BALER_LLM_MODEL", "qwen2.5:7b")
    monkeypatch.delenv("BALER_TAG_MODEL", raising=False)
    m = llm_classifier.model_from_env()
    assert isinstance(m, llm_classifier.OpenAITagger) and m.model == "qwen2.5:7b"

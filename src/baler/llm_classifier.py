"""
Model clients for the tagging ladder's model step (model_step.py), through
the same BALER_LLM_PROVIDER switch as Cx (commissioning/llm.py):

  - anthropic: the Claude API (the `llm` extra, ANTHROPIC_API_KEY);
  - openai: any OpenAI-compatible endpoint (BALER_LLM_BASE_URL,
    BALER_LLM_API_KEY), e.g. a local Ollama;
  - none: no model. The model step still runs and every unresolved point
    becomes a proposal carrying the deterministic candidates.

The model is BALER_TAG_MODEL, else (openai) BALER_LLM_MODEL, else (anthropic)
commissioning.llm.DEFAULT_ANTHROPIC_TAG_MODEL, the current Haiku, defined
beside Cx's default so the two move together. Cx's BALER_CX_MODEL is
deliberately not used: it defaults to the current Opus, meant for reading
PDFs, and this step asks twice per naming pattern.

A client is anything with `.name` and `.ask(prompt) -> dict` (the reply
JSON); tests and the eval harness pass fakes. `anthropic` is imported only
inside ClaudeTagger, so the rest of Baler never needs it.
"""

from __future__ import annotations

import os

from baler import tracing
from baler.commissioning import llm

tracer = tracing.get_tracer(__name__)

DEFAULT_ANTHROPIC_MODEL = llm.DEFAULT_ANTHROPIC_TAG_MODEL
# Room for the reply plus the thinking Haiku 5.5 does by default (adaptive,
# counted against max_tokens); the JSON itself is a few hundred tokens.
MAX_TOKENS = 4096


class LLMClassificationError(Exception):
    """A reply that can't be read as the candidates JSON. autotag treats it
    like a network error (retry, then leave the pattern for review)."""


def tag_model_name(provider: str) -> str:
    explicit = (os.environ.get("BALER_TAG_MODEL") or "").strip()
    if explicit:
        return explicit
    return llm.openai_model() if provider == "openai" else DEFAULT_ANTHROPIC_MODEL


try:  # the `llm` extra; only the anthropic provider needs it
    from pydantic import BaseModel

    class _Candidate(BaseModel):
        brick_class: str
        reason: str

    class _Reply(BaseModel):
        candidates: list[_Candidate]
        confidence: float
except ImportError:  # pragma: no cover - servers without the extra
    BaseModel = None  # type: ignore[assignment,misc]


class ClaudeTagger:
    def __init__(self, model: str | None = None, client=None):
        self.model = model or tag_model_name("anthropic")
        self.name = f"anthropic:{self.model}"
        self._client = client

    def ask(self, prompt: str) -> dict:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        with tracer.start_as_current_span("llm_classifier.ask") as span:
            span.set_attribute("model", self.model)
            response = self._client.messages.parse(
                model=self.model, max_tokens=MAX_TOKENS, messages=[{"role": "user", "content": prompt}], output_format=_Reply,
            )
            parsed = response.parsed_output
            if parsed is None:
                raise LLMClassificationError("model returned no parsed output")
            return parsed.model_dump()


_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {"type": "array", "items": {"type": "object", "properties": {
            "brick_class": {"type": "string"}, "reason": {"type": "string"}}, "required": ["brick_class", "reason"]}},
        "confidence": {"type": "number"},
    },
    "required": ["candidates", "confidence"],
}
_FORMATS: list[dict | None] = [
    {"type": "json_schema", "json_schema": {"name": "brick_candidates", "schema": _SCHEMA}},
    {"type": "json_object"},
    None,
]


class OpenAITagger:
    def __init__(self, chat=None, model: str | None = None):
        self.model = model or tag_model_name("openai")
        self.chat = chat or llm.OpenAIChat(model=self.model)
        self.name = f"openai:{self.model}"
        self._fmt = 0

    def ask(self, prompt: str) -> dict:
        messages = [{"role": "user", "content": prompt}]
        while True:
            fmt = _FORMATS[self._fmt]
            params: dict = {"max_tokens": MAX_TOKENS, "temperature": 0}
            if fmt is not None:
                params["response_format"] = fmt
            try:
                resp = self.chat.chat(messages, **params)
                break
            except llm.LLMError as exc:  # step down formats the endpoint rejects, like spec_llm
                if getattr(exc, "status", None) in (400, 404, 415, 422) and self._fmt < len(_FORMATS) - 1:
                    self._fmt += 1
                    continue
                raise
        content = ((resp["choices"][0].get("message") or {}).get("content")) or ""
        try:
            parsed = llm.parse_json(content)
        except ValueError as exc:
            raise LLMClassificationError(f"reply wasn't JSON: {content[:200]!r}") from exc
        if not isinstance(parsed, dict):
            raise LLMClassificationError("reply JSON isn't an object")
        return parsed


def model_from_env():
    """The configured model client, or None for BALER_LLM_PROVIDER=none."""
    provider = llm.provider()
    if provider == "anthropic":
        return ClaudeTagger()
    if provider == "openai":
        return OpenAITagger()
    return None

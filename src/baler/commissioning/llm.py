"""
Which language model the commissioning features use (Cx 7 spec PDFs, Cx 10
agent), and a small client for OpenAI-compatible endpoints.

BALER_LLM_PROVIDER picks one of:

  - anthropic: the Claude API (the `llm` extra, ANTHROPIC_API_KEY). The
    model is BALER_CX_MODEL, else BALER_LLM_MODEL, else DEFAULT_ANTHROPIC_MODEL.
  - openai: any OpenAI-compatible Chat Completions endpoint at
    BALER_LLM_BASE_URL (Ollama, Groq, Gemini, OpenRouter, OpenAI, vLLM),
    with BALER_LLM_API_KEY (blank for Ollama) and BALER_LLM_MODEL.
  - none: no model. PDF tables are read locally (spec_pdf); the agent
    answers that it needs a model.

Unset: anthropic when ANTHROPIC_API_KEY is set, else openai when
BALER_LLM_BASE_URL is set, else none.

The OpenAI-compatible client is plain `requests` (a core dependency), so
no extra SDK is needed and every endpoint above takes the same request.
"""

from __future__ import annotations

import os
import time

import requests

PROVIDERS = ("anthropic", "openai", "none")
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
DEFAULT_TIMEOUT = 300.0  # seconds; a CPU-only Ollama is slow
RETRIES = 3  # on 429/503 - free tiers rate-limit hard


class LLMError(Exception):
    pass


class ToolCallsUnsupported(LLMError):
    pass


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def provider() -> str:
    p = _env("BALER_LLM_PROVIDER").lower()
    if p:
        if p not in PROVIDERS:
            raise LLMError(f"BALER_LLM_PROVIDER must be one of {', '.join(PROVIDERS)}, not {p!r}")
        return p
    if _env("ANTHROPIC_API_KEY"):
        return "anthropic"
    if _env("BALER_LLM_BASE_URL"):
        return "openai"
    return "none"


def anthropic_model() -> str:
    return _env("BALER_CX_MODEL") or _env("BALER_LLM_MODEL") or DEFAULT_ANTHROPIC_MODEL


def openai_model() -> str:
    return _env("BALER_LLM_MODEL")


def unavailable_reason(feature: str) -> str | None:
    """Why `feature` ("extract" or "agent") can't run with the configured
    provider on this server, or None when it can."""
    try:
        p = provider()
    except LLMError as exc:
        return str(exc)
    if p == "anthropic":
        try:
            import anthropic  # noqa: F401
            import pydantic  # noqa: F401
        except ImportError:
            return "BALER_LLM_PROVIDER=anthropic needs the llm extra (anthropic, pydantic) installed on the server"
        return None
    if p == "openai":
        if not _env("BALER_LLM_BASE_URL"):
            return "BALER_LLM_PROVIDER=openai needs BALER_LLM_BASE_URL (e.g. http://ollama:11434/v1)"
        if not openai_model():
            return "BALER_LLM_PROVIDER=openai needs BALER_LLM_MODEL (the endpoint's model name)"
    if feature == "agent" and p == "none":
        return ("the commissioning agent needs a model: set BALER_LLM_PROVIDER=openai with BALER_LLM_BASE_URL and "
                "BALER_LLM_MODEL (free: Ollama, Groq, Gemini, OpenRouter - see .env.example), or ANTHROPIC_API_KEY")
    if feature == "extract":
        try:
            import pdfplumber  # noqa: F401
        except ImportError:
            return "PDF extraction without Claude needs pdfplumber (the cx-spec extra) installed on the server"
    return None


class OpenAIChat:
    """POST {base_url}/chat/completions. `http` is anything with
    requests' post(url, json=, headers=, timeout=) - tests pass a fake."""

    kind = "openai"

    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None,
                 timeout: float | None = None, http=None, sleep=time.sleep):
        self.base_url = (base_url if base_url is not None else _env("BALER_LLM_BASE_URL")).rstrip("/")
        self.api_key = api_key if api_key is not None else _env("BALER_LLM_API_KEY")
        self.model = model or openai_model()
        self.timeout = timeout or float(_env("BALER_LLM_TIMEOUT") or DEFAULT_TIMEOUT)
        self.http = http or requests
        self.sleep = sleep
        if not self.base_url:
            raise LLMError("BALER_LLM_BASE_URL is not set")
        if not self.model:
            raise LLMError("BALER_LLM_MODEL is not set")

    def chat(self, messages: list[dict], **params) -> dict:
        """The response JSON. Raises LLMError (with `.status`) on an HTTP
        error after retrying rate limits."""
        headers = {"Content-Type": "application/json", "X-Title": "Baler"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {"model": self.model, "messages": messages, **params}
        for attempt in range(RETRIES + 1):
            try:
                r = self.http.post(f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                raise LLMError(f"could not reach {self.base_url}: {exc}") from exc
            if r.status_code in (429, 503) and attempt < RETRIES:
                try:
                    wait = float((getattr(r, "headers", None) or {}).get("Retry-After") or 0)
                except ValueError:
                    wait = 0
                self.sleep(min(30.0, wait or 2.0 * 2 ** attempt))
                continue
            if r.status_code >= 400:
                err = LLMError(f"{self.base_url} answered {r.status_code}: {str(getattr(r, 'text', ''))[:500]}")
                err.status = r.status_code  # type: ignore[attr-defined]
                raise err
            try:
                data = r.json()
            except ValueError as exc:
                raise LLMError(f"{self.base_url} answered something that isn't JSON") from exc
            if not isinstance(data, dict) or not data.get("choices"):
                raise LLMError(f"{self.base_url} answered without choices: {str(data)[:300]}")
            return data
        raise LLMError("unreachable")  # pragma: no cover

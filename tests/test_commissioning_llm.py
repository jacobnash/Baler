"""Cx 7 / Cx 10 on providers other than Anthropic: provider selection, the
OpenAI-compatible client (fake HTTP), spec extraction with no model
(pdfplumber tables) and on an OpenAI-compatible model (quotes checked
against page text), and the agent's loop with function calling."""

from __future__ import annotations

import io
import json
import time
from types import SimpleNamespace

import cx_fixtures as fx
import pytest
import requests
import test_commissioning_agent as ta
import test_commissioning_openapi as api

from baler.commissioning import agent, llm, spec_llm, spec_pdf

PROVIDER_ENV = ("BALER_LLM_PROVIDER", "ANTHROPIC_API_KEY", "BALER_LLM_BASE_URL", "BALER_LLM_API_KEY", "BALER_LLM_MODEL",
                "BALER_CX_MODEL", "BALER_LLM_MAX_CHARS", "BALER_LLM_MAX_TOKENS")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in PROVIDER_ENV:
        monkeypatch.delenv(k, raising=False)


# ------------------------------------------------------------- provider


def test_provider_resolution(monkeypatch):
    assert llm.provider() == "none"
    monkeypatch.setenv("BALER_LLM_BASE_URL", "http://ollama:11434/v1")
    assert llm.provider() == "openai"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert llm.provider() == "anthropic"  # the current behaviour wins when its key is set
    monkeypatch.setenv("BALER_LLM_PROVIDER", "OpenAI")
    assert llm.provider() == "openai"
    monkeypatch.setenv("BALER_LLM_PROVIDER", "none")
    assert llm.provider() == "none"
    monkeypatch.setenv("BALER_LLM_PROVIDER", "bogus")
    with pytest.raises(llm.LLMError):
        llm.provider()


def test_models_and_unavailable_reasons(monkeypatch):
    assert llm.anthropic_model() == llm.DEFAULT_ANTHROPIC_MODEL
    monkeypatch.setenv("BALER_LLM_MODEL", "generic")
    assert llm.anthropic_model() == "generic"
    monkeypatch.setenv("BALER_CX_MODEL", "cx")
    assert llm.anthropic_model() == "cx" and spec_llm.model_name() == "cx" and agent.model_name() == "cx"
    monkeypatch.delenv("BALER_LLM_MODEL")
    assert "agent needs a model" in llm.unavailable_reason("agent")
    monkeypatch.setenv("BALER_LLM_PROVIDER", "openai")
    assert "BALER_LLM_BASE_URL" in llm.unavailable_reason("agent")
    monkeypatch.setenv("BALER_LLM_BASE_URL", "https://api.groq.com/openai/v1")
    assert "BALER_LLM_MODEL" in llm.unavailable_reason("agent")
    monkeypatch.setenv("BALER_LLM_MODEL", "llama-3.3-70b-versatile")
    assert llm.unavailable_reason("agent") is None


# ---------------------------------------------------------- OpenAI client


class FakeHTTP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        status, body = self.responses.pop(0)
        return SimpleNamespace(status_code=status, headers={}, text=body if isinstance(body, str) else "",
                               json=lambda b=body: b)


def _completion(content=None, tool_calls=None, finish="stop"):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"model": "m-served", "choices": [{"message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3}}


def test_openai_client_posts_and_retries_rate_limits():
    http = FakeHTTP((429, "slow down"), (200, _completion("hi")))
    slept = []
    c = llm.OpenAIChat("https://generativelanguage.googleapis.com/v1beta/openai/", "key", "gemini-2.5-flash",
                       http=http, sleep=slept.append)
    out = c.chat([{"role": "user", "content": "x"}], temperature=0)
    assert out["choices"][0]["message"]["content"] == "hi" and len(slept) == 1
    call = http.calls[-1]
    assert call["url"] == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer key"
    assert call["json"] == {"model": "gemini-2.5-flash", "messages": [{"role": "user", "content": "x"}], "temperature": 0}


def test_openai_client_without_key_and_errors():
    http = FakeHTTP((400, "bad request"))
    c = llm.OpenAIChat("http://ollama:11434/v1", "", "llama3.2", http=http)
    with pytest.raises(llm.LLMError) as e:
        c.chat([])
    assert e.value.status == 400 and "Authorization" not in http.calls[0]["headers"]
    with pytest.raises(llm.LLMError):
        llm.OpenAIChat("", "", "m")


# ----------------------------------------------------------------- PDFs


def make_pdf() -> bytes:
    """Page 1: an equipment schedule. Page 2: a point list and a sequence
    of operation in prose (which only a model reads)."""
    pytest.importorskip("pdfplumber")
    pytest.importorskip("reportlab")
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Table,
        TableStyle,
    )

    buf = io.BytesIO()
    st = getSampleStyleSheet()
    grid = TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)])
    sched = Table([["FAN SCHEDULE", "", ""], ["Tag", "Serves", "Airflow"], ["EF-1", "Toilet rooms", "800 CFM"],
                   ["AHU-1", "Level 1", "12,000 CFM"]], style=grid)
    points = Table([["Equipment", "Point Name", "Units"], ["AHU-1", "Supply Air Temperature", "degF"]], style=grid)
    seq = Paragraph("SEQUENCE OF OPERATION AHU-1: The supply air temperature setpoint shall be 55 degF.", st["Normal"])
    SimpleDocTemplate(buf, pagesize=letter).build([Paragraph("Mechanical schedules", st["Heading1"]), sched, PageBreak(),
                                                   points, Paragraph(" ", st["Normal"]), seq])
    return buf.getvalue()


def test_none_provider_reads_tables_offline():
    data = make_pdf()
    items, info = spec_llm.extract_pdf(data, "M-601.pdf")  # no key, no URL: provider none
    by = {(i["kind"], i["tag"]): i for i in items}
    assert set(by) == {("equipment", "EF-1"), ("equipment", "AHU-1"), ("point", "Supply Air Temperature")}
    ef = by[("equipment", "EF-1")]
    assert ef["source"] == {"document": "M-601.pdf", "page": 1, "quote": "EF-1 | Toilet rooms | 800 CFM", "method": "table"}
    assert ef["brick_class"] and "Fan" in ef["brick_class"]  # the title row "FAN SCHEDULE" is the type hint
    assert ef["properties"]["Airflow"] == "800 CFM"
    pt = by[("point", "Supply Air Temperature")]
    assert pt["equip_tag"] == "AHU-1" and pt["source"]["page"] == 2 and pt["brick_class"] == "Supply_Air_Temperature_Sensor"
    assert info["provider"] == "none" and info["model"] is None
    pages = spec_pdf.read(data, "M-601.pdf")
    assert all(spec_pdf.quote_on(i["source"]["quote"], pages[i["source"]["page"] - 1].text) for i in items)
    assert "SEQUENCE OF OPERATION" in pages[1].rest and "EF-1" not in pages[0].rest  # tables cut from what a model reads


def test_none_provider_bad_pdf_fails_loudly(monkeypatch):
    pytest.importorskip("pdfplumber")
    monkeypatch.setenv("BALER_LLM_PROVIDER", "none")
    with pytest.raises(spec_llm.ExtractionError):
        spec_llm.extract_pdf(b"%PDF-1.4 not really", "x.pdf")


def test_quote_matching():
    text = "AHU-1  Rooftop\nunit 12,000 CFM — supply “air”"
    assert spec_pdf.quote_on("ahu-1 rooftop unit", text)
    assert spec_pdf.quote_on("AHU-1 ... 12,000 CFM - supply \"air\"", text)
    assert spec_pdf.quote_on("AHU-1 | Rooftop", text)
    assert not spec_pdf.quote_on("12,000 CFM ... AHU-1", text)  # fragments in order
    assert not spec_pdf.quote_on("AHU-2", text) and not spec_pdf.quote_on("", text)


class FakeOpenAI:
    kind = "openai"
    model = "llama3.2:3b"

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    def chat(self, messages, **params):
        self.requests.append({"messages": messages, **params})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(messages, params) if callable(step) else step


def _rejected(status=400, text="response_format json_schema not supported"):
    e = llm.LLMError(text)
    e.status = status
    return e


def test_openai_extraction_checks_quotes_and_skips_tables():
    data = make_pdf()
    reply = {"items": [
        {"kind": "sequence", "tag": "AHU-1 SOO", "name": "Supply air temperature control", "equipment_tag": "AHU-1",
         "properties": {"SAT setpoint": "55 degF"}, "page": 2, "quote": "supply air temperature setpoint shall be 55 degF",
         "confidence": "0.8"},
        {"kind": "equipment", "tag": "EF-1", "name": "Exhaust fan", "page": 1, "quote": "EF-1 Toilet rooms", "confidence": 0.9},
        {"kind": "equipment", "tag": "B-9", "name": "Boiler", "page": 2, "quote": "B-9 condensing boiler 2,000 MBH", "confidence": 0.9},
        {"kind": "point", "tag": "SAT", "equipment_tag": "AHU-1", "name": "SAT", "page": 1,
         "quote": "SEQUENCE OF OPERATION AHU-1", "confidence": 0.5},
        {"tag": "no quote"},
    ], "notes": "ok"}
    client = FakeOpenAI(_rejected(), {**_completion("```json\n" + json.dumps(reply) + "\n```")})
    items, info = spec_llm.extract_pdf(data, "M-601.pdf", client)
    first, second = client.requests
    assert first["response_format"]["type"] == "json_schema" and second["response_format"] == {"type": "json_object"}
    sent = second["messages"][1]["content"]
    assert "=== PAGE 2 ===" in sent and "SEQUENCE OF OPERATION" in sent
    assert "EF-1" not in sent and "=== PAGE 1 ===\nMechanical schedules" in sent  # table rows aren't re-sent
    assert "EF-1" in second["messages"][0]["content"]  # ...the model is told they're done
    by = {i["tag"]: i for i in items}
    assert "B-9" not in by and info["dropped"] == 1 and "B-9" in info["notes"]  # invented quote dropped
    assert by["EF-1"]["source"]["method"] == "table"  # table item kept over the model's duplicate
    soo = by["AHU-1 SOO"]
    assert soo["kind"] == "sequence" and soo["source"]["quote_verified"] is True and soo["confidence"] == 0.8
    assert soo["source"]["method"] == "llm" and soo["source"]["model"] == "m-served" and soo["properties"] == {"SAT setpoint": "55 degF"}
    assert by["SAT"]["source"]["page"] == 2 and by["SAT"]["source"]["cited_page"] == 1  # wrong page corrected
    assert info["provider"] == "openai" and info["usage"] == {"input_tokens": 7, "output_tokens": 3}


def test_openai_extraction_chunks_and_survives_a_bad_chunk(monkeypatch):
    data = make_pdf()
    monkeypatch.setenv("BALER_LLM_MAX_CHARS", "10")  # one page per request
    client = FakeOpenAI(_completion("not json at all"), _completion('{"items": []}'))
    items, info = spec_llm.extract_pdf(data, "M.pdf", client)
    assert len(client.requests) == 2 and "page 1: reply wasn't JSON" in info["notes"]
    assert {i["tag"] for i in items} == {"EF-1", "AHU-1", "Supply Air Temperature"}


def test_openai_extraction_text_layer_required():
    pytest.importorskip("pdfplumber")
    with pytest.raises(spec_llm.ExtractionError):
        spec_llm.extract_pdf(b"%PDF-1.4 x", "x.pdf", FakeOpenAI())


def test_parse_json_defensively():
    assert spec_llm.parse_json('Sure! Here it is: {"items": []} hope that helps') == {"items": []}
    assert spec_llm.parse_json('[{"tag": "A"}]') == [{"tag": "A"}]
    with pytest.raises(ValueError):
        spec_llm.parse_json("no")


def test_anthropic_extraction_marks_unverified_quotes(monkeypatch):
    pytest.importorskip("pydantic")
    from test_commissioning_spec import _fake_client

    data = make_pdf()
    client, calls = _fake_client([
        {"kind": "sequence", "tag": "AHU-1 SOO", "name": "SAT control", "page": 2,
         "quote": "setpoint shall be 55 degF", "confidence": 0.9},
        {"kind": "equipment", "tag": "B-9", "name": "Boiler", "page": 2, "quote": "B-9 boiler", "confidence": 0.9},
    ])
    items, info = spec_llm.extract_pdf(data, "M.pdf", client)
    by = {i["tag"]: i for i in items}
    assert by["AHU-1 SOO"]["source"]["quote_verified"] is True and by["B-9"]["source"]["quote_verified"] is False
    assert by["EF-1"]["source"]["method"] == "table" and "EF-1" in calls[0]["messages"][0]["content"][1]["text"]
    assert info["provider"] == "anthropic"


# ---------------------------------------------------------------- agent


def _call(i, name, **args):
    return {"id": f"call{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_openai_tools_translation():
    tools = agent.openai_tools()
    assert [t["function"]["name"] for t in tools] == [t["name"] for t in agent.TOOLS]
    prop = next(t for t in tools if t["function"]["name"] == "propose")["function"]["parameters"]
    assert prop["required"] == ["kind", "payload", "reason"] and "additionalProperties" not in json.dumps(prop)


def test_openai_agent_loop_runs_tools_and_proposes():
    repo = ta._repo()
    ctx = ta._ctx(repo)
    client = FakeOpenAI(
        _completion("Looking.", [_call(1, "inventory_summary"), _call(2, "list_runs")], "tool_calls"),
        _completion(None, [_call(3, "propose", kind="issue", payload={"title": "RAT stale", "equip_uri": fx.AHU},
                                 reason="exists failed")], "tool_calls"),
        _completion("I proposed one issue."),
    )
    messages, reply = agent.run_turn(ctx, [], "What next?", client)
    assert reply == "I proposed one issue."
    req = client.requests[0]
    assert req["messages"][0] == {"role": "system", "content": agent.SYSTEM} and req["tools"] == agent.openai_tools()
    tool_msgs = [m for m in messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["call1", "call2", "call3"]
    assert "system" not in [m["role"] for m in messages]  # not stored: prepended each call
    assert len(repo.find_rows("cx_agent_proposals", project_id="p1")) == 1
    acts = [e["action"] for e in repo.list_events("p1")]
    assert {"agent.inventory_summary", "agent.list_runs", "agent.propose"} <= set(acts)
    client2 = FakeOpenAI(_completion("ok"))
    agent.run_turn(ctx, messages, "thanks", client2)
    assert client2.requests[0]["messages"][1: len(messages) + 1] == messages  # replayed unchanged


def test_openai_agent_bad_arguments_are_errors_to_the_model():
    repo = ta._repo()
    ctx = ta._ctx(repo)
    bad = {"id": "c1", "type": "function", "function": {"name": "equipment_points", "arguments": "{oops"}}
    client = FakeOpenAI(_completion(None, [bad], "tool_calls"), _completion("sorry"))
    messages, _ = agent.run_turn(ctx, [], "x", client)
    assert "error" in json.loads(messages[2]["content"])


def test_openai_agent_model_without_tools_is_a_clear_error():
    repo = ta._repo()
    ctx = ta._ctx(repo)
    client = FakeOpenAI(_rejected(400, "registry.ollama.ai/library/gemma:2b does not support tools"))
    with pytest.raises(llm.ToolCallsUnsupported, match="can't call tools"):
        agent.run_turn(ctx, [], "x", client)
    client = FakeOpenAI(_completion('{"name": "inventory_summary", "arguments": {}}'))
    with pytest.raises(llm.ToolCallsUnsupported):
        agent.run_turn(ctx, [], "x", client)


def test_agent_refuses_to_mix_providers():
    repo = ta._repo()
    ctx = ta._ctx(repo)
    anthropic_history = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    with pytest.raises(llm.LLMError, match="new conversation"):
        agent.run_turn(ctx, anthropic_history, "x", FakeOpenAI())
    with pytest.raises(llm.LLMError, match="agent needs a model"):
        agent.run_turn(ctx, [], "x")  # provider none


# ------------------------------------------------------------------ api


def _serve(monkeypatch, agent_client=None):
    from http.server import ThreadingHTTPServer
    from threading import Thread

    from baler.commissioning_api import make_handler

    repo = ta._repo()
    monkeypatch.setenv("BALER_GATEWAY_SECRET", api.GATEWAY)
    monkeypatch.setenv("BALER_JWT_SECRET", api.JWT)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(repo, clock=lambda: fx.NOW, agent_client=agent_client))
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_api_openai_agent_transcript(monkeypatch):
    client = FakeOpenAI(
        _completion(None, [_call(1, "run_results", run_id="nope")], "tool_calls"),
        _completion("That run doesn't exist."),
    )
    server, base = _serve(monkeypatch, client)
    try:
        p = api._project(base)
        r = requests.post(f"{base}/projects/{p['id']}/agent/sessions", json={"message": "results?"}, headers=api.A)
        assert r.status_code == 202, r.text
        deadline = time.monotonic() + 10
        while (s := requests.get(f"{base}/agent-sessions/{r.json()['id']}", headers=api.A).json())["status"] == "working":
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert s["status"] == "idle", s
        assert s["transcript"][0] == {"role": "user", "text": "results?"}
        assert s["transcript"][1] == {"role": "tool", "tool": "run_results", "input": {"run_id": "nope"}}
        assert s["transcript"][2]["role"] == "tool" and "error" in s["transcript"][2]
        assert s["transcript"][-1] == {"role": "assistant", "text": "That run doesn't exist."}
    finally:
        server.shutdown()


def test_api_without_a_model(monkeypatch):
    pytest.importorskip("pdfplumber")
    import base64

    server, base = _serve(monkeypatch)
    try:
        p = api._project(base)
        r = requests.post(f"{base}/projects/{p['id']}/agent/sessions", json={"message": "hi"}, headers=api.A)
        assert r.status_code == 501 and "needs a model" in r.json()["error"]
        pdf = base64.b64encode(make_pdf()).decode()
        r = requests.post(f"{base}/projects/{p['id']}/spec/extract", json={"data": pdf, "filename": "M.pdf"}, headers=api.A)
        assert r.status_code == 202 and r.json()["method"] == "table"
        deadline = time.monotonic() + 10
        while (d := requests.get(f"{base}/spec-documents/{r.json()['id']}", headers=api.A).json())["status"] in ("queued", "running"):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert d["status"] == "completed" and d["items"] == 3, d
    finally:
        server.shutdown()


def test_openai_agent_replays_provider_extras_on_tool_calls():
    """Gemini's thinking models sign each tool call (extra_content.google.
    thought_signature) and 400 the next request if the replayed history
    drops it - found running the agent against Gemini's free tier."""
    repo = ta._repo()
    ctx = ta._ctx(repo)
    signed = _call(1, "inventory_summary")
    signed["extra_content"] = {"google": {"thought_signature": "sig-abc"}}
    client = FakeOpenAI(_completion(None, [signed], "tool_calls"), _completion("done"))
    agent.run_turn(ctx, [], "What's here?", client)
    replayed = [m for m in client.requests[1]["messages"] if m.get("tool_calls")]
    assert replayed[0]["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "sig-abc"}}

"""Live check of the non-Anthropic LLM path (Cx 7/10) against a real
OpenAI-compatible endpoint - by default Google Gemini's free tier.

Skipped unless BALER_LIVE_LLM_API_KEY is set (CI passes the repo's
GEMINI_API_KEY secret). Costs nothing on the free tier. It proves the
plumbing - a PDF's equipment schedule comes back as page-cited, verified
proposals, and the agent calls tools and proposes - not model quality.
"""

import os

import pytest

KEY = os.environ.get("BALER_LIVE_LLM_API_KEY")
pytestmark = pytest.mark.skipif(not KEY, reason="BALER_LIVE_LLM_API_KEY not set (live LLM check)")


@pytest.fixture
def live_env(monkeypatch):
    monkeypatch.setenv("BALER_LLM_PROVIDER", "openai")
    monkeypatch.setenv("BALER_LLM_BASE_URL",
                       os.environ.get("BALER_LIVE_LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"))
    monkeypatch.setenv("BALER_LLM_API_KEY", KEY or "")
    monkeypatch.setenv("BALER_LLM_MODEL", os.environ.get("BALER_LIVE_LLM_MODEL", "gemini-flash-lite-latest"))
    monkeypatch.setenv("BALER_JWT_SECRET", "x")


def _spec_pdf() -> bytes:
    import io

    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Table

    buf = io.BytesIO()
    s = getSampleStyleSheet()
    SimpleDocTemplate(buf, pagesize=letter).build([
        Paragraph("Section 23 09 93: Sequence of Operations", s["Title"]),
        Paragraph("The building is served by two air handling units.", s["BodyText"]),
        PageBreak(),
        Paragraph("AIR HANDLING UNIT SCHEDULE", s["Heading2"]),
        Table([["Tag", "Serves", "Supply CFM"], ["AHU-1", "Clinic wing", "12,000"], ["AHU-2", "Admin", "8,500"]]),
        PageBreak(),
        Paragraph("The supply air temperature setpoint shall be 55 degF, reset to 65 degF on zone demand.", s["BodyText"]),
    ])
    return buf.getvalue()


def test_live_spec_extraction(live_env):
    from baler.commissioning import spec_llm

    items, meta = spec_llm.extract_pdf(_spec_pdf(), "live.pdf")
    tags = {i.get("tag") for i in items if i.get("kind") == "equipment"}
    assert {"AHU-1", "AHU-2"} <= tags, items
    assert all((i.get("source") or {}).get("quote_verified") for i in items if (i.get("source") or {}).get("method") == "llm")
    assert meta["provider"] == "openai"


def test_live_agent_turn(live_env):
    import test_commissioning_agent as t

    from baler.commissioning import agent

    repo = t._repo()
    ctx = t._ctx(repo)
    messages, reply = agent.run_turn(ctx, [], "What equipment is in this project? Use your tools.")
    called = [c["function"]["name"] for m in messages for c in (m.get("tool_calls") or [])]
    assert called, "the model called no tools"
    assert reply.strip()
    assert any(e["action"].startswith("agent.") for e in repo.list_events("p1"))

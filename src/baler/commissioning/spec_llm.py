"""
PDF spec extraction via the Claude API (Cx 7, Baler#43) - the `llm` extra
(anthropic, pydantic), imported only when a project extracts a PDF, the
same arrangement as llm_classifier. Everything extracted is a proposal
(`proposed` spec items) a human accepts, corrects or rejects; each cites
the document, page and verbatim text it was read from.
"""

from __future__ import annotations

import base64
import os

from pydantic import BaseModel, Field

from baler import brick_vocab
from baler.commissioning.spec import KINDS, MAX_PDF_BYTES, brick_class_for

# The Claude model for PDF extraction. Override per deployment with
# BALER_CX_MODEL (.env.example).
DEFAULT_MODEL = "claude-opus-5-5"


class _Property(BaseModel):
    name: str
    value: str


class _Item(BaseModel):
    kind: str = Field(description="equipment, point or sequence")
    tag: str = Field(description="Equipment tag as printed (e.g. AHU-1), or the point's name for a point, or a short title for a sequence")
    name: str = Field(description="Descriptive name or type as printed")
    equipment_tag: str | None = Field(default=None, description="For a point or sequence: the tag of the equipment it belongs to")
    brick_class: str | None = Field(default=None, description="Best Brick class (e.g. Air_Handling_Unit, Supply_Air_Temperature_Sensor), or null")
    properties: list[_Property] = Field(default_factory=list, description="Schedule columns as printed: capacity, airflow, model, units, ...")
    page: int = Field(description="1-based PDF page the item was read from")
    quote: str = Field(description="The exact text on that page the item was read from, verbatim, under 300 characters")
    confidence: float = Field(description="0-1: how sure the reading is (illegible, ambiguous or inferred -> low)")


class _Extraction(BaseModel):
    items: list[_Item]
    notes: str = Field(default="", description="Anything the reader should know: illegible areas, pages skipped, ambiguities")


PROMPT = """This PDF is part of the design documents for a building's HVAC and controls (equipment schedules,
points lists, sequences of operation, drawings). Extract what a commissioning agent needs:

- every piece of HVAC equipment in a schedule or list (kind "equipment"): its tag exactly as printed,
  its type/name, and the schedule's columns as properties;
- every control point in a points list (kind "point"), with the tag of its equipment;
- each sequence of operation (kind "sequence"), titled, with the equipment it applies to, its key
  setpoints and limits as properties.

Rules:
- Only what the document actually says. Never invent a tag, a value or a point that isn't printed.
- Every item cites the 1-based page and quotes the text it came from, verbatim.
- If a value is illegible or ambiguous, still list the item but lower its confidence and say why in notes.
- brick_class is your best Brick Schema class name for the item (null if unsure)."""


class ExtractionError(Exception):
    pass


def model_name() -> str:
    return os.environ.get("BALER_CX_MODEL") or DEFAULT_MODEL


def extract_pdf(data: bytes, filename: str, client=None) -> tuple[list[dict], dict]:
    """(proposed spec items without ids, info {model, notes, usage}).
    Raises ExtractionError on a refusal, truncation or unparseable reply."""

    if len(data) > MAX_PDF_BYTES:
        raise ExtractionError(f"PDF is over {MAX_PDF_BYTES // (1024 * 1024)} MB; split it")
    if client is None:
        import anthropic

        client = anthropic.Anthropic()
    model = model_name()
    response = client.beta.messages.parse(
        model=model,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "high"},
        output_format=_Extraction,
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                            "data": base64.b64encode(data).decode("ascii")}, "title": filename[:200]},
            {"type": "text", "text": PROMPT},
        ]}],
    )
    if response.stop_reason == "refusal":
        raise ExtractionError("the model declined to read this document")
    if response.stop_reason == "max_tokens":
        raise ExtractionError("the document produced more items than one extraction holds; split the PDF and extract the parts")
    parsed = response.parsed_output
    if parsed is None:
        raise ExtractionError("the model's reply could not be parsed")
    vocab = brick_vocab.load()
    items = []
    for it in parsed.items:
        kind = it.kind if it.kind in KINDS else "equipment"
        cls = vocab.canonical_class(it.brick_class) if it.brick_class else None
        if kind == "equipment" and cls is None:
            cls, _ = brick_class_for(it.name, it.tag)
        items.append({
            "kind": kind, "tag": it.tag[:200], "name": it.name[:500], "equip_tag": it.equipment_tag,
            "brick_class": cls, "properties": {p.name[:100]: p.value[:500] for p in it.properties[:50]},
            "source": {"document": filename, "page": it.page, "quote": it.quote[:500], "method": "llm", "model": response.model},
            "confidence": max(0.0, min(1.0, float(it.confidence))),
        })
    usage = getattr(response, "usage", None)
    return items, {"model": response.model, "notes": parsed.notes[:4000],
                   "usage": {"input_tokens": getattr(usage, "input_tokens", None), "output_tokens": getattr(usage, "output_tokens", None)}}

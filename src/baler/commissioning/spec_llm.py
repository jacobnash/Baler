"""
PDF spec extraction (Cx 7, Baler#43). Everything extracted is a proposal
(`proposed` spec items) a human accepts, corrects or rejects; each cites
the document, page and verbatim text it was read from.

The provider is llm.provider() (BALER_LLM_PROVIDER):

  - none: tables only, locally (spec_pdf) - offline, free.
  - anthropic: the Claude API reads the PDF itself (the `llm` extra,
    anthropic + pydantic, imported only here, like llm_classifier).
  - openai: any OpenAI-compatible endpoint. Many free models take neither
    PDFs nor images, so the PDF's text is extracted locally page by page
    and sent with page markers; each item's quote must then really be on
    a page, or the item is dropped (weaker models invent quotes).

For every provider the local table pass runs first; a model is told what
the tables already found, and on `openai` only sees the text outside them.
"""

from __future__ import annotations

import base64
import os

from baler import brick_vocab
from baler.commissioning import llm, spec_pdf
from baler.commissioning.llm import parse_json
from baler.commissioning.spec import KINDS, MAX_PDF_BYTES, brick_class_for

try:  # the `llm` extra; only the anthropic provider needs it
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover - exercised on servers without the extra
    BaseModel = None  # type: ignore[assignment,misc]

# The Claude model for PDF extraction. Override per deployment with
# BALER_CX_MODEL (.env.example).
DEFAULT_MODEL = llm.DEFAULT_ANTHROPIC_MODEL

if BaseModel is not None:

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
    return llm.anthropic_model()


def extract_pdf(data: bytes, filename: str, client=None) -> tuple[list[dict], dict]:
    """(proposed spec items without ids, info {model, provider, notes, usage}).
    Raises ExtractionError on a refusal, truncation or unparseable reply.
    `client` (tests): an anthropic.Anthropic-like object, or an
    llm.OpenAIChat-like one (kind == "openai")."""

    if len(data) > MAX_PDF_BYTES:
        raise ExtractionError(f"PDF is over {MAX_PDF_BYTES // (1024 * 1024)} MB; split it")
    if client is not None:
        provider = getattr(client, "kind", "anthropic")
    else:
        try:
            provider = llm.provider()
        except llm.LLMError as exc:
            raise ExtractionError(str(exc)) from exc
    if provider == "none":
        try:
            return spec_pdf.extract(data, filename)
        except (ValueError, ImportError) as exc:
            raise ExtractionError(str(exc)) from exc
    pages, why = spec_pdf.safe_read(data, filename)
    tables = spec_pdf.merge([], [it for p in pages for it in p.items])
    if provider == "openai":
        items, info = _extract_openai(pages, why, tables, filename, client)
    else:
        items, info = _extract_anthropic(data, pages, tables, filename, client)
    if tables:
        info["notes"] = (f"{len(tables)} item(s) read from tables without a model. " + info.get("notes", "")).strip()
    return items, info


# ------------------------------------------------------------- common


def _item(raw: dict, filename: str, model: str, vocab) -> dict | None:
    """A spec item from a model's item dict (tolerant of weaker models:
    properties as a dict or a list, numbers as strings)."""
    if not isinstance(raw, dict):
        return None
    tag = str(raw.get("tag") or "").strip()
    quote = str(raw.get("quote") or "").strip()
    if not tag or not quote:
        return None
    try:
        page = int(raw.get("page"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    kind = raw.get("kind") if raw.get("kind") in KINDS else "equipment"
    name = str(raw.get("name") or tag).strip()
    brick = raw.get("brick_class") if isinstance(raw.get("brick_class"), str) else None
    cls = vocab.canonical_class(brick) if brick else None
    if kind == "equipment" and cls is None:
        cls, _ = brick_class_for(name, tag)
    props = raw.get("properties") or {}
    if isinstance(props, list):
        props = {str(p.get("name")): p.get("value") for p in props if isinstance(p, dict) and p.get("name")}
    if not isinstance(props, dict):
        props = {}
    try:
        conf = max(0.0, min(1.0, float(raw.get("confidence", 0.5))))
    except (TypeError, ValueError):
        conf = 0.5
    equip = raw.get("equipment_tag") or raw.get("equip_tag")
    return {
        "kind": kind, "tag": tag[:200], "name": name[:500], "equip_tag": str(equip)[:200] if equip else None,
        "brick_class": cls, "properties": {str(k)[:100]: str(v)[:500] for k, v in list(props.items())[:50] if v is not None},
        "source": {"document": filename, "page": page, "quote": quote[:500], "method": "llm", "model": model},
        "confidence": conf,
    }


def _already(tables: list[dict]) -> str:
    if not tables:
        return ""
    tags = sorted({f"{t['kind']} {t['tag']}" + (f" ({t['equip_tag']})" if t.get("equip_tag") else "") for t in tables})
    shown = ", ".join(tags[:300])
    return ("\n\nThese were already read from the document's tables; don't list them again, only what they miss "
            f"(sequences, prose, tables that weren't recognised): {shown}")


# ---------------------------------------------------------- anthropic


def _extract_anthropic(data: bytes, pages: list, tables: list[dict], filename: str, client) -> tuple[list[dict], dict]:
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
            {"type": "text", "text": PROMPT + _already(tables)},
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
    found = [i for i in (_item(it.model_dump(), filename, response.model, vocab) for it in parsed.items) if i]
    has_text = any(p.text.strip() for p in pages)
    unverified = 0
    for it in found:  # Claude reads the page images too, so mark rather than drop
        if has_text:
            ok = spec_pdf.verify(it, pages) != "missing"
            it["source"]["quote_verified"] = ok
            unverified += not ok
    usage = getattr(response, "usage", None)
    notes = parsed.notes[:4000]
    if unverified:
        notes += f" {unverified} item(s)' quotes weren't found in the PDF's text layer (marked quote_verified=false)."
    return spec_pdf.merge(tables, found), {
        "model": response.model, "provider": "anthropic", "notes": notes.strip(),
        "usage": {"input_tokens": getattr(usage, "input_tokens", None), "output_tokens": getattr(usage, "output_tokens", None)}}


# ------------------------------------------------------------- openai

OPENAI_PROMPT = PROMPT + """

The document's text follows, page by page, each page after a line "=== PAGE n ===". Tables may have lost
their layout. Reply with JSON only, no prose, in exactly this shape:

{"items": [{"kind": "equipment", "tag": "AHU-1", "name": "Air handling unit", "equipment_tag": null,
            "brick_class": "Air_Handling_Unit", "properties": [{"name": "Airflow", "value": "12,000 CFM"}],
            "page": 3, "quote": "AHU-1 ROOFTOP 12,000 CFM", "confidence": 0.9}],
 "notes": ""}

"page" is the number from the PAGE marker the text is under. "quote" is copied character for character
from that page's text (at most 300 characters) - items whose quote isn't on that page are discarded.
If there is nothing to extract, reply {"items": [], "notes": "..."}."""

_PROP = {"type": "object", "properties": {"name": {"type": "string"}, "value": {"type": "string"}}, "required": ["name", "value"]}
ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)}, "tag": {"type": "string"}, "name": {"type": "string"},
        "equipment_tag": {"type": ["string", "null"]}, "brick_class": {"type": ["string", "null"]},
        "properties": {"type": "array", "items": _PROP}, "page": {"type": "integer"}, "quote": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["kind", "tag", "name", "page", "quote", "confidence"],
}
SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": ITEM_SCHEMA}, "notes": {"type": "string"}},
          "required": ["items"]}
RESPONSE_FORMATS: list[dict | None] = [
    {"type": "json_schema", "json_schema": {"name": "spec_extraction", "schema": SCHEMA}},
    {"type": "json_object"},
    None,  # endpoints with neither: the prompt asks for JSON, parse_json digs it out
]
DEFAULT_MAX_CHARS = 12000  # per request: small local models have small contexts
DEFAULT_MAX_TOKENS = 4096


def _chunks(pages: list, limit: int) -> list[list]:
    out: list[list] = []
    cur: list = []
    size = 0
    for p in pages:
        n = len(p.rest)
        if cur and size + n > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(p)
        size += n
    if cur:
        out.append(cur)
    return out


def _call(client, chunk: list, extra: str, max_tokens: int, fmt_index: list[int]) -> tuple[dict, dict]:
    """(parsed reply, response) for one chunk of pages, stepping down
    RESPONSE_FORMATS when the endpoint rejects one (remembered in fmt_index)."""
    text = "\n\n".join(f"=== PAGE {p.number} ===\n{p.rest[: DEFAULT_MAX_CHARS * 4]}" for p in chunk)
    messages = [{"role": "system", "content": OPENAI_PROMPT + extra}, {"role": "user", "content": text}]
    while True:
        fmt = RESPONSE_FORMATS[fmt_index[0]]
        params: dict = {"max_tokens": max_tokens, "temperature": 0}
        if fmt is not None:
            params["response_format"] = fmt
        try:
            resp = client.chat(messages, **params)
            break
        except llm.LLMError as exc:
            if getattr(exc, "status", None) in (400, 404, 415, 422) and fmt_index[0] < len(RESPONSE_FORMATS) - 1:
                fmt_index[0] += 1
                continue
            raise
    choice = resp["choices"][0]
    if choice.get("finish_reason") == "length":
        raise ExtractionError("reply cut off (lower BALER_LLM_MAX_CHARS or raise BALER_LLM_MAX_TOKENS)")
    content = (choice.get("message") or {}).get("content") or ""
    try:
        parsed = parse_json(content)
    except ValueError as exc:
        raise ExtractionError(f"reply wasn't JSON: {content[:200]!r}") from exc
    if isinstance(parsed, list):
        parsed = {"items": parsed}
    if not isinstance(parsed, dict) or not isinstance(parsed.get("items", []), list):
        raise ExtractionError("reply JSON has no items list")
    return parsed, resp


def _extract_openai(pages: list, why: str | None, tables: list[dict], filename: str, client) -> tuple[list[dict], dict]:
    if not pages:
        raise ExtractionError(f"the PDF's text couldn't be read locally ({why}); OpenAI-compatible models get text only")
    if not any(p.text.strip() for p in pages):
        raise ExtractionError("the PDF has no text layer (scanned?); OpenAI-compatible models get text only - "
                              "use BALER_LLM_PROVIDER=anthropic, or OCR the PDF first")
    if client is None:
        try:
            client = llm.OpenAIChat()
        except llm.LLMError as exc:
            raise ExtractionError(str(exc)) from exc
    model = client.model
    todo = [p for p in pages if p.rest.strip()]
    limit = int(os.environ.get("BALER_LLM_MAX_CHARS") or DEFAULT_MAX_CHARS)
    max_tokens = int(os.environ.get("BALER_LLM_MAX_TOKENS") or DEFAULT_MAX_TOKENS)
    vocab = brick_vocab.load()
    extra = _already(tables)
    fmt_index = [0]
    found: list[dict] = []
    notes: list[str] = []
    failed: list[str] = []
    dropped: list[str] = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    for chunk in _chunks(todo, limit):
        span = f"page {chunk[0].number}" + (f"-{chunk[-1].number}" if len(chunk) > 1 else "")
        try:
            parsed, resp = _call(client, chunk, extra, max_tokens, fmt_index)
        except ExtractionError as exc:
            failed.append(f"{span}: {exc}")
            continue
        model = resp.get("model") or model
        u = resp.get("usage") or {}
        usage["input_tokens"] += int(u.get("prompt_tokens") or 0)
        usage["output_tokens"] += int(u.get("completion_tokens") or 0)
        if parsed.get("notes"):
            notes.append(str(parsed["notes"])[:1000])
        for raw in parsed.get("items") or []:
            it = _item(raw, filename, model, vocab)
            if it is None:
                continue
            verdict = spec_pdf.verify(it, pages)
            if verdict == "missing":
                dropped.append(f"{it['kind']} {it['tag']} (p{it['source']['page']})")
                continue
            it["source"]["quote_verified"] = True
            if verdict == "moved":
                notes.append(f"{it['tag']}: quote found on page {it['source']['page']}, not the cited {it['source']['cited_page']}")
            found.append(it)
    if todo and len(failed) == len(_chunks(todo, limit)) and not tables:
        raise ExtractionError("every request failed: " + "; ".join(failed)[:1500])
    if dropped:
        notes.append(f"dropped {len(dropped)} item(s) whose quote isn't in the PDF: " + ", ".join(dropped[:30]))
    if failed:
        notes.append("not read: " + "; ".join(failed))
    if not todo:
        notes.append("the tables covered every page; no model call was needed")
    return spec_pdf.merge(tables, found), {"model": model, "provider": "openai", "notes": " ".join(notes)[:4000],
                                           "usage": usage, "dropped": len(dropped)}

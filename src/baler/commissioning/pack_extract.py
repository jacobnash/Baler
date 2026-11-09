"""
Requirement rows extracted from a document the user holds, as proposals
(Baler#206, epic #221 §3.1-3.2).

A user uploads their own copy of a standard edition, an owner design guide
or a local code, and a model proposes requirement rows for a draft pack.
This is how packs grow without code changes. It is Cx 7's pattern:

- every proposal cites the page and the verbatim text it was read from,
  and the quote must really be on that page of the user's PDF, or the
  proposal is dropped (models invent quotes);
- nothing is applied by the extraction: a person accepts, corrects or
  rejects each proposal, and an accepted row lands in the draft pack
  unconfirmed, so its numbers are confirmed by a person too (#205);
- the quote stays on the proposal, in that org's own database, as
  provenance. It is never copied into the pack row (rows carry citations
  only) and Baler never ships it. The PDF itself isn't kept: only its
  sha256, name and size (keeping the file is the document store's job,
  #201).

The model reads the PDF's text layer, page by page, with page markers
(the same local read as Cx 7's OpenAI-compatible path), for both
providers, so every quote can be checked. A scanned PDF needs OCR first.
BALER_LLM_PROVIDER picks the model; `none` can't extract requirements.
"""

from __future__ import annotations

import os
import re

from baler.commissioning import llm, packs, spec_pdf
from baler.commissioning.llm import parse_json

MAX_QUOTE = 300
MAX_CHARS = 12000
NUMERIC = ("pressure_to_adjacent", "total_ach", "outdoor_ach", "temperature", "relative_humidity", "airflow")

PROMPT = """You read a building standard, design guide or code that the user owns, and list its
measurable HVAC requirements as rows for a requirement pack. For each requirement:

- id: a short slug you make up (lowercase, digits, -), e.g. or-rh
- title: a short description in YOUR OWN WORDS (never copy the document's sentences), under 120 characters
- ref: where it is in the document: the table, section or clause number as printed
- page: the 1-based page number from the === PAGE n === marker
- quote: the exact text on that page the requirement is read from, verbatim, under 300 characters
- space_type: list of space-type slugs it applies to (e.g. operating_room, airborne_isolation_room), or []
- equip_class: list of Brick equipment classes it applies to (e.g. Air_Handling_Unit), or []
- quantity: one of %s
- op: one of >=, <=, >, <, between, ==
- value, or min and max for "between": numbers exactly as printed; null if not printed
- unit: as printed, normalised to one of: inH2O, Pa, 1/h, degF, degC, %%RH, cfm, L/s, MERV
- confidence: 0-1

Only requirements the text actually states. Never invent a number. If a cell is ambiguous, still
list it with low confidence. Reply with JSON only: {"items": [...], "notes": "..."}"""


class ExtractionError(Exception):
    pass


def chat_fn(client=None):
    """fn(system, text) -> reply text for the configured provider.
    `client` (tests): anything with .chat(messages, **params) -> OpenAI-style dict."""
    if client is not None:
        return lambda system, text: _openai_reply(client, system, text)
    try:
        p = llm.provider()
    except llm.LLMError as exc:
        raise ExtractionError(str(exc)) from exc
    if p == "none":
        raise ExtractionError("extracting requirements needs a model: set BALER_LLM_PROVIDER (see .env.example)")
    if p == "openai":
        try:
            c = llm.OpenAIChat()
        except llm.LLMError as exc:
            raise ExtractionError(str(exc)) from exc
        return lambda system, text: _openai_reply(c, system, text)
    import anthropic

    a = anthropic.Anthropic()

    def call(system: str, text: str) -> str:
        r = a.messages.create(model=llm.anthropic_model(), max_tokens=8000, system=system,
                              messages=[{"role": "user", "content": text}])
        if r.stop_reason == "refusal":
            raise ExtractionError("the model declined to read this document")
        return "".join(getattr(b, "text", "") for b in r.content)
    return call


def _openai_reply(client, system: str, text: str) -> str:
    resp = client.chat([{"role": "system", "content": system}, {"role": "user", "content": text}], temperature=0,
                       max_tokens=int(os.environ.get("BALER_LLM_MAX_TOKENS") or 4096))
    choice = resp["choices"][0]
    if choice.get("finish_reason") == "length":
        raise ExtractionError("reply cut off (lower BALER_LLM_MAX_CHARS or raise BALER_LLM_MAX_TOKENS)")
    return (choice.get("message") or {}).get("content") or ""


def _chunks(pages: list, limit: int) -> list[list]:
    out: list[list] = []
    cur: list = []
    size = 0
    for p in pages:
        if cur and size + len(p.text) > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(p)
        size += len(p.text)
    if cur:
        out.append(cur)
    return out


def _slug(v, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9_.-]+", "-", str(v or "").lower()).strip("-.")[:60]
    return s or fallback


def to_row(raw: dict, source: dict, n: int) -> dict:
    """A pack row from a model item: citation ref + page only (the quote
    stays on the proposal), values as read, check defaulted by quantity.
    Raises ValueError when the item can't be a valid row."""
    quantity = raw.get("quantity")
    if quantity not in NUMERIC:
        raise ValueError(f"no engine judges {quantity!r} from data yet")
    op = raw.get("op") if raw.get("op") in packs.OPS else None
    crit: dict = {"quantity": quantity, "op": op, "unit": raw.get("unit") or None}
    if op == "between":
        crit.update(min=raw.get("min"), max=raw.get("max"))
    else:
        crit["value"] = raw.get("value")
    row = {"id": _slug(raw.get("id"), f"row-{n}"), "title": str(raw.get("title") or quantity)[:packs.MAX_TITLE],
           "citation": {"ref": str(raw.get("ref") or f"page {source['page']}")[:120], "page": source["page"]},
           "applies_to": {"space_type": [s for s in raw.get("space_type") or [] if isinstance(s, str)],
                          "equip_class": [c for c in raw.get("equip_class") or [] if isinstance(c, str)]},
           "criterion": crit,
           "evidence": {"point_class": packs.QUANTITIES[quantity]["point_class"], "window_hours": 24, "coverage": 0.95},
           "check": {"engine": "procedure", "criterion": "range"}}
    return packs.normalize_row(row, f"proposal {n}", shacl_ids=set())


def extract(pages: list, filename: str, chat) -> tuple[list[dict], dict]:
    """([{row, source, confidence}], info). `pages` are spec_pdf.Page."""
    if not any(p.text.strip() for p in pages):
        raise ExtractionError("the PDF has no text layer (scanned?): OCR it first")
    limit = int(os.environ.get("BALER_LLM_MAX_CHARS") or MAX_CHARS)
    system = PROMPT % ", ".join(NUMERIC)
    found: list[dict] = []
    notes: list[str] = []
    dropped: list[str] = []
    failed: list[str] = []
    seen: set[str] = set()
    for chunk in _chunks([p for p in pages if p.text.strip()], limit):
        text = "\n\n".join(f"=== PAGE {p.number} ===\n{p.text[: limit * 2]}" for p in chunk)
        span = f"page {chunk[0].number}" + (f"-{chunk[-1].number}" if len(chunk) > 1 else "")
        try:
            parsed = parse_json(chat(system, text))
        except (ValueError, llm.LLMError, ExtractionError) as exc:
            failed.append(f"{span}: {exc}")
            continue
        if isinstance(parsed, list):
            parsed = {"items": parsed}
        if parsed.get("notes"):
            notes.append(str(parsed["notes"])[:500])
        for raw in parsed.get("items") or []:
            if not isinstance(raw, dict):
                continue
            quote = str(raw.get("quote") or "")[:MAX_QUOTE]
            try:
                page = int(raw.get("page") or chunk[0].number)
            except (TypeError, ValueError):
                page = chunk[0].number
            src = {"document": filename, "page": page, "quote": quote}
            verdict = spec_pdf.verify({"source": src}, pages)
            if verdict == "missing" or not quote.strip():
                dropped.append(f"{raw.get('id') or raw.get('title')} (p{page}): quote not in the PDF")
                continue
            try:
                row = to_row(raw, src, len(found) + 1)
            except (ValueError, TypeError) as exc:
                dropped.append(f"{raw.get('id') or raw.get('title')} (p{src['page']}): {exc}")
                continue
            if row["id"] in seen:
                row["id"] = f"{row['id']}-{len(found) + 1}"
            seen.add(row["id"])
            conf = raw.get("confidence")
            found.append({"row": row, "source": {**src, "quote_verified": True},
                          "confidence": float(conf) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else None})
    if failed and not found and len(failed) == len(_chunks([p for p in pages if p.text.strip()], limit)):
        raise ExtractionError("every request failed: " + "; ".join(failed)[:1500])
    if dropped:
        notes.append(f"dropped {len(dropped)}: " + "; ".join(dropped[:30]))
    if failed:
        notes.append("not read: " + "; ".join(failed))
    return found, {"notes": " ".join(notes)[:4000], "dropped": len(dropped), "pages": len(pages)}

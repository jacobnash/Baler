"""
Facts from O&M manuals and submittals, as proposals (#202, design §5).

The Cx 7 pattern (commissioning.spec_llm), applied to the document store:
read a stored document's pages, propose facts, and cite for each the page
and the verbatim text it came from. A person confirms each one (optionally
correcting it) or rejects it. **Nothing is applied on its own**: a
confirmed warranty term is not a warranty until someone records it on an
asset.

Targets (KINDS):

  - warranty: duration, what it covers, when it starts;
  - pm_task: a maintenance task and its interval (and parts, e.g. a filter
    size);
  - nameplate: manufacturer, model, serial;
  - startup_checklist: a start-up checklist's steps (a candidate Cx 6
    procedure);
  - tab_measurement: a TAB report's design and measured value per space.

Providers (commissioning.llm.provider(), BALER_LLM_PROVIDER):

  - none: local rules only (regular expressions over page text). Offline
    and free; it finds the plainly worded warranty durations, PM intervals,
    filter sizes, model and serial numbers and numbered checklists. It
    doesn't read TAB tables.
  - anthropic: Claude reads the page text (structured output, the same
    call shape and model setting as Cx 7: BALER_CX_MODEL, else
    BALER_LLM_MODEL, else the repo default).
  - openai: any OpenAI-compatible endpoint, page text in chunks.

The rules always run first. Every proposal's quote must really be on the
page it cites (spec_pdf.quote_on): a model's proposal whose quote isn't
there is dropped and counted, since weaker models invent quotes. With no
model configured, or the model failing, extraction still returns the rules'
proposals and says why the model didn't run.

Schema: migration 0017.
"""

from __future__ import annotations

import os
import re
from typing import Any

from baler.commissioning import llm
from baler.commissioning.spec_pdf import quote_on
from baler.lifecycle import documents
from baler.lifecycle import rows as rows_mod
from baler.lifecycle.routes import HTTPError, Request, route, text

rows_mod.register("lc_doc_extractions", ("id", "org_id", "document_id", "provider", "model", "status", "proposals",
                                         "info", "error", "created_by", "created_at", "finished_at"), ("info",))
rows_mod.register("lc_doc_proposals", ("id", "org_id", "document_id", "extraction_id", "kind", "payload", "page",
                                       "quote", "quote_verified", "confidence", "method", "model", "status",
                                       "decided_payload", "decided_by", "decided_at", "note", "created_at"),
                  ("payload", "decided_payload"))

KINDS = ("warranty", "pm_task", "nameplate", "startup_checklist", "tab_measurement")
MAX_QUOTE = 500
DEFAULT_MAX_CHARS = 12000
DEFAULT_MAX_TOKENS = 4096

# ------------------------------------------------------------------ rules

_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
              "ten": 10, "twelve": 12, "eighteen": 18, "twenty": 20, "twenty-four": 24, "thirty-six": 36}
_NUM = r"(\d{1,3}(?:,\d{3})*|" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + r")"
_SENTENCE = re.compile(r"[^.\n]*(?:\n(?![ \t]*\n)[^.\n]*)*[.\n]?")
_WARRANTY = re.compile(r"warrant\w*[^.]{0,160}?\b" + _NUM + r"\s*(?:\(\s*\d+\s*\)\s*)?(year|month)s?\b", re.IGNORECASE)
_WARRANTY_START = re.compile(r"\bfrom\s+(?:the\s+)?(date\s+of\s+)?(start[- ]?up|shipment|substantial\s+completion|"
                             r"installation|purchase|beneficial\s+use)", re.IGNORECASE)
_PM_VERB = re.compile(r"\b(replace|inspect|lubricat|grease|clean|check|change|test|tighten|drain|flush|calibrat)\w*", re.IGNORECASE)
_INTERVAL = re.compile(r"\b(?:every|each|once\s+every)\s+" + _NUM + r"?\s*(hour|day|week|month|year)s?\b|"
                       r"\b(daily|weekly|monthly|quarterly|semi-?annually|annually|yearly)\b", re.IGNORECASE)
_RUN_HOURS = re.compile(r"\b" + _NUM + r"\s*(?:operating|run(?:ning)?)\s+hours\b", re.IGNORECASE)
_FILTER = re.compile(r"\b(\d{1,2})\s*[xX×]\s*(\d{1,2})\s*[xX×]\s*(\d{1,2})\b")
_MODEL = re.compile(r"\bmodel\s*(?:no\.?|number|#)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-./]{2,40})", re.IGNORECASE)
_SERIAL = re.compile(r"\bserial\s*(?:no\.?|number|#)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-./]{2,40})", re.IGNORECASE)
_MAKER = re.compile(r"\b(?:manufacturer|manufactured\s+by|mfr\.?)\s*[:\-]?\s*([A-Z][A-Za-z0-9&.,' \-]{1,60})", re.IGNORECASE)
_CHECKLIST_HEAD = re.compile(r"(start[- ]?up|pre[- ]?start|commissioning)\s+(check\s*list|procedure)", re.IGNORECASE)
_STEP = re.compile(r"^\s*(?:\d{1,2}[.)]|[-•▪☐□]|\[\s?\])\s+(.{3,200})$")
_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365, "daily": 1, "weekly": 7, "monthly": 30, "quarterly": 91,
         "semiannually": 182, "semi-annually": 182, "annually": 365, "yearly": 365}


def _n(s: str) -> int:
    return _NUM_WORDS.get(s.lower(), 0) if not s[0].isdigit() else int(s.replace(",", ""))


def _sentences(page_text: str):
    for m in _SENTENCE.finditer(page_text):
        s = re.sub(r"\s+", " ", m.group(0)).strip()
        if len(s) > 5:
            yield s


def _prop(kind: str, payload: dict, page: int, quote: str, confidence: float) -> dict:
    return {"kind": kind, "payload": payload, "page": page, "quote": quote[:MAX_QUOTE], "confidence": confidence,
            "method": "rules", "model": None}


def rules_extract(pages: list[dict]) -> list[dict]:
    """Proposals from page text with regular expressions (no model)."""
    out: list[dict] = []
    for p in pages:
        page, body = p["page"], p["text"]
        for s in _sentences(body):
            w = _WARRANTY.search(s)
            if w:
                n = _n(w.group(1))
                months = n * 12 if w.group(2).lower() == "year" else n
                start = _WARRANTY_START.search(s)
                cover = ("parts_and_labor" if re.search(r"parts\s+and\s+labou?r", s, re.IGNORECASE)
                         else "parts" if re.search(r"\bparts\b", s, re.IGNORECASE)
                         else "labor" if re.search(r"\blabou?r\b", s, re.IGNORECASE) else None)
                if months:
                    out.append(_prop("warranty", {"duration_months": months, "covers": cover,
                                                  "starts_from": re.sub(r"\s+", " ", start.group(2)).lower() if start else None},
                                     page, s, 0.6))
                continue
            verb = _PM_VERB.search(s)
            iv, rh = _INTERVAL.search(s), _RUN_HOURS.search(s)
            if verb and (iv or rh):
                payload: dict[str, Any] = {"title": s[:120]}
                if rh:
                    payload.update(trigger="run_hours", interval_hours=_n(rh.group(1)))
                elif iv.group(3):  # type: ignore[union-attr]
                    payload.update(trigger="calendar", interval_days=_DAYS[iv.group(3).lower()])  # type: ignore[union-attr]
                else:
                    count = _n(iv.group(1)) if iv.group(1) else 1  # type: ignore[union-attr]
                    unit = iv.group(2).lower()  # type: ignore[union-attr]
                    if unit == "hour":
                        payload.update(trigger="run_hours", interval_hours=count)
                    else:
                        payload.update(trigger="calendar", interval_days=count * _DAYS[unit])
                f = _FILTER.search(s)
                if f:
                    payload["parts"] = [f"filter {f.group(1)}x{f.group(2)}x{f.group(3)}"]
                out.append(_prop("pm_task", payload, page, s, 0.5))
        plate: dict[str, str] = {}
        quotes: list[str] = []
        for rx, key in ((_MAKER, "manufacturer"), (_MODEL, "model"), (_SERIAL, "serial")):
            m = rx.search(body)
            if m:
                plate[key] = m.group(1).strip().rstrip(".,")
                line = body[body.rfind("\n", 0, m.start()) + 1: (body.find("\n", m.end()) + 1 or len(body) + 1) - 1]
                quotes.append(re.sub(r"\s+", " ", line).strip())
        if plate.get("model") or plate.get("serial"):
            out.append(_prop("nameplate", plate, page, " ... ".join(dict.fromkeys(quotes)), 0.5))
        lines = body.splitlines()
        for i, line in enumerate(lines):
            if _CHECKLIST_HEAD.search(line):
                steps, raw = [], [line.strip()]
                for nxt in lines[i + 1:i + 60]:
                    m = _STEP.match(nxt)
                    if m:
                        steps.append(m.group(1).strip())
                        raw.append(nxt.strip())
                    elif steps and nxt.strip():
                        break
                if len(steps) >= 2:
                    out.append(_prop("startup_checklist", {"title": line.strip()[:200], "steps": steps}, page,
                                     " ... ".join([raw[0], raw[-1]]), 0.6))
    return out


# ------------------------------------------------------------------- LLM

PROMPT = """These are pages of an O&M manual, submittal, start-up guide, warranty or TAB report for building
equipment. Propose the facts an owner needs to maintain it. Kinds and their payload fields:

- warranty: {"duration_months": int, "covers": "parts"|"labor"|"parts_and_labor"|null, "starts_from": text|null, "terms": text|null}
- pm_task: {"title": text, "trigger": "calendar"|"run_hours", "interval_days": int|null, "interval_hours": number|null, "parts": [text]}
- nameplate: {"manufacturer": text|null, "model": text|null, "serial": text|null, "tag": text|null}
- startup_checklist: {"title": text, "steps": [text]}
- tab_measurement: {"space": text, "terminal": text|null, "quantity": text, "design": number|null, "measured": number|null, "unit": text}

Rules:
- Only what the pages actually say. Never invent a value.
- Each item cites the page number from the "=== PAGE n ===" marker it is under and quotes the text it came from,
  copied character for character from that page (at most 300 characters). Items whose quote isn't on that page are discarded.
- confidence 0-1: lower when the text is ambiguous or you inferred something.

Reply with JSON only: {"items": [{"kind": "...", "payload": {...}, "page": 3, "quote": "...", "confidence": 0.8}], "notes": ""}"""

try:  # the `llm` extra; only the anthropic provider needs it
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover
    BaseModel = None  # type: ignore[assignment,misc]

if BaseModel is not None:

    class _Item(BaseModel):
        kind: str = Field(description="warranty, pm_task, nameplate, startup_checklist or tab_measurement")
        payload_json: str = Field(description="The payload object for the kind, as a JSON string")
        page: int = Field(description="Page number from the PAGE marker")
        quote: str = Field(description="Verbatim text from that page, at most 300 characters")
        confidence: float

    class _Extraction(BaseModel):
        items: list[_Item]
        notes: str = ""


def _page_text(pages: list[dict], limit: int) -> list[str]:
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    for p in pages:
        block = f"=== PAGE {p['page']} ===\n{p['text']}"
        if cur and size + len(block) > limit:
            chunks.append("\n\n".join(cur))
            cur, size = [], 0
        cur.append(block[: limit * 4])
        size += len(block)
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


def _llm_items(client, provider: str, pages: list[dict]) -> tuple[list[dict], dict]:
    limit = int(os.environ.get("BALER_LLM_MAX_CHARS") or DEFAULT_MAX_CHARS)
    max_tokens = int(os.environ.get("BALER_LLM_MAX_TOKENS") or DEFAULT_MAX_TOKENS)
    raw: list[dict] = []
    notes: list[str] = []
    model = None
    usage = {"input_tokens": 0, "output_tokens": 0}
    for chunk in _page_text(pages, limit):
        if provider == "anthropic":
            if client is None:
                import anthropic

                client = anthropic.Anthropic()
            resp = client.beta.messages.parse(
                model=llm.anthropic_model(), max_tokens=16000, betas=["server-side-fallback-2026-07-01"],
                fallbacks="default", output_config={"effort": "high"}, output_format=_Extraction,
                messages=[{"role": "user", "content": PROMPT + "\n\n" + chunk}])
            if resp.stop_reason in ("refusal", "max_tokens") or resp.parsed_output is None:
                notes.append(f"a request ended with {resp.stop_reason}")
                continue
            model = resp.model
            u = getattr(resp, "usage", None)
            usage["input_tokens"] += getattr(u, "input_tokens", 0) or 0
            usage["output_tokens"] += getattr(u, "output_tokens", 0) or 0
            for it in resp.parsed_output.items:
                try:
                    payload = llm.parse_json(it.payload_json)
                except ValueError:
                    continue
                raw.append({"kind": it.kind, "payload": payload, "page": it.page, "quote": it.quote,
                            "confidence": it.confidence})
            if resp.parsed_output.notes:
                notes.append(resp.parsed_output.notes[:1000])
        else:
            if client is None:
                client = llm.OpenAIChat()
            resp = client.chat([{"role": "system", "content": PROMPT}, {"role": "user", "content": chunk}],
                               max_tokens=max_tokens, temperature=0)
            model = resp.get("model") or getattr(client, "model", None)
            u = resp.get("usage") or {}
            usage["input_tokens"] += int(u.get("prompt_tokens") or 0)
            usage["output_tokens"] += int(u.get("completion_tokens") or 0)
            choice = resp["choices"][0]
            try:
                parsed = llm.parse_json((choice.get("message") or {}).get("content") or "")
            except ValueError:
                notes.append("a reply wasn't JSON")
                continue
            items = parsed.get("items", []) if isinstance(parsed, dict) else parsed
            raw.extend(i for i in items if isinstance(i, dict))
            if isinstance(parsed, dict) and parsed.get("notes"):
                notes.append(str(parsed["notes"])[:1000])
    out = []
    for r in raw:
        if r.get("kind") not in KINDS or not isinstance(r.get("payload"), dict):
            continue
        try:
            page = int(r.get("page"))  # type: ignore[arg-type]
            conf = max(0.0, min(1.0, float(r.get("confidence", 0.5))))
        except (TypeError, ValueError):
            continue
        out.append({"kind": r["kind"], "payload": r["payload"], "page": page, "quote": str(r.get("quote") or "")[:MAX_QUOTE],
                    "confidence": conf, "method": "llm", "model": model})
    return out, {"model": model, "usage": usage, "notes": " ".join(notes)}


# ------------------------------------------------------------- pipeline


def verify(items: list[dict], pages: list[dict]) -> tuple[list[dict], list[dict]]:
    """(kept, dropped): an item is kept only if its quote is on its page, or
    on another page (then the page is corrected)."""
    by_no = {p["page"]: p["text"] for p in pages}
    kept, dropped = [], []
    for it in items:
        if it["quote"] and quote_on(it["quote"], by_no.get(it["page"], "")):
            kept.append({**it, "quote_verified": True})
            continue
        other = next((n for n, t in by_no.items() if it["quote"] and quote_on(it["quote"], t)), None)
        if other is not None:
            kept.append({**it, "page": other, "quote_verified": True})
        else:
            dropped.append(it)
    return kept, dropped


def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        key = (it["kind"], it["page"], re.sub(r"\W+", "", it["quote"].lower())[:80])
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


def extract(req: Request, doc_id: str, kinds: list[str] | None = None) -> dict:
    doc = documents._get_doc(req, doc_id)
    pages = documents.page_texts(req, doc["blob_id"])
    if not pages:
        blob = req.rows.get("lc_blobs", doc["blob_id"]) or {}
        raise HTTPError(409, f"the document has no text to read ({blob.get('text_status')})")
    want = set(kinds or KINDS)
    if not want <= set(KINDS):
        raise ValueError(f"kinds are among {', '.join(KINDS)}")
    client = req.services.get("llm")
    if client is not None:
        provider = getattr(client, "kind", "anthropic")
        why = None
    else:
        try:
            provider = llm.provider()
        except llm.LLMError as exc:
            provider, why = "none", str(exc)
        else:
            why = llm.unavailable_reason("agent") if provider != "none" else None
            if why:
                provider = "none"
    info: dict[str, Any] = {"pages": len(pages)}
    items = rules_extract(pages)
    error = None
    if provider != "none":
        try:
            found, meta = _llm_items(client, provider, pages)
            info.update(meta)
            items += found
        except Exception as exc:  # noqa: BLE001 - any model failure leaves the rules' proposals
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
            info["notes"] = f"the model failed ({error}); these are the local rules' proposals only"
    else:
        info["notes"] = ("no model configured: local rules only" + (f" ({why})" if why else "")
                         + ". Rules don't read TAB tables.")
    kept, dropped = verify([i for i in items if i["kind"] in want], pages)
    kept = _dedupe(kept)
    info["dropped"] = len(dropped)
    if dropped:
        info["dropped_examples"] = [f"{d['kind']} p{d['page']}" for d in dropped[:20]]
    run = {"id": rows_mod.new_id(), "org_id": req.org_id, "document_id": doc_id, "provider": provider,
           "model": info.get("model"), "status": "completed", "proposals": len(kept), "info": info, "error": error,
           "created_by": req.actor(), "created_at": req.now, "finished_at": req.now}
    req.rows.insert("lc_doc_extractions", run)
    rows_out = []
    for it in kept:
        row = {"id": rows_mod.new_id(), "org_id": req.org_id, "document_id": doc_id, "extraction_id": run["id"],
               "kind": it["kind"], "payload": it["payload"], "page": it["page"], "quote": it["quote"],
               "quote_verified": True, "confidence": it["confidence"], "method": it["method"], "model": it["model"],
               "status": "proposed", "created_at": req.now}
        req.rows.insert("lc_doc_proposals", row)
        rows_out.append(rows_mod.public(row))
    req.rows.audit(rows_mod.entity_uri("document", doc_id), "document.extract", req.who,
                   {"extraction_id": run["id"], "provider": provider, "proposals": len(kept), "dropped": len(dropped)})
    return {"extraction": rows_mod.public(run), "proposals": rows_out}


# --------------------------------------------------------------- routes


def _view(req: Request, p: dict) -> dict:
    d = req.rows.get("lc_documents", p["document_id"]) or {}
    out = rows_mod.public(p) or {}
    out["citation"] = f"{d.get('title') or d.get('filename')}, p. {p['page']}"
    return out


@route("POST", r"/documents/([^/]+)/extract")
def _extract(req: Request, doc_id: str):
    body = req.json()
    kinds = body.get("kinds")
    if kinds is not None and (not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds)):
        raise ValueError("kinds is a list")
    return 201, extract(req, doc_id, kinds)


@route("GET", r"/documents/([^/]+)/proposals")
def _doc_proposals(req: Request, doc_id: str):
    documents._get_doc(req, doc_id)
    st = req.query.get("status")
    return [_view(req, p) for p in req.rows.find("lc_doc_proposals", org_id=req.org_id, document_id=doc_id)
            if not st or p["status"] == st]


@route("GET", r"/document-proposals")
def _all(req: Request):
    q = req.query
    out = []
    for p in req.rows.find("lc_doc_proposals", org_id=req.org_id):
        if (q.get("status") and p["status"] != q["status"]) or (q.get("kind") and p["kind"] != q["kind"]):
            continue
        d = req.rows.get("lc_documents", p["document_id"])
        if d is None or d.get("deleted_at"):
            continue
        out.append(_view(req, p))
    return out


@route("POST", r"/document-proposals/([^/]+)/decide")
def _decide(req: Request, pid: str):
    p = req.rows.get("lc_doc_proposals", pid)
    if p is None or p["org_id"] != req.org_id:
        raise HTTPError(404, "no such proposal")
    documents._get_doc(req, p["document_id"])
    body = req.json()
    action = body.get("action")
    if action not in ("confirm", "reject"):
        raise ValueError("action is confirm or reject")
    if p["status"] != "proposed":
        raise HTTPError(409, f"already {p['status']}")
    payload = body.get("payload", p["payload"]) if action == "confirm" else None
    if payload is not None and not isinstance(payload, dict):
        raise ValueError("payload is an object (the corrected fact)")
    changes = {"status": "confirmed" if action == "confirm" else "rejected", "decided_payload": payload,
               "decided_by": req.actor(), "decided_at": req.now, "note": text(body, "note", 2000)}
    req.rows.update("lc_doc_proposals", pid, changes)
    req.rows.audit(rows_mod.entity_uri("document", p["document_id"]), f"proposal.{action}", req.who,
                   {"proposal_id": pid, "kind": p["kind"], "page": p["page"],
                    "corrected": action == "confirm" and payload != p["payload"]})
    return _view(req, req.rows.get("lc_doc_proposals", pid))  # type: ignore[arg-type]

"""
Equipment and VAV schedules from drawings, one schedule at a time (#203,
design §5).

A whole print sheet in one shot is the wrong unit: task-specific, staged
pipelines beat one-shot general vision models on engineering documents
(arXiv 2602.13345). So this is staged:

  1. **Find the schedule regions** on each page. A schedule is a title line
     with SCHEDULE in it; its region runs down from the title to the next
     title below it (or a large vertical gap, which is where general notes
     and title blocks usually start), and across to the next title beside
     it.
  2. **Read each region's table**, one region at a time:
       - `local`: pdfplumber on the cropped region: the ruled table if it
         has lines, else columns by text alignment. Offline and free; needs
         a text layer.
       - `vision`: the cropped region rendered to an image and read by the
         configured model (BALER_LLM_PROVIDER=anthropic: Claude, with the
         same model setting as Cx 7). Works without a text layer.

Two-row headers ("MOTOR" over "HP / V / PH") are joined into one name per
column ("MOTOR HP").

Every row becomes a proposal (kind `schedule_row` in lc_doc_proposals, the
#202 table): its schedule, tag and column values, the page, and the row's
cells as the quote. A local read's quote is checked against the page text;
a vision read's is checked when the page has text, and marked when it
can't be. A person confirms each row; nothing is applied on its own.

How well it works is measured on the eval set in schedule_eval, not
assumed: see docs-site/pages/drawing-schedules.mdx for the numbers.
"""

from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass, field
from typing import Any

from baler.commissioning import llm
from baler.commissioning.spec_pdf import quote_on

TITLE = re.compile(r"\bSCHEDULE\b", re.IGNORECASE)
GAP_LINES = 3.0  # a vertical gap this many line-heights tall ends a region
MAX_PAGES = 50


@dataclass
class Region:
    page: int  # 1-based
    title: str
    bbox: tuple[float, float, float, float]  # x0, top, x1, bottom


@dataclass
class Schedule:
    page: int
    title: str
    headers: list[str]
    rows: list[list[str]]
    reader: str
    notes: list[str] = field(default_factory=list)


def _lines(words: list[dict], tol: float = 2.5) -> list[dict]:
    """Words grouped into lines: [{text, x0, x1, top, bottom}]."""
    out: list[dict] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        cur = out[-1] if out else None
        if cur and abs(w["top"] - cur["top"]) <= tol and w["x0"] - cur["x1"] < 3 * (w["bottom"] - w["top"]):
            cur["text"] += " " + w["text"]
            cur["x1"] = max(cur["x1"], w["x1"])
            cur["bottom"] = max(cur["bottom"], w["bottom"])
        else:
            out.append({"text": w["text"], "x0": w["x0"], "x1": w["x1"], "top": w["top"], "bottom": w["bottom"]})
    return out


def find_regions(page, number: int) -> list[Region]:
    """Stage 1 for one pdfplumber page."""
    words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    lines = _lines(words)
    titles = [ln for ln in lines if TITLE.search(ln["text"])]
    out = []
    for t in titles:
        right = [o for o in titles if o is not t and abs(o["top"] - t["top"]) < 6 and o["x0"] > t["x1"]]
        x1 = min((o["x0"] for o in right), default=page.width) - 2
        below = [o for o in titles if o is not t and o["top"] > t["bottom"] + 1 and o["x0"] < x1 and o["x1"] > t["x0"]]
        bottom = min((o["top"] for o in below), default=page.height) - 1
        x0 = max(0.0, t["x0"] - 4)
        # stop at the first big vertical gap below the title, inside the region's columns
        inside = sorted((ln for ln in lines if ln["top"] > t["bottom"] and ln["top"] < bottom
                         and ln["x0"] >= x0 - 1 and ln["x0"] < x1), key=lambda ln: ln["top"])
        last = t["bottom"]
        height = max(t["bottom"] - t["top"], 4.0)
        for ln in inside:
            if ln["top"] - last > GAP_LINES * height * 1.6:
                break
            last = max(last, ln["bottom"])
        bottom = min(bottom, last + height)
        if bottom > t["bottom"] + 2:
            out.append(Region(number, re.sub(r"\s+", " ", t["text"]).strip(), (x0, t["bottom"] + 0.5, x1, bottom)))
    return out


def _clean(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def _looks_like_data(row: list[str]) -> bool:
    return bool(row) and bool(re.search(r"\d", row[0]))


def to_schedule(grid: list[list[Any]]) -> tuple[list[str], list[list[str]]]:
    """A table's cells -> (headers, rows), joining a two-row header."""
    rows = [[_clean(c) for c in r] for r in grid if r and any(_clean(c) for c in r)]
    if not rows:
        return [], []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    head = rows[0]
    body = rows[1:]
    if len(rows) > 2 and any(not c for c in head) and sum(1 for c in rows[1] if c) > sum(1 for c in head if c) \
            and not _looks_like_data(rows[1]):
        group = ""
        joined = []
        for top_label, sub in zip(head, rows[1], strict=True):
            group = top_label or group if top_label else group
            joined.append(f"{group} {sub}".strip() if group and sub and top_label != sub else (sub or top_label))
        head, body = joined, rows[2:]
    # drop columns that are empty in the header and every row
    keep = [i for i in range(width) if head[i] or any(r[i] for r in body)]
    return [head[i] for i in keep], [[r[i] for i in keep] for r in body if _looks_like_data(r) or any(r[1:])]


def read_local(page, region: Region) -> Schedule:
    """Stage 2 without a model: pdfplumber on the cropped region."""
    crop = page.crop(region.bbox)
    tables = crop.find_tables()
    notes = []
    if tables:
        grid = max(tables, key=lambda t: len(t.rows)).extract()
    else:
        grid = crop.extract_table({"vertical_strategy": "text", "horizontal_strategy": "text",
                                   "min_words_vertical": 2, "text_x_tolerance": 2}) or []
        notes.append("no ruling lines: columns by text alignment")
    headers, rows = to_schedule(grid)
    return Schedule(region.page, region.title, headers, rows, "local", notes)


VISION_PROMPT = """This image is one schedule cut from an HVAC drawing: "{title}". Read the table exactly as printed.
If the header has two rows (a group label spanning columns over sub-labels), join them per column: "MOTOR HP".
Reply with JSON only: {{"headers": ["TAG", ...], "rows": [["AHU-1", ...], ...]}}. One list per row, one string
per column, "" for an empty cell. Never invent a value; if a cell is illegible, use "?"."""


def _png(page, region: Region, resolution: int = 200) -> bytes:
    img = page.crop(region.bbox).to_image(resolution=resolution).original
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def read_vision(page, region: Region, client=None) -> Schedule:
    """Stage 2 with a vision model, on the region's image only."""
    png = base64.b64encode(_png(page, region)).decode("ascii")
    prompt = VISION_PROMPT.format(title=region.title)
    kind = getattr(client, "kind", None) or llm.provider()
    if kind == "anthropic":
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        resp = client.beta.messages.create(
            model=llm.anthropic_model(), max_tokens=16000, betas=["server-side-fallback-2026-07-01"],
            fallbacks="default", output_config={"effort": "high"},
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png}},
                {"type": "text", "text": prompt}]}])
        if resp.stop_reason in ("refusal", "max_tokens"):
            raise llm.LLMError(f"the model stopped: {resp.stop_reason}")
        content = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
    elif kind == "openai":
        client = client or llm.OpenAIChat()
        resp = client.chat([{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png}"}}]}], temperature=0, max_tokens=4096)
        content = (resp["choices"][0].get("message") or {}).get("content") or ""
    else:
        raise llm.LLMError("no vision model configured (BALER_LLM_PROVIDER)")
    parsed = llm.parse_json(content)
    headers = [_clean(h) for h in parsed.get("headers") or []] if isinstance(parsed, dict) else []
    rows = [[_clean(c) for c in r] for r in (parsed.get("rows") or []) if isinstance(r, list)] if isinstance(parsed, dict) else []
    return Schedule(region.page, region.title, headers, rows, "vision")


def extract(data: bytes, reader: str = "auto", client=None) -> tuple[list[Schedule], dict]:
    """Every schedule in a PDF. reader: local | vision | auto (vision when a
    model is configured, falling back to local per region on any error)."""
    import pdfplumber

    if reader == "auto":
        reader = "vision" if client is not None or llm.unavailable_reason("agent") is None else "local"
    out: list[Schedule] = []
    info: dict[str, Any] = {"reader": reader, "regions": 0, "fallbacks": []}
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for n, page in enumerate(pdf.pages[:MAX_PAGES], start=1):
            for region in find_regions(page, n):
                info["regions"] += 1
                if reader == "vision":
                    try:
                        out.append(read_vision(page, region, client))
                        continue
                    except Exception as exc:  # noqa: BLE001 - any model failure falls back to the local read
                        info["fallbacks"].append(f"p{n} {region.title}: {type(exc).__name__}: {str(exc)[:200]}")
                out.append(read_local(page, region))
    return out, info


# ------------------------------------------------------------ proposals


def proposals(schedules: list[Schedule], page_text: dict[int, str]) -> list[dict]:
    """One `schedule_row` proposal per row."""
    out = []
    for s in schedules:
        for row in s.rows:
            cells = [c for c in row if c]
            if not cells:
                continue
            quote = " ... ".join(cells)[:500]
            text = page_text.get(s.page, "")
            verified = bool(text) and quote_on(quote, text)
            if s.reader == "local" and not verified:
                continue  # a local read comes from the text layer, so this shouldn't happen
            values = {h or f"column {i + 1}": v for i, (h, v) in enumerate(zip(s.headers, row, strict=False)) if v}
            out.append({"kind": "schedule_row", "payload": {"schedule": s.title, "tag": row[0] if row else None,
                                                           "values": values},
                        "page": s.page, "quote": quote, "quote_verified": verified,
                        "confidence": 0.7 if verified else 0.4, "method": "llm" if s.reader == "vision" else "rules"})
    return out


# --------------------------------------------------------------- route

from baler.lifecycle import (
    documents,
)
from baler.lifecycle import rows as rows_mod
from baler.lifecycle.routes import HTTPError, Request, route


@route("POST", r"/documents/([^/]+)/schedules")
def _extract_schedules(req: Request, doc_id: str):
    """Read every schedule on a drawing into schedule_row proposals."""
    doc = documents._get_doc(req, doc_id)
    if doc["media_type"] != "application/pdf":
        raise HTTPError(415, "schedules are read from PDF drawings")
    reader = req.json().get("reader") or "auto"
    if reader not in ("auto", "local", "vision"):
        raise ValueError("reader is auto, local or vision")
    blob = req.rows.get("lc_blob_data", doc["blob_id"])
    if blob is None:
        raise HTTPError(404, "the file's bytes are missing")
    try:
        found, info = extract(bytes(blob["data"]), reader, req.services.get("llm"))
    except ImportError:
        raise HTTPError(501, "reading drawings needs pdfplumber (the cx-spec extra) on the server") from None
    page_text = {p["page"]: p["text"] for p in documents.page_texts(req, doc["blob_id"])}
    props = proposals(found, page_text)
    run = {"id": rows_mod.new_id(), "org_id": req.org_id, "document_id": doc_id,
           "provider": "vision" if info["reader"] == "vision" else "none", "model": None, "status": "completed",
           "proposals": len(props), "info": {**info, "schedules": [s.title for s in found], "kind": "schedules"},
           "error": None, "created_by": req.actor(), "created_at": req.now, "finished_at": req.now}
    req.rows.insert("lc_doc_extractions", run)
    out = []
    for p in props:
        row = {"id": rows_mod.new_id(), "org_id": req.org_id, "document_id": doc_id, "extraction_id": run["id"], **p,
               "model": None, "status": "proposed", "created_at": req.now}
        req.rows.insert("lc_doc_proposals", row)
        out.append(rows_mod.public(row))
    req.rows.audit(rows_mod.entity_uri("document", doc_id), "document.schedules", req.who,
                   {"extraction_id": run["id"], "schedules": len(found), "rows": len(out)})
    return 201, {"extraction": rows_mod.public(run), "schedules": [
        {"page": s.page, "title": s.title, "headers": s.headers, "rows": len(s.rows), "reader": s.reader, "notes": s.notes}
        for s in found], "proposals": out}

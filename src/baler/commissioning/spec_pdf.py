"""
Reading a spec PDF locally, with no model (Cx 7): page text and the
equipment-schedule / point-list tables pdfplumber finds, turned into the
same proposed spec items a model extraction produces - each citing its
page and the row's text as the quote. Offline and free; it's the whole
extraction under BALER_LLM_PROVIDER=none and the first pass for every
other provider (a model then only reads what the tables didn't cover).

Also: checking that a model's quote really is on the page it cites.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from baler.commissioning import spec

MAX_PAGES = 500


@dataclass
class Page:
    number: int  # 1-based
    text: str  # the whole page
    rest: str  # the text outside tables that produced items
    items: list[dict] = field(default_factory=list)


def _cell(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def _header_index(rows: list[list[str]]) -> int | None:
    """The first of the top rows that names a tag column, or equipment
    and point-name columns."""
    for i, row in enumerate(rows[:4]):
        fmap = spec._field_map([c for c in row if c])
        if "tag" in fmap or ("equipment" in fmap and "name" in fmap):
            return i
    return None


def table_items(rows: list[list], document: str, page: int) -> list[dict]:
    """Proposed items from one table's rows (header detected among the
    first rows; a single-cell title row above it is the type hint)."""
    grid = [[_cell(c) for c in r] for r in rows if r]
    h = _header_index(grid)
    if h is None:
        return []
    headers = grid[h]
    title = next((r[0] for r in reversed(grid[:h]) if sum(1 for c in r if c) == 1 and r[0]), None)
    if title:
        title = re.sub(r"\bschedule\b", "", title, flags=re.IGNORECASE).strip(" -:") or None
    out = []
    for r in grid[h + 1:]:
        if not any(r):
            continue
        row = {headers[i]: v for i, v in enumerate(r) if i < len(headers) and headers[i] and v}
        if title and "type" not in spec._field_map(row.keys()) and "type" not in row:
            row["type"] = title
        quote = " | ".join(c for c in r if c)[:500]
        for it in spec.items_from_rows([row], document):
            it["source"] = {"document": document, "page": page, "quote": quote, "method": "table"}
            it["properties"] = {k: v for k, v in it["properties"].items() if v is not None}
            if it["confidence"] is None:
                it["confidence"] = 0.7 if it["brick_class"] else 0.5
            out.append(it)
    return out


def read(data: bytes, document: str) -> list[Page]:
    """Every page's text and table items. Raises ValueError on a file
    pdfplumber can't open (and ImportError without pdfplumber)."""
    import pdfplumber

    pages: list[Page] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for n, pg in enumerate(pdf.pages[:MAX_PAGES], start=1):
                text = pg.extract_text() or ""
                items: list[dict] = []
                rest_page = pg
                for t in pg.find_tables():
                    found = table_items(t.extract(), document, n)
                    if found:
                        items += found
                        rest_page = rest_page.outside_bbox(t.bbox)
                rest = text if rest_page is pg else (rest_page.extract_text() or "")
                pages.append(Page(n, text, rest, items))
    except Exception as exc:  # pdfminer raises many kinds on a bad file
        raise ValueError(f"could not read the PDF: {exc}") from exc
    return pages


def safe_read(data: bytes, document: str) -> tuple[list[Page], str | None]:
    """read(), but ([], why) when the PDF can't be read locally."""
    try:
        return read(data, document), None
    except ImportError:
        return [], "pdfplumber isn't installed, so tables weren't read locally"
    except ValueError as exc:
        return [], str(exc)


# ------------------------------------------------------------ quotes

_FOLD = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " ",
                       "ﬁ": "fi", "ﬂ": "fl", "|": " "})


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").translate(_FOLD).lower()).strip()


def quote_on(quote: str, text: str) -> bool:
    """Is `quote` on a page with `text`? Whitespace, case, curly quotes
    and dashes don't matter; "..." may elide text between fragments, which
    must appear in order."""
    hay = _norm(text)
    if not hay:
        return False
    parts = [p for p in (_norm(x) for x in re.split(r"\.\.\.|…", quote or "")) if p]
    if not parts:
        return False
    for haystack, pieces in ((hay, parts), (hay.replace(" ", ""), [p.replace(" ", "") for p in parts])):
        # second pass: pdfplumber may run table cells together without a space
        pos = 0
        for p in pieces:
            i = haystack.find(p, pos)
            if i < 0:
                break
            pos = i + len(p)
        else:
            return True
    return False


def verify(item: dict, pages: list[Page]) -> str:
    """'ok' (quote on its cited page), 'moved' (found on another page; the
    item's page is corrected) or 'missing'."""
    src = item.get("source") or {}
    quote = src.get("quote") or ""
    by_no = {p.number: p for p in pages}
    page = by_no.get(src.get("page"))  # type: ignore[arg-type]
    if page is not None and quote_on(quote, page.text):
        return "ok"
    for p in pages:
        if quote_on(quote, p.text):
            src["cited_page"] = src.get("page")
            src["page"] = p.number
            return "moved"
    return "missing"


def key(item: dict) -> tuple[str, str, str]:
    return (item.get("kind") or "", spec.normalize_tag(item.get("tag")), spec.normalize_tag(item.get("equip_tag") or ""))


def merge(first: list[dict], more: list[dict]) -> list[dict]:
    """`first` plus the items of `more` it doesn't already have."""
    seen = {key(i) for i in first}
    out = list(first)
    for it in more:
        k = key(it)
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out


def extract(data: bytes, filename: str) -> tuple[list[dict], dict]:
    """The `none` provider: tables only."""
    if len(data) > spec.MAX_PDF_BYTES:
        raise ValueError(f"PDF is over {spec.MAX_PDF_BYTES // (1024 * 1024)} MB; split it")
    pages = read(data, filename)
    items = merge([], [it for p in pages for it in p.items])
    notes = f"read {len(pages)} page(s) locally without a model: equipment schedules and point lists in tables only"
    if not items:
        notes += "; no table with a tag or equipment/point column was found (scanned pages and prose need a model)"
    if not any(p.text.strip() for p in pages):
        notes += "; the PDF has no text layer (scanned?)"
    return items, {"model": None, "provider": "none", "notes": notes, "usage": None}

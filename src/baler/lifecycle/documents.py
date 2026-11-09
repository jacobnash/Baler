"""
The building document store (#201, design §5): O&M manuals, prints,
submittals, start-up guides, TAB reports, warranties, photos.

- **Content-addressed.** A file is stored once, keyed by its sha256 (the
  cx_spec_documents pattern, but keeping the bytes). Uploading the same
  file again gives back the existing document; `GET .../verify` re-hashes
  the stored bytes, so integrity is checkable for as long as the record
  lives.
- **Linked** to positions, assets, spaces, systems and Cx phases, a whole
  document or one page of it.
- **Searchable.** Each page's text is extracted once per file (PDF via
  pdfplumber, the cx-spec extra; plain text as is) and indexed with
  Postgres full-text search. Every hit names the document and the page, so
  an answer built on it can cite both.

Bytes live in Postgres (lc_blob_data), so backups already include them and
no service writes files under the app's directory. Schema: migration 0003.
"""

from __future__ import annotations

import hashlib
import io
import re
from typing import Any

from baler.lifecycle import rows as rows_mod
from baler.lifecycle.routes import (
    HTTPError,
    Raw,
    Request,
    need,
    require_role,
    route,
    text,
)

rows_mod.register("lc_blobs", ("id", "size", "media_type", "page_count", "text_status", "created_at"))
rows_mod.register("lc_blob_data", ("id", "data"), order="id")
rows_mod.register("lc_document_pages", ("id", "blob_id", "page", "text"), order="page")
rows_mod.register("lc_documents", ("id", "org_id", "site_id", "blob_id", "filename", "media_type", "size", "title",
                                   "kind", "notes", "created_by", "created_at", "deleted_at", "deleted_by"))
rows_mod.register("lc_document_links", ("id", "org_id", "document_id", "target_kind", "target", "page", "note",
                                        "created_by", "created_at"))

KINDS = ("om_manual", "submittal", "drawing", "specification", "startup_guide", "tab_report", "warranty",
         "cx_report", "photo", "certificate", "other")
TARGET_KINDS = ("position", "asset", "space", "system", "phase")
MAX_PAGES = 2000
SNIPPET_CHARS = 240
_MEDIA = {".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv",
          ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".xlsx":
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".dwg": "application/acad"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def media_type(filename: str, declared: str) -> str:
    if declared and declared not in ("application/octet-stream", "application/json"):
        return declared
    m = re.search(r"\.[A-Za-z0-9]+$", filename or "")
    return _MEDIA.get(m.group(0).lower(), "application/octet-stream") if m else "application/octet-stream"


def extract_pages(data: bytes, mtype: str) -> tuple[list[str], str]:
    """(page texts, status). Never raises: a file we can't read is still
    stored, it just isn't searchable."""
    if mtype == "application/pdf" or data[:5] == b"%PDF-":
        try:
            import pdfplumber
        except ImportError:
            return [], "unsupported: PDF text needs pdfplumber (the cx-spec extra) on the server"
        try:
            pages = []
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                for i, page in enumerate(pdf.pages):
                    if i >= MAX_PAGES:
                        break
                    pages.append(page.extract_text() or "")
        except Exception as exc:  # noqa: BLE001 - pdfminer raises many types on a damaged file
            return [], f"failed: {type(exc).__name__}: {str(exc)[:200]}"
        return pages, "extracted" if any(p.strip() for p in pages) else "no_text"
    if mtype.startswith("text/"):
        try:
            body = data.decode("utf-8")
        except UnicodeDecodeError:
            body = data.decode("latin-1")
        # a text file has no pages; form feeds are the one convention
        return body.split("\f"), "extracted"
    return [], "unsupported"


# --------------------------------------------------------------- store


def _get_doc(req: Request, doc_id: str) -> dict:
    d = req.rows.get("lc_documents", doc_id)
    if d is None or d["org_id"] != req.org_id or d.get("deleted_at"):
        raise HTTPError(404, "no such document")
    return d


def store_file(req: Request, data: bytes, filename: str, declared_type: str = "", kind: str = "other",
               title: str | None = None, site_id: str | None = None) -> tuple[dict, bool]:
    """(document, duplicate). The blob is written once per sha256; the
    org gets one live document per blob."""
    if not data:
        raise ValueError("the file is empty")
    if kind not in KINDS:
        raise ValueError(f"kind is one of {', '.join(KINDS)}")
    digest = sha256(data)
    mtype = media_type(filename, declared_type)
    if req.rows.get("lc_blobs", digest) is None:
        pages, status = extract_pages(data, mtype)
        req.rows.insert("lc_blobs", {"id": digest, "size": len(data), "media_type": mtype,
                                     "page_count": len(pages) or None, "text_status": status, "created_at": req.now})
        req.rows.insert("lc_blob_data", {"id": digest, "data": data})
        for i, t in enumerate(pages, start=1):
            if t.strip():
                req.rows.insert("lc_document_pages", {"id": f"{digest}:{i}", "blob_id": digest, "page": i,
                                                      "text": t.replace("\x00", "")})
    live = [d for d in req.rows.find("lc_documents", org_id=req.org_id, blob_id=digest) if not d.get("deleted_at")]
    if live:
        return live[0], True
    doc = {"id": rows_mod.new_id(), "org_id": req.org_id, "site_id": site_id, "blob_id": digest,
           "filename": (filename or digest)[:300], "media_type": mtype, "size": len(data), "title": title, "kind": kind,
           "created_by": req.actor(), "created_at": req.now}
    req.rows.insert("lc_documents", doc)
    req.rows.audit(rows_mod.entity_uri("document", doc["id"]), "document.add", req.who,
                   {"sha256": digest, "filename": doc["filename"], "kind": kind})
    return doc, False


def page_texts(req: Request, blob_id: str) -> list[dict]:
    """[{page, text}] for a blob: what extraction (#202) reads."""
    return [{"page": p["page"], "text": p["text"]} for p in req.rows.find("lc_document_pages", blob_id=blob_id)]


def _links(req: Request, doc_id: str) -> list[dict]:
    return [rows_mod.public(x) for x in req.rows.find("lc_document_links", org_id=req.org_id, document_id=doc_id)
            if x["target_kind"] != "position" or req.allows(x["target"])]


def view(req: Request, d: dict, with_links: bool = False) -> dict:
    blob = req.rows.get("lc_blobs", d["blob_id"]) or {}
    out = {**(rows_mod.public(d) or {}), "sha256": d["blob_id"], "page_count": blob.get("page_count"),
           "text_status": blob.get("text_status")}
    if with_links:
        out["links"] = _links(req, d["id"])
    return out


# -------------------------------------------------------------- search


def _terms(q: str) -> list[str]:
    return [t for t in re.findall(r"[A-Za-z0-9]+", q.lower()) if len(t) > 1]


def _snippet(body: str, terms: list[str]) -> str:
    low = body.lower()
    at = min((i for i in (low.find(t) for t in terms) if i >= 0), default=0)
    start = max(0, at - SNIPPET_CHARS // 3)
    s = re.sub(r"\s+", " ", body[start:start + SNIPPET_CHARS]).strip()
    return ("…" if start else "") + s + ("…" if start + SNIPPET_CHARS < len(body) else "")


def _search_pg(req: Request, q: str, doc_ids: list[str] | None, limit: int) -> list[dict]:
    sql = """
        SELECT d.id, d.filename, d.title, d.kind, p.page,
               ts_headline('english', p.text, websearch_to_tsquery('english', %(q)s),
                           'MaxWords=40, MinWords=15, StartSel=«, StopSel=»') AS snippet,
               ts_rank(p.tsv, websearch_to_tsquery('english', %(q)s)) AS rank
          FROM lc_document_pages p
          JOIN lc_documents d ON d.blob_id = p.blob_id AND d.org_id = %(org)s AND d.deleted_at IS NULL
         WHERE p.tsv @@ websearch_to_tsquery('english', %(q)s)
           AND (%(ids)s::text[] IS NULL OR d.id = ANY(%(ids)s))
         ORDER BY rank DESC, d.id, p.page
         LIMIT %(limit)s"""
    with req.rows.conn() as conn:
        got = conn.execute(sql, {"q": q, "org": req.org_id, "ids": doc_ids, "limit": limit}).fetchall()
    return [{"document_id": r[0], "filename": r[1], "title": r[2], "kind": r[3], "page": r[4], "snippet": r[5],
             "rank": float(r[6])} for r in got]


def _search_memory(req: Request, q: str, doc_ids: list[str] | None, limit: int) -> list[dict]:
    terms = _terms(q)
    if not terms:
        return []
    hits = []
    for d in req.rows.find("lc_documents", org_id=req.org_id):
        if d.get("deleted_at") or (doc_ids is not None and d["id"] not in doc_ids):
            continue
        for p in req.rows.find("lc_document_pages", blob_id=d["blob_id"]):
            low = p["text"].lower()
            if all(t in low for t in terms):
                hits.append({"document_id": d["id"], "filename": d["filename"], "title": d.get("title"),
                             "kind": d["kind"], "page": p["page"], "snippet": _snippet(p["text"], terms),
                             "rank": float(sum(low.count(t) for t in terms))})
    hits.sort(key=lambda h: (-h["rank"], h["document_id"], h["page"]))
    return hits[:limit]


def search(req: Request, q: str, kind: str | None = None, target_kind: str | None = None, target: str | None = None,
           limit: int = 50) -> list[dict]:
    """Pages matching `q` (Postgres websearch syntax: words, "phrases",
    -exclusions), best first, each with a citation "filename, p. N"."""
    if not q or not q.strip():
        raise ValueError("q is required")
    doc_ids: list[str] | None = None
    if target_kind or target:
        doc_ids = [x["document_id"] for x in req.rows.find("lc_document_links", org_id=req.org_id)
                   if (not target_kind or x["target_kind"] == target_kind) and (not target or x["target"] == target)]
    if kind:
        kinds = {d["id"] for d in req.rows.find("lc_documents", org_id=req.org_id, kind=kind)}
        doc_ids = [i for i in (doc_ids if doc_ids is not None else kinds) if i in kinds]
    if doc_ids is not None and not doc_ids:
        return []
    limit = max(1, min(limit, 200))
    hits = _search_pg(req, q, doc_ids, limit) if req.rows.pg else _search_memory(req, q, doc_ids, limit)
    for h in hits:
        h["citation"] = f"{h['title'] or h['filename']}, p. {h['page']}"
    return hits


# --------------------------------------------------------------- routes


@route("POST", r"/documents")
def _upload(req: Request):
    if req.raw is None:
        raise HTTPError(415, "send the file itself as the body (any Content-Type but JSON), "
                             "with ?filename=&kind=&title= in the query")
    q = req.query
    doc, dup = store_file(req, req.raw, q.get("filename") or "document", req.content_type, q.get("kind") or "other",
                          text(q, "title", 300), q.get("site_id") or None)
    return (200 if dup else 201), {**view(req, doc), "duplicate": dup}


@route("GET", r"/documents")
def _list(req: Request):
    q = req.query
    docs = [d for d in req.rows.find("lc_documents", org_id=req.org_id) if not d.get("deleted_at")]
    if q.get("kind"):
        docs = [d for d in docs if d["kind"] == q["kind"]]
    if q.get("target_kind") or q.get("target"):
        linked = {x["document_id"] for x in req.rows.find("lc_document_links", org_id=req.org_id)
                  if (not q.get("target_kind") or x["target_kind"] == q["target_kind"])
                  and (not q.get("target") or x["target"] == q["target"])}
        docs = [d for d in docs if d["id"] in linked]
    return [view(req, d) for d in sorted(docs, key=lambda d: str(d["created_at"]), reverse=True)]


@route("GET", r"/documents/search")
def _search(req: Request):
    q = req.query
    return search(req, q.get("q") or "", q.get("kind"), q.get("target_kind"), q.get("target"),
                  int(q.get("limit") or 50))


@route("GET", r"/documents/kinds")
def _kinds(req: Request):
    return {"kinds": list(KINDS), "target_kinds": list(TARGET_KINDS)}


@route("GET", r"/documents/([^/]+)")
def _get(req: Request, doc_id: str):
    return view(req, _get_doc(req, doc_id), with_links=True)


@route("PATCH", r"/documents/([^/]+)")
def _patch(req: Request, doc_id: str):
    d = _get_doc(req, doc_id)
    body = req.json()
    unknown = set(body) - {"title", "kind", "notes", "site_id", "filename"}
    if unknown:
        raise ValueError(f"not editable: {', '.join(sorted(unknown))} (the file itself never changes; upload a new one)")
    changes: dict[str, Any] = {}
    for k, limit in (("title", 300), ("notes", 4000), ("site_id", 100), ("filename", 300)):
        if k in body:
            changes[k] = text(body, k, limit)
    if "kind" in body:
        if body["kind"] not in KINDS:
            raise ValueError(f"kind is one of {', '.join(KINDS)}")
        changes["kind"] = body["kind"]
    if changes.get("filename") is None:
        changes.pop("filename", None)
    diff = {k: {"from": d.get(k), "to": v} for k, v in changes.items() if d.get(k) != v}
    if diff:
        req.rows.update("lc_documents", doc_id, changes)
        req.rows.audit(rows_mod.entity_uri("document", doc_id), "document.edit", req.who, diff)
    return view(req, _get_doc(req, doc_id), with_links=True)


@route("DELETE", r"/documents/([^/]+)")
def _delete(req: Request, doc_id: str):
    require_role(req, "admin")
    _get_doc(req, doc_id)
    req.rows.update("lc_documents", doc_id, {"deleted_at": req.now, "deleted_by": req.actor()})
    req.rows.audit(rows_mod.entity_uri("document", doc_id), "document.delete", req.who, {})
    return {"deleted": True}


@route("GET", r"/documents/([^/]+)/content")
def _content(req: Request, doc_id: str):
    d = _get_doc(req, doc_id)
    blob = req.rows.get("lc_blob_data", d["blob_id"])
    if blob is None:
        raise HTTPError(404, "the file's bytes are missing")
    return Raw(bytes(blob["data"]), d["media_type"], d["filename"])


@route("GET", r"/documents/([^/]+)/verify")
def _verify(req: Request, doc_id: str):
    d = _get_doc(req, doc_id)
    blob = req.rows.get("lc_blob_data", d["blob_id"])
    got = sha256(bytes(blob["data"])) if blob else None
    return {"sha256": d["blob_id"], "computed": got, "ok": got == d["blob_id"]}


@route("GET", r"/documents/([^/]+)/pages/(\d+)")
def _page(req: Request, doc_id: str, page: str):
    d = _get_doc(req, doc_id)
    p = req.rows.get("lc_document_pages", f"{d['blob_id']}:{int(page)}")
    if p is None:
        raise HTTPError(404, "no text for that page")
    return {"document_id": doc_id, "page": p["page"], "text": p["text"],
            "citation": f"{d.get('title') or d['filename']}, p. {p['page']}"}


@route("POST", r"/documents/([^/]+)/links")
def _link(req: Request, doc_id: str):
    _get_doc(req, doc_id)
    body = req.json()
    tk = need(body, "target_kind")
    if tk not in TARGET_KINDS:
        raise ValueError(f"target_kind is one of {', '.join(TARGET_KINDS)}")
    target = need(body, "target")
    if len(target) > 1000:
        raise ValueError("target is too long")
    if tk == "position" and not req.allows(target):
        raise HTTPError(404, "no such position in your sites")
    page = body.get("page")
    if page is not None and (not isinstance(page, int) or isinstance(page, bool) or page < 1):
        raise ValueError("page is a 1-based page number")
    link = {"id": rows_mod.new_id(), "org_id": req.org_id, "document_id": doc_id, "target_kind": tk, "target": target,
            "page": page, "note": text(body, "note", 1000), "created_by": req.actor(), "created_at": req.now}
    req.rows.insert("lc_document_links", link)
    req.rows.audit(rows_mod.entity_uri("document", doc_id), "document.link", req.who,
                   {"target_kind": tk, "target": target, "page": page})
    return 201, rows_mod.public(link)


@route("DELETE", r"/documents/([^/]+)/links/([^/]+)")
def _unlink(req: Request, doc_id: str, link_id: str):
    _get_doc(req, doc_id)
    link = req.rows.get("lc_document_links", link_id)
    if link is None or link["document_id"] != doc_id or link["org_id"] != req.org_id:
        raise HTTPError(404, "no such link")
    req.rows.delete("lc_document_links", id=link_id)
    req.rows.audit(rows_mod.entity_uri("document", doc_id), "document.unlink", req.who,
                   {"target_kind": link["target_kind"], "target": link["target"]})
    return {"deleted": True}


@route("GET", r"/documents/([^/]+)/audit")
def _audit(req: Request, doc_id: str):
    _get_doc(req, doc_id)
    return req.rows.audit_log(rows_mod.entity_uri("document", doc_id))

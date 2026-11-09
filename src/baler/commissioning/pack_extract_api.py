"""
HTTP routes for extracting requirement rows from a user's own document
(Baler#206), under /cx/. The extraction runs on the commissioning worker
(202, poll GET /pack-extractions/{id}); each proposal is accepted (with
corrections) or rejected by a person.

BALER_PACK_EXTRACTION=0 turns extraction off on a deployment (default on);
the rest of packs works either way. Every upload carries
"rights_confirmed": true, and the extraction records who confirmed and when.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import traceback

from baler.commissioning import db, llm, pack_extract, packs, packs_api, spec, spec_pdf
from baler.commissioning.routes import Route
from baler.http_handler_base import respond_error, respond_json

log = logging.getLogger(__name__)

EXTRACTIONS = "cx_pack_extractions"
PROPOSALS = "cx_pack_proposals"

db.ROW_TABLES[EXTRACTIONS] = (("id", "org_id", "pack_ref", "filename", "sha256", "size", "status", "proposals", "info",
                               "error", "created_by", "created_at", "finished_at", "rights_confirmed_by",
                               "rights_confirmed_at"), frozenset({"info"}))
db.ROW_TABLES[PROPOSALS] = (("id", "org_id", "pack_ref", "extraction_id", "row", "source", "confidence", "status",
                             "decided_by", "decided_at", "created_at"), frozenset({"row", "source"}))

RIGHTS_TEXT = "I have the rights to this document and may process it with AI tools."
OFF_MESSAGE = "document extraction is turned off on this deployment"


def enabled() -> bool:
    """BALER_PACK_EXTRACTION: on unless set to 0 (or false/off/no)."""
    return (os.environ.get("BALER_PACK_EXTRACTION") or "1").strip().lower() not in ("0", "false", "off", "no")


# Tests swap the model: fn(system, text) -> reply text.
CHAT = None


def run(repo, extraction: dict, data: bytes, now_fn) -> None:
    """Read the PDF locally, ask the model, store the proposals. Never raises."""
    try:
        repo.update_row(EXTRACTIONS, extraction["id"], {"status": "running"})
        pages, why = spec_pdf.safe_read(data, extraction["filename"])
        if not pages:
            raise pack_extract.ExtractionError(f"the PDF couldn't be read locally: {why}")
        found, info = pack_extract.extract(pages, extraction["filename"], CHAT or pack_extract.chat_fn())
        now = now_fn()
        repo.put_rows(PROPOSALS, [{"id": db.new_id(), "org_id": extraction["org_id"], "pack_ref": extraction["pack_ref"],
                                   "extraction_id": extraction["id"], "row": f["row"],
                                   "source": {**f["source"], "sha256": extraction["sha256"]}, "confidence": f["confidence"],
                                   "status": "proposed", "decided_by": None, "decided_at": None, "created_at": now}
                                  for f in found])
        repo.update_row(EXTRACTIONS, extraction["id"], {"status": "completed", "proposals": len(found), "info": info,
                                                       "finished_at": now})
    except Exception as exc:  # noqa: BLE001 - recorded on the row
        log.error("pack extraction %s failed:\n%s", extraction["id"], traceback.format_exc())
        repo.update_row(EXTRACTIONS, extraction["id"], {"status": "failed", "error": str(exc)[:2000], "finished_at": now_fn()})


def _extract(h, span, claims, ref) -> None:
    if not enabled():
        respond_error(h, 403, OFF_MESSAGE)
        return
    pack = packs_api.get_pack(h, ref, claims)
    if not pack:
        return
    if pack["status"] != "draft":
        raise ValueError("proposals go into a draft pack version")
    body = h.read_json_body()
    if not isinstance(body, dict) or body.get("rights_confirmed") is not True:
        raise ValueError(f'confirm "rights_confirmed": true: {RIGHTS_TEXT}')
    try:
        raw = base64.b64decode(str(body.get("data") or ""), validate=True)
    except ValueError as exc:
        raise ValueError("data must be the PDF as base64") from exc
    if not raw.startswith(b"%PDF"):
        raise ValueError("that isn't a PDF")
    if len(raw) > spec.MAX_PDF_BYTES:
        raise ValueError(f"PDF is over {spec.MAX_PDF_BYTES // (1024 * 1024)} MB; split it")
    if CHAT is None:
        # a model is required: unlike Cx 7's spec tables, requirements have no table-only path
        why = llm.unavailable_reason("agent")
        if why and llm.provider() == "none":
            why = "extracting requirements needs a model: set BALER_LLM_PROVIDER (see .env.example)"
        if why:
            respond_error(h, 501, why)
            return
    now = h.NOW()
    ex = {"id": db.new_id(), "org_id": claims["org_id"], "pack_ref": ref, "filename": str(body.get("filename") or "document.pdf")[:200],
          "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw), "status": "queued", "proposals": 0, "info": None,
          "error": None, "created_by": claims.get("sub"), "created_at": now, "finished_at": None,
          "rights_confirmed_by": claims.get("sub"), "rights_confirmed_at": now}
    h.REPO.put_rows(EXTRACTIONS, [ex])
    repo, now_fn = h.REPO, h.NOW
    h.WORKER.submit(lambda: run(repo, ex, raw, now_fn))
    respond_json(h, 202, db.public_row(ex))


def _get_extraction(h, span, claims, eid) -> None:
    ex = h.REPO.get_row(EXTRACTIONS, eid)
    if ex is None or ex["org_id"] != claims.get("org_id"):
        respond_error(h, 404, "no such extraction")
        return
    respond_json(h, 200, db.public_row(ex))


def _list_proposals(h, span, claims, ref) -> None:
    if packs_api.get_pack(h, ref, claims):
        respond_json(h, 200, [db.public_row(p) for p in h.REPO.find_rows(PROPOSALS, pack_ref=ref)])


def _proposal(h, pid: str, claims: dict) -> dict | None:
    p = h.REPO.get_row(PROPOSALS, pid)
    if p is None or p["org_id"] != claims.get("org_id"):
        respond_error(h, 404, "no such proposal")
        return None
    if p["status"] != "proposed":
        raise ValueError(f"this proposal was already {p['status']}")
    return p


def _accept(h, span, claims, pid) -> None:
    """The proposed row, with the person's corrections ({"row": {...}}),
    joins the draft pack unconfirmed: its numbers are confirmed there."""
    p = _proposal(h, pid, claims)
    if not p:
        return
    pack = h.REPO.get_row(packs_api.PACKS, p["pack_ref"])
    if pack is None or pack["status"] != "draft":
        raise ValueError("the pack is no longer a draft: import a new version to add rows")
    fix = h.read_json_body().get("row") or {}
    if not isinstance(fix, dict):
        raise TypeError("row is an object of corrected fields")
    row = packs.normalize_row({**p["row"], **fix, "verified_by": None}, "row")
    if any(r["id"] == row["id"] for r in pack["requirements"]):
        raise ValueError(f"the pack already has a row {row['id']}: give the row another id")
    now = h.NOW()
    hist = [*pack.get("history", []), {"at": now.isoformat(), "by": claims.get("sub"), "action": "accept_proposal",
                                       "row": row["id"], "proposal": pid, "corrected": sorted(fix)}]
    h.REPO.update_row(packs_api.PACKS, pack["id"], {"requirements": [*pack["requirements"], row], "history": hist, "updated_at": now})
    h.REPO.update_row(PROPOSALS, pid, {"status": "accepted", "row": row, "decided_by": claims.get("sub"), "decided_at": now})
    respond_json(h, 200, db.public_row({**p, "status": "accepted", "row": row, "decided_by": claims.get("sub"), "decided_at": now}))


def _reject(h, span, claims, pid) -> None:
    p = _proposal(h, pid, claims)
    if not p:
        return
    now = h.NOW()
    h.REPO.update_row(PROPOSALS, pid, {"status": "rejected", "decided_by": claims.get("sub"), "decided_at": now})
    respond_json(h, 200, db.public_row({**p, "status": "rejected", "decided_by": claims.get("sub"), "decided_at": now}))


ROUTES: list[Route] = [
    ("POST", re.compile(r"/packs/([^/]+)/extract"), "extract_pack_rows", _extract),
    ("GET", re.compile(r"/packs/([^/]+)/proposals"), "list_pack_proposals", _list_proposals),
    ("GET", re.compile(r"/pack-extractions/([^/]+)"), "get_pack_extraction", _get_extraction),
    ("POST", re.compile(r"/pack-proposals/([^/]+)/accept"), "accept_pack_proposal", _accept),
    ("POST", re.compile(r"/pack-proposals/([^/]+)/reject"), "reject_pack_proposal", _reject),
]

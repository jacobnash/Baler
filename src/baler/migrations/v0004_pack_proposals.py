"""
0004 pack proposals (#206, epic #221 §3.1): requirement rows a model read
from a document the user holds, waiting for a person.

- cx_pack_extractions: one per uploaded PDF: its sha256, name and size
  (the file itself isn't kept here), status, how many proposals, notes.
- cx_pack_proposals: a proposed pack row, its source (page, the verbatim
  quote checked to be on that page, the document's sha256) and its fate
  (proposed | accepted | rejected, who, when). The quote stays here, in the
  org's own database: an accepted row carries the citation only.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 4
NAME = "pack proposals"

EXTRACTIONS = """
CREATE TABLE IF NOT EXISTS cx_pack_extractions (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL,
    pack_ref    TEXT NOT NULL REFERENCES cx_packs(id),
    filename    TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    size        INTEGER NOT NULL,
    status      TEXT NOT NULL,              -- queued | running | completed | failed
    proposals   INTEGER NOT NULL DEFAULT 0,
    info        JSONB,
    error       TEXT,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ
)"""

PROPOSALS = """
CREATE TABLE IF NOT EXISTS cx_pack_proposals (
    id            TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL,
    pack_ref      TEXT NOT NULL REFERENCES cx_packs(id),
    extraction_id TEXT NOT NULL REFERENCES cx_pack_extractions(id),
    row           JSONB NOT NULL,
    source        JSONB NOT NULL,           -- document, sha256, page, quote, quote_verified
    confidence    DOUBLE PRECISION,
    status        TEXT NOT NULL,            -- proposed | accepted | rejected
    decided_by    TEXT,
    decided_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL
)"""


def plan(conn) -> list[Step]:
    return [
        Step(EXTRACTIONS, "cx_pack_extractions: uploaded documents read for requirement rows"),
        Step(PROPOSALS, "cx_pack_proposals: proposed rows awaiting a person"),
        Step("CREATE INDEX IF NOT EXISTS cx_pack_proposals_pack ON cx_pack_proposals (pack_ref, status)", "proposals by pack"),
    ]

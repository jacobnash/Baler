"""
0017 facts extracted from documents, as proposals (#202, epic #221).

- lc_doc_extractions: one extraction run over a stored document: which
  provider and model read it, what it found, any error.
- lc_doc_proposals: one proposed fact (a warranty term, a PM task and
  interval, nameplate data, a start-up checklist, a TAB measurement), with
  the page and the verbatim text it was read from. A person confirms
  (optionally correcting it), or rejects it. Nothing is applied on its own.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 17
NAME = "lifecycle document extraction proposals"

EXTRACTIONS = """
CREATE TABLE IF NOT EXISTS lc_doc_extractions (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL REFERENCES orgs(id),
    document_id TEXT NOT NULL REFERENCES lc_documents(id),
    provider    TEXT NOT NULL,          -- none | anthropic | openai
    model       TEXT,
    status      TEXT NOT NULL,          -- completed | failed
    proposals   INT NOT NULL DEFAULT 0,
    info        JSONB,
    error       TEXT,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ
)"""

PROPOSALS = """
CREATE TABLE IF NOT EXISTS lc_doc_proposals (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL REFERENCES orgs(id),
    document_id     TEXT NOT NULL REFERENCES lc_documents(id),
    extraction_id   TEXT REFERENCES lc_doc_extractions(id),
    kind            TEXT NOT NULL,      -- warranty | pm_task | nameplate | startup_checklist | tab_measurement
    payload         JSONB NOT NULL,
    page            INT NOT NULL,
    quote           TEXT NOT NULL,
    quote_verified  BOOLEAN NOT NULL,
    confidence      DOUBLE PRECISION,
    method          TEXT NOT NULL,      -- rules | llm
    model           TEXT,
    status          TEXT NOT NULL CHECK (status IN ('proposed', 'confirmed', 'rejected')),
    decided_payload JSONB,
    decided_by      TEXT,
    decided_at      TIMESTAMPTZ,
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL
)"""


def plan(conn) -> list[Step]:
    return [
        Step(EXTRACTIONS, "lc_doc_extractions: extraction runs over documents"),
        Step(PROPOSALS, "lc_doc_proposals: extracted facts awaiting a person"),
        Step("CREATE INDEX IF NOT EXISTS lc_doc_proposals_doc ON lc_doc_proposals (document_id, status)", "by document"),
        Step("CREATE INDEX IF NOT EXISTS lc_doc_proposals_org ON lc_doc_proposals (org_id, status, kind)", "by org"),
    ]

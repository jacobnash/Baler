"""
0012 the building document store (#201, epic #221).

Generalises cx_spec_documents' sha256 pattern, but keeps the file:

- lc_blobs / lc_blob_data: each distinct file once, keyed by its sha256
  (lc_blobs.id is the hex sha256), so a duplicate upload stores nothing
  new and the bytes can be re-hashed to check integrity for decades.
  The bytes live in Postgres (bytea, TOASTed) so they are in every
  pg_dump backup with the rest of the record, and nothing is written
  under the app's directory.
- lc_document_pages: each page's text, extracted once per blob, with a
  generated tsvector and a GIN index for full-text search. Search
  results cite document and page.
- lc_documents: an org's document: a blob plus filename, kind, title.
  Soft-deleted (deleted_at), never dropped.
- lc_document_links: a document (or one page of it) linked to a position,
  asset, space, system or Cx phase.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 12
NAME = "lifecycle document store"

BLOBS = """
CREATE TABLE IF NOT EXISTS lc_blobs (
    id          TEXT PRIMARY KEY CHECK (id ~ '^[0-9a-f]{64}$'),  -- the sha256
    size        BIGINT NOT NULL,
    media_type  TEXT NOT NULL,
    page_count  INT,
    text_status TEXT NOT NULL,      -- extracted | no_text | unsupported | failed: ...
    created_at  TIMESTAMPTZ NOT NULL
)"""

BLOB_DATA = """
CREATE TABLE IF NOT EXISTS lc_blob_data (
    id   TEXT PRIMARY KEY REFERENCES lc_blobs(id),
    data BYTEA NOT NULL
)"""

PAGES = """
CREATE TABLE IF NOT EXISTS lc_document_pages (
    id      TEXT PRIMARY KEY,           -- <sha256>:<page>
    blob_id TEXT NOT NULL REFERENCES lc_blobs(id),
    page    INT NOT NULL,
    text    TEXT NOT NULL,
    tsv     tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
)"""

DOCUMENTS = """
CREATE TABLE IF NOT EXISTS lc_documents (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL REFERENCES orgs(id),
    site_id     TEXT REFERENCES sites(id),
    blob_id     TEXT NOT NULL REFERENCES lc_blobs(id),
    filename    TEXT NOT NULL,
    media_type  TEXT NOT NULL,
    size        BIGINT NOT NULL,
    title       TEXT,
    kind        TEXT NOT NULL,
    notes       TEXT,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL,
    deleted_at  TIMESTAMPTZ,
    deleted_by  TEXT
)"""

LINKS = """
CREATE TABLE IF NOT EXISTS lc_document_links (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL REFERENCES orgs(id),
    document_id TEXT NOT NULL REFERENCES lc_documents(id),
    target_kind TEXT NOT NULL CHECK (target_kind IN ('position', 'asset', 'space', 'system', 'phase')),
    target      TEXT NOT NULL,
    page        INT,
    note        TEXT,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL
)"""


def plan(conn) -> list[Step]:
    return [
        Step(BLOBS, "lc_blobs: each distinct file once, by sha256"),
        Step(BLOB_DATA, "lc_blob_data: the bytes"),
        Step(PAGES, "lc_document_pages: page text with a tsvector"),
        Step("CREATE INDEX IF NOT EXISTS lc_document_pages_tsv ON lc_document_pages USING GIN (tsv)", "full-text index"),
        Step("CREATE INDEX IF NOT EXISTS lc_document_pages_blob ON lc_document_pages (blob_id, page)", "pages by blob"),
        Step(DOCUMENTS, "lc_documents: an org's documents"),
        Step("CREATE INDEX IF NOT EXISTS lc_documents_org ON lc_documents (org_id, created_at DESC)", "documents by org"),
        Step("CREATE UNIQUE INDEX IF NOT EXISTS lc_documents_one_per_blob ON lc_documents (org_id, blob_id) "
             "WHERE deleted_at IS NULL", "one live document per file per org"),
        Step(LINKS, "lc_document_links: documents linked to positions, assets, spaces, systems, phases"),
        Step("CREATE INDEX IF NOT EXISTS lc_document_links_target ON lc_document_links (org_id, target_kind, target)",
             "links by target"),
        Step("CREATE INDEX IF NOT EXISTS lc_document_links_document ON lc_document_links (document_id)", "links by document"),
    ]

"""
0005 warranties on assets (#198, epic #221).

lc_warranties: one row per warranty on an asset (parts, labour, an
extended term), with its start and end dates, its terms, and where it
came from (`source`: the document, page and quoted text, when it was read
from one). Corrections are audited in record_edits; a warranty entered in
error is voided (voided_at, voided_reason), never deleted.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 5
NAME = "lifecycle warranties"

WARRANTIES = """
CREATE TABLE IF NOT EXISTS lc_warranties (
    id            TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL REFERENCES orgs(id),
    asset_id      TEXT NOT NULL REFERENCES lc_assets(id),
    kind          TEXT NOT NULL,          -- parts | labor | parts_and_labor | extended | other
    provider      TEXT,
    starts_on     DATE NOT NULL,
    ends_on       DATE NOT NULL CHECK (ends_on >= starts_on),
    terms         TEXT,
    source        JSONB,                  -- {document_id, sha256, page, quote}
    created_by    TEXT,
    created_at    TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL,
    voided_at     TIMESTAMPTZ,
    voided_reason TEXT
)"""


def plan(conn) -> list[Step]:
    return [
        Step(WARRANTIES, "lc_warranties: warranties on assets, with their source page"),
        Step("CREATE INDEX IF NOT EXISTS lc_warranties_asset ON lc_warranties (asset_id)", "warranties by asset"),
        Step("CREATE INDEX IF NOT EXISTS lc_warranties_org_end ON lc_warranties (org_id, ends_on)",
             "warranties by end date (expiry alerts)"),
    ]

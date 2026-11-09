"""
0006 spare parts by asset type (#204, epic #221): COBie's Spare sheet,
which some Cx tools skip, kept as the operator's spares list.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 6
NAME = "lifecycle spares"

SPARES = """
CREATE TABLE IF NOT EXISTS lc_spares (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL REFERENCES orgs(id),
    type_name   TEXT NOT NULL,
    name        TEXT NOT NULL,
    category    TEXT,
    suppliers   TEXT,
    set_number  TEXT,
    part_number TEXT,
    description TEXT,
    source      JSONB,                    -- e.g. {"cobie": "<file sha256>", "row": 12}
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL
)"""


def plan(conn) -> list[Step]:
    return [
        Step(SPARES, "lc_spares: spare parts per asset type"),
        Step("CREATE INDEX IF NOT EXISTS lc_spares_type ON lc_spares (org_id, type_name)", "by type"),
    ]

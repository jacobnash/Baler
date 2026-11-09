"""
0020 requirement packs (#205, epic #221 §3.2): standards as versioned data,
read and written by baler.commissioning.packs_api.

- cx_packs: one row per (org, pack_id, version). `requirements` holds the
  rows (citation, applicability, criterion, evidence window, the existing
  engine that checks it, who confirmed the numbers); `history` is who did
  what to the version. A draft is edited; a published version is frozen.
- cx_project_packs: which pack version a project is judged by. Adopting a
  newer version retires the older adoption (retired_at), never deletes it,
  so the version a building was accepted under stays on record.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 20
NAME = "requirement packs"

PACKS = """
CREATE TABLE IF NOT EXISTS cx_packs (
    id           TEXT PRIMARY KEY,
    org_id       TEXT NOT NULL,
    pack_id      TEXT NOT NULL,
    version      TEXT NOT NULL,
    status       TEXT NOT NULL,             -- draft | published | retired
    title        TEXT NOT NULL,
    source       JSONB NOT NULL,            -- standard, edition, publisher, kind
    jurisdiction JSONB NOT NULL DEFAULT '[]',
    notice       TEXT,
    requirements JSONB NOT NULL,
    history      JSONB NOT NULL DEFAULT '[]',
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL,
    published_by TEXT,
    published_at TIMESTAMPTZ,
    UNIQUE (org_id, pack_id, version)
)"""

ADOPTED = """
CREATE TABLE IF NOT EXISTS cx_project_packs (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES cx_projects(id),
    pack_ref    TEXT NOT NULL REFERENCES cx_packs(id),
    pack_id     TEXT NOT NULL,
    version     TEXT NOT NULL,
    adopted_by  TEXT,
    adopted_at  TIMESTAMPTZ NOT NULL,
    retired_at  TIMESTAMPTZ
)"""


def plan(conn) -> list[Step]:
    return [
        Step(PACKS, "cx_packs: requirement pack versions per org"),
        Step(ADOPTED, "cx_project_packs: the pack version each project is judged by"),
        Step("CREATE INDEX IF NOT EXISTS cx_project_packs_project ON cx_project_packs (project_id)", "adoptions by project"),
    ]

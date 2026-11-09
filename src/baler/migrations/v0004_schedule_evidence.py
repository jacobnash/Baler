"""
0004 schedule evidence (#218, epic #221 §9): links from schedule activities
(by activity code, so they survive a re-import) to the evidence that shows
them done, and the sign-offs a person records for form evidence. Read and
written by baler.commissioning.earned_api.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 4
NAME = "schedule evidence"

LINKS = """
CREATE TABLE IF NOT EXISTS cx_schedule_links (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL REFERENCES cx_projects(id),
    activity_code TEXT NOT NULL,
    kind          TEXT NOT NULL,            -- points_online | procedure_passed | form_signed
    params        JSONB NOT NULL,
    note          TEXT,
    created_by    TEXT,
    created_at    TIMESTAMPTZ NOT NULL
)"""

SIGNOFFS = """
CREATE TABLE IF NOT EXISTS cx_evidence_signoffs (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES cx_projects(id),
    link_id         TEXT NOT NULL REFERENCES cx_schedule_links(id),
    signed_by       TEXT,
    signed_by_email TEXT,
    signed_at       TIMESTAMPTZ NOT NULL,
    document        JSONB,                  -- {title, ref}: until the document store (#201) can be pointed at
    note            TEXT
)"""


def plan(conn) -> list[Step]:
    return [
        Step(LINKS, "cx_schedule_links: activity -> evidence"),
        Step("CREATE INDEX IF NOT EXISTS cx_schedule_links_project ON cx_schedule_links (project_id, activity_code)", "links by activity"),
        Step(SIGNOFFS, "cx_evidence_signoffs: form evidence signed by a person"),
    ]

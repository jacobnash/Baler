"""
0008 manual commissioning results (Baler#275 slice a), and who closed an
issue.

- cx_manual_results: a person's pass / fail / N-A for one procedure step on
  one equipment in one test run, with who (sub, email, name, role - always
  the person who sent it), when (recorded_at; observed_at for an entry made
  offline), note, witness, attachment list, the device's client_id (one row
  per entry however often a phone resends it) and every earlier version in
  history.
- cx_manual_attachments: the files, content-addressed by sha256.
- cx_issues.closed_by: the person who closed an issue (it was only on the
  close event's JWT claims before).
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 8
NAME = "manual commissioning results"

RESULTS = """
CREATE TABLE IF NOT EXISTS cx_manual_results (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES cx_projects(id),
    procedure_id   TEXT NOT NULL,
    test_run_id    TEXT NOT NULL,
    equip_uri      TEXT NOT NULL,
    step_id        TEXT NOT NULL,
    status         TEXT NOT NULL,          -- pass | fail | not_applicable
    note           TEXT,
    witness        JSONB,                  -- {name, org, role}
    attachments    JSONB NOT NULL DEFAULT '[]',
    recorded_by    TEXT NOT NULL,          -- the caller's sub, never someone else's
    recorded_email TEXT,
    recorded_name  TEXT,
    recorded_role  TEXT,
    recorded_at    TIMESTAMPTZ NOT NULL,
    observed_at    TIMESTAMPTZ NOT NULL,
    client_id      TEXT,
    history        JSONB NOT NULL DEFAULT '[]',
    updated_at     TIMESTAMPTZ NOT NULL
)"""
BY_RUN = "CREATE INDEX IF NOT EXISTS cx_manual_results_run ON cx_manual_results (test_run_id, equip_uri, step_id)"
ONE_PER_ENTRY = """CREATE UNIQUE INDEX IF NOT EXISTS cx_manual_results_client
    ON cx_manual_results (project_id, client_id) WHERE client_id IS NOT NULL"""
ATTACHMENTS = """
CREATE TABLE IF NOT EXISTS cx_manual_attachments (
    id         TEXT PRIMARY KEY,
    result_id  TEXT NOT NULL REFERENCES cx_manual_results(id),
    project_id TEXT NOT NULL,
    filename   TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size       INTEGER NOT NULL,
    sha256     TEXT NOT NULL,
    data       BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
)"""
CLOSED_BY = "ALTER TABLE cx_issues ADD COLUMN IF NOT EXISTS closed_by TEXT"


def plan(conn) -> list[Step]:
    return [
        Step(RESULTS, "cx_manual_results: a person's step results"),
        Step(BY_RUN, "cx_manual_results: a test run's results"),
        Step(ONE_PER_ENTRY, "cx_manual_results: an offline entry resent is stored once"),
        Step(ATTACHMENTS, "cx_manual_attachments: photos and signed sheets"),
        Step(CLOSED_BY, "cx_issues.closed_by: who closed it"),
    ]

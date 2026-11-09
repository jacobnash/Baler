"""
0005 compliance log (#207, epic #221 §3.3): adopted packs run on a schedule
after acceptance, read and written by baler.commissioning.compliance.

- cx_compliance_runs: one per run (schedule or a person), its summary and
  the alerts it opened/resolved.
- cx_compliance_log: one row per space and requirement per run: status,
  window, evidence (per point: samples, min/max, coverage, exceedances),
  the pack version and citation it was judged by. Kept for surveys.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 5
NAME = "compliance log"

RUNS = """
CREATE TABLE IF NOT EXISTS cx_compliance_runs (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES cx_projects(id),
    status       TEXT NOT NULL,             -- queued | running | completed | failed
    triggered_by TEXT,                      -- a user sub, or 'schedule'
    window_end   TIMESTAMPTZ NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL,
    finished_at  TIMESTAMPTZ,
    summary      JSONB,
    error        TEXT,
    created_at   TIMESTAMPTZ NOT NULL
)"""

LOG = """
CREATE TABLE IF NOT EXISTS cx_compliance_log (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES cx_compliance_runs(id),
    project_id   TEXT NOT NULL REFERENCES cx_projects(id),
    space_id     TEXT NOT NULL REFERENCES cx_spaces(id),
    pack_ref     TEXT NOT NULL REFERENCES cx_packs(id),
    pack_id      TEXT NOT NULL,
    version      TEXT NOT NULL,
    row_id       TEXT NOT NULL,
    title        TEXT NOT NULL,
    citation     JSONB NOT NULL,
    status       TEXT NOT NULL,             -- pass | fail | inconclusive
    window_start TIMESTAMPTZ NOT NULL,
    window_end   TIMESTAMPTZ NOT NULL,
    evidence     JSONB NOT NULL,
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL
)"""


def plan(conn) -> list[Step]:
    return [
        Step(RUNS, "cx_compliance_runs: scheduled and on-demand compliance runs"),
        Step("CREATE INDEX IF NOT EXISTS cx_compliance_runs_project ON cx_compliance_runs (project_id, started_at)", "runs by project"),
        Step(LOG, "cx_compliance_log: per space, per requirement, per run"),
        Step("CREATE INDEX IF NOT EXISTS cx_compliance_log_space ON cx_compliance_log (space_id, window_end)", "log by space"),
        Step("CREATE INDEX IF NOT EXISTS cx_compliance_log_run ON cx_compliance_log (run_id)", "log by run"),
    ]

"""
0024 savings cases (#216, epic #221 §8.3): replace-vs-keep cases with every
assumption stated (value, unit, source: measured | user | default, note),
the computed result, and the history of edits. Read and written by
baler.commissioning.savings_api.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 24
NAME = "savings cases"

CASES = """
CREATE TABLE IF NOT EXISTS cx_savings_cases (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES cx_projects(id),
    equip_uri   TEXT NOT NULL,
    asset_id    TEXT,
    baseline_id TEXT REFERENCES cx_performance_baselines(id),
    title       TEXT NOT NULL,
    assumptions JSONB NOT NULL,
    result      JSONB NOT NULL,
    history     JSONB NOT NULL DEFAULT '[]',
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL
)"""


def plan(conn) -> list[Step]:
    return [
        Step(CASES, "cx_savings_cases: replace-vs-keep cases with their assumptions"),
        Step("CREATE INDEX IF NOT EXISTS cx_savings_cases_project ON cx_savings_cases (project_id)", "cases by project"),
    ]

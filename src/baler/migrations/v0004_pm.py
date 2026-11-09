"""
0004 preventive maintenance schedules (#199, epic #221).

- lc_pm_tasks: a PM task for one asset, or for every installed asset of a
  type. Triggered by the calendar (interval_days), by run hours counted
  from a status point's raw history (interval_hours), or by a condition
  on a point (filter differential pressure over a limit). Points can be
  given relative to the position (`sf-status` under the asset's position).
- lc_pm_completions: append-only record of PM done on an asset; the next
  due date or run-hour count starts from the latest one.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 4
NAME = "lifecycle pm schedules"

TASKS = """
CREATE TABLE IF NOT EXISTS lc_pm_tasks (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL REFERENCES orgs(id),
    title           TEXT NOT NULL,
    description     TEXT,
    asset_id        TEXT REFERENCES lc_assets(id),
    asset_type      TEXT,
    trigger         TEXT NOT NULL CHECK (trigger IN ('calendar', 'run_hours', 'condition')),
    interval_days   INT,
    interval_hours  DOUBLE PRECISION,
    run_point       TEXT,
    condition_point TEXT,
    condition_op    TEXT,
    condition_value DOUBLE PRECISION,
    condition_unit  TEXT,
    duration_hours  DOUBLE PRECISION,
    source          JSONB,
    status          TEXT NOT NULL,          -- active | paused
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    CHECK ((asset_id IS NULL) <> (asset_type IS NULL))
)"""

COMPLETIONS = """
CREATE TABLE IF NOT EXISTS lc_pm_completions (
    id         TEXT PRIMARY KEY,
    org_id     TEXT NOT NULL REFERENCES orgs(id),
    task_id    TEXT NOT NULL REFERENCES lc_pm_tasks(id),
    asset_id   TEXT NOT NULL REFERENCES lc_assets(id),
    done_at    TIMESTAMPTZ NOT NULL,
    run_hours  DOUBLE PRECISION,
    note       TEXT,
    work_ref   TEXT,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL
)"""


def plan(conn) -> list[Step]:
    return [
        Step(TASKS, "lc_pm_tasks: PM tasks by calendar, run hours or condition"),
        Step("CREATE INDEX IF NOT EXISTS lc_pm_tasks_org ON lc_pm_tasks (org_id, status)", "tasks by org"),
        Step(COMPLETIONS, "lc_pm_completions: PM done, append-only"),
        Step("CREATE INDEX IF NOT EXISTS lc_pm_completions_task ON lc_pm_completions (task_id, asset_id, done_at)",
             "completions by task and asset"),
    ]

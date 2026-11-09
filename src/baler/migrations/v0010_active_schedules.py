"""
0010 scheduled active retests (Baler#275 slice e): a fleet run that
repeats - seasonal retesting - under an admin approval that names its
scope and window (baler.commissioning.retest_schedules).

- cx_active_schedules: the schedule (scope: equipment, test, limits, mode;
  timing: first window start, cadence, length, valid_until), its status, the
  approval snapshot the schedule may only narrow inside (approval), the
  hash the approver typed, who approved and with which scope (claims),
  and which window ran last and as which fleet.
- cx_fleet_runs.schedule_id: a scheduled run names its schedule.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 10
NAME = "scheduled active retests"

SCHEDULES = """
CREATE TABLE IF NOT EXISTS cx_active_schedules (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES cx_projects(id),
    name            TEXT NOT NULL,
    status          TEXT NOT NULL,          -- proposed | approved | expired | revoked
    scope           JSONB NOT NULL,         -- {equip_uris, kind, test, response_minutes, limits..., mode}
    timing          JSONB NOT NULL,         -- {first_window_start, window_minutes, every_days, valid_until}
    approval        JSONB,                  -- the approved {scope, timing, hash}: PATCH only narrows inside it
    approval_hash   TEXT NOT NULL,          -- sha256 of scope + timing; the approver types it
    approved_by     TEXT,
    approved_at     TIMESTAMPTZ,
    approver_claims JSONB,                  -- {org_id, site_ids}: whose view of the inventory runs plan from
    valid_until     TIMESTAMPTZ NOT NULL,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    last_occurrence INTEGER,                -- the last scheduled window handled (run or skipped)
    last_fleet_id   TEXT,
    revoked_by      TEXT,
    revoked_at      TIMESTAMPTZ,
    history         JSONB NOT NULL DEFAULT '[]'
)"""
BY_PROJECT = "CREATE INDEX IF NOT EXISTS cx_active_schedules_project ON cx_active_schedules (project_id, created_at DESC)"
SCHEDULE_ID = "ALTER TABLE cx_fleet_runs ADD COLUMN IF NOT EXISTS schedule_id TEXT"


def plan(conn) -> list[Step]:
    return [
        Step(SCHEDULES, "cx_active_schedules: approved, repeating fleet retests"),
        Step(BY_PROJECT, "cx_active_schedules: a project's schedules, newest first"),
        Step(SCHEDULE_ID, "cx_fleet_runs.schedule_id: a scheduled run's schedule"),
    ]

"""
0003 construction schedules (#217, epic #221 §9): imported P6 XER / MS
Project XML schedules and their activities, read and written by
baler.commissioning.schedules_api.

- cx_schedules: one row per import (a version): file name, sha256, format,
  the source's project name and data date, how many activities.
- cx_schedule_activities: code, name, WBS, planned and actual dates as the
  file states them (local wall time, no zone invented), percent complete,
  status, milestone/summary flags, predecessor codes.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 3
NAME = "schedules"

SCHEDULES = """
CREATE TABLE IF NOT EXISTS cx_schedules (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES cx_projects(id),
    filename       TEXT NOT NULL,
    sha256         TEXT NOT NULL,
    format         TEXT NOT NULL,           -- xer | mspdi
    source_project TEXT,
    data_date      TEXT,
    activities     INTEGER NOT NULL,
    info           JSONB,
    created_by     TEXT,
    created_at     TIMESTAMPTZ NOT NULL
)"""

ACTIVITIES = """
CREATE TABLE IF NOT EXISTS cx_schedule_activities (
    id               TEXT PRIMARY KEY,
    schedule_id      TEXT NOT NULL REFERENCES cx_schedules(id),
    project_id       TEXT NOT NULL REFERENCES cx_projects(id),
    code             TEXT NOT NULL,
    name             TEXT NOT NULL,
    wbs              TEXT,
    planned_start    TEXT,
    planned_finish   TEXT,
    actual_start     TEXT,
    actual_finish    TEXT,
    percent_complete DOUBLE PRECISION,
    status           TEXT NOT NULL,         -- not_started | in_progress | complete
    milestone        BOOLEAN NOT NULL DEFAULT FALSE,
    summary          BOOLEAN NOT NULL DEFAULT FALSE,
    predecessors     JSONB NOT NULL DEFAULT '[]'
)"""


def plan(conn) -> list[Step]:
    return [
        Step(SCHEDULES, "cx_schedules: imported schedule versions"),
        Step(ACTIVITIES, "cx_schedule_activities: their activities"),
        Step("CREATE INDEX IF NOT EXISTS cx_schedule_activities_schedule ON cx_schedule_activities (schedule_id, planned_start)",
             "activities by schedule"),
    ]

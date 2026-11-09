"""
0009 fleet active runs (Baler#275 slice d): one active test across many
equipment in one window (baler.commissioning.fleet).

- cx_fleet_runs: the fleet - its test and limits (params), every member
  with its steps, system, slot, child run id and outcome (members), the dry
  run listing every write (plan) and the hash the typed confirmation must
  repeat (plan_hash), the coordinator's heartbeat, and why it stopped.
- cx_active_runs.fleet_id: a member's own active run names its fleet, so
  the sweeper can relinquish a stopped or crashed fleet's levels from
  persisted state alone.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 9
NAME = "fleet active runs"

FLEETS = """
CREATE TABLE IF NOT EXISTS cx_fleet_runs (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES cx_projects(id),
    kind            TEXT NOT NULL,          -- command_response | library:<test id>
    params          JSONB NOT NULL,         -- test, concurrency_per_system, max_concurrent, stagger_seconds, max_failures
    members         JSONB NOT NULL,         -- [{equip_uri, system, steps, slot_start, slot_end, state, run_id, outcome}]
    skipped         JSONB NOT NULL DEFAULT '[]',
    plan            JSONB NOT NULL,         -- the dry run: every write across the fleet
    plan_hash       TEXT NOT NULL,          -- sha256 the typed confirmation repeats
    status          TEXT NOT NULL,          -- planned | confirmed | running | completed | stopped | aborted | expired | failed | cancelled
    window_start    TIMESTAMPTZ NOT NULL,
    window_end      TIMESTAMPTZ NOT NULL,
    retest_of       TEXT,                   -- the fleet whose failures this retests
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL,
    confirmed_by    TEXT,
    confirmed_at    TIMESTAMPTZ,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    heartbeat_at    TIMESTAMPTZ,
    failures        INTEGER NOT NULL DEFAULT 0,
    stop_reason     TEXT,
    error           TEXT,
    abort_requested BOOLEAN NOT NULL DEFAULT false
)"""
BY_PROJECT = "CREATE INDEX IF NOT EXISTS cx_fleet_runs_project ON cx_fleet_runs (project_id, created_at DESC)"
FLEET_ID = "ALTER TABLE cx_active_runs ADD COLUMN IF NOT EXISTS fleet_id TEXT"
BY_FLEET = "CREATE INDEX IF NOT EXISTS cx_active_runs_fleet ON cx_active_runs (fleet_id) WHERE fleet_id IS NOT NULL"


def plan(conn) -> list[Step]:
    return [
        Step(FLEETS, "cx_fleet_runs: one active test across many equipment"),
        Step(BY_PROJECT, "cx_fleet_runs: a project's fleets, newest first"),
        Step(FLEET_ID, "cx_active_runs.fleet_id: a member run's fleet"),
        Step(BY_FLEET, "cx_active_runs: a fleet's member runs"),
    ]

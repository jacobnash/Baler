"""
0003 audited operator writes (Baler#213): every point, schedule and
calendar write a person makes from the UI, with who, why, the level, when
it ends and how it ended, the before-state and a log of every command sent
(baler.commissioning.operator_writes).
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 3
NAME = "operator writes"

WRITES = """
CREATE TABLE IF NOT EXISTS cx_operator_writes (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL REFERENCES cx_projects(id),
    kind          TEXT NOT NULL,           -- point | schedule | calendar
    target        TEXT NOT NULL,           -- urn:point: URI, or <site>/<instance>/<connection>/<object>
    equip_uri     TEXT,
    site          TEXT,
    instance      TEXT,
    connection_id TEXT,
    level         SMALLINT,                -- priority 8-16 (point writes)
    value         JSONB,                   -- the value, or {kind, object, fields} for a schedule/calendar
    before        JSONB,                   -- priority array + latest value, or the object as read before
    reason        TEXT NOT NULL,
    who           TEXT,                    -- the person's email (or sub)
    user_sub      TEXT,
    role          TEXT,
    status        TEXT NOT NULL,           -- pending | active | permanent | relinquished | expired | superseded | reverted | conflict | failed
    expires_at    TIMESTAMPTZ,             -- auto-relinquish / revert time
    created_at    TIMESTAMPTZ NOT NULL,
    ended_at      TIMESTAMPTZ,
    ended_by      TEXT,
    end_reason    TEXT,
    error         TEXT,
    log           JSONB NOT NULL DEFAULT '[]'
)"""
BY_PROJECT = "CREATE INDEX IF NOT EXISTS cx_operator_writes_project ON cx_operator_writes (project_id, created_at DESC)"
ACTIVE = "CREATE INDEX IF NOT EXISTS cx_operator_writes_active ON cx_operator_writes (expires_at) WHERE status = 'active'"


def plan(conn) -> list[Step]:
    return [
        Step(WRITES, "cx_operator_writes: audited operator point/schedule/calendar writes"),
        Step(BY_PROJECT, "cx_operator_writes: a project's writes, newest first"),
        Step(ACTIVE, "cx_operator_writes: what the sweeper ends next"),
    ]

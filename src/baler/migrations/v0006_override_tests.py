"""
0006 passive override tests (Baler#212): what Windrow reads off the
controllers that the override detector needs.

- bacnet_schedules: the latest snapshot of each BACnet Schedule and
  Calendar object on an in-scope connection, exactly as Windrow's
  `connection_schedules` command answers it (body), with the urn:point:
  URIs a schedule controls resolved from its labels (points). One row per
  object, replaced on each refresh; read_at says how old it is.
- point_priority_arrays: priority-array observations, from the `priority`
  key Windrow puts on a commandable point's reading (Windrow#25, PR #34;
  mqtt_listener via baler.priority_arrays): per point, the held levels. A
  row is added only when a point's levels differ from its previous row, so
  it is a change log: "level 8 was held from T" can be answered for the
  past.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 6
NAME = "override tests: schedules and priority arrays"

SCHEDULES = """
CREATE TABLE IF NOT EXISTS bacnet_schedules (
    id            TEXT PRIMARY KEY,          -- <site>/<instance>/<connection>/<object>
    site          TEXT NOT NULL,
    instance      TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    topic_prefix  TEXT NOT NULL,
    kind          TEXT NOT NULL,             -- schedule | calendar
    object        TEXT NOT NULL,             -- "schedule,1"
    name          TEXT,
    body          JSONB NOT NULL,            -- Windrow's JSON for the object
    points        TEXT[] NOT NULL DEFAULT '{}',  -- urn:point: URIs it controls
    read_at       TIMESTAMPTZ NOT NULL
)"""
SCHEDULE_POINTS = "CREATE INDEX IF NOT EXISTS bacnet_schedules_points ON bacnet_schedules USING GIN (points)"
SCHEDULE_PREFIX = "CREATE INDEX IF NOT EXISTS bacnet_schedules_prefix ON bacnet_schedules (topic_prefix)"

PRIORITY = """
CREATE TABLE IF NOT EXISTS point_priority_arrays (
    point_uri   TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    levels      JSONB NOT NULL,              -- {"8": {"value": 68.0}, "16": {"value": 72.0}}
    PRIMARY KEY (point_uri, observed_at)
)"""


def plan(conn) -> list[Step]:
    return [
        Step(SCHEDULES, "bacnet_schedules: schedule/calendar snapshots read from Windrow"),
        Step(SCHEDULE_POINTS, "bacnet_schedules: find the schedules controlling a point"),
        Step(SCHEDULE_PREFIX, "bacnet_schedules: list a connection's objects"),
        Step(PRIORITY, "point_priority_arrays: priority-array change log"),
    ]

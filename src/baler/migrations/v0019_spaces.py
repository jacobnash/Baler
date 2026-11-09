"""
0019 spaces (#209, epic #221 §4): rooms, zones, suites and floors of a
project's building, which rooms are next to which, and the terminal units
that serve each one (the rest of the chain is the graph's brick:feeds).
Read and written by baler.commissioning.spaces_api.

- cx_spaces: kind (building | floor | suite | zone | room), parent_id for
  the hierarchy, space_type (the slug a requirement pack's
  applies_to.space_type matches), optional REC/Brick class, the room's own
  points, attributes (area, volume). Archived, never deleted.
- cx_space_adjacency: one row per pair (space_id < other_id), relation
  adjacent | door.
- cx_space_serving: space -> the equipment (a position, never an asset)
  that serves it directly.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 19
NAME = "spaces"

SPACES = """
CREATE TABLE IF NOT EXISTS cx_spaces (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES cx_projects(id),
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,              -- building | floor | suite | zone | room
    space_type  TEXT,                       -- operating_room, corridor, ... (packs match this)
    parent_id   TEXT REFERENCES cx_spaces(id),
    rec_class   TEXT,
    brick_class TEXT,
    point_uris  JSONB NOT NULL DEFAULT '[]',
    attributes  JSONB NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL,              -- active | archived
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL
)"""

ADJACENCY = """
CREATE TABLE IF NOT EXISTS cx_space_adjacency (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES cx_projects(id),
    space_id    TEXT NOT NULL REFERENCES cx_spaces(id),
    other_id    TEXT NOT NULL REFERENCES cx_spaces(id),
    relation    TEXT NOT NULL,              -- adjacent | door
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL,
    CHECK (space_id < other_id),
    UNIQUE (space_id, other_id)
)"""

SERVING = """
CREATE TABLE IF NOT EXISTS cx_space_serving (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES cx_projects(id),
    space_id    TEXT NOT NULL REFERENCES cx_spaces(id),
    equip_uri   TEXT NOT NULL,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL,
    UNIQUE (space_id, equip_uri)
)"""


def plan(conn) -> list[Step]:
    return [
        Step(SPACES, "cx_spaces: rooms, zones, suites, floors"),
        Step("CREATE INDEX IF NOT EXISTS cx_spaces_project ON cx_spaces (project_id, status)", "cx_spaces by project"),
        Step(ADJACENCY, "cx_space_adjacency: which spaces are next to which"),
        Step("CREATE INDEX IF NOT EXISTS cx_space_adjacency_project ON cx_space_adjacency (project_id)", "adjacency by project"),
        Step(SERVING, "cx_space_serving: the terminal units serving each space"),
        Step("CREATE INDEX IF NOT EXISTS cx_space_serving_equip ON cx_space_serving (project_id, equip_uri)", "serving by equipment"),
    ]

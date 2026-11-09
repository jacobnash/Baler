"""
Spaces: rooms, zones, suites and floors, their types, which rooms are next
to which, and the equipment that serves them (Baler#209, epic #221 §4).

Requirements like pressure relationship and air changes belong to a room,
not to an AHU, and "is the surgery suite ready" is a question over rooms.
Until this Baler modelled equipment and points only.

- A space has a `kind` (building, floor, suite, zone, room) and an optional
  `parent_id`, so a suite is the rooms under it.
- A room has a `space_type`, a slug such as `operating_room`. That slug is
  what a requirement pack's `applies_to.space_type` matches (#205). The
  built-in types below are a starting list; any slug is allowed.
- Locations follow RealEstateCore and equipment follows Brick (their
  published alignment: REC leads location classes, Brick leads equipment).
  A space may name its `rec_class` and/or `brick_class`. We only pre-fill a
  Brick class where Brick 1.4's vendored ontology really has one (checked
  in tests/test_spaces.py); REC class names are left for the user to set
  from the REC release they use, never guessed here.
- Adjacency is between two spaces of the same project, stored once
  (space_id < other_id), with a relation: `adjacent` or `door` (shares a
  door: what a pressure relationship is judged across).
- Serving equipment: a space lists the equipment that serves it directly
  (its VAV or terminal unit, by equip URI). The rest of the chain
  (terminal -> AHU -> plant) is read from the graph's `brick:feeds`, so it
  is never entered twice.

Positions and assets stay separate (#197): a space is served by a
position (the Brick equipment record), never by the asset bolted there.
"""

from __future__ import annotations

import re

KINDS = ("building", "floor", "suite", "zone", "room")
RELATIONS = ("adjacent", "door")
MAX_POINTS = 50
MAX_SERVING = 20
CHAIN_DEPTH = 6
_SLUG = re.compile(r"[a-z][a-z0-9_]{0,59}")
_CLASS = re.compile(r"[A-Za-z][A-Za-z0-9_:.#/-]{0,119}")

# A starting list, not a standard. `brick_class` only where Brick has the
# class (Brick has no operating-room class: REC's location classes lead).
SPACE_TYPES: dict[str, dict] = {
    "operating_room": {"label": "Operating room", "brick_class": "Room"},
    "procedure_room": {"label": "Procedure room", "brick_class": "Room"},
    "airborne_isolation_room": {"label": "Airborne infection isolation room", "brick_class": "Room"},
    "protective_environment_room": {"label": "Protective environment room", "brick_class": "Room"},
    "sterile_storage": {"label": "Sterile storage", "brick_class": "Storage_Room"},
    "patient_room": {"label": "Patient room", "brick_class": "Room"},
    "laboratory": {"label": "Laboratory", "brick_class": "Laboratory"},
    "cleanroom": {"label": "Cleanroom", "brick_class": "Room"},
    "server_room": {"label": "Server room / data hall", "brick_class": "Server_Room"},
    "office": {"label": "Office", "brick_class": "Office"},
    "corridor": {"label": "Corridor", "brick_class": "Hallway"},
    "mechanical_room": {"label": "Mechanical room", "brick_class": "Mechanical_Room"},
}


def _text(v, what: str, limit: int = 200, required: bool = False) -> str | None:
    if v is None or v == "":
        if required:
            raise ValueError(f"{what} is required")
        return None
    if not isinstance(v, str) or len(v.strip()) > limit:
        raise ValueError(f"{what} must be a string of at most {limit} characters")
    return v.strip()


def normalize(body: dict, existing: dict | None = None) -> dict:
    """The editable fields of a space, checked. With `existing`, only the
    fields in `body` change (PATCH)."""
    if not isinstance(body, dict):
        raise TypeError("a space is an object")
    unknown = set(body) - {"name", "kind", "space_type", "parent_id", "rec_class", "brick_class", "point_uris", "attributes"}
    if unknown:
        raise ValueError(f"unknown space fields: {', '.join(sorted(unknown))}")
    out = dict(existing or {})
    merged = {**out, **body}
    out["name"] = _text(merged.get("name"), "name", required=True)
    kind = merged.get("kind") or "room"
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    out["kind"] = kind
    st = merged.get("space_type")
    if st is not None and (not isinstance(st, str) or not _SLUG.fullmatch(st)):
        raise ValueError("space_type is a slug like operating_room (lowercase letters, digits, _)")
    out["space_type"] = st
    out["parent_id"] = _text(merged.get("parent_id"), "parent_id", 64)
    for k in ("rec_class", "brick_class"):
        v = merged.get(k)
        if v is not None and (not isinstance(v, str) or not _CLASS.fullmatch(v)):
            raise ValueError(f"{k} is a class name like Room or rec:Room")
        out[k] = v
    if out["brick_class"] is None and st in SPACE_TYPES and "brick_class" not in body:
        out["brick_class"] = SPACE_TYPES[st]["brick_class"]
    pts = merged.get("point_uris") or []
    if not isinstance(pts, list) or len(pts) > MAX_POINTS or not all(isinstance(p, str) and p.startswith("urn:point:") for p in pts):
        raise ValueError(f"point_uris is a list of up to {MAX_POINTS} urn:point: ids (the room's own sensors)")
    out["point_uris"] = list(dict.fromkeys(pts))
    attrs = merged.get("attributes") or {}
    if not isinstance(attrs, dict) or len(attrs) > 30:
        raise ValueError("attributes is an object of up to 30 fields (e.g. area_ft2, volume_ft3)")
    for k in ("area_ft2", "volume_ft3", "area_m2", "volume_m3"):
        v = attrs.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0):
            raise ValueError(f"attributes.{k} must be a positive number")
    out["attributes"] = attrs
    return out


def check_parent(space_id: str | None, parent_id: str | None, by_id: dict[str, dict]) -> None:
    """The parent exists in the same project and isn't the space itself or
    one of its descendants (no cycles)."""
    if parent_id is None:
        return
    if parent_id not in by_id:
        raise ValueError("parent_id is not a space in this project")
    seen = set()
    cur: str | None = parent_id
    while cur is not None:
        if cur == space_id or cur in seen:
            raise ValueError("parent_id would make a cycle")
        seen.add(cur)
        cur = by_id[cur].get("parent_id") if cur in by_id else None


def descendants(space_id: str, spaces: list[dict]) -> list[dict]:
    """Every space under `space_id` (the rooms of a suite), depth first."""
    kids: dict[str | None, list[dict]] = {}
    for s in spaces:
        kids.setdefault(s.get("parent_id"), []).append(s)
    out: list[dict] = []
    stack = list(reversed(kids.get(space_id, [])))
    while stack:
        s = stack.pop()
        out.append(s)
        stack.extend(reversed(kids.get(s["id"], [])))
    return out


def pair(a: str, b: str) -> tuple[str, str]:
    """Adjacency is stored once per pair, in a fixed order."""
    if a == b:
        raise ValueError("a space isn't adjacent to itself")
    return (a, b) if a < b else (b, a)


def neighbours(space_id: str, adjacency: list[dict]) -> list[dict]:
    """[{space_id, relation}] for the other side of each pair."""
    out = []
    for r in adjacency:
        if r["space_id"] == space_id:
            out.append({"space_id": r["other_id"], "relation": r["relation"]})
        elif r["other_id"] == space_id:
            out.append({"space_id": r["space_id"], "relation": r["relation"]})
    return sorted(out, key=lambda x: x["space_id"])


def check_serving(equip_uris) -> list[str]:
    if not isinstance(equip_uris, list) or len(equip_uris) > MAX_SERVING \
            or not all(isinstance(u, str) and u.startswith("urn:equip:") for u in equip_uris):
        raise ValueError(f"equip_uris is a list of up to {MAX_SERVING} urn:equip: ids (the terminal units serving the space)")
    return list(dict.fromkeys(equip_uris))


FEEDS_QUERY = ("PREFIX brick: <https://brickschema.org/schema/Brick#> "
               "SELECT ?up ?down WHERE {{ VALUES ?down {{ {values} }} ?up brick:feeds ?down }}")


def serving_chain(store, equip_uris: list[str], allows=lambda uri: True, depth: int = CHAIN_DEPTH) -> list[dict]:
    """The equipment upstream of the space's own terminals, by Brick
    `feeds`: [{equip_uri, feeds, level}], level 1 being the terminals
    themselves. `allows` is the caller's scope: equipment it can't read is
    left out (and not walked through). Cycles in the graph stop the walk."""
    out: list[dict] = [{"equip_uri": u, "feeds": None, "level": 1} for u in equip_uris if allows(u)]
    seen: set[str] = {x["equip_uri"] for x in out}
    frontier: list[str] = sorted(seen)
    level = 1
    while frontier and level < depth:
        level += 1
        values = " ".join(f"<{u}>" for u in frontier)
        nxt: list[str] = []
        for row in store.query(FEEDS_QUERY.format(values=values)):
            up, down = str(row.up), str(row.down)
            if up in seen or not allows(up):
                continue
            seen.add(up)
            out.append({"equip_uri": up, "feeds": down, "level": level})
            nxt.append(up)
        frontier = sorted(nxt)
    return out


def spaces_served_by(equip_uri: str, serving: list[dict]) -> list[str]:
    """Space ids a terminal serves directly (the other direction, for
    tracing a failing room's equipment back to the rooms it affects)."""
    return sorted({r["space_id"] for r in serving if r["equip_uri"] == equip_uri})

"""
HTTP routes for spaces (Baler#209), served by commissioning_api under
/cx/. Every change is a cx_events row (space.create, space.update,
space.archive, space.adjacency, space.serving) with who made it.
"""

from __future__ import annotations

import re
from typing import Any

from baler.commissioning import db, spaces
from baler.commissioning.routes import Route, owned_row, query
from baler.http_handler_base import respond_json

SPACES = "cx_spaces"
ADJ = "cx_space_adjacency"
SERVING = "cx_space_serving"

db.ROW_TABLES[SPACES] = (("id", "project_id", "name", "kind", "space_type", "parent_id", "rec_class", "brick_class",
                          "point_uris", "attributes", "status", "created_by", "created_at", "updated_at"),
                         frozenset({"point_uris", "attributes"}))
db.ROW_TABLES[ADJ] = (("id", "project_id", "space_id", "other_id", "relation", "created_by", "created_at"), frozenset())
db.ROW_TABLES[SERVING] = (("id", "project_id", "space_id", "equip_uri", "created_by", "created_at"), frozenset())


def project_spaces(repo, pid: str, include_archived: bool = False) -> list[dict]:
    rows = repo.find_rows(SPACES, order="name", project_id=pid)
    return [r for r in rows if include_archived or r["status"] != "archived"]


def public(row: dict) -> dict:
    return db.public_row(row)


def detail(h: Any, space: dict, claims: dict) -> dict:
    """A space with its neighbours, its children and the serving chain read
    from the graph in the caller's scope."""
    repo = h.REPO
    pid = space["project_id"]
    adjacency = repo.find_rows(ADJ, project_id=pid)
    serving = [r["equip_uri"] for r in repo.find_rows(SERVING, space_id=space["id"])]
    scope = repo.scope_for(claims)
    chain = spaces.serving_chain(repo.store, serving, scope.allows) if serving and repo.store is not None else []
    kids = [public(s) for s in project_spaces(repo, pid) if s.get("parent_id") == space["id"]]
    return {**public(space), "adjacent": spaces.neighbours(space["id"], adjacency), "serving": serving,
            "serving_chain": chain, "children": kids}


def _list(h, span, claims, pid) -> None:
    if h._project(pid, claims) is None:
        return
    q = query(h)
    rows = project_spaces(h.REPO, pid, q.get("include_archived", ["false"])[0] == "true")
    if q.get("space_type"):
        rows = [r for r in rows if r.get("space_type") == q["space_type"][0]]
    respond_json(h, 200, [public(r) for r in rows])


def _create(h, span, claims, pid) -> None:
    if h._project(pid, claims) is None:
        return
    fields = spaces.normalize(h.read_json_body())
    by_id = {s["id"]: s for s in project_spaces(h.REPO, pid)}
    spaces.check_parent(None, fields["parent_id"], by_id)
    now = h.NOW()
    row = {"id": db.new_id(), "project_id": pid, **fields, "status": "active", "created_by": claims.get("sub"),
           "created_at": now, "updated_at": now}
    h.REPO.put_rows(SPACES, [row])
    h._event(pid, "space.create", claims, {"space": row["id"], "name": row["name"], "space_type": row["space_type"]})
    respond_json(h, 201, public(row))


def _get(h, span, claims, sid) -> None:
    hit = owned_row(h, SPACES, sid, claims, "space")
    if hit:
        respond_json(h, 200, detail(h, hit[0], claims))


def _patch(h, span, claims, sid) -> None:
    hit = owned_row(h, SPACES, sid, claims, "space")
    if not hit:
        return
    row, _ = hit
    body = h.read_json_body()
    fields = spaces.normalize(body, {k: row[k] for k in ("name", "kind", "space_type", "parent_id", "rec_class",
                                                         "brick_class", "point_uris", "attributes")})
    spaces.check_parent(sid, fields["parent_id"], {s["id"]: s for s in project_spaces(h.REPO, row["project_id"])})
    changes = {**fields, "updated_at": h.NOW()}
    h.REPO.update_row(SPACES, sid, changes)
    h._event(row["project_id"], "space.update", claims, {"space": sid, "fields": sorted(body)})
    respond_json(h, 200, detail(h, {**row, **changes}, claims))


def _archive(h, span, claims, sid) -> None:
    hit = owned_row(h, SPACES, sid, claims, "space")
    if not hit:
        return
    row, _ = hit
    h.REPO.update_row(SPACES, sid, {"status": "archived", "updated_at": h.NOW()})
    h._event(row["project_id"], "space.archive", claims, {"space": sid, "name": row["name"]})
    h.send_response(204)
    h.end_headers()


def _put_adjacent(h, span, claims, sid) -> None:
    """Replace this space's neighbours: {"adjacent": [{space_id, relation}]}."""
    hit = owned_row(h, SPACES, sid, claims, "space")
    if not hit:
        return
    row, _ = hit
    pid = row["project_id"]
    wanted = h.read_json_body().get("adjacent")
    if not isinstance(wanted, list) or len(wanted) > 50:
        raise ValueError('body is {"adjacent": [{"space_id": ..., "relation": "adjacent" | "door"}]}, up to 50')
    ids = {s["id"] for s in project_spaces(h.REPO, pid)}
    now = h.NOW()
    rows = {}
    for w in wanted:
        other = w.get("space_id") if isinstance(w, dict) else None
        rel = (w.get("relation") if isinstance(w, dict) else None) or "adjacent"
        if other not in ids:
            raise ValueError(f"{other!r} is not a space in this project")
        if rel not in spaces.RELATIONS:
            raise ValueError(f"relation must be one of {', '.join(spaces.RELATIONS)}")
        a, b = spaces.pair(sid, other)
        rows[(a, b)] = {"id": db.new_id(), "project_id": pid, "space_id": a, "other_id": b, "relation": rel,
                        "created_by": claims.get("sub"), "created_at": now}
    h.REPO.delete_rows(ADJ, project_id=pid, space_id=sid)
    h.REPO.delete_rows(ADJ, project_id=pid, other_id=sid)
    h.REPO.put_rows(ADJ, list(rows.values()))
    h._event(pid, "space.adjacency", claims, {"space": sid, "adjacent": [{"space_id": b if a == sid else a, "relation": r["relation"]}
                                                                         for (a, b), r in rows.items()]})
    respond_json(h, 200, detail(h, row, claims))


def _put_serving(h, span, claims, sid) -> None:
    """Replace the equipment serving this space directly: {"equip_uris": [...]}."""
    hit = owned_row(h, SPACES, sid, claims, "space")
    if not hit:
        return
    row, _ = hit
    uris = spaces.check_serving(h.read_json_body().get("equip_uris"))
    scope = h.REPO.scope_for(claims)
    hidden = [u for u in uris if not scope.allows(u)]
    if hidden:
        raise ValueError(f"not equipment you can see: {', '.join(hidden)}")
    now = h.NOW()
    h.REPO.delete_rows(SERVING, space_id=sid)
    h.REPO.put_rows(SERVING, [{"id": db.new_id(), "project_id": row["project_id"], "space_id": sid, "equip_uri": u,
                               "created_by": claims.get("sub"), "created_at": now} for u in uris])
    h._event(row["project_id"], "space.serving", claims, {"space": sid, "equip_uris": uris})
    respond_json(h, 200, detail(h, row, claims))


def _types(h, span, claims) -> None:
    respond_json(h, 200, [{"id": k, **v} for k, v in spaces.SPACE_TYPES.items()])


ROUTES: list[Route] = [
    ("GET", re.compile(r"/space-types"), "space_types", _types),
    ("GET", re.compile(r"/projects/([^/]+)/spaces"), "list_spaces", _list),
    ("POST", re.compile(r"/projects/([^/]+)/spaces"), "create_space", _create),
    ("GET", re.compile(r"/spaces/([^/]+)"), "get_space", _get),
    ("PATCH", re.compile(r"/spaces/([^/]+)"), "patch_space", _patch),
    ("DELETE", re.compile(r"/spaces/([^/]+)"), "archive_space", _archive),
    ("PUT", re.compile(r"/spaces/([^/]+)/adjacent"), "put_space_adjacent", _put_adjacent),
    ("PUT", re.compile(r"/spaces/([^/]+)/serving"), "put_space_serving", _put_serving),
]

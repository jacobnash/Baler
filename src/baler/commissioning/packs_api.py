"""
HTTP routes for requirement packs (Baler#205), under /cx/.

Packs belong to an org (a library its projects adopt from), so they are
checked against the caller's org, not a project. Every change to a pack is
appended to the pack's own `history` (who, when, what); adopting a pack
version is a project event (pack.adopt).
"""

from __future__ import annotations

import re

from baler.commissioning import db, packs
from baler.commissioning.routes import Route, query
from baler.http_handler_base import respond_error, respond_json

PACKS = "cx_packs"
ADOPTED = "cx_project_packs"

db.ROW_TABLES[PACKS] = (("id", "org_id", "pack_id", "version", "status", "title", "source", "jurisdiction", "notice",
                         "requirements", "history", "created_by", "created_at", "updated_at", "published_by",
                         "published_at"), frozenset({"source", "jurisdiction", "requirements", "history"}))
db.ROW_TABLES[ADOPTED] = (("id", "project_id", "pack_ref", "pack_id", "version", "adopted_by", "adopted_at",
                           "retired_at"), frozenset())


def public(row: dict, full: bool = True) -> dict:
    out = db.public_row(row)
    out["summary"] = packs.summary(row)
    if not full:
        out.pop("requirements", None)
        out.pop("history", None)
    return out


def get_pack(h, pack_ref: str, claims: dict) -> dict | None:
    row = h.REPO.get_row(PACKS, pack_ref)
    if row is None or row["org_id"] != claims.get("org_id"):
        respond_error(h, 404, "no such pack")
        return None
    return row


def project_packs(repo, project_id: str) -> list[tuple[dict, dict]]:
    """(adoption, pack version) for each pack the project has adopted and not retired."""
    out = []
    for a in repo.find_rows(ADOPTED, order="adopted_at", project_id=project_id):
        if a.get("retired_at") is None:
            p = repo.get_row(PACKS, a["pack_ref"])
            if p is not None:
                out.append((a, p))
    return out


def _list(h, span, claims) -> None:
    rows = h.REPO.find_rows(PACKS, order="pack_id", org_id=claims["org_id"])
    q = query(h)
    if q.get("pack_id"):
        rows = [r for r in rows if r["pack_id"] == q["pack_id"][0]]
    respond_json(h, 200, [public(r, full=False) for r in rows])


def _import(h, span, claims) -> None:
    """A pack document becomes a draft version in the caller's org."""
    doc = packs.normalize(h.read_json_body())
    have = h.REPO.find_rows(PACKS, org_id=claims["org_id"], pack_id=doc["pack_id"], version=doc["version"])
    if have:
        raise ValueError(f"{doc['pack_id']} {doc['version']} already exists: give a new version")
    now = h.NOW()
    row = {"id": db.new_id(), "org_id": claims["org_id"], **doc, "status": "draft",
           "history": [{"at": now.isoformat(), "by": claims.get("sub"), "action": "import"}],
           "created_by": claims.get("sub"), "created_at": now, "updated_at": now, "published_by": None, "published_at": None}
    h.REPO.put_rows(PACKS, [row])
    respond_json(h, 201, public(row))


def _get(h, span, claims, ref) -> None:
    row = get_pack(h, ref, claims)
    if row:
        respond_json(h, 200, public(row))


def _values(h, span, claims, ref, rid) -> None:
    row = get_pack(h, ref, claims)
    if not row:
        return
    try:
        changes = packs.set_values(row, rid, h.read_json_body(), claims.get("sub"), h.NOW())
    except KeyError:
        respond_error(h, 404, "no such requirement in this pack")
        return
    h.REPO.update_row(PACKS, ref, {**changes, "updated_at": h.NOW()})
    respond_json(h, 200, public({**row, **changes}))


def _confirm(h, span, claims, ref, rid) -> None:
    row = get_pack(h, ref, claims)
    if not row:
        return
    sub = claims.get("sub")
    try:
        changes = packs.confirm(row, rid, sub, h.REPO.user_email(sub), h.NOW())
    except KeyError:
        respond_error(h, 404, "no such requirement in this pack")
        return
    h.REPO.update_row(PACKS, ref, {**changes, "updated_at": h.NOW()})
    respond_json(h, 200, public({**row, **changes}))


def _publish(h, span, claims, ref) -> None:
    """Freeze a draft. Unconfirmed rows are published too, and stay inactive."""
    row = get_pack(h, ref, claims)
    if not row:
        return
    if row["status"] != "draft":
        raise ValueError("only a draft can be published")
    now = h.NOW()
    changes = {"status": "published", "published_by": claims.get("sub"), "published_at": now, "updated_at": now,
               "history": [*row.get("history", []), {"at": now.isoformat(), "by": claims.get("sub"), "action": "publish"}]}
    h.REPO.update_row(PACKS, ref, changes)
    respond_json(h, 200, public({**row, **changes}))


def _project_list(h, span, claims, pid) -> None:
    if h._project(pid, claims) is None:
        return
    respond_json(h, 200, [{"adoption": db.public_row(a), "pack": public(p, full=False)} for a, p in project_packs(h.REPO, pid)])


def _adopt(h, span, claims, pid) -> None:
    """The project is judged by this published pack version from now on. A
    newer version of the same pack replaces it only when adopted by choice."""
    if h._project(pid, claims) is None:
        return
    ref = h.read_json_body().get("pack")
    row = h.REPO.get_row(PACKS, ref) if isinstance(ref, str) else None
    if row is None or row["org_id"] != claims.get("org_id"):
        raise ValueError("pack is the id of one of your org's packs")
    if row["status"] != "published":
        raise ValueError("only a published pack version can be adopted")
    now = h.NOW()
    replaced = []
    for a, p in project_packs(h.REPO, pid):
        if p["pack_id"] == row["pack_id"]:
            h.REPO.update_row(ADOPTED, a["id"], {"retired_at": now})
            replaced.append(p["version"])
    adoption = {"id": db.new_id(), "project_id": pid, "pack_ref": row["id"], "pack_id": row["pack_id"],
                "version": row["version"], "adopted_by": claims.get("sub"), "adopted_at": now, "retired_at": None}
    h.REPO.put_rows(ADOPTED, [adoption])
    h._event(pid, "pack.adopt", claims, {"pack_id": row["pack_id"], "version": row["version"], "replaced": replaced})
    respond_json(h, 201, {"adoption": db.public_row(adoption), "pack": public(row, full=False)})


def _schema(h, span, claims) -> None:
    respond_json(h, 200, {"engines": list(packs.ENGINES), "cx_families": list(packs.CX_FAMILIES), "phases": list(packs.PHASES),
                          "ops": list(packs.OPS), "quantities": packs.QUANTITIES})


ROUTES: list[Route] = [
    ("GET", re.compile(r"/pack-schema"), "pack_schema", _schema),
    ("GET", re.compile(r"/packs"), "list_packs", _list),
    ("POST", re.compile(r"/packs"), "import_pack", _import),
    ("GET", re.compile(r"/packs/([^/]+)"), "get_pack", _get),
    ("PATCH", re.compile(r"/packs/([^/]+)/requirements/([^/]+)"), "set_pack_values", _values),
    ("POST", re.compile(r"/packs/([^/]+)/requirements/([^/]+)/confirm"), "confirm_pack_row", _confirm),
    ("POST", re.compile(r"/packs/([^/]+)/publish"), "publish_pack", _publish),
    ("GET", re.compile(r"/projects/([^/]+)/packs"), "list_project_packs", _project_list),
    ("POST", re.compile(r"/projects/([^/]+)/packs"), "adopt_pack", _adopt),
]

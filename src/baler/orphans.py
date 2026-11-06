"""
Orphaned points (Ops -> Orphaned points): points in the graph that nothing
maintains any more, listed for an admin to review and remove.

Two kinds, both cheap graph queries (no scan of point_history):

- `derivation_gone`: a computed output (urn:point:computed/<derivation id>/...)
  whose derivation no longer exists in derivations.json. Nothing will write
  it again; its last value just sits there looking current (Baler#58).
- `no_equip`: a point linked to no equipment (no brick:isPointOf), so it
  shows up in no equipment tree. Often a delete or re-import left it
  behind, or ingest never got its tags. These deserve a look rather than an
  automatic delete, which is why removal is always an explicit choice.

Removing one deletes its graph record (as record_edit's delete does) and,
only if asked, its history rows, and logs it in record_edits. Only points
that are orphans at the time of the request can be removed through here.
"""

from __future__ import annotations

from baler import json_store, record_edit, timeseries

COMPUTED_PREFIX = "urn:point:computed/"
LIMIT = 1000

_PREFIXES = (
    "PREFIX brick: <https://brickschema.org/schema/Brick#>\n"
    "PREFIX hs: <urn:baler:haystack#>\n"
)


def _rows(store, sparql: str):
    return list(store.query(sparql))


def _scope_filter(scope, var: str) -> str:
    expr = scope.sparql(var) if scope is not None else None
    return f"FILTER({expr})" if expr else ""


def find(store, conn, scope=None, derivations_path: str | None = None) -> list[dict]:
    """Every orphaned point the scope can see, newest data first."""
    derivation_ids = {d["id"] for d in json_store.load(derivations_path or json_store.data_file("derivations.json"))}
    found: dict[str, dict] = {}

    for r in _rows(store, f"""{_PREFIXES}
        SELECT DISTINCT ?p ?dis ?equip WHERE {{
            ?p a ?t .
            FILTER(STRSTARTS(STR(?p), "{COMPUTED_PREFIX}"))
            OPTIONAL {{ ?p hs:dis ?dis }}
            OPTIONAL {{ ?p brick:isPointOf ?equip }}
            {_scope_filter(scope, "p")}
        }} LIMIT {LIMIT}"""):
        uri = str(r.p)
        derivation_id = uri[len(COMPUTED_PREFIX):].split("/", 1)[0]
        if derivation_id in derivation_ids:
            continue
        found[uri] = {"id": uri, "kind": "derivation_gone", "dis": str(r.dis) if r.dis else None,
                      "equip": str(r.equip) if r.equip else None,
                      "reason": f"its derivation {derivation_id} no longer exists"}

    for r in _rows(store, f"""{_PREFIXES}
        SELECT DISTINCT ?p ?dis WHERE {{
            {{ ?p a ?t }} UNION {{ ?p hs:hasTag "point" }}
            FILTER(STRSTARTS(STR(?p), "urn:point:") && !STRSTARTS(STR(?p), "{COMPUTED_PREFIX}"))
            FILTER NOT EXISTS {{ ?p brick:isPointOf ?e }}
            OPTIONAL {{ ?p hs:dis ?dis }}
            {_scope_filter(scope, "p")}
        }} LIMIT {LIMIT}"""):
        uri = str(r.p)
        found.setdefault(uri, {"id": uri, "kind": "no_equip", "dis": str(r.dis) if r.dis else None,
                               "equip": None, "reason": "not linked to any equipment"})

    latest = timeseries.read_latest_many(conn, list(found)) if found else {}
    for uri, item in found.items():
        ts, value = latest.get(uri, (None, None))
        item["last_ts"] = ts.isoformat() if ts else None
        item["last_value"] = value
    return sorted(found.values(), key=lambda o: (o["kind"], o["id"]))


def remove(store, conn, uris: list[str], purge_history: bool, who: tuple[str | None, str | None],
           scope=None, derivations_path: str | None = None) -> dict:
    """Remove the given points if they're (still) orphans the caller can
    see; anything else is reported back as skipped, not touched."""
    if not isinstance(uris, list) or not all(isinstance(u, str) for u in uris):
        raise TypeError("ids must be a list of point ids")
    current = {o["id"]: o for o in find(store, conn, scope, derivations_path)}
    removed, skipped = [], []
    for uri in dict.fromkeys(uris):
        orphan = current.get(uri)
        if orphan is None:
            skipped.append(uri)
            continue
        record_edit.delete_record(store, uri)
        rows = 0
        if purge_history:
            rows = conn.execute("DELETE FROM point_history WHERE point_uri = %s", (uri,)).rowcount
        record_edit.log_edit(conn, uri, "delete", who[0], who[1], {
            "orphan": orphan["kind"], "reason": orphan["reason"], "purge_history": purge_history,
            "history_rows_deleted": rows,
        })
        removed.append(uri)
    return {"removed": removed, "skipped": skipped}

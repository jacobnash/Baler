"""
The commissioning inventory: every equipment record a caller may see,
with its points, read Brick-first and Haystack-second.

Scoping is applied inside the SPARQL (Scope.sparql), to the equipment
*and* to each point - the gap PR #5 left open (it read the whole graph).
A run passes several scopes (the caller's own, and the project site's
topic prefixes); an entity must satisfy all of them.

Each row carries its Brick class(es), its Haystack markers and the ph
Xeto spec it fits (hisquery.xeto_spec_for - the same matching
haystack_api uses for `spec`), so the same inventory reads naturally to
a Brick tool and to a SkySpark user.
"""

from __future__ import annotations

from baler import hisquery
from baler.scope import Scope, entity_key

BRICK_NS = "https://brickschema.org/schema/Brick#"


def _split(value) -> list[str]:
    return [v for v in str(value or "").split(" ") if v]


def site_scope(prefixes) -> Scope:
    """A project site's own scope: its topic prefixes, or no restriction
    when the site has none (single-tenant installs)."""
    clean = tuple(sorted({p.strip().rstrip("/") for p in prefixes or [] if p and p.strip()}))
    return Scope(clean) if clean else Scope.everything()


def build(store, scopes: list[Scope]) -> list[dict]:
    """Equipment rows, sorted by id:
    {id, dis, brickClass, brickClasses, tags, spec, points: [...]}.
    A point row is hisquery.describe_points' shape plus `spec`."""
    # Every equip and its points, unfiltered, then scoped here: equipment and
    # points are keyed by topic prefix, and for those Scope.allows is exactly
    # Scope.sparql. As SPARQL FILTERs on every row the scope tests were
    # ~0.33 s of a 0.35 s query (#150). A point URI without a key (none are
    # minted today) still gets the graph-aware SPARQL test.
    pairs = store.query(
        hisquery.PREFIXES
        + """SELECT DISTINCT ?equip ?point WHERE {
            { ?equip a ?_t } UNION { ?equip brick:hasPoint ?_p }
            FILTER(STRSTARTS(STR(?equip), "urn:equip:"))
            OPTIONAL { ?equip brick:hasPoint ?point }
        }"""
    )

    def allowed(uri: str) -> bool:
        return all(s.allows(uri) for s in scopes)

    unkeyed = sorted({str(r.point) for r in pairs if getattr(r, "point", None) is not None
                      and entity_key(str(r.point)) is None})
    visible_unkeyed = set(unkeyed)
    for s in scopes:
        visible_unkeyed &= set(hisquery.visible_uris(store, sorted(visible_unkeyed), s))
    points_of: dict[str, list[str]] = {}
    for row in pairs:
        equip = str(row.equip)
        if not allowed(equip):
            continue
        point = getattr(row, "point", None)
        points_of.setdefault(equip, [])
        if point is not None and (allowed(str(point)) if entity_key(str(point)) is not None
                                  else str(point) in visible_unkeyed):
            points_of[equip].append(str(point))
    if not points_of:
        return []

    values = " ".join(f"<{hisquery._uri(u)}>" for u in points_of)
    meta = {}
    for row in store.query(
        hisquery.PREFIXES
        + f"""SELECT ?equip
                 (GROUP_CONCAT(DISTINCT COALESCE(STR(?type), ""); separator=" ") AS ?types)
                 (GROUP_CONCAT(DISTINCT COALESCE(STR(?tag), ""); separator=" ") AS ?tags)
                 (SAMPLE(?dis) AS ?dis_)
        WHERE {{
            VALUES ?equip {{ {values} }}
            OPTIONAL {{ ?equip a ?type }}
            OPTIONAL {{ ?equip haystack:hasTag ?tag }}
            OPTIONAL {{ ?equip haystack:dis ?dis }}
        }}
        GROUP BY ?equip"""
    ):
        meta[str(row.equip)] = row

    all_points = sorted({p for ps in points_of.values() for p in ps})
    described = hisquery.describe_points(store, all_points)

    out = []
    for equip in sorted(points_of):
        row = meta.get(equip)
        types = _split(getattr(row, "types", None)) if row is not None else []
        brick = sorted(t[len(BRICK_NS):] for t in types if t.startswith(BRICK_NS))
        tags = sorted(_split(getattr(row, "tags", None))) if row is not None else []
        dis = getattr(row, "dis_", None) if row is not None else None
        pts = []
        for uri in sorted(set(points_of[equip])):
            d = dict(described[uri])
            d.pop("_dis_from_tag", None)
            d["spec"] = hisquery.xeto_spec_for(set(d["tags"]) | {"point"}, d["brickClass"])
            pts.append(d)
        out.append({
            "id": equip,
            "dis": str(dis) if dis else equip.replace("urn:equip:", "", 1),
            "brickClass": brick[0] if brick else None,
            "brickClasses": brick,
            "tags": tags,
            "spec": hisquery.xeto_spec_for(set(tags) | {"equip"}, brick[0] if brick else None),
            "points": pts,
        })
    return out

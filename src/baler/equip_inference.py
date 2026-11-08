"""
Equipment inference (Baler#187): once points are classified, much of the
equipment model follows. Everything here is a *proposal* with its
evidence - for a person to confirm in the review queue (#184), never
asserted silently.

  * **Equip type from its point set.** An equip with chilled water supply
    and return temperatures, a run status and a start/stop command is a
    chiller. The signatures are Cx's own expected-point templates
    (ontology/cx-expected-points.ttl: AHU, VAV, FCU, chiller, boiler,
    pump), plus SIGNATURES below for the kinds Cx doesn't template
    (meters, fans). Each is scored by how many of its requirements the
    equip's classified points meet (required counts double) and by how
    much of the equip's point set it explains. The model step shows the
    top three as candidates for an equip, source "points".
  * **Cx's expected-point check.** An equip the ladder would auto-accept
    as a class whose template's *required* points are missing - an AHU
    with no fan status - is vetoed (stays a proposal), once enough of its
    points are classified for the absence to mean something.
  * **isPointOf.** Points with no equipment, grouped by their topic's
    parent (a device or a prefix group): proposed as one equip.
  * **feeds.** A VAV's discharge air temperature that tracks one AHU's
    supply air temperature (Pearson r over 5-minute means) suggests that
    AHU feeds it. Proposed only when the best AHU clearly beats the next.

Relationship proposals live in `relationship_proposals`; accepting one in
the review queue writes the edge (and, for isPointOf, the equip).
"""

from __future__ import annotations

import json
import math
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from rdflib import URIRef

from baler import brick_vocab, schema
from baler.store import BRICK, TD

TOP_N = 3
# Fraction of an equip's points that must be classified before a missing
# required point counts against a class. All of them: an unclassified point
# may be the missing one (measured: at 0.8 the check vetoed 6 correct
# memory answers on the mock hospital whose expected point was simply
# still in review).
CHECK_MIN_CLASSIFIED = 1.0
FEEDS_MIN_R = 0.8
FEEDS_MIN_MARGIN = 0.1
FEEDS_MIN_SAMPLES = 24


@dataclass(frozen=True)
class Signature:
    target_class: str
    requirements: tuple[tuple[str, tuple[str, ...], str], ...]  # (name, classes, required|expected)


# Kinds Cx has no expected-point template for.
SIGNATURES = (
    Signature("Electrical_Meter", (("power", ("Electric_Power_Sensor",), "required"),
                                   ("energy", ("Electric_Energy_Sensor",), "expected"))),
    Signature("Fan", (("status", ("Fan_Status",), "required"), ("command", ("Fan_Command",), "expected"))),
)


def signatures() -> list[Signature]:
    """Cx's expected-point templates, then SIGNATURES."""
    from baler.commissioning import templates

    out = [Signature(t.target_class, tuple((r.name, r.classes, r.severity) for r in t.requirements if r.severity != "optional"))
           for t in templates.load()]
    return out + list(SIGNATURES)


def _family(vocab, classes: Iterable[str]) -> set[str]:
    fam: set[str] = set()
    for c in classes:
        fam |= vocab.descendants(c) or {c}
    return fam


def infer(point_classes: list[str | None], vocab: brick_vocab.Vocab | None = None, sigs: list[Signature] | None = None) -> list[dict]:
    """[{brick_class, score, reason, met, missing_required}] best first, for
    an equip whose points have these classes (None = unclassified)."""
    vocab = vocab or brick_vocab.load()
    sigs = sigs if sigs is not None else signatures()
    classified = [c for c in point_classes if c]
    if not classified:
        return []
    out = []
    for sig in sigs:
        weight = met_w = 0.0
        met, missing_req = [], []
        union: set[str] = set()
        for name, classes, severity in sig.requirements:
            fam = _family(vocab, classes)
            union |= fam
            w = 2.0 if severity == "required" else 1.0
            weight += w
            if any(c in fam for c in classified):
                met_w += w
                met.append(name)
            elif severity == "required":
                missing_req.append(name)
        if not met:
            continue
        explained = sum(c in union for c in classified) / len(classified)
        score = round(0.6 * (met_w / weight) + 0.4 * explained, 3)
        reason = f"has {len(met)} of {len(sig.requirements)} expected points ({', '.join(met)})"
        if missing_req:
            reason += f"; missing required: {', '.join(missing_req)}"
        out.append({"brick_class": sig.target_class, "score": score, "reason": reason, "met": met, "missing_required": missing_req})
    out.sort(key=lambda r: (-float(r["score"]), str(r["brick_class"])))  # type: ignore[arg-type]
    return out[:TOP_N]


def cx_check(brick_class: str, point_classes: list[str | None], vocab: brick_vocab.Vocab | None = None,
             sigs: list[Signature] | None = None) -> str | None:
    """Why `brick_class` is suspicious for an equip with these points (a
    required expected point missing), or None. Silent while fewer than
    CHECK_MIN_CLASSIFIED of its points are classified - absence then
    means nothing yet."""
    vocab = vocab or brick_vocab.load()
    if not point_classes or sum(1 for c in point_classes if c) / len(point_classes) < CHECK_MIN_CLASSIFIED:
        return None
    classified = [c for c in point_classes if c]
    for sig in sigs if sigs is not None else signatures():
        if brick_class not in (vocab.descendants(sig.target_class) or {sig.target_class}):
            continue
        missing = [name for name, classes, sev in sig.requirements
                   if sev == "required" and not any(c in _family(vocab, classes) for c in classified)]
        if missing:
            return f"Cx expects a {sig.target_class} to have {', '.join(missing)}; none of its points is one"
    return None


# --- relationships ----------------------------------------------------------

# Schema (relationship_proposals): baler/migrations/, from 0001 (#194). Change
# it with a new migration there, not here.


def unlinked_point_groups(store, min_points: int = 2) -> list[dict]:
    """Points with no equipment, grouped by their topic's parent:
    [{equip_uri, points, labels}] - each a proposed equip."""
    rows = store.query(f"""
        SELECT ?p ?t WHERE {{ ?p <{TD.sourceTopic}> ?t .
          FILTER(STRSTARTS(STR(?p), "urn:point:"))
          FILTER NOT EXISTS {{ ?e <{BRICK.hasPoint}> ?p }} }}""")
    groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for r in rows:
        topic = str(r.t)
        if "/" in topic:
            groups[topic.rsplit("/", 1)[0]].append((str(r.p), topic))
    return [{"equip_uri": f"urn:equip:{parent}", "points": sorted(p for p, _ in members),
             "labels": sorted(t.rsplit("/", 1)[1] for _, t in members)}
            for parent, members in sorted(groups.items()) if len(members) >= min_points]


def _buckets(rows, seconds: int = 300) -> dict[int, float]:
    sums: dict[int, list[float]] = defaultdict(list)
    for ts, v in rows:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            sums[int(ts.timestamp()) // seconds].append(float(v))
    return {k: sum(vs) / len(vs) for k, vs in sums.items()}


def pearson(a: dict[int, float], b: dict[int, float]) -> tuple[float | None, int]:
    keys = sorted(set(a) & set(b))
    n = len(keys)
    if n < 3:
        return None, n
    xs, ys = [a[k] for k in keys], [b[k] for k in keys]
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None, n
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy), n


def feeds_candidates(ahu_series: dict[str, list], vav_series: dict[str, list]) -> list[dict]:
    """{ahu_uri: [(ts, value)]} supply air temps, {vav_uri: [(ts, value)]}
    discharge air temps -> [{ahu, vav, r, samples, runner_up}] where one
    AHU clearly tracks the VAV best."""
    ahus = {u: _buckets(rows) for u, rows in ahu_series.items()}
    out = []
    for vav, rows in sorted(vav_series.items()):
        vb = _buckets(rows)
        scored = []
        for ahu, ab in ahus.items():
            r, n = pearson(ab, vb)
            if r is not None and n >= FEEDS_MIN_SAMPLES:
                scored.append((r, n, ahu))
        scored.sort(reverse=True)
        if not scored or scored[0][0] < FEEDS_MIN_R:
            continue
        runner = scored[1][0] if len(scored) > 1 else None
        if runner is not None and scored[0][0] - runner < FEEDS_MIN_MARGIN:
            continue
        out.append({"ahu": scored[0][2], "vav": vav, "r": round(scored[0][0], 3), "samples": scored[0][1],
                    "runner_up": None if runner is None else round(runner, 3)})
    return out


SUPPLY_AIR_TEMP = ("Supply_Air_Temperature_Sensor",)  # Discharge_ is its Brick alias


def feeds_from_history(store, ts_conn, days: int = 3) -> list[dict]:
    """feeds_candidates over the last `days` of point_history: each AHU's
    and each VAV's supply/discharge air temperature points."""
    from baler import timeseries

    vocab = brick_vocab.load()
    fam = _family(vocab, SUPPLY_AIR_TEMP)
    rows = store.query(f"""
        SELECT ?e ?etype ?p ?ptype WHERE {{ ?e <{BRICK.hasPoint}> ?p . ?e a ?etype . ?p a ?ptype .
          FILTER(STRSTARTS(STR(?etype), "{BRICK}") && STRSTARTS(STR(?ptype), "{BRICK}")) }}""")
    ahu_fam = _family(vocab, ("Air_Handling_Unit",))
    vav_fam = _family(vocab, ("Variable_Air_Volume_Box",))
    ahu_pts: dict[str, str] = {}
    vav_pts: dict[str, str] = {}
    for r in rows:
        etype, ptype = str(r.etype)[len(str(BRICK)):], str(r.ptype)[len(str(BRICK)):]
        if ptype not in fam:
            continue
        if etype in ahu_fam:
            ahu_pts.setdefault(str(r.e), str(r.p))
        elif etype in vav_fam:
            vav_pts.setdefault(str(r.e), str(r.p))
    if not ahu_pts or not vav_pts:
        return []
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    hist = timeseries.read_range_many(ts_conn, list(ahu_pts.values()) + list(vav_pts.values()), start, end, 100000)
    cands = feeds_candidates({e: hist[p][0] for e, p in ahu_pts.items()}, {e: hist[p][0] for e, p in vav_pts.items()})
    return [{**c, "ahu_point": ahu_pts[c["ahu"]], "vav_point": vav_pts[c["vav"]]} for c in cands]


class PgRelationships:
    def __init__(self, conn):
        self.conn = conn

    def ensure_schema(self) -> None:
        schema.ensure(self.conn)

    def propose(self, subject: str, predicate: str, objects: list[str], evidence: dict) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO relationship_proposals (id, subject, predicate, objects, evidence, status, created_at)
               VALUES (%s, %s, %s, %s, %s, 'proposed', %s)
               ON CONFLICT (subject, predicate) WHERE status = 'proposed' DO UPDATE SET objects = EXCLUDED.objects,
                 evidence = EXCLUDED.evidence""",
            (uuid.uuid4().hex, subject, predicate, json.dumps(objects), json.dumps(evidence, default=str), now),
        )

    def list(self, status: str = "proposed") -> list[dict]:
        keys = ("id", "subject", "predicate", "objects", "evidence", "status", "decided_by", "created_at")
        rows = self.conn.execute(f"SELECT {', '.join(keys)} FROM relationship_proposals WHERE status = %s ORDER BY predicate, subject",
                                 (status,)).fetchall()
        out = [dict(zip(keys, r)) for r in rows]
        for r in out:
            r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
        return out

    def decide(self, proposal_id: str, status: str, by: str | None) -> dict | None:
        row = self.conn.execute(
            """UPDATE relationship_proposals SET status = %s, decided_by = %s, decided_at = %s
               WHERE id = %s AND status = 'proposed' RETURNING subject, predicate, objects""",
            (status, by, datetime.now(timezone.utc), proposal_id),
        ).fetchone()
        return None if row is None else {"subject": row[0], "predicate": row[1], "objects": list(row[2])}


def write_relationship(store, subject: str, predicate: str, objects: list[str]) -> None:
    """What accepting a relationship proposal writes."""
    from baler import ingest

    if predicate == "hasPoint":
        equip = ingest.ingest_equip_tags(store, subject.removeprefix("urn:equip:"), {})
        for p in objects:
            ingest.link_point_to_equip(store, URIRef(p), equip)
    elif predicate == "feeds":
        for o in objects:
            store.add_relationship(URIRef(subject), BRICK.feeds, URIRef(o))
            store.add_relationship(URIRef(o), BRICK.isFedBy, URIRef(subject))
    else:
        raise ValueError(f"unknown predicate {predicate!r}")


def propose_relationships(store, rels: PgRelationships, ts_conn=None) -> dict:
    """autotag's last step: isPointOf groups, and feeds when history is
    available. Proposals only."""
    counts = {"hasPoint": 0, "feeds": 0}
    for g in unlinked_point_groups(store):
        rels.propose(g["equip_uri"], "hasPoint", g["points"],
                     {"why": f"{len(g['points'])} points under {g['equip_uri'].removeprefix('urn:equip:')} with no equipment",
                      "labels": g["labels"][:20]})
        counts["hasPoint"] += 1
    if ts_conn is not None:
        for c in feeds_from_history(store, ts_conn):
            rels.propose(c["ahu"], "feeds", [c["vav"]],
                         {"why": f"its supply air temp tracks this VAV's discharge air temp (r={c['r']}, {c['samples']} 5-min samples"
                                 + (f"; next best AHU r={c['runner_up']}" if c["runner_up"] is not None else "") + ")",
                          **c})
            counts["feeds"] += 1
    return counts

"""
The mapping review queue (Baler#184): the human end of the tagging ladder.

The model step (model_step.py) leaves what it can't settle as mapping
proposals (mapping_proposals). This module is what ops_api serves over
them:

  * groups(): held proposals (proposed or escalated) grouped by naming
    pattern, so "41 points like `VAV-{n}-DA-T`" is one decision, ranked by
    impact - first a pattern a fault rule needs (its topic_glob matches a
    member, or a candidate class is one a brick_class rule targets), then
    one on a site with an active Cx project, then the rest; bigger groups
    first within a tier.
  * decide(): a person's answer, for one proposal or a whole group -
      accept   a candidate or any other class (vocab-checked), written
               through record_edit like a hand edit and logged in
               record_edits; for a group, also saved as a site rule the
               model step applies to later points with that pattern;
      escalate to a named specialist (the site's controls contractor or
               MSI, or our expert), notified through the webhook path;
               they answer on the same screen;
      site_visit "can't tell without going there": opens a Cx issue on
               the project covering the point, never a guess.
  * summary(): how much of the building Baler understands - points by
    who classified them (rule, site rule, model, person; memory once #185
    exists), proposed, escalated, unmapped.

Every accepted answer stays in mapping_proposals with the evidence the
person saw: that is memory's training data (#185).
"""

from __future__ import annotations

import fnmatch
import threading
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone

from rdflib import RDF, URIRef

from baler import brick_vocab, json_store, record_edit, webhooks
from baler.mapping_proposals import HELD
from baler.store import BRICK, PROJ, TD

ACTIONS = ("accept", "escalate", "site_visit")
TIER_REASONS = {1: "a fault rule needs it", 2: "on a site with an active Cx project", 3: ""}


def _topic(uri: str) -> str:
    return uri.removeprefix("urn:point:").removeprefix("urn:equip:")


def _label_shape(pattern: str) -> str:
    parts = pattern.split("|")
    return parts[1] if len(parts) > 1 else pattern


# --- impact ---------------------------------------------------------------


def fault_rule_needs(rules: list[dict], vocab: brick_vocab.Vocab):
    """(uri, candidate classes) -> True if a fault rule would need this
    entity: a topic_glob rule matching it, or a brick_class rule whose class
    (or a subclass) is among its candidates."""
    globs = [r["applies_to"]["topic_glob"] for r in rules if "topic_glob" in r.get("applies_to", {})]
    classes: set[str] = set()
    for r in rules:
        c = r.get("applies_to", {}).get("brick_class")
        if c:
            classes |= vocab.descendants(c)

    def needs(uri: str, candidates: list[str]) -> bool:
        topic = _topic(uri)
        return any(fnmatch.fnmatch(topic, g) for g in globs) or any(c in classes for c in candidates)

    return needs


def active_cx_prefixes(conn) -> list[str] | None:
    """Topic prefixes of sites with an active (not archived) Cx project;
    None when a project covers every site (it has no site), [] when there
    are none or Cx's tables don't exist yet."""
    try:
        rows = conn.execute(
            "SELECT p.site_id, s.topic_prefixes FROM cx_projects p LEFT JOIN sites s ON s.id = p.site_id WHERE p.archived_at IS NULL"
        ).fetchall()
    except Exception:  # noqa: BLE001 - no Cx schema on this install: no Cx tier
        return []
    out: list[str] = []
    for site_id, prefixes in rows:
        if site_id is None:
            return None
        out.extend(p.rstrip("/") for p in (prefixes or []))
    return out


def _under(topic: str, prefixes: Sequence[str] | None) -> bool:
    if prefixes is None:
        return True
    return any(topic == p or topic.startswith(p + "/") for p in prefixes)


# --- the queue ------------------------------------------------------------


def groups(rows: list[dict], needs=None, cx_prefixes: Sequence[str] | None = (), allows=None) -> list[dict]:
    """Held proposals -> ranked groups. `allows(uri)` is the caller's site
    scope; rows outside it are dropped before anything is counted."""
    by_pattern: dict[str, list[dict]] = {}
    for r in rows:
        if r["status"] not in HELD or (allows is not None and not allows(r["entity_uri"])):
            continue
        by_pattern.setdefault(r["pattern"], []).append(r)
    out = []
    for pat, members in by_pattern.items():
        members.sort(key=lambda r: r["entity_uri"])
        first = members[0]
        cands = [c["brick_class"] for c in first["candidates"]]
        tier = 3
        if needs is not None and any(needs(m["entity_uri"], [c["brick_class"] for c in m["candidates"]]) for m in members):
            tier = 1
        elif (cx_prefixes is None or len(cx_prefixes) > 0) and any(_under(_topic(m["entity_uri"]), cx_prefixes) for m in members):
            tier = 2
        refused = Counter(m["refused_because"] for m in members).most_common(1)[0][0]
        out.append({
            "pattern": pat,
            "label": _label_shape(pat),
            "kind": pat.split("|", 1)[0],
            "count": len(members),
            "tier": tier,
            "why_first": TIER_REASONS[tier],
            "candidates": first["candidates"],
            "candidate_classes": cands,
            "refused_because": refused,
            "escalated": sum(m["status"] == "escalated" for m in members),
            "assignee": next((m["assignee"] for m in members if m.get("assignee")), None),
            "evidence": first["evidence"],
            "asks": first["asks"],
            "members": [{
                "id": m["id"], "uri": m["entity_uri"], "label": (m["evidence"] or {}).get("label") or _topic(m["entity_uri"]),
                "status": m["status"], "assignee": m.get("assignee"), "note": m.get("note"),
                "unit": ((m["evidence"] or {}).get("values") or {}).get("unit"),
                "value_stats": (m["evidence"] or {}).get("value_stats"),
            } for m in members],
        })
    out.sort(key=lambda g: (g["tier"], -g["count"], g["pattern"]))
    return out


# --- answers --------------------------------------------------------------


class DecisionError(ValueError):
    pass


def _canonical(brick_class) -> str:
    c = brick_vocab.load().canonical_class(str(brick_class or ""))
    if not c:
        raise DecisionError(f"no Brick class named {brick_class!r}")
    return c


def decide(proposals, store, conn, ids: list[str], action: str, user: dict, brick_class=None, assignee=None, note=None,
           save_rule: bool = False, allows=None, webhooks_path: str | None = None, cx_repo=None) -> dict:
    """Apply one answer to the held proposals `ids` (one, or a whole
    pattern group). Validates everything before writing anything."""
    if action not in ACTIONS:
        raise DecisionError(f"action must be one of {', '.join(ACTIONS)}")
    if not ids:
        raise DecisionError("no proposals given")
    rows = proposals.get(ids)
    found = {r["id"] for r in rows}
    missing = [i for i in ids if i not in found]
    if missing:
        raise DecisionError(f"no such proposal: {', '.join(missing)}")
    if allows is not None and not all(allows(r["entity_uri"]) for r in rows):
        raise DecisionError(f"no such proposal: {', '.join(r['id'] for r in rows if not allows(r['entity_uri']))}")
    done = [r for r in rows if r["status"] not in HELD]
    if done:
        raise DecisionError(f"already decided: {', '.join(r['id'] for r in done)}")
    by = user.get("email") or user.get("sub")
    note = (str(note).strip()[:2000] or None) if note is not None else None

    if action == "accept":
        cls = _canonical(brick_class)
        for r in rows:
            uri = r["entity_uri"]
            changed = record_edit.apply_edit(store, uri, {"brickClass": cls})
            _drop_fallback_types(store, uri)
            if conn is not None:
                record_edit.log_edit(conn, uri, "mapping", user.get("sub"), user.get("email"),
                                     {**changed, "proposal": r["id"], "pattern": r["pattern"]})
            proposals.decide(r["id"], "accepted", cls, by, note=note)
        patterns = sorted({r["pattern"] for r in rows})
        if save_rule:
            for pat in patterns:
                proposals.add_site_rule(pat, cls, by, rows[0]["id"])
        return {"action": "accept", "brick_class": cls, "decided": len(rows), "site_rules": patterns if save_rule else []}

    if action == "escalate":
        assignee = str(assignee or "").strip()[:200]
        if not assignee:
            raise DecisionError("escalate needs an assignee: who should answer")
        for r in rows:
            proposals.decide(r["id"], "escalated", None, by, assignee=assignee, note=note)
        payload = {"event": "mapping.escalated", "assignee": assignee, "note": note, "by": by,
                   "proposals": [{"id": r["id"], "uri": r["entity_uri"], "pattern": r["pattern"],
                                  "candidates": [c["brick_class"] for c in r["candidates"]]} for r in rows],
                   "link": "/ui/mapping.html"}
        notified = notify(webhooks_path, payload) if webhooks_path else 0
        return {"action": "escalate", "assignee": assignee, "decided": len(rows), "notified": notified}

    # site_visit
    for r in rows:
        proposals.decide(r["id"], "site_visit", None, by, note=note)
    issue = open_site_visit_issue(conn, cx_repo, store, rows, user, note) if cx_repo is not None else None
    return {"action": "site_visit", "decided": len(rows), "cx_issue": issue,
            "hint": None if issue else "no active Cx project covers these points, so no Cx issue was opened"}


def _drop_fallback_types(store, uri: str) -> None:
    """The PROJ/TD:UnmappedPoint a rule miss left, now that a person gave a
    real class - the same swap mapping.classify_on_ingest makes."""
    rows = store.query(f"SELECT ?t WHERE {{ <{uri}> a ?t }}")
    for row in rows:
        t = URIRef(str(row.t))
        if str(t).startswith(str(PROJ)) or t == TD.UnmappedPoint:
            store.remove_relationship(URIRef(uri), RDF.type, t)


def notify(webhooks_path: str, payload: dict) -> int:
    """Through the fault webhooks registry, off the request thread: every
    webhook with no filter, or filtered to rule_id "mapping". Same SSRF
    rules as fault delivery (webhooks.deliver). Returns how many were
    started."""
    import os

    allow_private = os.environ.get("BALER_ALLOW_PRIVATE_WEBHOOKS") == "1"
    started = 0
    for hook in json_store.load(webhooks_path):
        if not webhooks.matches_filter(hook, "mapping"):
            continue
        threading.Thread(target=webhooks.deliver, args=(hook["url"], hook["secret"], payload),
                         kwargs={"allow_private": allow_private}, daemon=True).start()
        started += 1
    return started


def open_site_visit_issue(conn, cx_repo, store, rows: list[dict], user: dict, note: str | None) -> str | None:
    """A Cx issue on the newest active project covering the first point:
    "identify these on site". None when no project covers them."""
    from baler.commissioning import db as cx_db

    topic = _topic(rows[0]["entity_uri"])
    try:
        projects = conn.execute(
            """SELECT p.id, s.topic_prefixes, p.site_id FROM cx_projects p LEFT JOIN sites s ON s.id = p.site_id
               WHERE p.archived_at IS NULL ORDER BY p.updated_at DESC"""
        ).fetchall()
    except Exception:  # noqa: BLE001 - Cx not set up on this install
        return None
    project = next((pid for pid, prefixes, site in projects if site is None or _under(topic, [p.rstrip("/") for p in prefixes or []])), None)
    if project is None:
        return None
    equip = next((str(r.e) for r in store.query(f"SELECT ?e WHERE {{ ?e <{BRICK.hasPoint}> <{rows[0]['entity_uri']}> }}")), None)
    now = datetime.now(timezone.utc)
    issue_id = cx_db.new_id()
    labels = [(r["evidence"] or {}).get("label") or _topic(r["entity_uri"]) for r in rows]
    cx_repo.create_issue({
        "id": issue_id, "project_id": project, "equip_uri": equip, "check_id": None, "subject": rows[0]["pattern"],
        "status": "open", "title": f"Site visit: identify {len(rows)} point(s) like {_label_shape(rows[0]['pattern'])}"[:200],
        "severity": "minor", "assignee": None,
        "detail": {"proposals": [r["id"] for r in rows], "points": labels,
                   "candidates": [c["brick_class"] for c in rows[0]["candidates"]], "note": note},
        "source": "baler-mapping", "source_id": rows[0]["id"], "created_by": user.get("email") or user.get("sub"),
        "created_at": now, "updated_at": now,
    })
    cx_repo.add_issue_event(issue_id, "opened", user.get("sub"), user.get("email"), {"from": "mapping review"}, now)
    return issue_id


# --- how much of the building Baler understands ---------------------------

STATES = ("rule", "site_rule", "memory", "model", "person", "proposed", "escalated", "unmapped")


def summary(store, proposal_rows: list[dict], person_uris: set[str], allows=None) -> dict:
    """Points by state. `person_uris`: points a person classified (an
    accepted proposal or a hand edit of the class)."""
    points = {str(r.p) for r in store.query(f"""
        PREFIX haystack: <urn:baler:haystack#>
        SELECT DISTINCT ?p WHERE {{ {{ ?p a <{TD.RawPoint}> }} UNION {{ ?p haystack:hasTag ?t }}
        FILTER(STRSTARTS(STR(?p), "urn:point:")) }}""")}
    if allows is not None:
        points = {p for p in points if allows(p)}
    typed = {str(r.p) for r in store.query(f"""
        SELECT DISTINCT ?p WHERE {{ ?p a ?t . FILTER(STRSTARTS(STR(?p), "urn:point:") && STRSTARTS(STR(?t), "{BRICK}")) }}""")}
    by_def: dict[str, str] = {}
    for r in store.query("""
        PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
        SELECT ?p ?d WHERE { ?p skos:definition ?d .
          FILTER(STRSTARTS(STR(?p), "urn:point:") && (STRSTARTS(STR(?d), "model-classified") || STRSTARTS(STR(?d), "site-rule-classified"))) }"""):
        by_def[str(r.p)] = "site_rule" if str(r.d).startswith("site-rule") else "model"
    held = {r["entity_uri"]: r["status"] for r in proposal_rows if r["status"] in HELD}
    counts: Counter[str] = Counter()
    for p in points:
        if p in typed:
            counts["person" if p in person_uris else by_def.get(p, "rule")] += 1
        else:
            counts[held.get(p, "unmapped")] += 1
    total = len(points)
    understood = sum(counts[s] for s in ("rule", "site_rule", "memory", "model", "person"))
    return {"total": total, "understood": understood, "understood_pct": round(100 * understood / total, 1) if total else 0.0,
            "states": {s: counts[s] for s in STATES}}


def person_classified(conn) -> set[str]:
    """Points a person gave a class: an accepted proposal, or a hand edit
    that set brickClass (record_edits)."""
    out = {r[0] for r in conn.execute("SELECT entity_uri FROM mapping_proposals WHERE status = 'accepted'").fetchall()}
    out |= {r[0] for r in conn.execute("SELECT DISTINCT entity_uri FROM record_edits WHERE detail ? 'brickClass'").fetchall()}
    return out

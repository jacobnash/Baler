"""
Mapping proposals (Baler#182): what the tagging ladder couldn't settle on
its own, waiting for a person. The Cx 7 pattern: an extracted fact is a
proposal, a person confirms it.

The model step (model_step.run) writes one row per unresolved point or
equip: up to three candidate Brick classes with reasons, the evidence it
saw, what each model ask said, and why it didn't auto-accept. The review
queue (#184) reads, groups (by `pattern`) and decides them; the decision
writes the class through record_edit like any hand edit.

Rows live in Postgres, not in rules/*.yaml as llm_classifier.propose_rule
used to append them: that file sits in the bind-mounted checkout on
nash-srv (see CLAUDE.md on writes into /app).

One open proposal per entity: re-running autotag refreshes it rather than
piling up duplicates, and a decided one is left alone.
"""

from __future__ import annotations

import builtins
import json
import uuid
from datetime import datetime, timezone

from baler import schema

# Schema (mapping_proposals, mapping_site_rules): baler/migrations/, from 0001
# (#194). Change it with a new migration there, not here.

# A person has been asked: the ladder refreshes the evidence but never
# reopens, duplicates or auto-closes these.
HELD = ("proposed", "escalated")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PgProposals:
    def __init__(self, conn):
        self.conn = conn

    def ensure_schema(self) -> None:
        schema.ensure(self.conn)

    def propose(self, entity_uri: str, pattern: str, candidates: list, refused_because: str, evidence: dict, asks: list) -> None:
        now = _now()
        with self.conn.transaction():
            # An escalated proposal keeps its status (and its specialist):
            # refresh what it shows, don't open a second one beside it.
            cur = self.conn.execute(
                """UPDATE mapping_proposals SET pattern = %s, candidates = %s, refused_because = %s, evidence = %s, asks = %s,
                   updated_at = %s WHERE entity_uri = %s AND status = 'escalated'""",
                (pattern, json.dumps(candidates), refused_because, json.dumps(evidence, default=str), json.dumps(asks), now, entity_uri),
            )
            if cur.rowcount:
                return
            self.conn.execute(
                """INSERT INTO mapping_proposals (id, entity_uri, pattern, candidates, refused_because, evidence, asks, status, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'proposed', %s, %s)
                   ON CONFLICT (entity_uri) WHERE status = 'proposed' DO UPDATE SET
                     pattern = EXCLUDED.pattern, candidates = EXCLUDED.candidates, refused_because = EXCLUDED.refused_because,
                     evidence = EXCLUDED.evidence, asks = EXCLUDED.asks, updated_at = EXCLUDED.updated_at""",
                (uuid.uuid4().hex, entity_uri, pattern, json.dumps(candidates), refused_because, json.dumps(evidence, default=str),
                 json.dumps(asks), now, now),
            )

    def resolve(self, entity_uri: str, status: str, brick_class: str | None, by: str | None = None) -> None:
        """Close an entity's open proposal (the ladder or a person settled it)."""
        with self.conn.transaction():
            self.conn.execute(
                "UPDATE mapping_proposals SET status = %s, decided_class = %s, decided_by = %s, decided_at = %s, updated_at = %s"
                " WHERE entity_uri = %s AND status = 'proposed'",
                (status, brick_class, by, _now(), _now(), entity_uri),
            )

    _COLS = ("id", "entity_uri", "pattern", "candidates", "refused_because", "evidence", "asks", "status", "decided_class",
             "decided_by", "assignee", "note", "created_at", "updated_at", "decided_at")

    def list(self, status: str | tuple = "proposed", limit: int = 500) -> list[dict]:
        statuses = [status] if isinstance(status, str) else list(status)
        rows = self.conn.execute(
            f"SELECT {', '.join(self._COLS)} FROM mapping_proposals WHERE status = ANY(%s) ORDER BY pattern, entity_uri LIMIT %s",
            (statuses, limit),
        ).fetchall()
        return [dict(zip(self._COLS, r)) for r in rows]

    def get(self, ids: builtins.list[str]) -> builtins.list[dict]:
        rows = self.conn.execute(f"SELECT {', '.join(self._COLS)} FROM mapping_proposals WHERE id = ANY(%s)", (list(ids),)).fetchall()
        return [dict(zip(self._COLS, r)) for r in rows]

    def decide(self, proposal_id: str, status: str, brick_class: str | None, by: str | None, assignee: str | None = None,
               note: str | None = None) -> bool:
        """A person's answer on a held proposal. False if it wasn't held
        (already decided, or no such id)."""
        final = status not in HELD
        cur = self.conn.execute(
            """UPDATE mapping_proposals SET status = %s, decided_class = COALESCE(%s, decided_class), decided_by = %s,
               assignee = COALESCE(%s, assignee), note = COALESCE(%s, note), updated_at = %s,
               decided_at = CASE WHEN %s THEN %s ELSE decided_at END
               WHERE id = %s AND status = ANY(%s)""",
            (status, brick_class, by, assignee, note, _now(), final, _now(), proposal_id, list(HELD)),
        )
        return bool(cur.rowcount)

    def add_site_rule(self, pattern: str, brick_class: str, by: str | None, proposal_id: str | None) -> None:
        self.conn.execute(
            """INSERT INTO mapping_site_rules (pattern, brick_class, created_by, created_at, proposal_id) VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (pattern) DO UPDATE SET brick_class = EXCLUDED.brick_class, created_by = EXCLUDED.created_by,
               created_at = EXCLUDED.created_at, proposal_id = EXCLUDED.proposal_id""",
            (pattern, brick_class, by, _now(), proposal_id),
        )

    def site_rules(self) -> dict[str, str]:
        return {p: c for p, c in self.conn.execute("SELECT pattern, brick_class FROM mapping_site_rules").fetchall()}

    def confirmed_examples(self, limit: int = 5000) -> builtins.list[dict]:
        """What people answered, with the evidence they saw: the labelled
        examples memory (#185) learns from."""
        rows = self.conn.execute(
            "SELECT entity_uri, pattern, decided_class, evidence, decided_by, decided_at FROM mapping_proposals"
            " WHERE status = 'accepted' AND decided_class IS NOT NULL ORDER BY decided_at DESC LIMIT %s",
            (limit,),
        ).fetchall()
        return [dict(zip(("entity_uri", "pattern", "decided_class", "evidence", "decided_by", "decided_at"), r)) for r in rows]

    def counts(self) -> dict[str, int]:
        return {s: n for s, n in self.conn.execute("SELECT status, count(*) FROM mapping_proposals GROUP BY status").fetchall()}


class MemoryProposals:
    """The same interface in memory: the eval harness and tests."""

    def __init__(self):
        self.rows: list[dict] = []
        self.rules: dict[str, str] = {}

    def ensure_schema(self) -> None:
        pass

    def _open(self, entity_uri: str) -> dict | None:
        return next((r for r in self.rows if r["entity_uri"] == entity_uri and r["status"] == "proposed"), None)

    def propose(self, entity_uri: str, pattern: str, candidates: list, refused_because: str, evidence: dict, asks: list) -> None:
        row = self._open(entity_uri) or next((r for r in self.rows if r["entity_uri"] == entity_uri and r["status"] == "escalated"), None)
        fields = {"pattern": pattern, "candidates": candidates, "refused_because": refused_because, "evidence": evidence,
                  "asks": asks, "updated_at": _now()}
        if row is not None:
            row.update(fields)
            return
        self.rows.append({"id": uuid.uuid4().hex, "entity_uri": entity_uri, "status": "proposed", "decided_class": None,
                          "decided_by": None, "assignee": None, "note": None, "decided_at": None, "created_at": _now(), **fields})

    def resolve(self, entity_uri: str, status: str, brick_class: str | None, by: str | None = None) -> None:
        row = self._open(entity_uri)
        if row is not None:
            row.update(status=status, decided_class=brick_class, decided_by=by, decided_at=_now())

    def list(self, status: str | tuple = "proposed", limit: int = 500) -> list[dict]:
        statuses = {status} if isinstance(status, str) else set(status)
        return sorted((r for r in self.rows if r["status"] in statuses), key=lambda r: (r["pattern"], r["entity_uri"]))[:limit]

    def get(self, ids: builtins.list[str]) -> builtins.list[dict]:
        return [r for r in self.rows if r["id"] in set(ids)]

    def decide(self, proposal_id: str, status: str, brick_class: str | None, by: str | None, assignee: str | None = None,
               note: str | None = None) -> bool:
        row = next((r for r in self.rows if r["id"] == proposal_id and r["status"] in HELD), None)
        if row is None:
            return False
        row.update(status=status, decided_by=by, updated_at=_now())
        for k, v in (("decided_class", brick_class), ("assignee", assignee), ("note", note)):
            if v is not None:
                row[k] = v
        if status not in HELD:
            row["decided_at"] = _now()
        return True

    def add_site_rule(self, pattern: str, brick_class: str, by: str | None, proposal_id: str | None) -> None:
        self.rules[pattern] = brick_class

    def site_rules(self) -> dict[str, str]:
        return dict(self.rules)

    def confirmed_examples(self, limit: int = 5000) -> builtins.list[dict]:
        return [r for r in self.rows if r["status"] == "accepted" and r["decided_class"]][:limit]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows:
            out[r["status"]] = out.get(r["status"], 0) + 1
        return out

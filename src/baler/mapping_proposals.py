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

import json
import uuid
from datetime import datetime, timezone

from baler import ddl

SCHEMA = """
CREATE TABLE IF NOT EXISTS mapping_proposals (
    id              TEXT PRIMARY KEY,
    entity_uri      TEXT NOT NULL,
    pattern         TEXT NOT NULL,
    candidates      JSONB NOT NULL,       -- [{brick_class, reason, source}], best first
    refused_because TEXT NOT NULL,        -- why the ladder didn't auto-accept
    evidence        JSONB NOT NULL,
    asks            JSONB NOT NULL,       -- each model ask: model, variant, candidates, confidence, rejected
    status          TEXT NOT NULL,        -- proposed | accepted | rejected | escalated | auto-accepted
    decided_class   TEXT,
    decided_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    decided_at      TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS mapping_proposals_open ON mapping_proposals (entity_uri) WHERE status = 'proposed';
CREATE INDEX IF NOT EXISTS mapping_proposals_pattern ON mapping_proposals (status, pattern);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PgProposals:
    def __init__(self, conn):
        self.conn = conn

    def ensure_schema(self) -> None:
        ddl.run(self.conn, SCHEMA)

    def propose(self, entity_uri: str, pattern: str, candidates: list, refused_because: str, evidence: dict, asks: list) -> None:
        now = _now()
        with self.conn.transaction():
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

    def list(self, status: str = "proposed", limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, entity_uri, pattern, candidates, refused_because, evidence, asks, status, decided_class, created_at, updated_at"
            " FROM mapping_proposals WHERE status = %s ORDER BY pattern, entity_uri LIMIT %s",
            (status, limit),
        ).fetchall()
        keys = ("id", "entity_uri", "pattern", "candidates", "refused_because", "evidence", "asks", "status", "decided_class", "created_at", "updated_at")
        return [dict(zip(keys, r)) for r in rows]


class MemoryProposals:
    """The same interface in memory: the eval harness and tests."""

    def __init__(self):
        self.rows: list[dict] = []

    def ensure_schema(self) -> None:
        pass

    def _open(self, entity_uri: str) -> dict | None:
        return next((r for r in self.rows if r["entity_uri"] == entity_uri and r["status"] == "proposed"), None)

    def propose(self, entity_uri: str, pattern: str, candidates: list, refused_because: str, evidence: dict, asks: list) -> None:
        row = self._open(entity_uri)
        fields = {"pattern": pattern, "candidates": candidates, "refused_because": refused_because, "evidence": evidence,
                  "asks": asks, "updated_at": _now()}
        if row is not None:
            row.update(fields)
            return
        self.rows.append({"id": uuid.uuid4().hex, "entity_uri": entity_uri, "status": "proposed", "decided_class": None,
                          "created_at": _now(), **fields})

    def resolve(self, entity_uri: str, status: str, brick_class: str | None, by: str | None = None) -> None:
        row = self._open(entity_uri)
        if row is not None:
            row.update(status=status, decided_class=brick_class, decided_by=by, decided_at=_now())

    def list(self, status: str = "proposed", limit: int = 500) -> list[dict]:
        return sorted((r for r in self.rows if r["status"] == status), key=lambda r: (r["pattern"], r["entity_uri"]))[:limit]

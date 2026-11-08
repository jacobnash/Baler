"""
Mapping memory (Baler#185): every point a person confirms is a labelled
example, and the next building should be cheaper for it.

An example is the evidence a reviewer saw (label, markers, unit, value
kind, equipment type, description and object type when Windrow sends them)
and the class they chose. Examples are stored per organisation
(`mapping_examples`) and written when a person accepts a proposal in the
review queue (#184).

Two uses in the model step (model_step.py):

  * **Nearest neighbour before any model.** TF-IDF over normalised tokens,
    cosine similarity. The site's own examples are tried first; if they
    don't settle it, the organisation's. If at least MIN_AGREE of the k
    nearest are above MIN_SIMILARITY and all name the same class (or a
    Brick equivalent), the class is taken, and the neighbours are recorded
    as the evidence.
  * **A voice and few-shot examples for the model.** When the neighbours
    don't agree, the nearest go into the prompt as examples. The most
    common class among them counts as a second opinion: if the model's
    first answer matches it, that agreement is enough, without a re-ask.

Examples never cross organisations: a lookup only sees examples with the
entity's own org (or, on a single-tenant install with no site prefixes,
the unscoped ones). Sharing across orgs would need explicit consent, and
there is no such switch.

Embeddings would be the next step only if the eval harness shows TF-IDF
leaving accuracy on the table (`python -m baler.mapping_eval --memory-from-seed`).
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from baler import brick_vocab, ddl

K = 5
MIN_SIMILARITY = 0.6  # chosen by a sweep on the mock hospital (0.5/0.6/0.7): see docs-site/pages/memory.mdx
MIN_AGREE = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS mapping_examples (
    id          BIGSERIAL PRIMARY KEY,
    org_id      TEXT,                 -- NULL on a single-tenant install (no site prefixes)
    site_id     TEXT,
    entity_uri  TEXT NOT NULL,
    kind        TEXT NOT NULL,        -- point | equip
    tokens      TEXT[] NOT NULL,
    evidence    JSONB NOT NULL,
    brick_class TEXT NOT NULL,
    source      TEXT NOT NULL,        -- review (a person in the queue)
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS mapping_examples_org ON mapping_examples (org_id, kind);
CREATE UNIQUE INDEX IF NOT EXISTS mapping_examples_entity ON mapping_examples (entity_uri, source);
"""

_SPLIT = re.compile(r"[^A-Za-z0-9]+")
_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def tokens(evidence: dict) -> list[str]:
    """Normalised features of one entity's evidence. Label words, and their
    first three letters too, so `TEM` meets `Temp` and `SUP` meets
    `Supply`; marker tags, the unit, the equipment type from the label's
    equip part, the value kind, and description/object type words when
    the evidence has them. Numbers are dropped: they are instance ids."""
    out: list[str] = []
    label = str(evidence.get("label") or "")
    parts = label.rstrip("/").split("/")
    leaf = parts[-1] if parts else label
    equip = parts[-2] if len(parts) >= 2 else ""
    if evidence.get("kind") == "equip":
        leaf, equip = parts[-1] if parts else "", ""
    for word in _words(leaf):
        out.append(f"w:{word}")
        out.append(f"p:{word[:3]}")
    for word in _words(re.sub(r"-?\d+$", "", equip)):
        out.append(f"e:{word}")
    for m in evidence.get("markers") or []:
        out.append(f"m:{m}")
    values = evidence.get("values") or {}
    if values.get("unit"):
        out.append(f"u:{values['unit']}")
    for key in ("description", "dis", "objectType", "bacnetObjectType"):
        for word in _words(str(values.get(key) or "")):
            out.append(f"d:{word}")
    stats = evidence.get("value_stats") or {}
    if stats:
        out.append("v:bool" if stats.get("boolean") else "v:analog")
    return out


def _words(text: str) -> list[str]:
    return [w.lower() for raw in _SPLIT.split(text) for w in _WORDS.findall(raw) if not w.isdigit() and len(w) > 1]


@dataclass
class Example:
    entity_uri: str
    kind: str
    brick_class: str
    tokens: list[str]
    org_id: str | None = None
    site_id: str | None = None
    label: str = ""


@dataclass
class Vote:
    """What memory says about one entity."""

    accepted: str | None  # set when the nearest agree
    suggestion: str | None  # the most common class among the nearest, if any
    neighbours: list[dict] = field(default_factory=list)  # [{label, brick_class, similarity, scope}]
    why: str = ""


def _same(vocab: brick_vocab.Vocab, a: str, b: str) -> bool:
    return a == b or (b in vocab.descendants(a) and a in vocab.descendants(b))


class Memory:
    """The examples one lookup may use, indexed for TF-IDF cosine."""

    def __init__(self, examples: Iterable[Example], vocab: brick_vocab.Vocab | None = None):
        self.examples = list(examples)
        self.vocab = vocab or brick_vocab.load()
        df: Counter[str] = Counter()
        for ex in self.examples:
            df.update(set(ex.tokens))
        n = max(len(self.examples), 1)
        self.idf = {t: math.log((1 + n) / (1 + c)) + 1.0 for t, c in df.items()}
        self.vectors = [self._vec(ex.tokens) for ex in self.examples]

    def _vec(self, toks: list[str]) -> dict[str, float]:
        tf = Counter(toks)
        v = {t: c * self.idf.get(t, math.log(1 + max(len(self.examples), 1)) + 1.0) for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    def nearest(self, evidence: dict, kind: str, org_id: str | None, site_id: str | None = None, k: int = K) -> list[tuple[float, Example]]:
        """The k most similar examples of the same kind and org, the site's
        own ranked first when `site_id` is given (see vote)."""
        q = self._vec(tokens(evidence))
        scored = []
        for ex, v in zip(self.examples, self.vectors):
            if ex.kind != kind or ex.org_id != org_id:  # never across orgs
                continue
            if site_id is not None and ex.site_id != site_id:
                continue
            sim = sum(x * v.get(t, 0.0) for t, x in q.items())
            scored.append((sim, ex))
        scored.sort(key=lambda p: (-p[0], p[1].entity_uri))
        return scored[:k]

    def vote(self, evidence: dict, kind: str, org_id: str | None = None, site_id: str | None = None) -> Vote:
        """Same site first, then the org. Accepts only when at least
        MIN_AGREE neighbours are above MIN_SIMILARITY and every one of
        those names the same class (or an equivalent)."""
        tried: list[dict] = []
        scopes = ([("site", site_id)] if site_id is not None else []) + [("org", None)]
        best_suggestion = None
        for scope_name, site in scopes:
            near = [(s, ex) for s, ex in self.nearest(evidence, kind, org_id, site) if s >= MIN_SIMILARITY]
            neighbours = [{"label": ex.label or ex.entity_uri, "brick_class": ex.brick_class, "similarity": round(s, 3), "scope": scope_name}
                          for s, ex in near]
            tried.extend(n for n in neighbours if n not in tried)
            if not near:
                continue
            counts = Counter(ex.brick_class for _, ex in near)
            best_suggestion = best_suggestion or counts.most_common(1)[0][0]
            first = near[0][1].brick_class
            if len(near) >= MIN_AGREE and all(_same(self.vocab, first, ex.brick_class) for _, ex in near):
                return Vote(first, first, neighbours, f"the {len(near)} nearest confirmed {kind}s ({scope_name}) are all {first}")
        return Vote(None, best_suggestion, tried[:K], "no agreement among the nearest confirmed examples" if tried else "no similar confirmed example")


# --- storage --------------------------------------------------------------


def ensure_schema(conn) -> None:
    ddl.run(conn, SCHEMA)


def add_examples(conn, rows: list[dict], brick_class: str, by: str | None) -> int:
    """Store the evidence behind accepted proposals as examples: one per
    entity, replaced if a person answers it again."""
    now = datetime.now(timezone.utc)
    locate = pg_locator(conn)
    for r in rows:
        ev = r.get("evidence") or {}
        org_id, site_id = locate(r["entity_uri"])
        kind = "equip" if r["entity_uri"].startswith("urn:equip:") else "point"
        conn.execute(
            """INSERT INTO mapping_examples (org_id, site_id, entity_uri, kind, tokens, evidence, brick_class, source, created_by, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'review', %s, %s)
               ON CONFLICT (entity_uri, source) DO UPDATE SET org_id = EXCLUDED.org_id, site_id = EXCLUDED.site_id,
                 tokens = EXCLUDED.tokens, evidence = EXCLUDED.evidence, brick_class = EXCLUDED.brick_class,
                 created_by = EXCLUDED.created_by, created_at = EXCLUDED.created_at""",
            (org_id, site_id, r["entity_uri"], kind, tokens({**ev, "kind": kind}), json.dumps(ev, default=str), brick_class, by, now),
        )
    return len(rows)


def load(conn, vocab: brick_vocab.Vocab | None = None) -> Memory:
    rows = conn.execute("SELECT entity_uri, kind, brick_class, tokens, org_id, site_id, evidence->>'label' FROM mapping_examples").fetchall()
    return Memory([Example(u, k, c, list(t), o, s, lbl or "") for u, k, c, t, o, s, lbl in rows], vocab)


def recall_for(memory: Memory, locate: Callable[[str], tuple[str | None, str | None]] | None = None):
    """The callable model_step takes: evidence -> Vote, for the entity's own
    org and site (`locate(uri)`; single-tenant when None)."""
    def recall(ev) -> Vote:
        org_id, site_id = locate(ev.uri) if locate else (None, None)
        return memory.vote({**ev.to_dict(), "kind": ev.kind}, ev.kind, org_id, site_id)

    return recall


def pg_locator(conn) -> Callable[[str], tuple[str | None, str | None]]:
    """uri -> (org_id, site_id) of the site whose topic prefix covers it
    (the longest), or (None, None) on a single-tenant install or an
    uncovered entity. Reads the sites table once."""
    rows = conn.execute("SELECT id, org_id, topic_prefixes FROM sites").fetchall()

    def locate(uri: str) -> tuple[str | None, str | None]:
        topic = uri.removeprefix("urn:point:").removeprefix("urn:equip:")
        best = (None, None, -1)
        for site_id, org_id, prefixes in rows:
            for p in prefixes or []:
                p = p.rstrip("/")
                if (topic == p or topic.startswith(p + "/")) and len(p) > best[2]:
                    best = (org_id, site_id, len(p))
        return best[0], best[1]

    return locate

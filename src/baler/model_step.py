"""
The model step of the tagging ladder (Baler#182): what happens to a point
or equip the rules couldn't place, before a person sees it.

    rules (mapping.classify_point) -> model step (here) -> review queue

What changed from the old one-call-per-point fallback, and why:

  * **Evidence, not just tags.** Each prompt carries the label, every
    Haystack tag (markers and values: unit, kind, writable, enum, dis,
    description, ...), value stats from history when a connection is
    given, and the equip's sibling points with their classes.
  * **Candidates, then top 3.** A deterministic ranker (`candidates`)
    picks the ~20 Brick classes whose names best match the evidence; the
    model picks its top 3 from Brick, with a reason each. Every answer is
    checked with brick_vocab.canonical_class (#181); a class Brick doesn't
    have is dropped, never written.
  * **Agreement, not self-reported confidence.** A model's own confidence
    is poorly calibrated. The pattern is asked twice, the second time with
    a reworded prompt and the candidates in reverse order. Its top-1 is
    auto-accepted only when both asks name the same class (or a Brick
    equivalent) and the unit veto passes. When memory (#185) lands it
    becomes a second voice here. Everything else becomes a mapping
    proposal (mapping_proposals) for a person.
  * **One call per pattern, not per point.** `CHW-SUP-TEM-12` on chiller 1
    and `CHW-SUP-TEM-31` on chiller 3 share a pattern (digits -> {n}, plus
    the tag set and unit), so they share one question.
  * **Any provider.** The model is anything with `.name` and
    `.ask(prompt) -> dict`; llm_classifier.model_from_env builds one from
    BALER_LLM_PROVIDER (Claude, OpenAI-compatible, or none), like Cx.

With no model at all the step still runs: every unresolved point becomes a
proposal carrying the deterministic candidates, so the review queue starts
with suggestions rather than a blank.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from rdflib import SKOS, Literal, URIRef

from baler import brick_vocab, tracing
from baler.store import BRICK, TD

tracer = tracing.get_tracer(__name__)

TOP_N = 3
CANDIDATES = 20
# Weak by design: an agreeing pair still needs at least this much stated
# confidence from both asks. Agreement is what auto-accepts, not this.
MIN_CONFIDENCE = 0.5
MAX_SIBLINGS = 15
MAX_EXAMPLES = 3  # member labels shown per pattern

# Haystack marker spellings -> the words Brick class names use. Only where
# they differ; the rest match Brick's own tag names case-insensitively.
_MARKER_WORDS = {
    "temp": "Temperature", "sp": "Setpoint", "cmd": "Command", "occ": "Occupied", "unocc": "Unoccupied",
    "elec": "Electric", "co2": "CO2", "run": "Run", "dewPoint": "Dewpoint",
}
# Haystack unit -> the quantity word a Brick class for it carries. "%" is
# left out: position, speed, humidity and signal all use it.
UNIT_QUANTITY = {
    "°F": "Temperature", "°C": "Temperature", "K": "Temperature", "Δ°F": "Temperature", "Δ°C": "Temperature",
    "%RH": "Humidity", "ppm": "CO2",
    "cfm": "Flow", "L/s": "Flow", "m³/h": "Flow", "gal/min": "Flow",
    "kW": "Power", "W": "Power", "MW": "Power", "BTU/h": "Power", "tonref": "Power",
    "kWh": "Energy",
    "Pa": "Pressure", "kPa": "Pressure", "inH₂O": "Pressure", "psi": "Pressure",
    "rpm": "Speed", "ft/min": "Speed",
    "A": "Current", "V": "Voltage", "Hz": "Frequency",
}
_QUANTITIES = frozenset(UNIT_QUANTITY.values()) | {"Position", "Level"}
_GENERIC = frozenset({"Point", "Sensor", "Setpoint", "Command", "Status", "Equipment", "Parameter", "Alarm"})
_GENERIC_LOWER = frozenset(w.lower() for w in _GENERIC)
_TOKEN = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


class Model(Protocol):
    name: str

    def ask(self, prompt: str) -> dict: ...


@dataclass
class Evidence:
    uri: str
    kind: str  # point | equip
    label: str
    markers: list[str]
    values: dict[str, str]  # valued Haystack tags (unit, kind, enum, dis, ...)
    siblings: list[dict] = field(default_factory=list)  # [{label, class}]
    value_stats: dict | None = None

    @property
    def unit(self) -> str | None:
        return self.values.get("unit")

    def to_dict(self) -> dict:
        return {
            "uri": self.uri, "kind": self.kind, "label": self.label, "markers": self.markers, "values": self.values,
            "siblings": self.siblings, "value_stats": self.value_stats,
        }


@dataclass
class Decision:
    uri: str
    pattern: str
    accepted: str | None  # the Brick class written, or None (-> proposal)
    candidates: list[dict]  # [{brick_class, reason, source}], best first
    refused_because: str | None
    asks: list[dict]  # what each ask said
    evidence: Evidence


# --- evidence -------------------------------------------------------------

_HAY = "urn:baler:haystack#"


def gather(store, uri: URIRef, kind: str, stats: Callable[[list[str]], dict] | None = None) -> Evidence:
    """Everything the graph knows about one entity that could tell a person
    (or a model) what it is."""
    rows = store.query(f"SELECT ?p ?o WHERE {{ <{uri}> ?p ?o }}")
    markers: list[str] = []
    values: dict[str, str] = {}
    label = ""
    for r in rows:
        p, o = str(r.p), str(r.o)
        if p == _HAY + "hasTag":
            markers.append(o)
        elif p.startswith(_HAY):
            values[p[len(_HAY):]] = o
        elif p == str(TD.sourceTopic):
            label = o
    if not label:
        label = str(uri).split(":", 2)[-1] if kind == "point" else str(uri).removeprefix("urn:equip:")
    ev = Evidence(uri=str(uri), kind=kind, label=label, markers=sorted(markers), values=dict(sorted(values.items())))
    ev.siblings = _siblings(store, uri, kind)
    if stats is not None:
        ev.value_stats = stats([str(uri)]).get(str(uri))
    return ev


def _siblings(store, uri: URIRef, kind: str) -> list[dict]:
    """A point's fellow points on its equip; an equip's own points."""
    if kind == "point":
        q = f"""SELECT ?s ?topic ?type WHERE {{
            ?e <{BRICK.hasPoint}> <{uri}> . ?e <{BRICK.hasPoint}> ?s . FILTER(?s != <{uri}>)
            OPTIONAL {{ ?s <{TD.sourceTopic}> ?topic }} OPTIONAL {{ ?s a ?type }} }}"""
    else:
        q = f"""SELECT ?s ?topic ?type WHERE {{
            <{uri}> <{BRICK.hasPoint}> ?s
            OPTIONAL {{ ?s <{TD.sourceTopic}> ?topic }} OPTIONAL {{ ?s a ?type }} }}"""
    found: dict[str, dict] = {}
    for r in store.query(q):
        s = str(r.s)
        entry = found.setdefault(s, {"label": _leaf(str(r.topic) if r.topic else s), "class": None})
        if r.type is not None and str(r.type).startswith(str(BRICK)):
            entry["class"] = str(r.type)[len(str(BRICK)):]
    return sorted(found.values(), key=lambda e: e["label"])[:MAX_SIBLINGS]


def _leaf(label: str) -> str:
    return label.rstrip("/").rsplit("/", 1)[-1]


def pattern(ev: Evidence) -> str:
    """Digits -> {n} in the label's equip and point parts, plus the marker
    set and unit: entities sharing this get one question."""
    shape = re.sub(r"\d+", "{n}", ev.label)
    return f"{ev.kind}|{shape}|{','.join(ev.markers)}|{ev.unit or ''}"


# --- deterministic candidates ---------------------------------------------


@dataclass
class _Index:
    vocab: brick_vocab.Vocab
    words: dict[str, frozenset[str]]  # class -> lowercased name words
    name_words: dict[str, str]  # lowercased word -> Brick spelling
    roots: dict[str, frozenset[str]]


_INDEX: _Index | None = None


def _index(vocab: brick_vocab.Vocab) -> _Index:
    global _INDEX
    if _INDEX is not None and _INDEX.vocab is vocab:
        return _INDEX
    words = {c: frozenset(w.lower() for w in c.split("_")) for c in vocab.classes}
    name_words = {w.lower(): w for c in vocab.classes for w in c.split("_")}
    roots = {"point": vocab.descendants("Point"), "equip": vocab.descendants("Equipment")}
    _INDEX = _Index(vocab, words, name_words, roots)
    return _INDEX


def evidence_words(ev: Evidence, vocab: brick_vocab.Vocab) -> dict[str, float]:
    """Lowercased Brick name words the evidence supports, with weights: a
    marker or unit is strong, a label token that only prefixes a Brick word
    (`tem` -> temperature) is weaker, and weaker still when it's ambiguous.
    No abbreviation lexicon here: that is shared work (openhay#7)."""
    idx = _index(vocab)
    out: dict[str, float] = {}

    def add(word: str, w: float) -> None:
        out[word.lower()] = max(out.get(word.lower(), 0.0), w)

    for m in ev.markers:
        add(_MARKER_WORDS.get(m, m), 2.0)
    if ev.unit in UNIT_QUANTITY:
        add(UNIT_QUANTITY[ev.unit], 2.0)
    for raw in re.split(r"[^A-Za-z0-9]+", ev.label):
        for tok in _TOKEN.findall(raw):
            t = tok.lower()
            if t.isdigit() or len(t) < 2:
                continue
            mapped = _MARKER_WORDS.get(t) or vocab.canonical_tag(t)
            if mapped:
                add(mapped, 1.5)
                continue
            if t in idx.name_words:
                add(t, 1.5)
                continue
            split = _compound(t, idx.name_words)  # airflow -> air, flow
            if split:
                for part in split:
                    add(part, 1.5)
                continue
            if len(t) >= 3:
                hits = [w for w in idx.name_words if w.startswith(t)]
                for w in hits:
                    add(w, 1.0 if len(hits) == 1 else 0.6)
    for key in ("dis", "description", "navName"):
        if key in ev.values:
            for tok in re.findall(r"[A-Za-z]+", ev.values[key]):
                if tok.lower() in idx.name_words:
                    add(tok, 1.5)
    if ev.kind == "point":
        # The point's function. Not `writable`: BACnet analog/binary values
        # are commandable whatever they are, so Windrow tags most points
        # writable. A quantity unit says "measured", unless something
        # already says setpoint; on/off values say status or command.
        if "sensor" in ev.markers:
            add("Sensor", 2.0)
        elif ev.unit in UNIT_QUANTITY and "setpoint" not in out:
            add("Sensor", 1.0)
        if (ev.value_stats or {}).get("boolean"):
            add("Status", 0.6)
            add("Command", 0.6)
    return out


def _compound(token: str, known) -> tuple[str, str] | None:
    """A run-together pair of Brick name words (`airflow`), else None."""
    for i in range(3, len(token) - 2):
        if token[:i] in known and token[i:] in known:
            return token[:i], token[i:]
    return None


def candidates(ev: Evidence, vocab: brick_vocab.Vocab, n: int = CANDIDATES) -> list[str]:
    """The Brick classes whose names the evidence best explains: matched
    word weight, minus a penalty for each name word with no support, so a
    specific match beats a generic one only when the evidence covers it."""
    idx = _index(vocab)
    words = evidence_words(ev, vocab)
    pool = idx.roots[ev.kind]
    direct = []
    if ev.kind == "equip":  # Brick aliases are short names: vav, ahu, ...
        for raw in re.split(r"[^A-Za-z0-9]+", ev.label):
            c = vocab.canonical_class(raw)
            if c and c in pool:
                direct.append(vocab.preferred_name(c))
    scored = []
    for c in pool:
        if vocab.preferred_name(c) != c:
            continue
        cw = idx.words[c]
        matched = [w for w in cw if w in words]
        if not matched or all(w in _GENERIC_LOWER for w in matched):
            continue
        hit = sum(words[w] for w in matched)
        miss = sum(1 for w in cw if w not in words)
        scored.append((hit - 0.75 * miss, -len(cw), c))
    scored.sort(reverse=True)
    out = list(dict.fromkeys(direct + [c for _, _, c in scored]))
    return out[:n]


# --- veto -----------------------------------------------------------------


def veto(ev: Evidence, brick_class: str, vocab: brick_vocab.Vocab) -> str | None:
    """A deterministic reason the class can't be right, or None. Only the
    unit and boolean-value checks the evidence supports today; the value
    range, spike and kind checks are #183's."""
    if ev.kind != "point":
        return None
    name_words = set(brick_class.split("_")) | set(vocab.effective_tags(brick_class))
    quantity = UNIT_QUANTITY.get(ev.unit or "")
    if quantity:
        class_q = name_words & _QUANTITIES
        if class_q and quantity not in class_q:
            return f"unit {ev.unit} is a {quantity.lower()}, the class measures {', '.join(sorted(class_q)).lower()}"
        if not class_q and name_words & {"Status", "Command"} and "Setpoint" not in name_words:
            return f"unit {ev.unit} is a {quantity.lower()}, the class is a binary {'status' if 'Status' in name_words else 'command'}"
    stats = ev.value_stats or {}
    if stats.get("boolean") and name_words & (_QUANTITIES - {"Position"}):
        return "values are on/off, the class measures a quantity"
    binary_class = bool(name_words & {"Status", "Command"}) and not name_words & (_QUANTITIES | {"Setpoint"})
    if stats and not stats.get("boolean") and stats.get("min") is not None and binary_class:
        return "values are analog, the class is an on/off status or command"
    return None


# --- asking ---------------------------------------------------------------

_INSTRUCTIONS = (
    "Pick the Brick 1.4 class for these building-automation {kind}s. They share one naming pattern, so one answer "
    "covers all of them. Prefer a class from the candidates; you may name another Brick class if none fits, but "
    "never invent one. Give your top {n}, best first, each with one short reason, and a confidence from 0 to 1 "
    "for the first."
)
_REWORDED = (
    "Which Brick class (Brick schema 1.4) describes each of these {kind}s? All of them follow the same naming "
    "pattern. List up to {n} real Brick classes, most likely first, a one-line reason for each, and how sure "
    "you are of the first (0 to 1). The candidate list is a starting point, not a limit, but only real classes count."
)
_REPLY = 'Reply with JSON only: {"candidates": [{"brick_class": "...", "reason": "..."}], "confidence": 0.0}'


def prompt(evs: list[Evidence], cands: list[str], variant: int) -> str:
    ev = evs[0]
    lines = [(_INSTRUCTIONS if variant == 0 else _REWORDED).format(kind=ev.kind, n=TOP_N), ""]
    lines.append("Examples of the pattern: " + "; ".join(e.label for e in evs[:MAX_EXAMPLES]) + f" ({len(evs)} in all)")
    lines.append(f"Haystack markers: {', '.join(ev.markers) or '(none)'}")
    if ev.values:
        lines.append("Other tags: " + ", ".join(f"{k}={v}" for k, v in ev.values.items()))
    if ev.value_stats:
        lines.append("Recent values: " + ", ".join(f"{k}={v}" for k, v in ev.value_stats.items()))
    if ev.siblings:
        lines.append(("Other points on the same equipment" if ev.kind == "point" else "Its points") + ": "
                     + "; ".join(f"{s['label']}" + (f" ({s['class']})" if s["class"] else "") for s in ev.siblings))
    ordered = cands if variant == 0 else list(reversed(cands))
    lines.append("Candidate Brick classes: " + (", ".join(ordered) or "(none found)"))
    lines += ["", _REPLY]
    return "\n".join(lines)


def _parse(reply: Any, vocab: brick_vocab.Vocab) -> tuple[list[dict], float, list[str]]:
    """(valid candidates, confidence, rejected names). Never raises on a
    malformed reply: it's treated as no answer."""
    if not isinstance(reply, dict):
        return [], 0.0, []
    out: list[dict] = []
    rejected: list[str] = []
    for item in reply.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("brick_class") or "")
        canon = vocab.canonical_class(name) if name else None
        if canon is None:
            if name:
                rejected.append(name)
            continue
        if all(c["brick_class"] != canon for c in out):
            out.append({"brick_class": canon, "reason": str(item.get("reason") or "")[:300]})
        if len(out) == TOP_N:
            break
    try:
        conf = float(reply.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    return out, conf, rejected


def _same(vocab: brick_vocab.Vocab, a: str, b: str) -> bool:
    return a == b or (b in vocab.descendants(a) and a in vocab.descendants(b))


def decide_pattern(evs: list[Evidence], model: Model | None, vocab: brick_vocab.Vocab) -> list[Decision]:
    """One question (asked twice) for a group of entities sharing a pattern;
    one Decision per entity."""
    key = pattern(evs[0])
    cands = candidates(evs[0], vocab)
    asks: list[dict] = []
    answers: list[tuple[list[dict], float]] = []
    if model is not None:
        for variant in (0, 1):
            with tracer.start_as_current_span("model_step.ask") as span:
                span.set_attribute("pattern", key)
                span.set_attribute("variant", variant)
                reply = model.ask(prompt(evs, cands, variant))
            got, conf, rejected = _parse(reply, vocab)
            answers.append((got, conf))
            asks.append({"model": model.name, "variant": variant, "candidates": got, "confidence": conf, "rejected": rejected})

    accepted: str | None = None
    refused: str | None
    if model is None:
        refused = "no model configured"
    elif not answers[0][0] or not answers[1][0]:
        refused = "the model gave no Brick class" + (f" (named {', '.join(asks[0]['rejected'] + asks[1]['rejected'])}, not in Brick)"
                                                      if asks[0]["rejected"] or asks[1]["rejected"] else "")
    elif not _same(vocab, answers[0][0][0]["brick_class"], answers[1][0][0]["brick_class"]):
        refused = f"asked twice, it said {answers[0][0][0]['brick_class']} then {answers[1][0][0]['brick_class']}"
    elif min(answers[0][1], answers[1][1]) < MIN_CONFIDENCE:
        refused = f"it agreed with itself but said it was unsure ({min(answers[0][1], answers[1][1]):.2f})"
    else:
        accepted, refused = answers[0][0][0]["brick_class"], None

    # Candidates for review: the model's (first ask, then any new from the
    # second), then the deterministic ranker's, top 3 overall.
    merged: list[dict] = []
    for got, _ in answers:
        for c in got:
            if all(not _same(vocab, m["brick_class"], c["brick_class"]) for m in merged):
                merged.append({**c, "source": "model"})
    for name in cands:
        if all(not _same(vocab, m["brick_class"], name) for m in merged):
            merged.append({"brick_class": name, "reason": "names match the label, tags and unit", "source": "ranker"})

    decisions = []
    for ev in evs:
        this_accepted, this_refused = accepted, refused
        if accepted is not None:
            why = veto(ev, accepted, vocab)
            if why:
                this_accepted, this_refused = None, f"vetoed: {why}"
        decisions.append(Decision(ev.uri, key, this_accepted, merged[:TOP_N], this_refused, asks, ev))
    return decisions


def run(
    store,
    entities: Iterable[tuple[URIRef, str]],
    model: Model | None,
    proposals=None,
    stats: Callable[[list[str]], dict] | None = None,
    vocab: brick_vocab.Vocab | None = None,
    site_rules: dict[str, str] | None = None,
) -> list[Decision]:
    """The model step over (uri, "point"|"equip") pairs the rules left
    unresolved. Points go first, so an equip's question sees its points'
    new classes. Auto-accepted classes are written to the graph; every
    other entity becomes a proposal in `proposals` (mapping_proposals.*),
    if given.

    `site_rules` ({pattern: Brick class}) are patterns a person approved
    in the review queue (#184): a matching group takes that class without
    asking a model, unless the veto objects for one of its entities."""
    vocab = vocab or brick_vocab.load()
    entities = list(entities)
    decisions: list[Decision] = []
    for kind in ("point", "equip"):
        evs = [gather(store, uri, kind) for uri, k in entities if k == kind]
        if stats is not None and evs:
            all_stats = stats([e.uri for e in evs])
            for e in evs:
                e.value_stats = all_stats.get(e.uri)
        groups: dict[str, list[Evidence]] = {}
        for e in evs:
            groups.setdefault(pattern(e), []).append(e)
        for key, group in sorted(groups.items()):
            approved = (site_rules or {}).get(key)
            for d in (by_site_rule(group, approved, vocab) if approved else decide_pattern(group, model, vocab)):
                decisions.append(d)
                _apply(store, d, proposals)
    return decisions


def by_site_rule(evs: list[Evidence], brick_class: str, vocab: brick_vocab.Vocab) -> list[Decision]:
    """A pattern a person already answered: their class, no model call."""
    key = pattern(evs[0])
    cand = [{"brick_class": brick_class, "reason": "a person approved this naming pattern", "source": "site rule"}]
    out = []
    for ev in evs:
        why = veto(ev, brick_class, vocab)
        out.append(Decision(ev.uri, key, None if why else brick_class, cand, f"vetoed: {why}" if why else None,
                            [{"model": "site rule", "candidates": cand}], ev))
    return out


def _apply(store, d: Decision, proposals) -> None:
    uri = URIRef(d.uri)
    if d.accepted is not None:
        store.add_entity(uri, BRICK[d.accepted])
        reason = next((c["reason"] for c in d.candidates if c["brick_class"] == d.accepted), "")
        site_rule = any(c.get("source") == "site rule" for c in d.candidates)
        store.add_relationship(uri, SKOS.definition, Literal(
            f"site-rule-classified: {reason}" if site_rule else f"model-classified (agreed on re-ask): {reason}"))
        if proposals is not None:
            proposals.resolve(d.uri, "auto-accepted", d.accepted)
        return
    if proposals is not None:
        proposals.propose(
            entity_uri=d.uri, pattern=d.pattern, candidates=d.candidates, refused_because=d.refused_because or "",
            evidence=d.evidence.to_dict(), asks=d.asks,
        )


def pg_stats(conn, days: int = 7) -> Callable[[list[str]], dict]:
    """Value stats from point_history for the model step's evidence: count,
    min, max, mean and distinct count over the last `days`, and whether
    every value was on/off."""
    def stats(uris: list[str]) -> dict:
        if not uris:
            return {}
        rows = conn.execute(
            """SELECT point_uri, count(*), min(value), max(value), avg(value), count(DISTINCT COALESCE(value::text, value_text, value_bool::text)),
                      bool_and(value IS NULL AND (value_bool IS NOT NULL OR lower(value_text) IN ('active', 'inactive', 'on', 'off', 'true', 'false')))
               FROM point_history WHERE point_uri = ANY(%s) AND ts > now() - make_interval(days => %s)
               GROUP BY point_uri""",
            (uris, days),
        ).fetchall()
        out = {}
        for uri, n, lo, hi, mean, distinct, boolean in rows:
            out[uri] = {"samples": n, "min": lo, "max": hi, "mean": None if mean is None else round(mean, 3), "distinct": distinct, "boolean": bool(boolean)}
        return out

    return stats


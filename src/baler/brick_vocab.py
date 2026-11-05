"""
Brick's class hierarchy, aliases, and tag vocabulary, read straight from
the vendored ontology/Brick-only.ttl - so hisquery.py can let people
query Brick-typed data the way they *talk* about it rather than the way
Brick spells it:

  readAll(Temperature_Sensor)      every subclass too, not just exact type
  readAll(AHU)                     Brick's own alias -> Air_Handling_Unit
  readAll(air_handling_unit)       class names are case-insensitive
  readAll(temperature and sensor)  Brick's per-class tag vocabulary
                                   (brick:hasAssociatedTag), which is what
                                   a class name *means* split into words

Why a regex over the Turtle file and not a SPARQL query against the live
graph or an rdflib parse:

  * Oxigraph evaluates `?t rdfs:subClassOf* ?c` with an unbound ?c in
    50+ seconds over the full Brick ontology (measured, 1.4.4) - it plans
    from the wrong end. Handing it a bound list of class IRIs instead
    (`FILTER(?c IN (...))`) is ~0.1-0.3s even for 800 classes. So the
    hierarchy reasoning happens here, in Python, once per process.
  * The live graph may not have the ontology loaded at all (nothing in
    docker-compose loads it - see architecture.mdx). Reading the vendored
    file means readAll(Temperature_Sensor) finds subclasses regardless.
  * rdflib parses this 1.6 MB file in seconds; the regex takes ~30 ms.
    The file is rdflib-serialized output from Brick's release pipeline
    (one `brick:Name a owl:Class` block per class, blank-line separated),
    and test_brick_vocab.py pins the facts we rely on so a Brick bump that
    changes the shape fails loudly rather than silently matching nothing.

Effective tags: Brick doesn't put hasAssociatedTag on every class -
aliases (brick:AHU, brick:Discharge_Air_Temperature_Sensor) carry none
and inherit from the class they're owl:equivalentClass to; so a class's
effective tag set is its own, plus its equivalents', plus every
ancestor's. That's what makes `temperature and sensor` find a point typed
Discharge_Air_Temperature_Sensor.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache

DEFAULT_ONTOLOGY_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "ontology", "Brick-only.ttl")

# Haystack marker spellings for the Brick tag words they mean. Only the
# ones where the two vocabularies genuinely differ - `zone`, `air`,
# `sensor`, `fan`, `damper`, ... are the same word in both and need no
# entry (matched case-insensitively against Brick's tag names directly).
# Each Brick side is verified to exist as a `tag:` in Brick 1.4.4.
HAYSTACK_TO_BRICK_TAG = {
    "temp": "Temperature",
    "sp": "Setpoint",
    "cmd": "Command",
    "equip": "Equipment",
    "elec": "Electrical",
    "occ": "Occupancy",
}

# `brick:X a owl:Class,\n  sh:NodeShape ;` for live classes; deprecated
# ones are `brick:X a owl:Class ;` on one line with no shape - match both.
_CLASS_BLOCK = re.compile(r"^brick:(\w+) a owl:Class\b(.*?)(?=\n\n|\Z)", re.MULTILINE | re.DOTALL)
_TAG_DECL = re.compile(r"^tag:(\w+) a brick:Tag\b", re.MULTILINE)
_SUBCLASS = re.compile(r"rdfs:subClassOf ((?:brick:\w+[,\s]*)+)")
# owl:equivalentClass is how Brick states aliases (AHU = Air_Handling_Unit);
# brick:isReplacedBy is how it states deprecations (Zone_Air_Temperature_
# Setpoint -> Target_Zone_Air_Temperature_Setpoint). For "what does this
# class mean" both are the same kind of link: same tags, same subtree.
_EQUIV = re.compile(r"(?:owl:equivalentClass|brick:isReplacedBy) ((?:brick:\w+[,\s]*)+)")
_ASSOC_TAGS = re.compile(r"brick:hasAssociatedTag ((?:tag:\w+[,\s]*)+)")


@dataclass
class BrickClass:
    name: str
    parents: set[str] = field(default_factory=set)
    equivalents: set[str] = field(default_factory=set)
    own_tags: set[str] = field(default_factory=set)


@dataclass
class Vocab:
    classes: dict[str, BrickClass]
    tags: set[str]
    _lower_class: dict[str, str] = field(default_factory=dict)
    _lower_tag: dict[str, str] = field(default_factory=dict)
    _children: dict[str, set[str]] = field(default_factory=dict)
    _effective_tags: dict[str, frozenset[str]] = field(default_factory=dict)
    _classes_by_tag: dict[str, frozenset[str]] = field(default_factory=dict)

    def __post_init__(self):
        self._lower_class = {name.lower(): name for name in self.classes}
        self._lower_tag = {t.lower(): t for t in self.tags}
        for c in self.classes.values():
            for p in c.parents:
                self._children.setdefault(p, set()).add(c.name)
        # equivalentClass is symmetric but Brick states it one way (alias
        # -> canonical); close it so either name reaches the other.
        for c in list(self.classes.values()):
            for e in list(c.equivalents):
                if e in self.classes:
                    self.classes[e].equivalents.add(c.name)
        by_tag: dict[str, set[str]] = {}
        for name in self.classes:
            eff = self._compute_effective_tags(name, set())
            self._effective_tags[name] = frozenset(eff)
            for t in eff:
                by_tag.setdefault(t, set()).add(name)
        self._classes_by_tag = {t: frozenset(cs) for t, cs in by_tag.items()}

    def _compute_effective_tags(self, name: str, seen: set[str]) -> set[str]:
        if name in seen or name not in self.classes:
            return set()
        seen.add(name)
        c = self.classes[name]
        tags = set(c.own_tags)
        for other in c.equivalents | c.parents:
            tags |= self._compute_effective_tags(other, seen)
        return tags

    # --- lookups ------------------------------------------------------

    def canonical_class(self, name: str) -> str | None:
        """`air_handling_unit` / `Air_handling_Unit` -> `Air_Handling_Unit`;
        None if Brick has no such class (it may still be a PROJ class)."""
        return self._lower_class.get(name.lower())

    def canonical_tag(self, word: str) -> str | None:
        """`temperature` -> `Temperature`, `temp` -> `Temperature` (Haystack
        synonym), `co2` -> `CO2`; None if it's not a Brick tag word."""
        mapped = HAYSTACK_TO_BRICK_TAG.get(word.lower())
        return mapped if mapped in self.tags else self._lower_tag.get(word.lower())

    def descendants(self, name: str) -> frozenset[str]:
        """The class itself, its equivalents, and everything below any of
        them - i.e. every Brick class an entity could be typed as and still
        count as a `name`."""
        out: set[str] = set()
        stack = [name]
        while stack:
            n = stack.pop()
            if n in out or n not in self.classes:
                continue
            out.add(n)
            stack.extend(self.classes[n].equivalents)
            stack.extend(self._children.get(n, ()))
        return frozenset(out)

    def classes_with_tag(self, tag: str) -> frozenset[str]:
        return self._classes_by_tag.get(tag, frozenset())

    def effective_tags(self, name: str) -> frozenset[str]:
        return self._effective_tags.get(name, frozenset())

    def suggest_class(self, name: str, n: int = 3) -> list[str]:
        return difflib.get_close_matches(name, list(self.classes), n=n, cutoff=0.75)

    def suggest_tag(self, word: str, n: int = 3) -> list[str]:
        words = sorted({t.lower() for t in self.tags} | set(HAYSTACK_TO_BRICK_TAG))
        return difflib.get_close_matches(word.lower(), words, n=n, cutoff=0.75)


def parse(path: str = DEFAULT_ONTOLOGY_PATH) -> Vocab:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    classes: dict[str, BrickClass] = {}
    for m in _CLASS_BLOCK.finditer(text):
        name, body = m.group(1), m.group(2)
        c = BrickClass(name)
        for rx, target in ((_SUBCLASS, c.parents), (_EQUIV, c.equivalents)):
            for hit in rx.finditer(body):
                target.update(re.findall(r"brick:(\w+)", hit.group(1)))
        tags_hit = _ASSOC_TAGS.search(body)
        if tags_hit:
            c.own_tags.update(re.findall(r"tag:(\w+)", tags_hit.group(1)))
        classes[name] = c
    tags = set(_TAG_DECL.findall(text))
    return Vocab(classes=classes, tags=tags)


@lru_cache(maxsize=1)
def load(path: str = DEFAULT_ONTOLOGY_PATH) -> Vocab:
    """The vendored Brick, parsed once per process. If the file is missing
    (a stripped-down deployment) this returns an empty Vocab and hisquery
    falls back to exact rdf:type matching - never an error at query time."""
    if not os.path.exists(path):
        return Vocab(classes={}, tags=set())
    return parse(path)

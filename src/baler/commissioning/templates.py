"""
Expected-point templates, read from SHACL (ontology/cx-expected-points.ttl
- Jacob, 2026-11-06: "SHACL, in ontology/").

The shapes file is real SHACL (pyshacl validates against it unchanged -
tests/test_commissioning_templates.py proves the two agree). This module
evaluates the same shapes the way commissioning needs, which plain SHACL
over the live graph can't:

  - subclass-aware without loading Brick into Oxigraph (brick_vocab reads
    the vendored ontology, same as hisquery does);
  - Brick first, Haystack second: a point with no Brick class still meets
    a requirement when its Haystack markers include every marker the
    mapping rules mint that class from (hisquery.markers_of_brick_class) -
    one model read two ways, not a third vocabulary.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from rdflib import RDF, RDFS, Graph, Namespace, URIRef
from rdflib.collection import Collection

from baler import brick_vocab, hisquery

DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "ontology", "cx-expected-points.ttl")

SH = Namespace("http://www.w3.org/ns/shacl#")
BRICK_NS = "https://brickschema.org/schema/Brick#"
SEVERITY = {str(SH.Violation): "required", str(SH.Warning): "expected", str(SH.Info): "optional"}


@dataclass(frozen=True)
class Requirement:
    id: str
    name: str
    classes: tuple[str, ...]  # Brick local names; any one satisfies it
    severity: str  # required | expected | optional
    message: str


@dataclass(frozen=True)
class Template:
    id: str
    label: str
    target_class: str
    requirements: tuple[Requirement, ...]

    def as_dict(self) -> dict:
        return {
            "id": self.id, "label": self.label, "target_class": self.target_class,
            "requirements": [
                {"id": r.id, "name": r.name, "classes": list(r.classes), "severity": r.severity, "message": r.message,
                 "haystack_markers": sorted(_markers_for(r.classes)), "xeto_spec": _spec_for(r.classes)}
                for r in self.requirements
            ],
        }


def _local(uri) -> str:
    s = str(uri)
    return s.rsplit("#", 1)[-1]


def _classes_of(g: Graph, value_shape) -> tuple[str, ...]:
    direct = [_local(c) for c in g.objects(value_shape, SH["class"])]
    for lst in g.objects(value_shape, SH["or"]):
        for member in Collection(g, lst):
            direct.extend(_local(c) for c in g.objects(member, SH["class"]))
    return tuple(direct)


def parse(path: str) -> list[Template]:
    g = Graph()
    g.parse(path, format="turtle")
    out = []
    for shape in g.subjects(RDF.type, SH.NodeShape):
        target = next(g.objects(shape, SH.targetClass), None)
        if target is None:
            continue
        reqs = []
        for prop in g.objects(shape, SH.property):
            if next(g.objects(prop, SH.path), None) != URIRef(BRICK_NS + "hasPoint"):
                raise ValueError(f"{_local(prop)}: only sh:path brick:hasPoint is supported")
            value_shape = next(g.objects(prop, SH.qualifiedValueShape), None)
            classes = _classes_of(g, value_shape) if value_shape is not None else ()
            if not classes:
                raise ValueError(f"{_local(prop)}: needs sh:qualifiedValueShape with sh:class or sh:or of sh:class")
            reqs.append(Requirement(
                id=_local(prop),
                name=str(next(g.objects(prop, SH.name), _local(prop))),
                classes=classes,
                severity=SEVERITY.get(str(next(g.objects(prop, SH.severity), SH.Violation)), "required"),
                message=str(next(g.objects(prop, SH.message), "")),
            ))
        out.append(Template(
            id=_local(shape),
            label=str(next(g.objects(shape, RDFS.label), _local(shape))),
            target_class=_local(target),
            requirements=tuple(sorted(reqs, key=lambda r: ({"required": 0, "expected": 1}.get(r.severity, 2), r.id))),
        ))
    return sorted(out, key=lambda t: t.id)


@lru_cache(maxsize=1)
def load() -> tuple[Template, ...]:
    return tuple(parse(DEFAULT_PATH))


def _markers_for(classes) -> frozenset[str]:
    """Markers a Haystack-only point needs to count as one of `classes`:
    the smallest non-empty marker set the mapping rules mint any of them
    from (empty when no rule mints any)."""
    by_class = hisquery.markers_of_brick_class()
    sets = [by_class[c] for c in classes if by_class.get(c)]
    return min(sets, key=len) if sets else frozenset()


def _spec_for(classes) -> str | None:
    for c in classes:
        spec = hisquery.xeto_spec_for({"point"}, c)
        if spec and spec != "ph::Point":
            return spec
    return None


def applies(template: Template, equip_classes) -> bool:
    family = brick_vocab.load().descendants(template.target_class) or {template.target_class}
    return any(c in family for c in equip_classes)


def templates_for(equip_classes, templates=None) -> list[Template]:
    return [t for t in (templates if templates is not None else load()) if applies(t, equip_classes)]


def evaluate(template: Template, points: list[dict]) -> list[dict]:
    """One outcome per requirement: the points that meet it and how
    (`brick` by class, `haystack` by markers). `points` are inventory rows
    ({id, brickClass, tags})."""
    vocab = brick_vocab.load()
    out = []
    for req in template.requirements:
        family: set[str] = set()
        for c in req.classes:
            family |= vocab.descendants(c) or {c}
        need = _markers_for(req.classes)
        by_brick = [p["id"] for p in points if p.get("brickClass") in family]
        by_haystack = [] if by_brick or not need else [
            p["id"] for p in points if not p.get("brickClass") and need <= set(p.get("tags") or ())
        ]
        matched = by_brick or by_haystack
        out.append({
            "requirement": req,
            "matched": matched,
            "matched_by": "brick" if by_brick else ("haystack" if by_haystack else None),
        })
    return out

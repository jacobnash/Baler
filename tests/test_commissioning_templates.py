"""SHACL expected-point templates (ontology/cx-expected-points.ttl, #37):
the file is real SHACL, and commissioning.templates reads it the same way
pyshacl does - plus subclass/alias awareness and a Haystack fallback."""

from __future__ import annotations

import cx_fixtures as fx
import pytest
from rdflib import RDF, Graph, Namespace, URIRef

from baler.commissioning import inventory
from baler.commissioning import templates as tpl
from baler.scope import Scope

BRICK = Namespace("https://brickschema.org/schema/Brick#")


def test_six_templates_parse_with_required_points():
    ts = {t.target_class: t for t in tpl.load()}
    assert set(ts) == {"Air_Handling_Unit", "Variable_Air_Volume_Box", "Fan_Coil_Unit", "Chiller", "Boiler", "Pump"}
    for t in ts.values():
        assert any(r.severity == "required" for r in t.requirements), t.id
        assert all(r.classes for r in t.requirements)
    vav_damper = next(r for r in ts["Variable_Air_Volume_Box"].requirements if r.id == "VAV_Damper")
    assert set(vav_damper.classes) == {"Damper_Position_Command", "Damper_Position_Sensor"}


def test_every_template_class_is_real_brick():
    from baler import brick_vocab

    vocab = brick_vocab.load()
    for t in tpl.load():
        assert vocab.canonical_class(t.target_class) == t.target_class
        for r in t.requirements:
            for c in r.classes:
                assert vocab.canonical_class(c) == c, f"{r.id}: {c}"


def test_applies_through_aliases_and_subclasses():
    ahu = next(t for t in tpl.load() if t.target_class == "Air_Handling_Unit")
    assert tpl.applies(ahu, ["AHU"])  # Brick alias
    assert tpl.applies(ahu, ["Rooftop_Unit"])  # subclass
    assert not tpl.applies(ahu, ["Chiller"])


def test_as_dict_carries_the_haystack_reading():
    sat = next(r for r in next(t for t in tpl.load() if t.id == "AHU_ExpectedPoints").as_dict()["requirements"] if r["id"] == "AHU_SAT")
    assert {"discharge", "air", "temp", "sensor"} <= set(sat["haystack_markers"])
    assert sat["xeto_spec"]


def _equips():
    return {e["id"]: e for e in inventory.build(fx.store(), [Scope(("fbf/a",))])}


def _outcomes(equip_id):
    e = _equips()[equip_id]
    (t,) = tpl.templates_for(e["brickClasses"])
    return {o["requirement"].id: o for o in tpl.evaluate(t, e["points"])}


def test_evaluate_brick_first_with_equivalents():
    o = _outcomes(fx.AHU)
    # Discharge_Air_Temperature_Sensor satisfies Supply_Air_... (owl:equivalentClass)
    assert o["AHU_SAT"]["matched"] == [fx.pt(fx.AHU, "SAT")] and o["AHU_SAT"]["matched_by"] == "brick"
    assert o["AHU_FanStatus"]["matched_by"] == "brick"
    assert o["AHU_FanCommand"]["matched"] == []


def test_evaluate_falls_back_to_haystack_markers():
    o = _outcomes(fx.VAV)
    assert o["VAV_Damper"]["matched"] == [fx.pt(fx.VAV, "DMP")] and o["VAV_Damper"]["matched_by"] == "haystack"
    # Air_Flow_Sensor has no mapping rule, so no marker fallback: just missing
    assert o["VAV_Flow"]["matched"] == []


def test_no_template_for_unknown_class():
    assert tpl.templates_for(["Electrical_Meter"]) == []


def test_pyshacl_agrees_on_exact_classes():
    """Same verdicts from pyshacl on the raw file, for a graph typed with
    the exact classes the shapes name (pyshacl has no Brick hierarchy)."""
    pyshacl = pytest.importorskip("pyshacl")
    data = Graph()
    good, bad = URIRef("urn:equip:t/vav-good"), URIRef("urn:equip:t/vav-bad")
    for e in (good, bad):
        data.add((e, RDF.type, BRICK.Variable_Air_Volume_Box))
    for name, cls in (("znt", BRICK.Zone_Air_Temperature_Sensor), ("dmp", BRICK.Damper_Position_Sensor)):
        p = URIRef(f"urn:point:t/vav-good/{name}")
        data.add((p, RDF.type, cls))
        data.add((good, BRICK.hasPoint, p))
    shapes = Graph().parse(tpl.DEFAULT_PATH, format="turtle")
    _conforms, results, _ = pyshacl.validate(data, shacl_graph=shapes)
    sh = Namespace("http://www.w3.org/ns/shacl#")
    failing = {(str(results.value(r, sh.focusNode)), str(results.value(r, sh.sourceShape)).rsplit("#", 1)[-1])
               for r in results.subjects(RDF.type, sh.ValidationResult)}

    (vav,) = [t for t in tpl.load() if t.target_class == "Variable_Air_Volume_Box"]
    ours = set()
    for e, pts in ((good, [{"id": "znt", "brickClass": "Zone_Air_Temperature_Sensor", "tags": []},
                           {"id": "dmp", "brickClass": "Damper_Position_Sensor", "tags": []}]), (bad, [])):
        ours |= {(str(e), o["requirement"].id) for o in tpl.evaluate(vav, pts) if not o["matched"]}
    assert failing == ours
    assert (str(bad), "VAV_ZAT") in ours and (str(good), "VAV_Flow") in ours


def test_bad_shape_is_rejected(tmp_path):
    f = tmp_path / "bad.ttl"
    f.write_text("""@prefix sh: <http://www.w3.org/ns/shacl#> . @prefix brick: <https://brickschema.org/schema/Brick#> .
<urn:x> a sh:NodeShape ; sh:targetClass brick:Pump ; sh:property [ sh:path brick:isPointOf ; sh:qualifiedMinCount 1 ] .""")
    with pytest.raises(ValueError):
        tpl.parse(str(f))

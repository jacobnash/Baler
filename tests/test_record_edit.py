"""Hand edits to equip/point records (Baler#19)."""

from __future__ import annotations

import pytest
from rdflib import RDF, Literal, URIRef

from baler import record_edit
from baler.ingest import ingest_tags
from baler.store import BRICK, HAYSTACK, HUNIT, Store

P = "fbf/ahu-1/sat"


def _store():
    s = Store()
    uri = str(ingest_tags(s, P, {"temp": True, "sensor": True, "dis": "SAT", "max": 90}))
    s.graph.add((URIRef(uri), RDF.type, BRICK.Temperature_Sensor))
    return s, uri


def _objs(store, uri, pred):
    return sorted(str(o) for o in store.graph.objects(URIRef(uri), pred))


def test_set_remove_rename_and_reclassify():
    store, uri = _store()
    changed = record_edit.apply_edit(store, uri, {
        "dis": "Supply Air Temp", "brickClass": "supply_air_temperature_sensor",
        "set": {"air": True, "max": {"val": 95, "unit": "°F"}, "note": "replaced 2026"}, "remove": ["sensor"],
    })
    assert changed["brickClass"] == "Supply_Air_Temperature_Sensor" and changed["remove"] == ["sensor"]
    assert _objs(store, uri, HAYSTACK.dis) == ["Supply Air Temp"]  # replaced, not added
    assert _objs(store, uri, HAYSTACK.max) == ["95.0"] and _objs(store, uri, HUNIT.max) == ["°F"]
    assert "sensor" not in _objs(store, uri, HAYSTACK.hasTag) and "air" in _objs(store, uri, HAYSTACK.hasTag)
    assert _objs(store, uri, RDF.type) == [str(BRICK.Supply_Air_Temperature_Sensor)]  # old Brick class gone
    assert _objs(store, uri, HAYSTACK.note) == ["replaced 2026"]


@pytest.mark.parametrize("body, msg", [
    ({"set": {"equipRef": "x"}}, "isn't hand-editable"),
    ({"set": {"Bad Name": True}}, "bad tag name"),
    ({"brickClass": "Not_A_Class"}, "no Brick class"),
    ({"set": {"a": [1, 2]}}, "a value is"),
    ({"set": {"a": True}, "remove": ["a"]}, "both set and removed"),
    ({}, "nothing to change"),
    ({"bogus": 1}, "unknown fields"),
])
def test_bad_edits_change_nothing(body, msg):
    store, uri = _store()
    before = set(store.graph)
    with pytest.raises((ValueError, TypeError), match=msg):
        record_edit.apply_edit(store, uri, body)
    assert set(store.graph) == before


def test_delete_removes_record_and_links_to_it():
    store, uri = _store()
    equip = URIRef("urn:equip:fbf/ahu-1")
    store.graph.add((equip, BRICK.hasPoint, URIRef(uri)))
    store.graph.add((equip, HAYSTACK.dis, Literal("AHU-1")))
    assert record_edit.exists(store, uri)
    record_edit.delete_record(store, uri)
    assert not record_edit.exists(store, uri)
    assert list(store.graph.triples((equip, BRICK.hasPoint, None))) == []  # the link went too
    assert _objs(store, str(equip), HAYSTACK.dis) == ["AHU-1"]  # the equip itself stays

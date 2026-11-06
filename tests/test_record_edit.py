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


def test_create_equip_and_point():
    store = Store()
    site = "urn:location:site-a"
    store.graph.add((URIRef(site), RDF.type, BRICK.Site))
    eq = record_edit.new_uri("equip", "fbf/a/ahu-9")
    record_edit.create_record(store, "equip", eq, {"dis": "AHU-9", "brickClass": "ahu", "tags": {"ahu": True}}, site)
    pt = record_edit.new_uri("point", "fbf/a/ahu-9/sat")
    record_edit.create_record(store, "point", pt, {"dis": "SAT", "tags": {"temp": True, "unit": "°F"}}, eq)
    assert (URIRef(site), BRICK.hasPart, URIRef(eq)) in store.graph
    assert (URIRef(pt), BRICK.isPointOf, URIRef(eq)) in store.graph and (URIRef(eq), BRICK.hasPoint, URIRef(pt)) in store.graph
    assert set(_objs(store, eq, HAYSTACK.hasTag)) == {"equip", "ahu"} and _objs(store, eq, RDF.type) == [str(BRICK.AHU)]
    assert _objs(store, pt, HAYSTACK.unit) == ["°F"]


@pytest.mark.parametrize("kind, key, body, link, msg", [
    ("thing", "a/b", {"dis": "x"}, None, "kind must be"),
    ("equip", "/abs", {"dis": "x"}, None, "key must be"),
    ("equip", "a b", {"dis": "x"}, None, "key must be"),
    ("point", "a/p", {"dis": "x"}, None, "needs its equip"),
    ("equip", "a/e", {}, None, "dis"),
    ("equip", "a/e", {"dis": "x", "tags": {"equipRef": "y"}}, None, "isn't hand-editable"),
])
def test_create_rejects_bad_input(kind, key, body, link, msg):
    store = Store()
    with pytest.raises((ValueError, TypeError), match=msg):
        record_edit.create_record(store, kind, record_edit.new_uri(kind, key), body, link)
    assert len(store.graph) == 0

"""Orphaned points (Ops -> Orphaned points): finding them in the graph,
and removing only what is really orphaned."""

import json
from datetime import datetime, timezone

import pytest
from rdflib import Literal, URIRef

from baler import orphans
from baler.ingest import link_point_to_equip
from baler.scope import Scope
from baler.store import BRICK, HAYSTACK, TD, Store


def _graph(tmp_path):
    store = Store()
    equip = URIRef("urn:equip:test-orph/ahu-1")
    store.add_entity(equip, BRICK.AHU)
    for uri, cls in [
        ("urn:point:computed/live-der/ahu-1", TD.ComputedPoint),
        ("urn:point:computed/gone-der/ahu-1", TD.ComputedPoint),
        ("urn:point:test-orph/ahu-1/sat", BRICK.Supply_Air_Temperature_Sensor),
    ]:
        store.add_entity(URIRef(uri), cls)
        link_point_to_equip(store, URIRef(uri), equip)
    loose = URIRef("urn:point:test-orph/loose")
    store.add_many([(loose, HAYSTACK.hasTag, Literal("point")), (loose, HAYSTACK.dis, Literal("Loose point"))])
    other_site = URIRef("urn:point:other-site/loose")
    store.add_entity(other_site, BRICK.Point)
    derivations = tmp_path / "derivations.json"
    derivations.write_text(json.dumps([{"id": "live-der"}]))
    return store, str(derivations)


class _NoHistory:
    def execute(self, *a, **k):
        raise AssertionError("no history read expected")


def test_finds_deleted_derivations_outputs_and_equipless_points(tmp_path, monkeypatch):
    store, derivations = _graph(tmp_path)
    monkeypatch.setattr(orphans.timeseries, "read_latest_many", lambda conn, uris: {})
    found = {o["id"]: o for o in orphans.find(store, None, Scope.everything(), derivations)}
    assert found["urn:point:computed/gone-der/ahu-1"]["kind"] == "derivation_gone"
    assert "gone-der" in found["urn:point:computed/gone-der/ahu-1"]["reason"]
    assert found["urn:point:test-orph/loose"]["kind"] == "no_equip"
    assert found["urn:point:test-orph/loose"]["dis"] == "Loose point"
    assert "urn:point:computed/live-der/ahu-1" not in found  # its derivation exists
    assert "urn:point:test-orph/ahu-1/sat" not in found  # attached


def test_scope_hides_other_sites_orphans(tmp_path, monkeypatch):
    store, derivations = _graph(tmp_path)
    monkeypatch.setattr(orphans.timeseries, "read_latest_many", lambda conn, uris: {})
    scoped = {o["id"] for o in orphans.find(store, None, Scope(("test-orph",)), derivations)}
    assert "urn:point:other-site/loose" not in scoped
    assert "urn:point:test-orph/loose" in scoped


@pytest.mark.integration
def test_remove_only_touches_current_orphans_and_logs_it(tmp_path):
    from baler import record_edit
    from baler.timeseries import connect, write_point_value

    store, derivations = _graph(tmp_path)
    conn = connect()
    record_edit.ensure_schema(conn)
    gone, attached = "urn:point:computed/gone-der/ahu-1", "urn:point:test-orph/ahu-1/sat"
    conn.execute("DELETE FROM point_history WHERE point_uri LIKE 'urn:point:%%test-orph%%' OR point_uri = %s", (gone,))
    conn.execute("DELETE FROM record_edits WHERE entity_uri = %s", (gone,))
    write_point_value(conn, gone, 42.0, datetime.now(timezone.utc))

    result = orphans.remove(store, conn, [gone, attached], purge_history=True, who=("u1", "admin@test"),
                            scope=Scope.everything(), derivations_path=derivations)
    assert result == {"removed": [gone], "skipped": [attached]}
    assert not list(store.query(f"SELECT ?p WHERE {{ <{gone}> ?p ?o }}"))
    assert list(store.query(f"SELECT ?p WHERE {{ <{attached}> ?p ?o }}"))  # untouched
    assert conn.execute("SELECT count(*) FROM point_history WHERE point_uri = %s", (gone,)).fetchone()[0] == 0
    edits = record_edit.list_edits(conn, gone)
    assert edits[0]["who"] == "admin@test" and edits[0]["detail"]["orphan"] == "derivation_gone"
    with pytest.raises(TypeError):
        orphans.remove(store, conn, "not-a-list", False, ("u1", None), Scope.everything(), derivations)

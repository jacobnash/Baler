"""
baler.tag_ingest (Baler#233): tags/equip messages applied to the graph in
batches, off the readings path - same facts as handling them one at a time.
"""

import json

from rdflib import RDF, URIRef

from baler import tag_ingest
from baler.store import BRICK, HAYSTACK, Store


def _msg(topic, value):
    return (topic, json.dumps(value), 1_800_000_000.0)


def _isPointOf(store, point):
    return {str(o) for o in store.graph.objects(URIRef(point), BRICK.isPointOf)}


def test_kinds():
    assert tag_ingest.kind("fbf/s/ahu/equip/tags") == "equip"
    assert tag_ingest.kind("fbf/hay/equip/AHU-1/tags") == "haystack_equip"
    assert tag_ingest.kind("fbf/s/ahu/sat/tags") == "point"
    assert tag_ingest.kind("fbf/s/ahu/sat") is None


def test_a_point_links_to_its_equip_whichever_lands_first():
    """Several workers commit in their own order, and tag_ingest applies in
    commit order: a point's tags can land before its equip rec's. The
    inline path only linked from the point side, so that point stayed
    unlinked until it was re-tagged."""
    for order in ("equip first", "point first"):
        store = Store()
        equip = _msg("fbf/hay/equip/AHU-1/tags", {"equip": True, "ahu": True, "dis": "AHU 1"})
        point = _msg("fbf/hay/ahu1/sat/tags", {"point": True, "equipRef": "AHU-1"})
        batches = [[equip], [point]] if order == "equip first" else [[point], [equip]]
        for b in batches:
            tag_ingest.apply(store, b)
        assert _isPointOf(store, "urn:point:fbf/hay/ahu1/sat") == {"urn:equip:haystack:AHU-1"}, order
        assert (URIRef("urn:equip:haystack:AHU-1"), BRICK.hasPoint, URIRef("urn:point:fbf/hay/ahu1/sat")) in store.graph


def test_a_point_and_its_equip_in_one_batch_link():
    store = Store()
    tag_ingest.apply(store, [_msg("fbf/hay/ahu1/sat/tags", {"point": True, "equipRef": "AHU-1"}),
                             _msg("fbf/hay/equip/AHU-1/tags", {"equip": True})])
    assert _isPointOf(store, "urn:point:fbf/hay/ahu1/sat") == {"urn:equip:haystack:AHU-1"}


def test_no_equip_rec_means_no_link():
    store = Store()
    tag_ingest.apply(store, [_msg("fbf/hay/ahu1/sat/tags", {"point": True, "equipRef": "AHU-9"})])
    assert _isPointOf(store, "urn:point:fbf/hay/ahu1/sat") == set()


def test_a_connection_equip_links_its_listed_points_and_is_classified():
    store = Store()
    counts = tag_ingest.apply(store, [_msg("fbf/s/ahu/equip/tags", {"tags": {"ahu": True, "equip": True},
                                                                    "points": ["sat", "rat"]})])
    assert _isPointOf(store, "urn:point:fbf/s/ahu/sat") == {"urn:equip:fbf/s/ahu"}
    assert _isPointOf(store, "urn:point:fbf/s/ahu/rat") == {"urn:equip:fbf/s/ahu"}
    assert (URIRef("urn:equip:fbf/s/ahu"), RDF.type, BRICK.Air_Handling_Unit) in store.graph
    assert counts["equips"] == 1 and counts["classified"] == 1


def test_tags_from_several_messages_for_one_point_add_up():
    """Tags are additive (never deleted): two messages for one point in a
    batch leave both messages' facts, as applying them in turn did."""
    store = Store()
    tag_ingest.apply(store, [_msg("fbf/s/a/p/tags", {"zone": True}), _msg("fbf/s/a/p/tags", {"temp": True, "unit": "°F"})])
    tags = {str(o) for o in store.graph.objects(URIRef("urn:point:fbf/s/a/p"), HAYSTACK.hasTag)}
    assert tags == {"zone", "temp"}


def test_malformed_tag_messages_are_counted_and_skipped():
    store = Store()
    logged = []
    counts = tag_ingest.apply(store, [("fbf/s/a/p/tags", "not json", None), ("fbf/s/a/q/tags", "[1]", None),
                                      ("fbf/s/a/r/tags", None, None), _msg("fbf/s/a/ok/tags", {"point": True})],
                              log=logged.append)
    assert counts["bad"] == 3 and len(logged) == 3 and counts["points"] == 1


# --- the controller behind a connection's equip (Windrow#24) ---

INVENTORY = {"bacnetVendorName": "Acme Controls", "bacnetModelName": "AC-500", "bacnetSerialNumber": "SN-0001",
             "bacnetFirmwareRevision": "4.2.1"}


def _values(store, subject, tag):
    return {str(o) for o in store.graph.objects(URIRef(subject), HAYSTACK[tag])}


def test_a_new_device_inventory_replaces_the_old_one():
    """A swapped controller: the equip says what's there now, not both."""
    store = Store()
    equip = "fbf/s/ahu/equip/tags"
    tag_ingest.apply(store, [_msg(equip, {"tags": {"ahu": True, **INVENTORY}, "points": ["sat"]})])
    (subject,) = {s for s in store.graph.subjects(HAYSTACK.bacnetSerialNumber, None)}
    uri = str(subject)
    assert _values(store, uri, "bacnetSerialNumber") == {"SN-0001"}

    tag_ingest.apply(store, [_msg(equip, {"tags": {"ahu": True, **INVENTORY, "bacnetSerialNumber": "SN-0999",
                                                   "bacnetModelName": "AC-600"}, "points": ["sat"]})])
    assert _values(store, uri, "bacnetSerialNumber") == {"SN-0999"}
    assert _values(store, uri, "bacnetModelName") == {"AC-600"}
    assert _values(store, uri, "bacnetVendorName") == {"Acme Controls"}
    # other tags stay additive, as before
    assert (URIRef(uri), HAYSTACK.hasTag, None) in store.graph


def test_the_last_inventory_in_a_batch_wins():
    store = Store()
    equip = "fbf/s/ahu/equip/tags"
    tag_ingest.apply(store, [_msg(equip, {"tags": {"bacnetSerialNumber": "SN-1"}, "points": []}),
                             _msg(equip, {"tags": {"bacnetSerialNumber": "SN-2"}, "points": []})])
    (subject,) = set(store.graph.subjects(HAYSTACK.bacnetSerialNumber, None))
    assert _values(store, str(subject), "bacnetSerialNumber") == {"SN-2"}


def test_an_equip_message_without_inventory_leaves_the_inventory_alone():
    """Older Windrow (or a read that failed) sends equip/tags without it."""
    store = Store()
    equip = "fbf/s/ahu/equip/tags"
    tag_ingest.apply(store, [_msg(equip, {"tags": dict(INVENTORY), "points": ["sat"]})])
    tag_ingest.apply(store, [_msg(equip, {"tags": {}, "points": ["sat", "rat"]})])
    (subject,) = set(store.graph.subjects(HAYSTACK.bacnetSerialNumber, None))
    assert _values(store, str(subject), "bacnetSerialNumber") == {"SN-0001"}

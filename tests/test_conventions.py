"""
Site naming conventions (#186). Pure parsing on strings, the ladder on an
in-memory graph, storage and the ops_api routes against live services.
"""

import uuid

import pytest
from rdflib import RDF, SKOS, Literal, URIRef

from baler import conventions as cv
from baler import mapping_eval
from baler.ingest import ingest_equip_tags, ingest_tags, link_point_to_equip
from baler.store import BRICK, TD, Store

CRYPTIC = "fbf/{equip_type:ahu}-{equip_id:1}/{point:DIS-AIR-TEM}-{ignore:12}"
WORDS = "fbf/{equip_type:ahu}-{equip_id:1}/{point:DischargeAirTemp}"


def test_an_annotation_compiles_to_a_template_that_generalises():
    t = cv.compile_annotation(CRYPTIC)
    assert cv.example_of(CRYPTIC) == "fbf/ahu-1/DIS-AIR-TEM-12"
    m = t.regex.match("fbf/chw-pump-3/PUM-SPD-FDB-372")
    assert dict(zip(t.roles, m.groups())) == {"equip_type": "chw-pump", "equip_id": "3", "point": "PUM-SPD-FDB", "ignore": "372"}
    assert t.regex.match("fbf/ahu-1/DischargeAirTemp") is None


@pytest.mark.parametrize("bad", ["fbf/{equip_type:ahu}-{equip_id:1}", "fbf/{colour:red}/{point:x}", "{point:a}/{point:b}"])
def test_bad_annotations_are_refused(bad):
    with pytest.raises(cv.ConventionError):
        cv.compile_annotation(bad)


def test_codes_are_normalised_so_styles_meet():
    conv = cv.Convention([CRYPTIC, WORDS], {"DischargeAirTemp": "Discharge_Air_Temperature_Sensor", "dis-air-tem": "supply_air_temperature_sensor"},
                         {"AHU": "Air_Handling_Unit"})
    for label in ("fbf/ahu-2/discharge_air_temp", "fbf/ahu-2/Discharge_Air_Temp", "fbf/ahu-2/DischargeAirTemp"):
        got = conv.parse(label)
        assert got["point_code"] == "dischargeairtemp" and got["point_class"] == "Discharge_Air_Temperature_Sensor", label
    got = conv.parse("fbf/ahu-4/DIS-AIR-TEM-88")
    assert got["template"] == 0 and got["point_class"] == "Supply_Air_Temperature_Sensor"  # Brick's spelling
    assert (got["equip_type"], got["equip_id"], got["equip_class"]) == ("ahu", "4", "Air_Handling_Unit")


def test_a_code_that_isnt_a_brick_class_is_refused():
    with pytest.raises(cv.ConventionError, match="no Brick class"):
        cv.Convention([WORDS], {"sat": "Supply_Air_Temp_Sensor"})


def test_scope_prefix_limits_where_it_applies():
    conv = cv.Convention([WORDS], scope_prefix="fbf/ahu-1")
    assert conv.parse("fbf/ahu-1/SAT") is not None and conv.parse("fbf/ahu-2/SAT") is None


def test_preview_ranks_unknown_codes_by_how_many_points_they_settle():
    labels = {f"urn:point:{t}": t for t in ["fbf/vav-1/ZN-T-1", "fbf/vav-2/ZN-T-2", "fbf/vav-3/ZN-T-3", "fbf/vav-1/DA-T-4",
                                              "fbf/ahu-1/SAT-5", "weird label"]}
    conv = cv.Convention(["fbf/{equip_type:vav}-{equip_id:1}/{point:ZN-T}-{ignore:1}"], {"SAT": "Supply_Air_Temperature_Sensor"})
    p = cv.preview(conv, labels)
    assert (p["points"], p["matched"], p["classified"], p["equipment"]) == (6, 5, 1, 4)
    assert [(c["code"], c["points"]) for c in p["unknown_point_codes"]] == [("znt", 3), ("dat", 1)]
    assert p["unknown_equip_codes"][0] == {"code": "vav", "points": 4}
    assert p["unmatched_sample"] == ["weird label"]
    assert cv.preview(conv, labels, classified={"urn:point:fbf/vav-1/ZN-T-1"}, only_unclassified=True)["points"] == 5


def _site():
    store = Store()
    for eq, pts in {"fbf/ahu-1": ["SAT-1", "RAT-2", "MYSTERY-3"], "fbf/ahu-2": ["SAT-4"]}.items():
        e = ingest_equip_tags(store, eq, {})
        for p in pts:
            topic = f"{eq}/{p}"
            u = ingest_tags(store, topic, {"unit": "°F"})
            store.add_many([(u, RDF.type, TD.RawPoint), (u, RDF.type, TD.UnmappedPoint), (u, TD.sourceTopic, Literal(topic))])
            link_point_to_equip(store, u, e)
    return store


CONV = cv.Convention(["fbf/{equip_type:ahu}-{equip_id:1}/{point:SAT}-{ignore:1}"],
                     {"sat": "Supply_Air_Temperature_Sensor", "rat": "Return_Air_Temperature_Sensor"}, {"ahu": "Air_Handling_Unit"}, name="test")


def test_the_ladder_classifies_known_codes_and_their_equipment():
    store = _site()
    done = cv.run_ladder([CONV], store, [str(u) for u in cv.point_labels(store)] + ["urn:equip:fbf/ahu-1", "urn:equip:fbf/ahu-2"])
    sat = URIRef("urn:point:fbf/ahu-1/SAT-1")
    assert (sat, RDF.type, BRICK.Supply_Air_Temperature_Sensor) in store.graph
    assert (sat, RDF.type, TD.UnmappedPoint) not in store.graph
    assert any(str(o).startswith("convention-classified") for o in store.graph.objects(sat, SKOS.definition))
    assert URIRef("urn:point:fbf/ahu-1/MYSTERY-3") not in {URIRef(u) for u in done}  # unknown code: left for the next rung
    assert (URIRef("urn:equip:fbf/ahu-1"), RDF.type, BRICK.Air_Handling_Unit) in store.graph
    assert len(done) == 5  # 3 known points, 2 equipment


def test_a_convention_never_overrides_an_existing_class():
    store = _site()
    sat = URIRef("urn:point:fbf/ahu-1/SAT-1")
    store.add_entity(sat, BRICK.Mixed_Air_Temperature_Sensor)
    cv.run_ladder([CONV], store, [str(sat)])
    assert (sat, RDF.type, BRICK.Supply_Air_Temperature_Sensor) not in store.graph


@pytest.mark.skipif(not mapping_eval.windrow_available(), reason="windrow submodule not checked out")
def test_on_mock_hospital_two_annotations_and_twenty_codes_settle_most_of_review():
    conv, n = mapping_eval.annotated_convention(mapping_eval.load_mock_hospital(), 20)
    assert n == 20 and len(conv.equip_codes) == 8
    s = mapping_eval.summarize(mapping_eval.evaluate(mapping_eval.load_mock_hospital(), convention=conv))
    assert s["points"]["steps"]["convention"]["points"] > 200 and s["points"]["auto_accepted_wrong"] == 0
    assert s["equipment"]["accuracy"] == 1.0


@pytest.mark.integration
def test_conventions_through_the_ops_api(monkeypatch):
    from http.server import ThreadingHTTPServer
    from threading import Thread

    import requests

    from baler import auth, record_edit, timeseries
    from baler.ops_api import make_handler
    from baler.remote_store import RemoteStore

    monkeypatch.setenv("BALER_GATEWAY_SECRET", "test-gateway-secret")
    pool = timeseries.connect_pool(min_size=1, max_size=2)
    store = RemoteStore()
    tag = uuid.uuid4().hex[:8]
    topics = [f"test-conv-{tag}/ahu-{i}/SAT-{i}" for i in (1, 2)] + [f"test-conv-{tag}/ahu-1/ODD-9"]
    for t in topics:
        u = ingest_tags(store, t, {"unit": "°F"})
        store.add_many([(u, RDF.type, TD.RawPoint), (u, TD.sourceTopic, Literal(t))])
    with pool.connection() as conn:
        auth.ensure_schema(conn)  # sites, for the caller's scope
        record_edit.ensure_schema(conn)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(pool, store))
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True).start()
    base, h = f"http://127.0.0.1:{server.server_address[1]}", {"X-Gateway-Secret": "test-gateway-secret"}
    body = {"name": f"t-{tag}", "scope_prefix": f"test-conv-{tag}",
            "templates": [f"test-conv-{tag}/{{equip_type:ahu}}-{{equip_id:1}}/{{point:SAT}}-{{ignore:1}}"],
            "point_codes": {"SAT": "Supply_Air_Temperature_Sensor"}}
    conv_id = None
    try:
        p = requests.post(f"{base}/conventions/preview", headers=h, json=body).json()
        assert (p["points"], p["matched"], p["classified"]) == (3, 3, 2) and p["unknown_point_codes"][0]["code"] == "odd"
        assert requests.post(f"{base}/conventions/preview", headers=h, json={**body, "point_codes": {"SAT": "Nope_Class"}}).status_code == 400
        saved = requests.post(f"{base}/conventions", headers=h, json=body).json()
        conv_id = saved["id"]
        assert saved["point_codes"] == {"sat": "Supply_Air_Temperature_Sensor"}
        assert any(c["id"] == conv_id for c in requests.get(f"{base}/conventions", headers=h).json())
        out = requests.post(f"{base}/conventions/{conv_id}/apply", headers=h, json={}).json()
        assert out["points"] == 2
        u = f"urn:point:{topics[0]}"
        assert list(store.query(f"SELECT ?t WHERE {{ <{u}> a <{BRICK.Supply_Air_Temperature_Sensor}> }}"))
        with pool.connection() as conn:
            assert record_edit.list_edits(conn, u)[0]["action"] == "convention"
        assert requests.post(f"{base}/conventions/nope/apply", headers=h, json={}).status_code == 404
        assert requests.delete(f"{base}/conventions/{conv_id}", headers=h).status_code == 204
        conv_id = None
    finally:
        server.shutdown()
        with pool.connection() as conn:
            if conv_id:
                conn.execute("DELETE FROM naming_conventions WHERE id = %s", (conv_id,))
            conn.execute("DELETE FROM record_edits WHERE entity_uri = ANY(%s)", ([f"urn:point:{t}" for t in topics],))
        for t in topics:
            store.update(f"DELETE WHERE {{ <urn:point:{t}> ?p ?o }}")
        pool.close()

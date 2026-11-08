"""
Equipment inference (#187): equip type from its point set, Cx's expected-
point check, isPointOf groupings and feeds - all proposals, never written
silently. No services, except the Postgres/ops_api tests.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from rdflib import RDF, Literal, URIRef

from baler import brick_vocab, equip_inference, mapping_eval, model_step
from baler.ingest import ingest_equip_tags, ingest_tags, link_point_to_equip
from baler.store import BRICK, TD, Store

VOCAB = brick_vocab.load()
CHILLER_POINTS = ["Chilled_Water_Supply_Temperature_Sensor", "Chilled_Water_Return_Temperature_Sensor", "Run_Status",
                  "Start_Stop_Command", "Electric_Power_Sensor"]


def test_a_chillers_points_say_chiller():
    [best, *_] = equip_inference.infer(CHILLER_POINTS, VOCAB)
    assert best["brick_class"] == "Chiller" and best["missing_required"] == []
    assert "chilled water supply" in best["reason"].lower()


def test_a_vavs_points_say_vav_and_a_meters_say_meter():
    vav = ["Zone_Air_Temperature_Sensor", "Damper_Position_Command", "Damper_Position_Sensor", "Supply_Air_Flow_Sensor"]
    assert equip_inference.infer(vav, VOCAB)[0]["brick_class"] == "Variable_Air_Volume_Box"
    assert equip_inference.infer(["Electric_Power_Sensor", "Electric_Energy_Sensor"], VOCAB)[0]["brick_class"] == "Electrical_Meter"


def test_unclassified_points_give_no_inference():
    assert equip_inference.infer([None, None], VOCAB) == []


def test_cx_check_flags_an_ahu_with_no_fan_status_only_when_all_points_are_classified():
    ahu = ["Supply_Air_Temperature_Sensor", "Return_Air_Temperature_Sensor", "Mixed_Air_Temperature_Sensor"]
    assert "Fan Status" in equip_inference.cx_check("Air_Handling_Unit", ahu, VOCAB) or "fan" in equip_inference.cx_check("Air_Handling_Unit", ahu, VOCAB).lower()
    assert equip_inference.cx_check("Air_Handling_Unit", ahu + [None], VOCAB) is None  # the missing one may be unclassified
    assert equip_inference.cx_check("Air_Handling_Unit", ahu + ["Fan_Status"], VOCAB) is None


def _equip_with(store, prefix, classes):
    e = ingest_equip_tags(store, prefix, {})
    for i, c in enumerate(classes):
        topic = f"{prefix}/p{i}"
        u = ingest_tags(store, topic, {"unit": "°F"})
        store.add_many([(u, RDF.type, TD.RawPoint), (u, TD.sourceTopic, Literal(topic))])
        if c:
            store.add_entity(u, BRICK[c])
        link_point_to_equip(store, u, e)
    return e


class Says:
    name = "fake"

    def __init__(self, cls):
        self.cls = cls

    def ask(self, prompt):
        return {"candidates": [{"brick_class": self.cls, "reason": "r"}], "confidence": 0.9}


def test_equipment_candidates_include_what_the_point_set_says():
    store = Store()
    e = _equip_with(store, "site/unit-7", CHILLER_POINTS)
    [d] = model_step.run(store, [(e, "equip")], None, vocab=VOCAB)
    points = [c for c in d.candidates if c["source"] == "points"]
    assert d.accepted is None and points and points[0]["brick_class"] == "Chiller"


def test_the_cx_check_vetoes_an_agreed_equipment_class_its_points_contradict():
    store = Store()
    e = _equip_with(store, "site/ahu-9", ["Supply_Air_Temperature_Sensor", "Return_Air_Temperature_Sensor"])
    [d] = model_step.run(store, [(e, "equip")], Says("Air_Handling_Unit"), vocab=VOCAB)
    assert d.accepted is None and d.refused_because.startswith("vetoed: Cx expects")
    assert (e, RDF.type, BRICK.Air_Handling_Unit) not in store.graph


def test_unlinked_points_are_grouped_by_their_topics_parent():
    store = Store()
    for t in ("site/dev-12/AI-1", "site/dev-12/AI-2", "site/dev-13/AI-1"):
        u = ingest_tags(store, t, {})
        store.add_many([(u, TD.sourceTopic, Literal(t))])
    linked = _equip_with(store, "site/ahu-1", ["Fan_Status"])
    groups = equip_inference.unlinked_point_groups(store)
    assert [(g["equip_uri"], len(g["points"])) for g in groups] == [("urn:equip:site/dev-12", 2)]
    assert linked  # points already on equipment are never regrouped


def test_accepting_a_grouping_creates_the_equipment_and_links_its_points():
    store = Store()
    pts = []
    for t in ("site/dev-12/AI-1", "site/dev-12/AI-2"):
        u = ingest_tags(store, t, {})
        store.add_many([(u, TD.sourceTopic, Literal(t))])
        pts.append(str(u))
    equip_inference.write_relationship(store, "urn:equip:site/dev-12", "hasPoint", pts)
    assert all((URIRef("urn:equip:site/dev-12"), BRICK.hasPoint, URIRef(p)) in store.graph for p in pts)
    equip_inference.write_relationship(store, "urn:equip:a", "feeds", ["urn:equip:b"])
    assert (URIRef("urn:equip:b"), BRICK.isFedBy, URIRef("urn:equip:a")) in store.graph


T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _series(f, n=96):
    return [(T0 + timedelta(minutes=5 * i), f(i)) for i in range(n)]


def test_feeds_picks_the_ahu_a_vav_tracks():
    import math

    ahu1 = _series(lambda i: 55 + 3 * math.sin(i / 6))
    ahu2 = _series(lambda i: 58 + 3 * math.cos(i / 4))
    vav = _series(lambda i: 56 + 3 * math.sin(i / 6) + 0.2 * math.sin(i * 7))  # ahu1 plus a little noise
    [c] = equip_inference.feeds_candidates({"ahu-1": ahu1, "ahu-2": ahu2}, {"vav-1": vav})
    assert c["ahu"] == "ahu-1" and c["vav"] == "vav-1" and c["r"] > 0.9


def test_feeds_proposes_nothing_when_two_ahus_track_equally():
    import math

    same = _series(lambda i: 55 + 3 * math.sin(i / 6))
    assert equip_inference.feeds_candidates({"ahu-1": same, "ahu-2": same}, {"vav-1": same}) == []


def test_feeds_needs_enough_overlapping_samples():
    import math

    short = _series(lambda i: 55 + math.sin(i), n=10)
    assert equip_inference.feeds_candidates({"ahu-1": short}, {"vav-1": short}) == []


@pytest.mark.skipif(not mapping_eval.windrow_available(), reason="windrow submodule not checked out")
def test_on_mock_hospital_true_point_classes_give_no_wrong_equipment_type():
    c = mapping_eval.load_mock_hospital()
    by_eq: dict[str, list] = {}
    for p in c.points:
        by_eq.setdefault(p.topic_prefix, []).append(p.truth[0] if p.truth else None)
    verdicts = [mapping_eval.judge(VOCAB, equip_inference.infer(by_eq[e.topic_prefix], VOCAB)[0]["brick_class"], e.truth) for e in c.equipment]
    assert verdicts.count("wrong") == 0 and verdicts.count("correct") >= 70


@pytest.mark.integration
def test_relationship_proposals_through_the_ops_api(monkeypatch):
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
    topics = [f"test-rel-{tag}/dev-1/AI-{i}" for i in (1, 2)]
    for t in topics:
        u = ingest_tags(store, t, {})
        store.add_many([(u, TD.sourceTopic, Literal(t))])
    with pool.connection() as conn:
        auth.ensure_schema(conn)
        record_edit.ensure_schema(conn)
        rels = equip_inference.PgRelationships(conn)
        rels.ensure_schema()
        equip_inference.propose_relationships(store, rels)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(pool, store))
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True).start()
    base, h = f"http://127.0.0.1:{server.server_address[1]}", {"X-Gateway-Secret": "test-gateway-secret"}
    equip = f"urn:equip:test-rel-{tag}/dev-1"
    try:
        [row] = [r for r in requests.get(f"{base}/mapping/relationships", headers=h).json() if r["subject"] == equip]
        assert row["predicate"] == "hasPoint" and len(row["objects"]) == 2
        assert requests.post(f"{base}/mapping/relationships/decide", headers=h, json={"ids": ["nope"], "action": "accept"}).status_code == 404
        ok = requests.post(f"{base}/mapping/relationships/decide", headers=h, json={"ids": [row["id"]], "action": "accept"})
        assert ok.status_code == 200 and ok.json()["decided"] == 1
        assert len(list(store.query(f"SELECT ?p WHERE {{ <{equip}> <{BRICK.hasPoint}> ?p }}"))) == 2
        with pool.connection() as conn:
            assert record_edit.list_edits(conn, equip)[0]["action"] == "relationship"
    finally:
        server.shutdown()
        with pool.connection() as conn:
            conn.execute("DELETE FROM relationship_proposals WHERE subject = %s", (equip,))
            conn.execute("DELETE FROM record_edits WHERE entity_uri = %s", (equip,))
        store.update(f"DELETE WHERE {{ <{equip}> ?p ?o }}")
        for t in topics:
            store.update(f"DELETE WHERE {{ <urn:point:{t}> ?p ?o }}")
        pool.close()

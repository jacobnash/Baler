"""
The mapping review queue (#184). Unit tests on an in-memory graph and
proposal store; the Postgres store is checked against a live database.
"""

import json
import uuid

import pytest
from rdflib import RDF, SKOS, Literal, URIRef

from baler import brick_vocab, mapping_proposals, mapping_review, model_step
from baler.ingest import ingest_tags
from baler.store import BRICK, PROJ, TD, Store

VOCAB = brick_vocab.load()
USER = {"sub": "u1", "email": "reviewer@example.com"}


def _propose(props, uri, pattern, cands=("Supply_Air_Flow_Sensor", "Air_Flow_Sensor"), label=None):
    props.propose(entity_uri=uri, pattern=pattern,
                  candidates=[{"brick_class": c, "reason": "r", "source": "ranker"} for c in cands],
                  refused_because="no model configured", evidence={"label": label or uri, "values": {"unit": "cfm"}}, asks=[])


def _queue():
    props = mapping_proposals.MemoryProposals()
    flow = "point|fbf/vav-{n}/ZoneAirflow-{n}||cfm"
    for i in (1, 2, 3):
        _propose(props, f"urn:point:fbf/vav-{i}/ZoneAirflow-{i}", flow)
    _propose(props, "urn:point:fbf/chiller-1/CHW-SUP-TEM-7", "point|fbf/chiller-{n}/CHW-SUP-TEM-{n}||°F",
             cands=("Chilled_Water_Supply_Temperature_Sensor",))
    _propose(props, "urn:point:fbf/boiler-1/FIR-RAT-3", "point|fbf/boiler-{n}/FIR-RAT-{n}||%", cands=("Boiler_Command",))
    return props


def test_groups_by_pattern_and_bigger_groups_first():
    gs = mapping_review.groups(_queue().list(mapping_proposals.HELD))
    assert (gs[0]["label"], gs[0]["count"]) == ("fbf/vav-{n}/ZoneAirflow-{n}", 3)
    assert all(g["tier"] == 3 for g in gs)


def test_a_pattern_a_fault_rule_needs_comes_first():
    rules = [{"id": "r1", "mode": "his", "applies_to": {"topic_glob": "fbf/boiler-*/FIR-*"}},
             {"id": "r2", "mode": "cur", "applies_to": {"brick_class": "Chilled_Water_Temperature_Sensor"}}]
    gs = mapping_review.groups(_queue().list(mapping_proposals.HELD), mapping_review.fault_rule_needs(rules, VOCAB))
    tiers = {g["label"]: g["tier"] for g in gs}
    assert tiers["fbf/boiler-{n}/FIR-RAT-{n}"] == 1  # topic_glob
    assert tiers["fbf/chiller-{n}/CHW-SUP-TEM-{n}"] == 1  # a candidate is a subclass of the rule's class
    assert tiers["fbf/vav-{n}/ZoneAirflow-{n}"] == 3
    assert gs[-1]["label"] == "fbf/vav-{n}/ZoneAirflow-{n}"


def test_points_on_a_site_with_an_active_cx_project_come_second():
    gs = mapping_review.groups(_queue().list(mapping_proposals.HELD), cx_prefixes=["fbf/chiller-1"])
    assert gs[0]["label"] == "fbf/chiller-{n}/CHW-SUP-TEM-{n}" and gs[0]["tier"] == 2


def test_the_queue_is_scoped_to_the_callers_sites():
    gs = mapping_review.groups(_queue().list(mapping_proposals.HELD), allows=lambda uri: "/vav-" in uri)
    assert [g["count"] for g in gs] == [3]


def _graph_with(uris):
    store = Store()
    for uri in uris:
        u = URIRef(uri)
        ingest_tags(store, uri.removeprefix("urn:point:"), {"unit": "cfm"})
        store.add_many([(u, RDF.type, TD.RawPoint), (u, RDF.type, TD.UnmappedPoint)])
    return store


def test_accepting_a_group_writes_the_class_and_saves_a_site_rule():
    props = _queue()
    flow = [r for r in props.list() if "ZoneAirflow" in r["entity_uri"]]
    store = _graph_with([r["entity_uri"] for r in flow])

    out = mapping_review.decide(props, store, None, [r["id"] for r in flow], "accept", USER,
                                brick_class="supply_air_flow_sensor", save_rule=True)

    assert out == {"action": "accept", "brick_class": "Supply_Air_Flow_Sensor", "decided": 3,
                   "site_rules": ["point|fbf/vav-{n}/ZoneAirflow-{n}||cfm"]}
    for r in flow:
        u = URIRef(r["entity_uri"])
        assert (u, RDF.type, BRICK.Supply_Air_Flow_Sensor) in store.graph
        assert (u, RDF.type, TD.UnmappedPoint) not in store.graph
    assert {r["status"] for r in props.get([r["id"] for r in flow])} == {"accepted"}
    assert props.site_rules() == {"point|fbf/vav-{n}/ZoneAirflow-{n}||cfm": "Supply_Air_Flow_Sensor"}
    assert len(props.confirmed_examples()) == 3  # training data for memory (#185)


def test_accept_refuses_a_class_brick_does_not_have():
    props = _queue()
    r = props.list()[0]
    with pytest.raises(mapping_review.DecisionError, match="no Brick class"):
        mapping_review.decide(props, Store(), None, [r["id"]], "accept", USER, brick_class="Zone_Airflow_Thing")
    assert props.get([r["id"]])[0]["status"] == "proposed"


def test_an_answered_proposal_cant_be_answered_again():
    props = _queue()
    r = props.list()[0]
    store = _graph_with([r["entity_uri"]])
    mapping_review.decide(props, store, None, [r["id"]], "accept", USER, brick_class=r["candidates"][0]["brick_class"])
    with pytest.raises(mapping_review.DecisionError, match="already decided"):
        mapping_review.decide(props, store, None, [r["id"]], "accept", USER, brick_class="Air_Flow_Sensor")


def test_a_proposal_off_the_callers_sites_is_not_found():
    props = _queue()
    r = props.list()[0]
    with pytest.raises(mapping_review.DecisionError, match="no such proposal"):
        mapping_review.decide(props, Store(), None, [r["id"]], "escalate", USER, assignee="x", allows=lambda uri: False)


def test_escalate_assigns_and_notifies_through_the_webhook_registry(tmp_path, monkeypatch):
    props = _queue()
    ids = [r["id"] for r in props.list() if "ZoneAirflow" in r["entity_uri"]]
    hooks = tmp_path / "webhooks.json"
    hooks.write_text(json.dumps([
        {"id": "h1", "url": "https://example.com/a", "secret": "s"},
        {"id": "h2", "url": "https://example.com/b", "secret": "s", "filter": {"rule_id": "some-fault-rule"}},
    ]))
    sent = []

    class NowThread:
        def __init__(self, target, args=(), kwargs=None, daemon=None):
            self.call = (target, args, kwargs or {})

        def start(self):
            target, args, kwargs = self.call
            target(*args, **kwargs)

    monkeypatch.setattr(mapping_review.threading, "Thread", NowThread)
    monkeypatch.setattr(mapping_review.webhooks, "deliver", lambda url, secret, payload, **k: sent.append((url, payload)) or (True, None))

    with pytest.raises(mapping_review.DecisionError, match="assignee"):
        mapping_review.decide(props, Store(), None, ids, "escalate", USER)
    out = mapping_review.decide(props, Store(), None, ids, "escalate", USER, assignee="Acme Controls", note="VAV airflow?",
                                webhooks_path=str(hooks))

    assert out["notified"] == 1 and [u for u, _ in sent] == ["https://example.com/a"]
    assert sent[0][1]["event"] == "mapping.escalated" and len(sent[0][1]["proposals"]) == 3
    held = props.list(mapping_proposals.HELD)
    assert {r["assignee"] for r in held if r["id"] in ids} == {"Acme Controls"}
    # escalated is still held: it stays in the queue for the specialist, and a
    # re-run of autotag refreshes it rather than opening a second proposal
    _propose(props, held[0]["entity_uri"], held[0]["pattern"])
    assert len([r for r in props.rows if r["entity_uri"] == held[0]["entity_uri"]]) == 1
    g = mapping_review.groups(props.list(mapping_proposals.HELD))[0]
    assert g["escalated"] == 3 and g["assignee"] == "Acme Controls"


class _FakeConn:
    def __init__(self, projects):
        self.projects = projects

    def execute(self, sql, args=None):
        rows = self.projects

        class R:
            def fetchall(self):
                return rows

        return R()


class _FakeRepo:
    def __init__(self):
        self.issues, self.events = [], []

    def create_issue(self, issue):
        self.issues.append(issue)

    def add_issue_event(self, *a):
        self.events.append(a)


def test_site_visit_opens_a_cx_issue_on_the_covering_project():
    props = _queue()
    r = next(r for r in props.list() if "FIR-RAT" in r["entity_uri"])
    repo = _FakeRepo()
    conn = _FakeConn([("p-other", ["fbf/elsewhere"], "s1"), ("p-hosp", ["fbf/boiler-1"], "s2")])

    out = mapping_review.decide(props, Store(), conn, [r["id"]], "site_visit", USER, note="no tag on the panel", cx_repo=repo)

    assert out["cx_issue"] == repo.issues[0]["id"] and out["hint"] is None
    issue = repo.issues[0]
    assert issue["project_id"] == "p-hosp" and issue["source"] == "baler-mapping" and issue["source_id"] == r["id"]
    assert "FIR-RAT" in issue["title"] and issue["detail"]["note"] == "no tag on the panel"
    assert props.get([r["id"]])[0]["status"] == "site_visit"


def test_site_visit_without_a_cx_project_still_records_the_answer():
    props = _queue()
    r = props.list()[0]
    out = mapping_review.decide(props, Store(), _FakeConn([]), [r["id"]], "site_visit", USER, cx_repo=_FakeRepo())
    assert out["cx_issue"] is None and "no active Cx project" in out["hint"]
    assert props.get([r["id"]])[0]["status"] == "site_visit"


def test_summary_counts_points_by_who_classified_them():
    store = Store()
    pts = {name: URIRef(f"urn:point:fbf/x/{name}") for name in ("rule", "model", "site", "person", "proposed", "escalated", "bare")}
    for u in pts.values():
        store.add_entity(u, TD.RawPoint)
    for name in ("rule", "model", "site", "person"):
        store.add_entity(pts[name], BRICK.Fan_Status)
    store.add_relationship(pts["model"], SKOS.definition, Literal("model-classified (agreed on re-ask): x"))
    store.add_relationship(pts["site"], SKOS.definition, Literal("site-rule-classified: x"))
    store.add_entity(pts["proposed"], PROJ.Something)
    rows = [{"entity_uri": str(pts["proposed"]), "status": "proposed"}, {"entity_uri": str(pts["escalated"]), "status": "escalated"}]

    s = mapping_review.summary(store, rows, {str(pts["person"])})

    assert s["states"] == {"convention": 0, "rule": 1, "site_rule": 1, "memory": 0, "model": 1, "person": 1, "proposed": 1, "escalated": 1, "unmapped": 1}
    assert (s["total"], s["understood"], s["understood_pct"]) == (7, 4, 57.1)
    assert mapping_review.summary(store, rows, set(), allows=lambda u: u.endswith("/rule"))["total"] == 1


def test_a_saved_site_rule_classifies_later_points_without_a_model():
    store = Store()
    uri = ingest_tags(store, "fbf/vav-9/ZoneAirflow-77", {"unit": "cfm"})
    store.add_many([(uri, TD.sourceTopic, Literal("fbf/vav-9/ZoneAirflow-77"))])

    class Explodes:
        name = "x"

        def ask(self, prompt):
            raise AssertionError("a site rule pattern must not reach the model")

    [d] = model_step.run(store, [(uri, "point")], Explodes(), vocab=VOCAB,
                         site_rules={"point|fbf/vav-{n}/ZoneAirflow-{n}||cfm": "Supply_Air_Flow_Sensor"})
    assert d.accepted == "Supply_Air_Flow_Sensor"
    assert (uri, RDF.type, BRICK.Supply_Air_Flow_Sensor) in store.graph
    assert any(str(o).startswith("site-rule-classified") for o in store.graph.objects(uri, SKOS.definition))


def test_a_site_rule_still_respects_the_veto():
    store = Store()
    uri = ingest_tags(store, "fbf/vav-9/ZoneAirflow-77", {"unit": "°F"})
    store.add_many([(uri, TD.sourceTopic, Literal("fbf/vav-9/ZoneAirflow-77"))])
    [d] = model_step.run(store, [(uri, "point")], None, vocab=VOCAB,
                         site_rules={"point|fbf/vav-{n}/ZoneAirflow-{n}||°F": "Supply_Air_Flow_Sensor"})
    assert d.accepted is None and d.refused_because.startswith("vetoed")


@pytest.mark.integration
def test_pg_store_holds_escalations_site_rules_and_examples():
    from baler import timeseries

    conn = timeseries.connect()
    props = mapping_proposals.PgProposals(conn)
    props.ensure_schema()
    uri = f"urn:point:test-review/{uuid.uuid4().hex}"
    pattern = f"point|test-review/{uuid.uuid4().hex}||"
    try:
        _propose(props, uri, pattern)
        [row] = [r for r in props.list() if r["entity_uri"] == uri]
        assert props.decide(row["id"], "escalated", None, "me", assignee="Acme") is True
        _propose(props, uri, pattern, cands=("Air_Flow_Sensor",))  # refresh, not a second row
        rows = [r for r in props.list(mapping_proposals.HELD) if r["entity_uri"] == uri]
        assert len(rows) == 1 and rows[0]["status"] == "escalated" and rows[0]["assignee"] == "Acme"
        assert rows[0]["candidates"][0]["brick_class"] == "Air_Flow_Sensor"
        assert props.decide(row["id"], "accepted", "Air_Flow_Sensor", "acme@example.com") is True
        assert props.decide(row["id"], "accepted", "Air_Flow_Sensor", "again") is False
        assert any(e["entity_uri"] == uri for e in props.confirmed_examples())
        props.add_site_rule(pattern, "Air_Flow_Sensor", "me", row["id"])
        assert props.site_rules()[pattern] == "Air_Flow_Sensor"
        assert props.counts().get("accepted", 0) >= 1
    finally:
        conn.execute("DELETE FROM mapping_proposals WHERE entity_uri = %s", (uri,))
        conn.execute("DELETE FROM mapping_site_rules WHERE pattern = %s", (pattern,))
        conn.close()


@pytest.mark.integration
def test_the_ops_api_routes_end_to_end(monkeypatch):
    """GET the queue and summary, POST an answer, through ops_api against
    live Postgres and Oxigraph; responses match ops-api-openapi.yaml."""
    from http.server import ThreadingHTTPServer
    from threading import Thread

    import requests
    from conftest import assert_matches_schema, load_spec

    from baler import auth, record_edit, timeseries
    from baler.ops_api import OPENAPI_SPEC_PATH, make_handler
    from baler.remote_store import RemoteStore

    monkeypatch.setenv("BALER_GATEWAY_SECRET", "test-gateway-secret")
    pool = timeseries.connect_pool(min_size=1, max_size=2)
    store = RemoteStore()
    tag = uuid.uuid4().hex[:8]
    topics = [f"test-review-{tag}/vav-{i}/ZoneAirflow-{i}" for i in (1, 2)]
    uris = [f"urn:point:{t}" for t in topics]
    pattern = f"point|test-review-{tag}/vav-{{n}}/ZoneAirflow-{{n}}||cfm"
    with pool.connection() as conn:
        auth.ensure_schema(conn)
        record_edit.ensure_schema(conn)
        props = mapping_proposals.PgProposals(conn)
        props.ensure_schema()
        for t, u in zip(topics, uris):
            ingest_tags(store, t, {"unit": "cfm"})
            store.add_many([(URIRef(u), RDF.type, TD.RawPoint)])
            _propose(props, u, pattern, label=t)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(pool, store))
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    h = {"X-Gateway-Secret": "test-gateway-secret"}
    spec = load_spec(OPENAPI_SPEC_PATH)
    try:
        assert requests.get(f"{base}/mapping/proposals").status_code == 401
        groups = requests.get(f"{base}/mapping/proposals", headers=h).json()
        [g] = [g for g in groups if g["pattern"] == pattern]
        assert g["count"] == 2
        assert_matches_schema(g, spec["components"]["schemas"]["MappingGroup"], spec)
        before = requests.get(f"{base}/mapping/summary", headers=h).json()
        assert before["states"]["proposed"] >= 2

        bad = requests.post(f"{base}/mapping/decide", headers=h, json={"ids": [m["id"] for m in g["members"]], "action": "accept",
                                                                      "brick_class": "Zone_Airflow_Thing"})
        assert bad.status_code == 400
        ok = requests.post(f"{base}/mapping/decide", headers=h, json={"ids": [m["id"] for m in g["members"]], "action": "accept",
                                                                     "brick_class": "Supply_Air_Flow_Sensor", "save_rule": True})
        assert ok.status_code == 200, ok.text
        assert ok.json()["decided"] == 2 and ok.json()["site_rules"] == [pattern]
        again = requests.post(f"{base}/mapping/decide", headers=h, json={"ids": [g["members"][0]["id"]], "action": "escalate", "assignee": "x"})
        assert again.status_code == 409
        for u in uris:
            assert list(store.query(f"SELECT ?t WHERE {{ <{u}> a <{BRICK.Supply_Air_Flow_Sensor}> }}"))
        with pool.connection() as conn:
            edits = record_edit.list_edits(conn, uris[0])
            assert edits and edits[0]["action"] == "mapping"
        after = requests.get(f"{base}/mapping/summary", headers=h).json()
        assert after["states"]["person"] >= before["states"]["person"] + 2
    finally:
        server.shutdown()
        with pool.connection() as conn:
            conn.execute("DELETE FROM mapping_proposals WHERE entity_uri = ANY(%s)", (uris,))
            conn.execute("DELETE FROM mapping_site_rules WHERE pattern = %s", (pattern,))
            conn.execute("DELETE FROM record_edits WHERE entity_uri = ANY(%s)", (uris,))
        for u in uris:
            store.update(f"DELETE WHERE {{ <{u}> ?p ?o }}")
        pool.close()

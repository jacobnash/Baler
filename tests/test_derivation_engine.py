"""
Unit tests for _topological_order (pure logic, no fixtures), plus
integration tests against real Postgres for evaluate_derivations - same
fixture pattern as test_ingest.py/test_fault_detector.py: in-memory Store
for the graph (fast, no live Oxigraph needed - Store and RemoteStore share
a method surface), real Postgres for point_history/faults/target health.
"""

import json
import time
from datetime import datetime, timezone

import pytest
from rdflib import URIRef

from baler.derivation_engine import (
    _output_uri,
    _topological_order,
    evaluate_derivations,
)
from baler.derivation_health import (
    enable_target,
    is_target_disabled,
    record_target_failure,
)
from baler.derivation_health import ensure_schema as ensure_health_schema
from baler.faults import ensure_schema as ensure_fault_schema
from baler.faults import open_fault
from baler.ingest import (
    link_part_of,
    link_point_to_equip,
    topic_prefix_to_equip_uri,
    topic_to_point_uri,
)
from baler.mapping import classify_point
from baler.mqtt_listener import make_on_message
from baler.mqtt_util import make_client
from baler.remote_store import RemoteStore
from baler.store import BRICK, PROV, Store
from baler.timeseries import connect, read_latest, write_point_value

AVG_FN_SOURCE = "def run(inputs, row):\n    vals = [v for s in inputs.values() for _, v in s[-1:]]\n    return sum(vals) / len(vals) if vals else None"


def test_topological_order_orders_dependencies_first():
    order = _topological_order({"a": ["b"], "b": []})
    assert order.index("b") < order.index("a")


def test_topological_order_prunes_unknown_dependency():
    order = _topological_order({"a": ["missing"], "b": []})
    assert order == ["b"]


def test_topological_order_prunes_cycle_and_cascades_to_dependents():
    order = _topological_order({"a": ["b"], "b": ["a"], "c": ["a"], "d": []})
    assert set(order) == {"d"}  # a<->b cyclic; c depends on the pruned a, so c is pruned too


@pytest.fixture
def ts_conn():
    conn = connect()
    conn.execute("DELETE FROM point_history WHERE point_uri LIKE %s", ("urn:point:test-de%",))
    return conn


@pytest.fixture
def fault_conn():
    conn = connect()
    ensure_fault_schema(conn)
    conn.execute("DELETE FROM faults WHERE rule_id LIKE 'test-de%'")
    return conn


@pytest.fixture
def health_conn():
    conn = connect()
    ensure_health_schema(conn)
    conn.execute("DELETE FROM derivation_target_health WHERE derivation_id LIKE 'test-de%'")
    return conn


def _seed_two_points(store, ts_conn, prefix, v1, v2, now):
    p1 = topic_to_point_uri(f"{prefix}/a")
    p2 = topic_to_point_uri(f"{prefix}/b")
    equip = topic_prefix_to_equip_uri(prefix)
    link_point_to_equip(store, p1, equip)
    link_point_to_equip(store, p2, equip)
    write_point_value(ts_conn, str(p1), v1, now)
    write_point_value(ts_conn, str(p2), v2, now)
    return equip, p1, p2


def _avg_derivation(derivation_id, p1, p2, **overrides):
    return {
        "id": derivation_id,
        "kind": "formula",
        "select": f"""
            PREFIX brick: <https://brickschema.org/schema/Brick#>
            SELECT ?target ?a ?b WHERE {{
                ?target brick:hasPoint ?a, ?b .
                FILTER(?a = <{p1}> && ?b = <{p2}>)
            }}
        """,
        "target_var": "target",
        "input_vars": ["a", "b"],
        "window_seconds": 3600,
        "fn_source": AVG_FN_SOURCE,
        "output": {},
        "depends_on": [],
        **overrides,
    }


@pytest.mark.integration
def test_formula_derivation_writes_attaches_and_derives(ts_conn, fault_conn, health_conn):
    store = Store()
    now = datetime.now(timezone.utc)
    equip, p1, p2 = _seed_two_points(store, ts_conn, "test-de/formula-basic", 70.0, 74.0, now)
    derivation = _avg_derivation("test-de:avg", p1, p2)

    trace = evaluate_derivations(store, ts_conn, fault_conn, health_conn, [derivation], now=now)

    assert len(trace) == 1
    assert trace[0]["computed_value"] == 72.0
    output_uri = trace[0]["would_write_uri"]
    assert output_uri == _output_uri("test-de:avg", str(equip))
    assert read_latest(ts_conn, output_uri) == 72.0
    assert (URIRef(output_uri), BRICK.isPointOf, equip) in store.graph
    assert (equip, BRICK.hasPoint, URIRef(output_uri)) in store.graph
    assert (URIRef(output_uri), PROV.wasDerivedFrom, p1) in store.graph
    assert (URIRef(output_uri), PROV.wasDerivedFrom, p2) in store.graph


@pytest.mark.integration
def test_a_pass_larger_than_the_read_budget_computes_every_row_from_its_own_inputs(
        ts_conn, fault_conn, health_conn, monkeypatch):
    """#336: 40 equips x 2 inputs x 30 readings (2,400) against a read
    budget of 200 readings and a write batch of 7: the pass runs in chunks
    against real Postgres, and every equip's output is computed from its
    own inputs' full windows, not a neighbour's or a truncated one."""
    from datetime import timedelta

    from baler import derivation_engine
    from baler.timeseries import write_point_values

    monkeypatch.setattr(derivation_engine, "READ_BUDGET", 200)
    monkeypatch.setattr(derivation_engine, "WRITE_BATCH", 7)
    reads = []
    real_bulk = derivation_engine.timeseries.read_range_bulk
    monkeypatch.setattr(derivation_engine.timeseries, "read_range_bulk",
                        lambda conn, uris, start, end: reads.append(len(uris)) or real_bulk(conn, uris, start, end))
    store = Store()
    now = datetime.now(timezone.utc)
    history = []
    for e in range(40):
        equip = topic_prefix_to_equip_uri(f"test-de/bulk/e{e}")
        for name, base in (("a", 1000.0 * e), ("b", 0.0)):
            p = topic_to_point_uri(f"test-de/bulk/e{e}/{name}")
            link_point_to_equip(store, p, equip)
            history += [(str(p), base + i, now - timedelta(seconds=10 * i)) for i in range(30)]
    write_point_values(ts_conn, history)
    derivation = {
        "id": "test-de:bulk", "kind": "formula",
        "select": """
            PREFIX brick: <https://brickschema.org/schema/Brick#>
            SELECT ?target ?a ?b WHERE {
                ?target brick:hasPoint ?a, ?b .
                FILTER(STRSTARTS(STR(?a), "urn:point:test-de/bulk/") && STRENDS(STR(?a), "/a")
                       && STRENDS(STR(?b), "/b"))
            }""",
        "target_var": "target", "input_vars": ["a", "b"], "window_seconds": 3600, "output": {}, "depends_on": [],
        # sum of a's window minus b's: 30 * base(a), only if each got all 30 of its own readings
        "fn_source": "def run(inputs, row):\n    return sum(v for _, v in inputs['a']) - sum(v for _, v in inputs['b'])",
    }

    trace = evaluate_derivations(store, ts_conn, fault_conn, health_conn, [derivation], now=now)

    assert len(trace) == 40 and len(reads) > 1 and max(reads) * 30 <= 200
    for entry in trace:
        e = int(entry["target"].rsplit("/e", 1)[1])
        assert entry["computed_value"] == 30 * 1000.0 * e
        assert read_latest(ts_conn, entry["would_write_uri"]) == 30 * 1000.0 * e


@pytest.mark.integration
def test_formula_derivation_returning_none_writes_nothing(ts_conn, fault_conn, health_conn):
    store = Store()
    now = datetime.now(timezone.utc)
    equip, p1, p2 = _seed_two_points(store, ts_conn, "test-de/none-result", 1.0, 2.0, now)
    derivation = _avg_derivation("test-de:none", p1, p2, fn_source="def run(inputs, row):\n    return None")

    trace = evaluate_derivations(store, ts_conn, fault_conn, health_conn, [derivation], now=now)

    assert trace[0]["computed_value"] is None
    assert "would_write_uri" not in trace[0]
    assert read_latest(ts_conn, _output_uri("test-de:none", str(equip))) is None


@pytest.mark.integration
def test_dry_run_computes_but_writes_nothing(ts_conn, fault_conn, health_conn):
    store = Store()
    now = datetime.now(timezone.utc)
    _equip, p1, p2 = _seed_two_points(store, ts_conn, "test-de/dryrun", 1.0, 3.0, now)
    derivation = _avg_derivation("test-de:dryrun", p1, p2)

    trace = evaluate_derivations(store, ts_conn, fault_conn, health_conn, [derivation], now=now, dry_run=True)

    assert trace[0]["computed_value"] == 2.0
    output_uri = trace[0]["would_write_uri"]
    assert read_latest(ts_conn, output_uri) is None
    assert list(store.graph.triples((URIRef(output_uri), None, None))) == []


@pytest.mark.integration
def test_chained_derivations_downstream_reads_upstream_output_same_pass(ts_conn, fault_conn, health_conn):
    store = Store()
    now = datetime.now(timezone.utc)
    equip, p1, p2 = _seed_two_points(store, ts_conn, "test-de/chain", 10.0, 20.0, now)

    upstream = _avg_derivation("test-de:chain-sum", p1, p2, fn_source="def run(inputs, row):\n    vals=[v for s in inputs.values() for _,v in s[-1:]]\n    return sum(vals) if vals else None")
    upstream_output = _output_uri("test-de:chain-sum", str(equip))
    downstream = {
        "id": "test-de:chain-double",
        "kind": "formula",
        "select": f"""
            PREFIX brick: <https://brickschema.org/schema/Brick#>
            SELECT ?target ?total WHERE {{ ?target brick:hasPoint ?total . FILTER(?total = <{upstream_output}>) }}
        """,
        "target_var": "target",
        "input_vars": ["total"],
        "window_seconds": 3600,
        "fn_source": "def run(inputs, row):\n    vals = inputs['total']\n    return vals[-1][1] * 2 if vals else None",
        "output": {},
        "depends_on": ["test-de:chain-sum"],
    }

    # deliberately listed downstream-before-upstream - topo sort must fix the order
    trace = evaluate_derivations(store, ts_conn, fault_conn, health_conn, [downstream, upstream], now=now)

    downstream_entries = [t for t in trace if t["derivation_id"] == "test-de:chain-double"]
    assert downstream_entries[0]["computed_value"] == 60.0  # (10 + 20) * 2


@pytest.mark.integration
def test_open_fault_excludes_input_from_evaluation(ts_conn, fault_conn, health_conn, tmp_path, monkeypatch):
    store = Store()
    now = datetime.now(timezone.utc)
    _equip, p1, p2 = _seed_two_points(store, ts_conn, "test-de/faulty", 70.0, 74.0, now)
    open_fault(fault_conn, "test-de:stale-rule", str(p1), now)
    _rules_file(tmp_path, monkeypatch, [{"id": "test-de:stale-rule", "type": "stale"}])

    derivation = _avg_derivation("test-de:fault-filtered", p1, p2, fn_source="def run(inputs, row):\n    return None if not inputs['a'] else 1.0")

    trace = evaluate_derivations(store, ts_conn, fault_conn, health_conn, [derivation], now=now)

    assert trace[0]["computed_value"] is None
    assert trace[0]["masked_inputs"] == {str(p1): "test-de:stale-rule"}  # says why, not silent (#58)


def _rules_file(tmp_path, monkeypatch, rules):
    import json

    from baler import derivation_engine, json_store

    path = tmp_path / "rules.json"
    path.write_text(json.dumps(rules))
    monkeypatch.setattr(derivation_engine, "_rules", json_store.RuleCache(str(path)))


@pytest.mark.integration
@pytest.mark.parametrize("rules", [
    [{"id": "test-de:range-rule", "type": "range"}],  # out of range isn't invalid data
    [],  # the rule was deleted: its fault can never resolve, so it mustn't mask forever
], ids=["non-validity-rule", "deleted-rule"])
def test_only_stale_or_stuck_faults_from_existing_rules_mask_inputs(ts_conn, fault_conn, health_conn, tmp_path, monkeypatch, rules):
    """Baler#58: any open fault used to mask an input - five AHUs' fan duty
    cycle stopped for two months behind faults from deleted rules."""
    store = Store()
    now = datetime.now(timezone.utc)
    _equip, p1, p2 = _seed_two_points(store, ts_conn, "test-de/notmasked", 70.0, 74.0, now)
    open_fault(fault_conn, "test-de:range-rule", str(p1), now)
    _rules_file(tmp_path, monkeypatch, rules)

    derivation = _avg_derivation("test-de:not-masked", p1, p2, fn_source="def run(inputs, row):\n    return None if not inputs['a'] else 1.0")
    trace = evaluate_derivations(store, ts_conn, fault_conn, health_conn, [derivation], now=now, dry_run=True)

    assert trace[0]["computed_value"] == 1.0
    assert "masked_inputs" not in trace[0]


@pytest.mark.integration
def test_uncaught_exception_disables_only_that_target(ts_conn, fault_conn, health_conn):
    store = Store()
    now = datetime.now(timezone.utc)
    equip_bad, _pb1, _pb2 = _seed_two_points(store, ts_conn, "test-de/bad", 0.0, 5.0, now)
    equip_good, _pg1, _pg2 = _seed_two_points(store, ts_conn, "test-de/good", 10.0, 20.0, now)

    derivation = {
        "id": "test-de:divider",
        "kind": "formula",
        "select": """
            PREFIX brick: <https://brickschema.org/schema/Brick#>
            SELECT ?target ?a ?b WHERE {
                ?target brick:hasPoint ?a, ?b .
                FILTER(STRENDS(STR(?a), "/a") && STRENDS(STR(?b), "/b"))
            }
        """,
        "target_var": "target",
        "input_vars": ["a", "b"],
        "window_seconds": 3600,
        "fn_source": "def run(inputs, row):\n    a = inputs['a'][-1][1]\n    b = inputs['b'][-1][1]\n    return b / a",
        "output": {},
        "depends_on": [],
    }

    for _ in range(6):
        evaluate_derivations(store, ts_conn, fault_conn, health_conn, [derivation], now=now)

    assert is_target_disabled(health_conn, "test-de:divider", str(equip_bad)) is True
    assert is_target_disabled(health_conn, "test-de:divider", str(equip_good)) is False


@pytest.mark.integration
def test_disabled_target_is_skipped_and_reenable_resumes(ts_conn, fault_conn, health_conn):
    store = Store()
    now = datetime.now(timezone.utc)
    equip, p1, p2 = _seed_two_points(store, ts_conn, "test-de/reenable", 1.0, 2.0, now)
    derivation = _avg_derivation("test-de:reenable", p1, p2)
    record_target_failure(health_conn, "test-de:reenable", str(equip), "boom", disable_threshold=1)
    assert is_target_disabled(health_conn, "test-de:reenable", str(equip)) is True

    trace = evaluate_derivations(store, ts_conn, fault_conn, health_conn, [derivation], now=now)
    assert trace == []

    enable_target(health_conn, "test-de:reenable", str(equip))
    trace = evaluate_derivations(store, ts_conn, fault_conn, health_conn, [derivation], now=now)
    assert trace[0]["computed_value"] == 1.5


def _rollup_derivation(derivation_id, root):
    return {
        "id": derivation_id,
        "kind": "rollup",
        "root_select": f"SELECT ?root WHERE {{ VALUES ?root {{ <{root}> }} }}",
        "part_relationship": "brick:hasPart",
        "leaf_point_class": "brick:Meter",
        "window_seconds": 3600,
        "fn_source": "def run(inputs, row):\n    vals = [s[-1][1] for s in inputs if s]\n    return sum(vals) if vals else None",
        "output": {},
        "depends_on": [],
    }


@pytest.mark.integration
def test_rollup_computes_bottom_up_and_traces_lineage_to_leaves(ts_conn, fault_conn, health_conn):
    store = Store()
    now = datetime.now(timezone.utc)

    root = URIRef("urn:equip:test-de:campus-1")
    equip_a = URIRef("urn:equip:test-de:equip-a")
    equip_b = URIRef("urn:equip:test-de:equip-b")
    store.add_relationship(root, BRICK.hasPart, equip_a)
    store.add_relationship(root, BRICK.hasPart, equip_b)

    meter_a1, meter_a2, meter_b1 = (topic_to_point_uri(f"test-de/meter-{n}") for n in ("a1", "a2", "b1"))
    for meter in (meter_a1, meter_a2):
        store.add_entity(meter, BRICK.Meter)
        link_point_to_equip(store, meter, equip_a)
    store.add_entity(meter_b1, BRICK.Meter)
    link_point_to_equip(store, meter_b1, equip_b)
    write_point_value(ts_conn, str(meter_a1), 10.0, now)
    write_point_value(ts_conn, str(meter_a2), 15.0, now)
    write_point_value(ts_conn, str(meter_b1), 5.0, now)

    derivation = _rollup_derivation("test-de:rollup", root)
    trace = evaluate_derivations(store, ts_conn, fault_conn, health_conn, [derivation], now=now)

    by_target = {t["target"]: t for t in trace}
    assert by_target[str(equip_a)]["computed_value"] == 25.0
    assert by_target[str(equip_b)]["computed_value"] == 5.0
    assert by_target[str(root)]["computed_value"] == 30.0

    root_output = by_target[str(root)]["would_write_uri"]
    rows = list(store.query(f"""
        PREFIX prov: <http://www.w3.org/ns/prov#>
        SELECT ?anc WHERE {{ <{root_output}> prov:wasDerivedFrom+ ?anc }}
    """))
    ancestors = {str(r.anc) for r in rows}
    assert {str(meter_a1), str(meter_a2), str(meter_b1)} <= ancestors


@pytest.mark.integration
def test_rollup_cyclic_containment_is_pruned_not_hung(ts_conn, fault_conn, health_conn):
    store = Store()
    now = datetime.now(timezone.utc)
    root = URIRef("urn:equip:test-de:cyclic-root")
    a = URIRef("urn:equip:test-de:cyclic-a")
    b = URIRef("urn:equip:test-de:cyclic-b")
    store.add_relationship(root, BRICK.hasPart, a)
    store.add_relationship(a, BRICK.hasPart, b)
    store.add_relationship(b, BRICK.hasPart, a)  # a <-> b cycle

    derivation = _rollup_derivation("test-de:rollup-cycle", root)

    # the point under test is that this returns promptly rather than hanging
    trace = evaluate_derivations(store, ts_conn, fault_conn, health_conn, [derivation], now=now)

    assert trace == []  # root transitively depends on the pruned cyclic pair, so it's pruned too


@pytest.mark.integration
def test_e2e_derivation_averages_bacnet_and_modbus_sourced_sensors_over_real_broker(ts_conn, fault_conn, health_conn):
    """Full pipeline end to end, not synthetic in-test graph state: two
    sensors from different source connections - simulating one BACnet-
    sourced point and one Modbus-sourced point, which from ingest.py's
    point of view are indistinguishable (it only ever sees Windrow's one
    shared wire envelope; "BACnet" vs "Modbus" is purely which
    connection/topic_prefix a reading arrives under, exactly like two
    real Windrow connections would differ) - ingested over a real MQTT broker
    via the real mqtt_listener.make_on_message, linked to a shared zone
    entity (a point can belong to more than one parent - its own device
    equip and a physical zone - with no conflict, same as any RDF graph),
    then averaged by a real formula derivation via evaluate_derivations.
    Uses RemoteStore (live Oxigraph), not the in-memory Store the other
    tests in this file use, for full realism.

    Also exercises the two things a real zone-temp point/equip actually
    carries and this test previously skipped as irrelevant to the
    averaging logic under test: real Haystack tags on each point (so
    mapping.classify_point resolves them to Zone_Air_Temperature_Sensor,
    same rule real hospital data hits) and the zone equip itself
    referencing a parent via brick:hasPart/isPartOf - the same two things
    scripts/seed_derivations_and_faults.py's seed_site_structure asserts
    for real equipment. classify_point is called explicitly, not
    expected to happen automatically: the MQTT ingest path never
    classifies on its own (only ingest_api.py's HTTP POST /tags path
    does, via reclassify) - that's what baler.autotag exists to
    do as a separate sweep, and this test mirrors that division rather
    than papering over it."""
    store = RemoteStore()
    unique = int(time.time())
    zone = URIRef(f"urn:equip:test-de:zone-e2e-{unique}")

    bacnet_topic = f"fbf/test-de-bacnet-ahu-{unique}/zone-temp"
    modbus_topic = f"fbf/test-de-modbus-meter-{unique}/zone-temp"
    bacnet_point = topic_to_point_uri(bacnet_topic)
    modbus_point = topic_to_point_uri(modbus_topic)

    listener = make_client()
    listener.on_message = make_on_message(store, ts_conn)
    listener.connect("localhost", 1883)
    listener.subscribe(f"fbf/test-de-bacnet-ahu-{unique}/#")
    listener.subscribe(f"fbf/test-de-modbus-meter-{unique}/#")

    # Real Haystack marker tags for a zone-temp sensor - the exact tag set
    # rules/haystack_to_brick.yaml maps directly to Zone_Air_Temperature_Sensor.
    zone_temp_tags = {"zone": True, "air": True, "temp": True, "sensor": True}

    publisher = make_client()
    publisher.connect("localhost", 1883)
    now_ts = time.time()
    publisher.publish(bacnet_topic, json.dumps({"value": 70.0, "ts": now_ts}), retain=True)
    publisher.publish(modbus_topic, json.dumps({"value": 74.0, "ts": now_ts}), retain=True)
    publisher.publish(f"{bacnet_topic}/tags", json.dumps({"value": json.dumps(zone_temp_tags), "ts": now_ts}), retain=True)
    publisher.publish(f"{modbus_topic}/tags", json.dumps({"value": json.dumps(zone_temp_tags), "ts": now_ts}), retain=True)
    publisher.disconnect()

    def _is_tagged(point_uri) -> bool:
        # RemoteStore.query() only exposes SELECT-shaped row iteration (no
        # ASK support, see ingest.link_equip_ref's own note on this) - a
        # SELECT ... LIMIT 1 existence check does the same job.
        tagged = store.query(f"""
            PREFIX haystack: <urn:baler:haystack#>
            SELECT ?tag WHERE {{ <{point_uri}> haystack:hasTag ?tag }} LIMIT 1
        """)
        return len(list(tagged)) > 0

    def _both_ingested():
        # Both points' *and* both points' tags - a flaky-under-load
        # regression found this only checked bacnet_point's tags, so the
        # wait loop could exit while modbus_point's separate /tags
        # message was still in flight, and classify_point(modbus_point)
        # below would see no tags yet (spurious "miss").
        return (
            read_latest(ts_conn, str(bacnet_point)) is not None
            and read_latest(ts_conn, str(modbus_point)) is not None
            and _is_tagged(bacnet_point)
            and _is_tagged(modbus_point)
        )

    # Poll until both readings and both tag sets are in, up to 20 s (CI
    # runners under load took over the old 3 s), and fail here if they never
    # arrive rather than at the classify asserts below with a misleading "miss".
    deadline = time.monotonic() + 20
    ingested = False
    while time.monotonic() < deadline:
        listener.loop(timeout=0.1)
        if _both_ingested():
            ingested = True
            break
    listener.disconnect()
    assert ingested, "both points and both tag sets should be ingested within 20 s"

    assert read_latest(ts_conn, str(bacnet_point)) == 70.0
    assert read_latest(ts_conn, str(modbus_point)) == 74.0

    # MQTT ingest never classifies on its own - see the module docstring
    # note above. Call it explicitly, the same "sweep after ingest" shape
    # baler.autotag uses for real data.
    assert classify_point(store, bacnet_point) == ("direct", "Zone_Air_Temperature_Sensor")
    assert classify_point(store, modbus_point) == ("direct", "Zone_Air_Temperature_Sensor")

    link_point_to_equip(store, bacnet_point, zone)
    link_point_to_equip(store, modbus_point, zone)

    # The zone itself references a parent, same as real equipment does via
    # seed_site_structure's Building/Floor/Zone hasPart chain.
    building = URIRef(f"urn:equip:test-de:building-e2e-{unique}")
    store.add_entity(building, BRICK.Building)
    link_part_of(store, zone, building)
    parent_of_zone = {str(r.parent) for r in store.query(f"""
        PREFIX brick: <https://brickschema.org/schema/Brick#>
        SELECT ?parent WHERE {{ <{zone}> brick:isPartOf ?parent }}
    """)}
    assert str(building) in parent_of_zone

    derivation = {
        "id": f"test-de:e2e-avg-{unique}",
        "kind": "formula",
        "select": f"""
            PREFIX brick: <https://brickschema.org/schema/Brick#>
            SELECT ?target ?bacnet ?modbus WHERE {{
                ?target brick:hasPoint ?bacnet, ?modbus .
                FILTER(?bacnet = <{bacnet_point}> && ?modbus = <{modbus_point}>)
            }}
        """,
        "target_var": "target",
        "input_vars": ["bacnet", "modbus"],
        "window_seconds": 3600,
        "fn_source": AVG_FN_SOURCE,
        "output": {"unit": "degF"},
        "depends_on": [],
    }

    now = datetime.now(timezone.utc)
    trace = evaluate_derivations(store, ts_conn, fault_conn, health_conn, [derivation], now=now)

    assert len(trace) == 1
    assert trace[0]["computed_value"] == 72.0
    output_uri = trace[0]["would_write_uri"]
    assert read_latest(ts_conn, output_uri) == 72.0

    linked = {str(r.p) for r in store.query(f"""
        PREFIX brick: <https://brickschema.org/schema/Brick#>
        SELECT ?p WHERE {{ <{zone}> brick:hasPoint ?p }}
    """)}
    assert {str(bacnet_point), str(modbus_point), output_uri} <= linked

    ancestors = {str(r.anc) for r in store.query(f"""
        PREFIX prov: <http://www.w3.org/ns/prov#>
        SELECT ?anc WHERE {{ <{output_uri}> prov:wasDerivedFrom ?anc }}
    """)}
    assert {str(bacnet_point), str(modbus_point)} <= ancestors


@pytest.mark.integration
def test_deleted_and_orphaned_rules_faults_get_resolved(fault_conn):
    from baler.faults import close_orphaned_faults, close_rule_faults

    now = datetime.now(timezone.utc)
    open_fault(fault_conn, "test-de-gone", "urn:point:test-de/x", now)
    open_fault(fault_conn, "test-de-gone", "urn:point:test-de/y", now)
    open_fault(fault_conn, "test-de-kept", "urn:point:test-de/x", now)
    open_fault(fault_conn, "test-de:mirrored", "urn:point:test-de/x", now)  # namespaced: not ours to judge
    open_fault(fault_conn, "test-de-deleted", "urn:point:test-de/z", now)

    assert close_rule_faults(fault_conn, "test-de-deleted", now) == 1
    # every rule id fault_detector knows, plus the kept one
    known = {r[0] for r in fault_conn.execute(
        "SELECT DISTINCT rule_id FROM faults WHERE ended_at IS NULL AND rule_id NOT LIKE 'test-de%'").fetchall()}
    assert close_orphaned_faults(fault_conn, known | {"test-de-kept"}, now) == 2
    still_open = {r[0] for r in fault_conn.execute(
        "SELECT rule_id FROM faults WHERE ended_at IS NULL AND rule_id LIKE 'test-de%'").fetchall()}
    assert still_open == {"test-de-kept", "test-de:mirrored"}

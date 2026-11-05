"""
Cur-mode via synthetic handle_cur_reading calls (no live MQTT needed -
that's just a transport, already proven by mqtt_listener.py's own tests).
His-mode via write_point_value synthetic rows, same fixture pattern as
test_ingest.py, called synchronously via evaluate_his_rules() rather than
waiting on a real sleep loop.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from baler.fault_detector import (
    RuleCache,
    evaluate_his_rules,
    evaluate_range_rule,
    handle_cur_reading,
)
from baler.faults import ensure_schema, list_faults
from baler.ingest import ingest_tags
from baler.mapping import classify_point
from baler.store import Store
from baler.timeseries import connect, write_point_value


@pytest.fixture
def fault_conn():
    conn = connect()
    ensure_schema(conn)
    conn.execute("DELETE FROM faults WHERE rule_id LIKE 'test-fd:%'")
    return conn


@pytest.fixture
def receiver(monkeypatch):
    # fault_detector.py's real delivery path rejects non-public webhook
    # targets by default (see webhooks.validate_url) - this is the
    # deployment-level escape hatch, exercised here because the receiver
    # this fixture spins up is deliberately a loopback address.
    monkeypatch.setenv("BALER_ALLOW_PRIVATE_WEBHOOKS", "1")
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/", received
    server.shutdown()


def test_evaluate_range_rule_flags_out_of_bounds_numeric_value():
    rule = {"min": 60.0, "max": 80.0}
    assert evaluate_range_rule(rule, 90.0) is True
    assert evaluate_range_rule(rule, 70.0) is False


def test_evaluate_range_rule_ignores_non_numeric_values():
    rule = {"min": 60.0, "max": 80.0}
    assert evaluate_range_rule(rule, "active") is False
    assert evaluate_range_rule(rule, None) is False
    assert evaluate_range_rule(rule, True) is False  # bool is an int subclass - must not slip through


def test_rule_cache_resolves_brick_class_roster_via_sparql(tmp_path):
    """No Postgres/MQTT needed - RuleCache's brick_class roster resolution
    is pure SPARQL against whatever store it's given, same in-memory Store
    test_mapping.py already uses for classify_point."""
    store = Store()
    point_uri = ingest_tags(store, "test-fd:brick-roster-point", {"zone": True, "air": True, "temp": True, "sensor": True})
    classify_point(store, point_uri)  # -> Zone_Air_Temperature_Sensor

    rules_file = tmp_path / "rules.json"
    rules_file.write_text(json.dumps([
        {"id": "test-fd:brick-rule", "mode": "cur", "type": "range", "applies_to": {"brick_class": "Zone_Air_Temperature_Sensor"}, "min": 60.0, "max": 80.0}
    ]))
    rule_cache = RuleCache(str(rules_file), store=store)

    rule_cache.get()  # triggers the roster build

    assert rule_cache.rule_ids_for_point(str(point_uri)) == {"test-fd:brick-rule"}
    assert rule_cache.rule_ids_for_point("urn:point:some-other-point") == set()


def test_rule_cache_brick_roster_is_empty_without_a_store(tmp_path):
    """store=None (the default) degrades to 'no brick_class rule ever
    matches' rather than crashing - a deployment that hasn't wired in
    Oxigraph yet keeps working for topic_glob rules."""
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(json.dumps([
        {"id": "test-fd:no-store-rule", "mode": "cur", "type": "range", "applies_to": {"brick_class": "Zone_Air_Temperature_Sensor"}, "min": 60.0, "max": 80.0}
    ]))
    rule_cache = RuleCache(str(rules_file))

    rule_cache.get()

    assert rule_cache.rule_ids_for_point("urn:point:anything") == set()


@pytest.mark.integration
def test_cur_range_rule_opens_fault_and_fires_webhook(fault_conn, receiver, tmp_path):
    receiver_url, received = receiver
    rules_file = tmp_path / "rules.json"
    webhooks_file = tmp_path / "webhooks.json"
    rules_file.write_text(json.dumps([
        {"id": "test-fd:range", "mode": "cur", "type": "range", "applies_to": {"topic_glob": "test-fd/*"}, "min": 60.0, "max": 80.0}
    ]))
    webhooks_file.write_text(json.dumps([
        {"id": "test-fd:hook", "url": receiver_url, "secret": "shh", "filter": None}
    ]))

    rule_cache = RuleCache(str(rules_file))

    handle_cur_reading(fault_conn, rule_cache, str(webhooks_file), "test-fd/zone-temp", 95.0)

    faults = list_faults(fault_conn, rule_id="test-fd:range", status="open")
    assert len(faults) == 1
    assert faults[0]["point_uri"] == "urn:point:test-fd/zone-temp"

    for _ in range(20):
        if received:
            break
        time.sleep(0.1)
    assert len(received) == 1
    assert received[0]["event"] == "fault.opened"
    assert received[0]["rule_id"] == "test-fd:range"


@pytest.mark.integration
def test_cur_range_rule_resolves_and_fires_resolved_webhook(fault_conn, receiver, tmp_path):
    receiver_url, received = receiver
    rules_file = tmp_path / "rules.json"
    webhooks_file = tmp_path / "webhooks.json"
    rules_file.write_text(json.dumps([
        {"id": "test-fd:range2", "mode": "cur", "type": "range", "applies_to": {"topic_glob": "test-fd/*"}, "min": 60.0, "max": 80.0}
    ]))
    webhooks_file.write_text(json.dumps([
        {"id": "test-fd:hook2", "url": receiver_url, "secret": "shh", "filter": None}
    ]))
    rule_cache = RuleCache(str(rules_file))

    handle_cur_reading(fault_conn, rule_cache, str(webhooks_file), "test-fd/zone-temp-2", 95.0)  # opens
    handle_cur_reading(fault_conn, rule_cache, str(webhooks_file), "test-fd/zone-temp-2", 70.0)  # resolves

    assert list_faults(fault_conn, rule_id="test-fd:range2", status="open") == []
    resolved = list_faults(fault_conn, rule_id="test-fd:range2", status="resolved")
    assert len(resolved) == 1

    for _ in range(20):
        if len(received) >= 2:
            break
        time.sleep(0.1)
    # each fires in its own daemon thread by design (delivery must never
    # block evaluation) - order across the two separate events isn't
    # guaranteed, only that both eventually arrive
    events = {r["event"] for r in received}
    assert events == {"fault.opened", "fault.resolved"}


@pytest.mark.integration
def test_his_stuck_rule_opens_on_synthetic_unchanging_history(fault_conn, tmp_path):
    ts_conn = connect()
    point_uri = "urn:point:test-fd:stuck-point"
    ts_conn.execute("DELETE FROM point_history WHERE point_uri = %s", (point_uri,))
    now = datetime.now(timezone.utc)
    for i in range(10):
        write_point_value(ts_conn, point_uri, 42.0, now - timedelta(seconds=i * 10))

    webhooks_file = tmp_path / "webhooks.json"
    webhooks_file.write_text("[]")
    rules = [{"id": "test-fd:stuck", "mode": "his", "type": "stuck", "applies_to": {"topic_glob": "test-fd:stuck-point"}, "stuck_seconds": 120}]

    evaluate_his_rules(ts_conn, fault_conn, rules, str(webhooks_file), now=now)

    faults = list_faults(fault_conn, rule_id="test-fd:stuck", status="open")
    assert any(f["point_uri"] == point_uri for f in faults)


@pytest.mark.integration
def test_his_stuck_rule_does_not_fire_on_real_changing_fan_status(fault_conn, tmp_path):
    """Negative case against real data: the mock device's fan_status
    genuinely toggles every 5s - a stuck rule with a reasonable window
    must never false-positive on it."""
    ts_conn = connect()

    webhooks_file = tmp_path / "webhooks.json"
    webhooks_file.write_text("[]")
    rules = [{"id": "test-fd:stuck-fan", "mode": "his", "type": "stuck", "applies_to": {"topic_glob": "fbf/mock-ahu-1/binaryValue,1"}, "stuck_seconds": 20}]

    evaluate_his_rules(ts_conn, fault_conn, rules, str(webhooks_file))

    assert list_faults(fault_conn, rule_id="test-fd:stuck-fan", status="open") == []


@pytest.mark.integration
def test_cur_brick_class_rule_opens_fault_without_a_topic_glob(fault_conn, tmp_path):
    """The point-scoping story from the plan: a rule targets a Brick class,
    not a topic string, and still opens a fault - proving the point's topic
    (whatever building/naming scheme it came from) never has to appear in
    the rule itself."""
    from baler.ingest import ingest_tags
    from baler.mapping import classify_point
    from baler.remote_store import RemoteStore

    store = RemoteStore()
    topic = "test-fd:brick-cur-point"
    point_uri = ingest_tags(store, topic, {"zone": True, "air": True, "temp": True, "sensor": True})
    classify_point(store, point_uri)  # -> Zone_Air_Temperature_Sensor

    rules_file = tmp_path / "rules.json"
    webhooks_file = tmp_path / "webhooks.json"
    rules_file.write_text(json.dumps([
        {"id": "test-fd:brick-cur-rule", "mode": "cur", "type": "range", "applies_to": {"brick_class": "Zone_Air_Temperature_Sensor"}, "min": 60.0, "max": 80.0}
    ]))
    webhooks_file.write_text("[]")
    rule_cache = RuleCache(str(rules_file), store=store)

    handle_cur_reading(fault_conn, rule_cache, str(webhooks_file), topic, 95.0)

    faults = list_faults(fault_conn, rule_id="test-fd:brick-cur-rule", status="open")
    assert any(f["point_uri"] == str(point_uri) for f in faults)


@pytest.mark.integration
def test_his_stale_rule_opens_when_no_recent_reading(fault_conn, tmp_path):
    ts_conn = connect()
    point_uri = "urn:point:test-fd:stale-point"
    ts_conn.execute("DELETE FROM point_history WHERE point_uri = %s", (point_uri,))
    old_ts = datetime.now(timezone.utc) - timedelta(hours=2)
    write_point_value(ts_conn, point_uri, 1.0, old_ts)

    webhooks_file = tmp_path / "webhooks.json"
    webhooks_file.write_text("[]")
    rules = [{"id": "test-fd:stale", "mode": "his", "type": "stale", "applies_to": {"topic_glob": "test-fd:stale-point"}, "max_age_seconds": 3600}]

    evaluate_his_rules(ts_conn, fault_conn, rules, str(webhooks_file))

    faults = list_faults(fault_conn, rule_id="test-fd:stale", status="open")
    assert any(f["point_uri"] == point_uri for f in faults)

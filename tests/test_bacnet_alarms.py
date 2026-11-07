"""Baler#168: BACnet controller alarms forwarded by Windrow are raised and
cleared as faults, not stored as readings. Unit tests use a fake faults
table; the Postgres-backed version is marked integration."""

import json
from datetime import datetime, timezone

import pytest

from baler import bacnet_alarms

TOPIC = "fbf/ahu-1/zone-temp/alarm"
POINT = "urn:point:fbf/ahu-1/zone-temp"


def _event(to_state="high-limit", from_state="normal", **extra):
    return {"object": "analog-value,1", "event_type": "out-of-range", "from_state": from_state, "to_state": to_state,
            "notify_type": "alarm", "priority": 100, "message": "zone too hot", "ts": 1794077000.0, **extra}


def test_is_alarm_needs_both_the_topic_shape_and_an_event():
    assert bacnet_alarms.is_alarm(TOPIC, _event())
    assert bacnet_alarms.is_alarm("fbf/ahu-1/alarms/binary-input-7", {"object": "binary-input,7", "event_state": "offnormal"})
    assert not bacnet_alarms.is_alarm(TOPIC, 1.0)  # a Modbus point someone labeled "alarm"
    assert not bacnet_alarms.is_alarm(TOPIC, {"object": "x"})
    assert not bacnet_alarms.is_alarm("fbf/ahu-1/zone-temp", _event())


def test_point_uris():
    assert bacnet_alarms.alarm_point_uri(TOPIC) == POINT
    assert bacnet_alarms.alarm_point_uri("fbf/ahu-1/alarms/binary-input-7") == "urn:point:fbf/ahu-1/alarms/binary-input-7"


@pytest.mark.parametrize("event, expected", [
    (_event(to_state="fault"), "critical"),
    (_event(notify_type="event"), "info"),
    (_event(priority=10), "critical"),
    (_event(priority=200), "warning"),
    ({"object": "x", "event_state": "high-limit"}, "warning"),
])
def test_severity(event, expected):
    assert bacnet_alarms.severity(event) == expected


class FakeFaults:
    """open/close/update with faults.py's return conventions."""

    def __init__(self):
        self.open = {}
        self.closed = []
        self.next_id = 1

    def open_fault(self, conn, rule_id, point_uri, started_at, detail=None, severity="warning"):
        if (rule_id, point_uri) in self.open:
            return None
        self.open[(rule_id, point_uri)] = {"id": self.next_id, "started_at": started_at, "detail": detail, "severity": severity}
        self.next_id += 1
        return self.open[(rule_id, point_uri)]["id"]

    def close_fault(self, conn, rule_id, point_uri, ended_at):
        fault = self.open.pop((rule_id, point_uri), None)
        if fault is None:
            return None
        self.closed.append((fault, ended_at))
        return fault["id"]

    def update_open_fault(self, conn, rule_id, point_uri, detail, severity):
        fault = self.open.get((rule_id, point_uri))
        if fault is None or (fault["detail"], fault["severity"]) == (detail, severity):
            return False
        fault.update(detail=detail, severity=severity)
        return True


@pytest.fixture
def table(monkeypatch):
    fake = FakeFaults()
    for name in ("open_fault", "close_fault", "update_open_fault"):
        monkeypatch.setattr(bacnet_alarms.faults, name, getattr(fake, name))
    return fake


def test_raise_update_and_clear(table):
    fired = []
    fire = lambda *args: fired.append(args[:4])  # noqa: E731

    assert bacnet_alarms.handle_alarm(None, TOPIC, _event(), fire=fire) == "opened"
    fault = table.open[("bacnet:alarm", POINT)]
    assert fault["started_at"] == datetime.fromtimestamp(1794077000.0, tz=timezone.utc)
    assert fault["detail"]["to_state"] == "high-limit" and fault["severity"] == "warning"

    assert bacnet_alarms.handle_alarm(None, TOPIC, _event(), fire=fire) is None  # retained replay: no change
    assert bacnet_alarms.handle_alarm(None, TOPIC, _event(to_state="low-limit", from_state="high-limit"), fire=fire) == "updated"
    assert table.open[("bacnet:alarm", POINT)]["detail"]["to_state"] == "low-limit"

    assert bacnet_alarms.handle_alarm(None, TOPIC, _event(to_state="normal", from_state="low-limit"), fire=fire) == "resolved"
    assert table.open == {} and len(table.closed) == 1
    assert bacnet_alarms.handle_alarm(None, TOPIC, _event(to_state="normal"), fire=fire) is None
    assert fired == [("fault.opened", "bacnet:alarm", POINT, "open"), ("fault.resolved", "bacnet:alarm", POINT, "resolved")]


def test_a_startup_summary_opens_and_a_normal_one_clears(table):
    summary = {"object": "analog-value,1", "event_state": "high-limit", "source": "summary", "ts": 1794077000.0}
    assert bacnet_alarms.handle_alarm(None, TOPIC, summary) == "opened"
    assert bacnet_alarms.handle_alarm(None, TOPIC, {**summary, "event_state": "normal"}) == "resolved"


def test_mqtt_listener_does_not_store_an_alarm_as_a_reading(monkeypatch):
    from baler import mqtt_listener

    stored = []
    monkeypatch.setattr(mqtt_listener, "ingest_reading", lambda *a: stored.append(a))
    on_message = mqtt_listener.make_on_message(store=None, ts_conn=None)

    class Msg:
        def __init__(self, topic, value):
            self.topic = topic
            self.payload = json.dumps({"point": "x", "value": value, "ts": 1.0}).encode()

    on_message(None, None, Msg(TOPIC, _event()))
    assert stored == []
    on_message(None, None, Msg("fbf/m/alarm", 1))  # a plain reading on a point named "alarm" still lands
    assert len(stored) == 1


def test_fault_detector_routes_alarms_and_not_through_the_rules(monkeypatch):
    from baler import fault_detector

    handled, evaluated, webhooks = [], [], []
    monkeypatch.setattr(fault_detector.bacnet_alarms, "handle_alarm",
                        lambda conn, topic, value, ts, fire: (handled.append(topic), fire("fault.opened", "bacnet:alarm", POINT, "open", value)))
    monkeypatch.setattr(fault_detector, "handle_cur_reading", lambda *a: evaluated.append(a[3]))
    monkeypatch.setattr(fault_detector, "_fire_webhooks", lambda path, *a: webhooks.append(a[:4]))
    on_message = fault_detector.make_on_message(None, None, "webhooks.json")

    class Msg:
        def __init__(self, topic, value):
            self.topic = topic
            self.payload = json.dumps({"value": value, "ts": 1.0}).encode()

    on_message(None, None, Msg(TOPIC, _event()))
    on_message(None, None, Msg("fbf/ahu-1/zone-temp", 71.0))
    assert handled == [TOPIC] and evaluated == ["fbf/ahu-1/zone-temp"]
    assert webhooks == [("fault.opened", "bacnet:alarm", POINT, "open")]


@pytest.mark.integration
def test_alarm_faults_in_postgres_and_the_orphan_sweep_leaves_them():
    from baler.faults import close_orphaned_faults, ensure_schema, list_faults
    from baler.timeseries import connect

    conn = connect()
    ensure_schema(conn)
    topic = "fbf/test-bacnet-alarm/zone-temp/alarm"
    point = "urn:point:fbf/test-bacnet-alarm/zone-temp"
    conn.execute("DELETE FROM faults WHERE point_uri = %s", (point,))

    assert bacnet_alarms.handle_alarm(conn, topic, _event(priority=5)) == "opened"
    assert close_orphaned_faults(conn, {"some-rule"}, datetime.now(timezone.utc)) >= 0
    (fault,) = list_faults(conn, status="open", point_uri=point)
    assert fault["rule_id"] == "bacnet:alarm" and fault["severity"] == "critical"
    assert fault["detail"]["message"] == "zone too hot"

    assert bacnet_alarms.handle_alarm(conn, topic, _event(to_state="low-limit", priority=5)) == "updated"
    assert list_faults(conn, status="open", point_uri=point)[0]["detail"]["to_state"] == "low-limit"
    assert bacnet_alarms.handle_alarm(conn, topic, _event(to_state="normal")) == "resolved"
    assert list_faults(conn, status="open", point_uri=point) == []
    assert list_faults(conn, status="resolved", point_uri=point)[0]["ended_at"] is not None

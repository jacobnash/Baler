"""
Unit test for mqtt_listener.py's reconnect fix: on_connect must re-issue
the subscribe on every call, not just the first one - a fresh (non
clean_session) broker session drops the old subscription on every
reconnect, and there's no other code path that restores it.
"""

import json

import pytest
from rdflib import RDF, URIRef

from baler.mqtt_listener import make_on_connect
from baler.store import BRICK, PROJ, TD, Store


class FakeClient:
    def __init__(self):
        self.subscribe_calls = []

    def subscribe(self, topic, qos=0):
        self.subscribe_calls.append((topic, qos))


def test_on_connect_subscribes():
    client = FakeClient()
    on_connect = make_on_connect("fbf/#")

    on_connect(client, None, {}, 0, None)

    assert client.subscribe_calls == [("fbf/#", 1)]


def test_on_connect_resubscribes_on_every_call():
    client = FakeClient()
    on_connect = make_on_connect("fbf/#")

    on_connect(client, None, {}, 0, None)  # first connect
    on_connect(client, None, {}, 0, None)  # simulated reconnect

    assert client.subscribe_calls == [("fbf/#", 1), ("fbf/#", 1)]


class _Msg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload


@pytest.mark.parametrize("payload", [b"1", b"[1, 2]", b'"text"', b"null", b"not json", b"\xff\xfe"])
def test_a_malformed_message_is_skipped_not_fatal(payload):
    """A crash here crash-loops the listener: its persistent session
    redelivers the same message after every restart (CI, 2026-11-06)."""
    from baler.mqtt_listener import make_on_message

    on_message = make_on_message(store=None, ts_conn=None)
    on_message(None, None, _Msg("fbf/site/ahu/sat", payload))  # must not raise


def test_a_data_error_while_handling_is_skipped(monkeypatch):
    from baler import mqtt_listener

    def boom(*a, **k):
        raise ValueError("bad value")

    monkeypatch.setattr(mqtt_listener, "ingest_reading", boom, raising=False)
    on_message = mqtt_listener.make_on_message(store=None, ts_conn=None)
    on_message(None, None, _Msg("fbf/site/ahu/sat", b'{"value": 1, "ts": 1}'))  # logged, not raised


# --- what a Windrow edge node publishes (Baler#81, #82) ---------------------


def _envelope(value) -> bytes:
    return json.dumps({"value": value, "ts": 1_800_000_000}).encode()


def _types(store, uri) -> set[str]:
    return {str(o) for o in store.graph.objects(URIRef(uri), RDF.type)}


def test_bacnet_binary_states_are_stored_as_bools(monkeypatch):
    """A BACnet binary-value arrives as "active"/"inactive" (#81): stored as
    a Bool it charts, rolls up and counts like Modbus's 0/1 does."""
    from baler import mqtt_listener

    written = []
    monkeypatch.setattr(mqtt_listener, "ingest_reading", lambda store, conn, topic, value, ts: written.append(value))
    on_message = mqtt_listener.make_on_message(store=None, ts_conn=None)
    for v in ("active", "inactive", "standby", 71.2, 1):
        on_message(None, None, _Msg("fbf/home/bacnet/fan_status", _envelope(v)))
    assert written == [True, False, "standby", 71.2, 1]


def _listener(store):
    from baler import mqtt_listener

    return mqtt_listener.make_on_message(store=store, ts_conn=None)


def test_edge_point_and_equip_tags_are_classified_on_arrival():
    """#82: tags over MQTT used to wait for a manual autotag run."""
    store = Store()
    on_message = _listener(store)
    on_message(None, None, _Msg("fbf/home/weather/equip/tags", _envelope(json.dumps(
        {"tags": {"weatherStation": True, "dis": "Pi weather"}, "points": ["temp"]}))))
    on_message(None, None, _Msg("fbf/home/bacnet/zone_temp/tags", _envelope(json.dumps(
        {"zone": True, "air": True, "temp": True, "sensor": True, "unit": "°F"}))))
    on_message(None, None, _Msg("fbf/home/bacnet/fan_status/tags", _envelope(json.dumps(
        {"fan": True, "run": True, "sensor": True}))))
    assert str(BRICK.Weather_Station) in _types(store, "urn:equip:fbf/home/weather")
    assert str(BRICK.Zone_Air_Temperature_Sensor) in _types(store, "urn:point:fbf/home/bacnet/zone_temp")
    assert str(BRICK.Fan_Status) in _types(store, "urn:point:fbf/home/bacnet/fan_status")


def test_no_markers_means_no_class_and_no_fallback_guess():
    store = Store()
    on_message = _listener(store)
    on_message(None, None, _Msg("fbf/home/bacnet/equip/tags", _envelope(json.dumps({"tags": {}, "points": ["x"]}))))
    on_message(None, None, _Msg("fbf/home/bacnet/x/tags", _envelope(json.dumps({"zone": True}))))
    assert _types(store, "urn:equip:fbf/home/bacnet") == set()
    assert _types(store, "urn:point:fbf/home/bacnet/x") == set()


def test_a_class_set_by_hand_is_kept_but_an_autotag_fallback_is_replaced():
    store = Store()
    hand, guessed = "urn:point:fbf/home/a/zone_temp", "urn:point:fbf/home/b/zone_temp"
    store.add_entity(URIRef(hand), BRICK.Temperature_Sensor)
    store.add_entity(URIRef(guessed), PROJ.Zone_Air_Temperature_Sensor_Variant)
    store.add_entity(URIRef(guessed), TD.UnmappedPoint)
    on_message = _listener(store)
    tags = _envelope(json.dumps({"zone": True, "air": True, "temp": True, "sensor": True}))
    on_message(None, None, _Msg("fbf/home/a/zone_temp/tags", tags))
    on_message(None, None, _Msg("fbf/home/b/zone_temp/tags", tags))
    assert _types(store, hand) == {str(BRICK.Temperature_Sensor)}
    assert _types(store, guessed) == {str(BRICK.Zone_Air_Temperature_Sensor)}


# --- ReadingBatcher (Baler#190): readings in batches, acked only once stored


class AckClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.acks = []

    def ack(self, mid, qos):
        self.acks.append((mid, qos))


class _QMsg(_Msg):
    def __init__(self, topic, payload, mid, qos=1):
        super().__init__(topic, payload)
        self.mid, self.qos = mid, qos


def _batcher(monkeypatch, written):
    from baler import mqtt_listener

    monkeypatch.setattr(mqtt_listener, "ingest_readings", lambda store, conn, rows: written.append(list(rows)))
    client = AckClient()
    return client, mqtt_listener.ReadingBatcher(client, store=None, ts_conn=None, max_rows=3, max_wait=0.01)


def test_readings_are_queued_and_acked_only_after_their_batch_is_stored(monkeypatch):
    from baler.mqtt_listener import make_on_message

    written: list = []
    client, batcher = _batcher(monkeypatch, written)
    on_message = make_on_message(None, None, batcher)
    for mid, value in ((1, 70.5), (2, "active"), (3, 71.0)):
        on_message(client, None, _QMsg(f"fbf/s/ahu/p{mid}", json.dumps({"point": "p", "value": value, "ts": 10.0 + mid}).encode(), mid))
    assert client.acks == [] and written == []  # queued, nothing acknowledged yet
    batcher.flush([batcher.queue.get_nowait() for _ in range(3)])
    assert written == [[("fbf/s/ahu/p1", 70.5, 11.0), ("fbf/s/ahu/p2", True, 12.0), ("fbf/s/ahu/p3", 71.0, 13.0)]]
    assert client.acks == [(1, 1), (2, 1), (3, 1)]


def test_a_failed_write_acknowledges_nothing(monkeypatch):
    from baler import mqtt_listener

    client = AckClient()
    batcher = mqtt_listener.ReadingBatcher(client, store=None, ts_conn=None)

    def boom(*a):
        raise RuntimeError("postgres down")

    monkeypatch.setattr(mqtt_listener, "ingest_readings", boom)
    batcher.put(_QMsg("fbf/s/a/p", b"", 7), "fbf/s/a/p", 1.0, 1.0)
    with pytest.raises(RuntimeError):
        batcher.flush([batcher.queue.get_nowait()])
    assert client.acks == []  # the broker keeps it and redelivers after the restart


def test_ids_from_before_a_reconnect_are_not_acked(monkeypatch):
    """Message ids are per connection and get reused; the broker resends
    whatever was unacknowledged when the connection dropped."""
    written: list = []
    client, batcher = _batcher(monkeypatch, written)
    on_connect = make_on_connect("fbf/#", batcher)
    on_connect(client, None, {}, 0, None)
    batcher.put(_QMsg("fbf/s/a/p", b"", 5), "fbf/s/a/p", 1.0, 1.0)
    on_connect(client, None, {}, 0, None)  # reconnect before the flush
    batcher.flush([batcher.queue.get_nowait()])
    assert written and client.acks == []


def test_non_readings_are_handled_inline_and_acked(monkeypatch):
    from baler import mqtt_listener

    written: list = []
    client, batcher = _batcher(monkeypatch, written)
    tagged = []
    monkeypatch.setattr(mqtt_listener, "ingest_tags", lambda store, topic, tags, ts: tagged.append(topic) or URIRef("urn:p"))
    monkeypatch.setattr(mqtt_listener, "link_equip_ref", lambda *a: None)
    monkeypatch.setattr(mqtt_listener, "_classify", lambda *a: None)
    on_message = mqtt_listener.make_on_message(None, None, batcher)
    on_message(client, None, _QMsg("fbf/s/a/p/tags", json.dumps({"value": json.dumps({"point": True})}).encode(), 9))
    on_message(client, None, _QMsg("fbf/s/a/p", b"not json", 10))  # skipped, but acked: never redelivered forever
    assert tagged == ["fbf/s/a/p"] and client.acks == [(9, 1), (10, 1)] and batcher.queue.empty()

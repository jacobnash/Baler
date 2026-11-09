"""
Per-sample quality flags (#183). The pure checks need nothing; the
storage, sweep and consumer tests need docker-compose's Postgres.
"""

from datetime import datetime, timedelta, timezone

import pytest

from baler import fault_detector, quality
from baler.store import BRICK, Store

# --- the envelope -------------------------------------------------------------


@pytest.mark.parametrize("raw,flag", [
    (None, None),
    ("ok", None),
    ("fault", "fault"),
    ("Out-Of-Service", "out_of_service"),
    ("nonsense", None),
    (42, None),
    ({"status_flags": ["in-alarm"]}, None),  # an alarm isn't bad data
    ({"status_flags": ["fault"]}, "fault"),
    ({"status_flags": ["overridden"]}, "overridden"),
    ({"status_flags": ["overridden", "out-of-service"]}, "out_of_service"),
    ({"statusFlags": {"fault": False, "overridden": True}}, "overridden"),
    ({"status_flags": [], "reliability": "no-fault-detected"}, None),
    ({"status_flags": [], "reliability": "open-loop"}, "fault"),
])
def test_from_envelope(raw, flag):
    assert quality.from_envelope(raw) == flag


# --- range ------------------------------------------------------------------


def test_range_is_by_class_and_unit_most_specific_first():
    assert quality.range_flag("Zone_Air_Temperature_Sensor", "°F", 180.0) == "out_of_range"
    assert quality.range_flag("Zone_Air_Temperature_Sensor", "°F", 72.0) is None
    assert quality.range_flag("Hot_Water_Supply_Temperature_Sensor", "°F", 180.0) is None  # fine for hot water
    assert quality.range_flag("Damper_Position_Sensor", "%", 140.0) == "out_of_range"
    assert quality.range_flag("Zone_Air_Humidity_Sensor", "%RH", -3.0) == "out_of_range"


def test_range_needs_a_known_class_unit_and_number():
    assert quality.range_flag(None, "°F", 900.0) is None
    assert quality.range_flag("Zone_Air_Temperature_Sensor", None, 900.0) is None
    assert quality.range_flag("Electric_Power_Sensor", "kW", -900.0) is None  # no range for power
    assert quality.range_flag("Zone_Air_Temperature_Sensor", "°F", True) is None
    assert quality.range_flag("Zone_Air_Temperature_Sensor", "°F", "active") is None


# --- spike ------------------------------------------------------------------


def test_an_isolated_jump_is_a_spike():
    values = [72.0, 72.1, 71.9, 72.0, 72.2, 72.1, 140.0, 72.0, 71.8, 72.1, 72.0, 72.1, 71.9]
    assert quality.spike_indexes(values) == [6]


def test_a_step_change_is_not_a_spike():
    values = [55.0] * 8 + [65.0] * 8
    assert quality.spike_indexes(values) == []


def test_a_flat_series_with_rounding_noise_has_no_spikes():
    values = [72.0, 72.0, 72.0, 72.01, 72.0, 72.0, 72.0, 71.99, 72.0, 72.0, 72.0, 72.0]
    assert quality.spike_indexes(values) == []


def test_samples_without_a_full_window_wait():
    values = [72.0] * 10 + [140.0]  # the newest, no neighbours after it yet
    assert quality.spike_indexes(values) == []


# --- unit -------------------------------------------------------------------


def test_celsius_values_labelled_fahrenheit_get_a_suggestion():
    s = quality.unit_suspicion("Zone_Air_Temperature_Sensor", "°F", [21.0, 21.5, 22.0, 22.4, 21.8] * 3)
    assert s is not None and s["suggested"] == "°C"


def test_fahrenheit_values_labelled_fahrenheit_get_none():
    assert quality.unit_suspicion("Zone_Air_Temperature_Sensor", "°F", [71.0, 72.5, 70.8] * 5) is None


def test_fahrenheit_values_labelled_celsius_get_a_suggestion():
    s = quality.unit_suspicion("Zone_Air_Temperature_Sensor", "°C", [71.0, 72.5, 70.8] * 5)
    assert s is not None and s["suggested"] == "°F"


def test_too_few_samples_get_no_suggestion():
    assert quality.unit_suspicion("Zone_Air_Temperature_Sensor", "°F", [21.0, 22.0]) is None


# --- judge ------------------------------------------------------------------

T0 = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


def _samples(values, flags=None):
    flags = flags or {}
    return [(T0 + timedelta(minutes=i), v, flags.get(i)) for i, v in enumerate(values)]


def test_judge_flags_range_and_spike_and_never_overrides_a_device_flag():
    values = [72.0, 72.1, 71.9, 72.0, 72.2, 72.1, 95.0, 72.0, 71.8, 72.1, 72.0, 72.1, 180.0, 72.0]
    flags, suggestion = quality.judge("Zone_Air_Temperature_Sensor", "°F", _samples(values, {3: "overridden"}))
    assert suggestion is None
    assert flags[T0 + timedelta(minutes=12)] == "out_of_range"
    assert flags[T0 + timedelta(minutes=6)] == "spike"
    assert T0 + timedelta(minutes=3) not in flags  # the device said overridden; that stands


def test_judge_marks_unit_suspect_samples():
    flags, suggestion = quality.judge("Zone_Air_Temperature_Sensor", "°F", _samples([21.0, 21.5, 22.0, 21.8, 22.1] * 3))
    assert suggestion["suggested"] == "°C"
    assert set(flags.values()) == {"unit_suspect"} and len(flags) == 15


def test_spike_indexes_below_start_are_neighbours_only():
    values = [72.0, 72.1, 71.9, 72.0, 72.2, 72.1, 140.0, 72.0, 71.8, 72.1, 72.0, 72.1, 71.9]
    assert quality.spike_indexes(values, start=6) == [6]
    assert quality.spike_indexes(values, start=7) == []


def test_spike_indexes_match_the_statistics_median_reference():
    import random
    import statistics

    def reference(values, half=5, k=6.0):
        out = []
        for i in range(half, len(values) - half):
            nb = values[i - half:i] + values[i + 1:i + 1 + half]
            med = statistics.median(nb)
            mad = statistics.median(abs(v - med) for v in nb)
            if abs(values[i] - med) > k * 1.4826 * max(mad, max(abs(med) * 0.001, 1e-6)):
                out.append(i)
        return out

    rng = random.Random(268)
    for _ in range(50):
        values = [72.0 + rng.gauss(0, rng.choice([0.0, 0.01, 0.5])) for _ in range(200)]
        for i in rng.sample(range(200), 5):
            values[i] += rng.choice([-1, 1]) * rng.uniform(0, 30)
        assert quality.spike_indexes(values) == reference(values)


def test_context_lets_the_previous_pass_s_unjudged_tail_be_judged_now():
    # The previous pass ended at a spike with too few right-hand neighbours;
    # this pass gets it back as context and judges it, without re-judging
    # the older context.
    values = [72.0, 72.1, 71.9, 72.0, 72.2, 72.1, 72.0, 72.1, 71.9, 140.0]
    older = [(T0 + timedelta(minutes=i), v) for i, v in enumerate(values)]
    new = [(T0 + timedelta(minutes=10 + i), v, None) for i, v in enumerate([72.0, 71.8, 72.1, 72.0, 72.1, 71.9])]
    flags, _ = quality.judge("Zone_Air_Temperature_Sensor", "°F", new, older)
    assert flags == {T0 + timedelta(minutes=9): "spike"}
    # an old spike deeper in the context (already judged last pass) isn't flagged again here
    older[2] = (older[2][0], 140.0)
    flags, _ = quality.judge("Zone_Air_Temperature_Sensor", "°F", new, older)
    assert T0 + timedelta(minutes=2) not in flags


def test_judge_only_flags_just_the_late_rows():
    values = [72.0, 72.1, 71.9, 72.0, 72.2, 72.1, 95.0, 72.0, 71.8, 72.1, 72.0, 72.1, 180.0, 72.0]
    samples = _samples(values)
    flags, _ = quality.judge("Zone_Air_Temperature_Sensor", "°F", samples, only={T0 + timedelta(minutes=6)})
    assert flags == {T0 + timedelta(minutes=6): "spike"}  # the 180 isn't a late row: someone else's job


def test_cur_mode_ignores_a_reading_the_device_flags(monkeypatch):
    seen = []
    monkeypatch.setattr(fault_detector, "handle_cur_reading", lambda *a: seen.append(a[3]))
    on_message = fault_detector.make_on_message(None, None, "unused.json")

    class Msg:
        def __init__(self, topic, payload):
            self.topic, self.payload = topic, payload.encode()

    on_message(None, None, Msg("fbf/vav-1/ZN-T", '{"value": 140.0, "quality": {"status_flags": ["fault"]}}'))
    on_message(None, None, Msg("fbf/vav-1/ZN-T", '{"value": 72.0, "quality": "ok"}'))
    on_message(None, None, Msg("fbf/vav-1/ZN-T", '{"value": 71.0}'))
    assert seen == ["fbf/vav-1/ZN-T", "fbf/vav-1/ZN-T"]


def test_point_meta_reads_class_and_unit_from_the_graph():
    from rdflib import Literal, URIRef

    store = Store()
    uri = URIRef("urn:point:fbf/vav-1/ZN-T")
    store.add_entity(uri, BRICK.Zone_Air_Temperature_Sensor)
    store.add_relationship(uri, URIRef("urn:baler:haystack#unit"), Literal("°F"))
    store.add_relationship(URIRef("urn:point:fbf/vav-1/NO-UNIT"), URIRef("urn:baler:haystack#unit"), Literal("°F"))
    assert quality.point_meta(store) == {str(uri): ("Zone_Air_Temperature_Sensor", "°F")}


# --- against Postgres ---------------------------------------------------------


@pytest.fixture
def conn():
    from baler import timeseries

    c = timeseries.connect()
    quality.ensure_schema(c)
    c.execute("DELETE FROM point_history WHERE point_uri LIKE %s", ("urn:test-quality:%",))
    c.execute("DELETE FROM quality_suggestions WHERE point_uri LIKE %s", ("urn:test-quality:%",))
    yield c
    c.execute("DELETE FROM point_history WHERE point_uri LIKE %s", ("urn:test-quality:%",))
    c.execute("DELETE FROM quality_suggestions WHERE point_uri LIKE %s", ("urn:test-quality:%",))
    c.close()


@pytest.mark.integration
def test_a_flag_is_stored_beside_the_value_and_read_back(conn):
    from baler import timeseries

    now = datetime.now(timezone.utc).replace(microsecond=0)
    timeseries.write_point_value(conn, "urn:test-quality:a", 140.0, now - timedelta(seconds=2), quality="fault")
    timeseries.write_point_value(conn, "urn:test-quality:a", 72.0, now - timedelta(seconds=1))
    rows = timeseries.read_range(conn, "urn:test-quality:a", now - timedelta(minutes=1), now, with_quality=True)
    assert [(v, q) for _, v, q in rows] == [(140.0, "fault"), (72.0, None)]  # the raw value is kept
    good = timeseries.read_range(conn, "urn:test-quality:a", now - timedelta(minutes=1), now, good_only=True)
    assert [v for _, v in good] == [72.0]
    latest = timeseries.read_latest_many(conn, ["urn:test-quality:a"], good_only=True)
    assert latest["urn:test-quality:a"][1] == 72.0
    many = timeseries.read_range_many(conn, ["urn:test-quality:a"], now - timedelta(minutes=1), now + timedelta(seconds=1), 10, good_only=True)
    assert [v for _, v in many["urn:test-quality:a"][0]] == [72.0]


@pytest.mark.integration
def test_sweep_flags_without_touching_values_and_suggests_a_unit(conn):
    from baler import timeseries

    now = datetime.now(timezone.utc).replace(microsecond=0)
    zone = [72.0, 72.1, 71.9, 72.0, 72.2, 72.1, 95.0, 72.0, 71.8, 72.1, 72.0, 72.1, 180.0, 72.0]
    for i, v in enumerate(zone):
        timeseries.write_point_value(conn, "urn:test-quality:zone", v, now - timedelta(minutes=len(zone) - i))
    for i in range(12):
        timeseries.write_point_value(conn, "urn:test-quality:celsius", 21.0 + i * 0.1, now - timedelta(minutes=12 - i))
    meta = {"urn:test-quality:zone": ("Zone_Air_Temperature_Sensor", "°F"),
            "urn:test-quality:celsius": ("Zone_Air_Temperature_Sensor", "°F")}

    counts = quality.sweep(conn, store=None, now=now, meta=meta)

    rows = timeseries.read_range(conn, "urn:test-quality:zone", now - timedelta(hours=1), now, with_quality=True)
    assert [v for _, v, _ in rows] == zone
    assert {v: q for _, v, q in rows if q} == {95.0: "spike", 180.0: "out_of_range"}
    assert counts["suggestions"] == 1
    [s] = [s for s in quality.open_suggestions(conn) if s["point_uri"] == "urn:test-quality:celsius"]
    assert (s["current"], s["suggested"]) == ("°F", "°C")

    again = quality.sweep(conn, store=None, now=now, meta=meta)  # idempotent: nothing new to flag
    assert again["flagged"] == 0
    assert len([s for s in quality.open_suggestions(conn) if s["point_uri"] == "urn:test-quality:celsius"]) == 1


@pytest.mark.integration
def test_stuck_ignores_flagged_samples(conn):
    from baler import timeseries

    now = datetime.now(timezone.utc).replace(microsecond=0)
    for i in range(5):
        timeseries.write_point_value(conn, "urn:test-quality:stuck", 72.0, now - timedelta(seconds=50 - i))
    timeseries.write_point_value(conn, "urn:test-quality:stuck", 99.0, now - timedelta(seconds=10), quality="spike")
    # one distinct good value in the window: stuck, the flagged 99 doesn't count as movement
    assert fault_detector._evaluate_stuck(conn, "urn:test-quality:stuck", 300, now) is True


def test_compact_his_carries_a_quality_array_only_when_something_is_flagged():
    from baler import hisquery

    clean = {"series": [{"history": [{"ts": 1.0, "value": 72.0}, {"ts": 2.0, "value": 72.1}]}]}
    assert "quality" not in hisquery.compact_series(clean)["series"][0]["history"]
    flagged = {"series": [{"history": [{"ts": 1.0, "value": 72.0}, {"ts": 2.0, "value": 140.0, "quality": "spike"}]}]}
    assert hisquery.compact_series(flagged)["series"][0]["history"]["quality"] == [None, "spike"]


def test_the_mqtt_listener_stores_the_envelope_s_quality(monkeypatch):
    from baler import mqtt_listener

    got = []
    monkeypatch.setattr(mqtt_listener, "ingest_reading", lambda store, conn, topic, value, ts, quality=None: got.append((value, quality)))
    on_message = mqtt_listener.make_on_message(store=None, ts_conn=None)

    class Msg:
        def __init__(self, payload):
            self.topic, self.payload = "fbf/vav-1/ZN-T", payload.encode()

    on_message(None, None, Msg('{"value": 140.0, "ts": 1800000000, "quality": {"status_flags": ["overridden"]}}'))
    on_message(None, None, Msg('{"value": 72.0, "ts": 1800000001}'))
    assert got == [(140.0, "overridden"), (72.0, None)]  # the value is stored as sent either way

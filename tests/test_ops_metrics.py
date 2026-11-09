"""GET /ops/metrics (#235): Prometheus text of the overload signals."""

from datetime import datetime, timedelta, timezone

from baler import ops_metrics
from baler.scope import Scope

NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)


def _snap(**over):
    snap = {
        "now": NOW,
        "newest_reading_at": NOW - timedelta(seconds=2.5),
        "prefixes": [("fbf/home", NOW - timedelta(seconds=20), 766), ("fbf/old", NOW - timedelta(hours=2), 3)],
        "heartbeats": {"mqtt_listener": NOW - timedelta(seconds=4), "fault_detector": NOW - timedelta(seconds=31)},
        "details": {
            "mqtt_listener": {"workers": 2, "writers": 2, "restarts": {"worker-1": 3}, "queue": 120, "queue_peak": 900,
                              "queue_max": 50000, "oldest_age_s": 0.42, "last_flush_s": 0.031, "flush_s": 12.5,
                              "blocked_s": 0.0, "received": 1000, "rows": 990, "batches": 40, "redelivered": 7,
                              "readings_per_s": 200.0, "messages_per_s": 202.0, "batches_per_s": 4.0,
                              "broker_dropped_total": 15, "broker_dropped_since_start": 5,
                              "broker_last_drop_at": (NOW - timedelta(seconds=60)).isoformat()},
            "fault_detector": {"his": {"rules": 4, "points_faulted": 2, "sweep_s": 1.25},
                               "quality": {"sweep_s": 3.5}, "cur_evaluations": 1234},
            "derivation_engine": {"tick": {"cycle_s": 0.2, "derivations_due": 3}, "cur_rows": 55},
        },
        "feed": {"fault_detector": {"lag_s": 1.5, "lag_readings": 300, "missed_readings": 0}},
    }
    snap.update(over)
    return snap


def test_render_is_prometheus_text_with_every_overload_signal():
    text = ops_metrics.render(_snap())
    m = ops_metrics.parse(text)
    assert m["baler_ingest_newest_reading_age_seconds"] == 2.5
    assert m['baler_ingest_prefix_newest_reading_age_seconds{prefix="fbf/home"}'] == 20.0
    assert m['baler_ingest_prefix_points{prefix="fbf/old"}'] == 3
    assert m["baler_listener_queue_depth"] == 120
    assert m["baler_listener_queue_oldest_age_seconds"] == 0.42
    assert m["baler_listener_redelivered_total"] == 7
    assert m["baler_listener_restarts_total"] == 3
    assert m["baler_broker_dropped_messages_total"] == 15
    assert m["baler_broker_last_drop_age_seconds"] == 60.0
    assert m['baler_reading_feed_lag_seconds{consumer="fault_detector"}'] == 1.5
    assert m['baler_rules_cycle_seconds{service="fault_detector",cycle="his"}'] == 1.25
    assert m['baler_rules_cycle_seconds{service="derivation_engine",cycle="scheduled"}'] == 0.2
    assert m["baler_fault_cur_evaluations_total"] == 1234
    assert m['baler_heartbeat_age_seconds{service="fault_detector"}'] == 31.0
    # one HELP/TYPE per family, before its first sample
    assert text.count("# TYPE baler_rules_cycle_seconds gauge") == 1
    lines = text.splitlines()
    assert lines.index("# TYPE baler_listener_queue_depth gauge") < lines.index("baler_listener_queue_depth 120")
    assert text.endswith("\n")


def test_an_empty_install_renders_without_errors():
    text = ops_metrics.render({"now": NOW})
    assert text == "\n" or "baler_listener" not in text


def test_the_stalest_prefixes_come_first_and_are_capped(monkeypatch):
    monkeypatch.setattr(ops_metrics, "MAX_PREFIXES", 1)
    m = ops_metrics.parse(ops_metrics.render(_snap()))
    assert 'baler_ingest_prefix_points{prefix="fbf/old"}' in m and 'baler_ingest_prefix_points{prefix="fbf/home"}' not in m


def test_label_values_are_escaped():
    text = ops_metrics.render(_snap(prefixes=[('fbf/a"b', NOW, 1)]))
    assert 'prefix="fbf/a\\"b"' in text


class _Conn:
    def __init__(self):
        self.sql = []

    def execute(self, sql, *a):
        self.sql.append(sql)
        conn = self

        class R:
            def fetchone(self):
                return (NOW - timedelta(seconds=1),)

            def fetchall(self):
                if "point_registry" in sql:
                    return [("fbf/home", NOW, "urn:point:fbf/home/a", 3), ("fbf/other", NOW, "urn:point:fbf/other/a", 1)]
                if "service_heartbeats" in sql:
                    return []
                return []

        conn.last = R()
        return conn.last

    def rollback(self):
        pass


def test_collect_limits_prefixes_to_the_callers_sites_and_caches_the_scan():
    ops_metrics.prefix_cache.clear()
    conn = _Conn()
    snap = ops_metrics.collect(conn, Scope(prefixes=("fbf/home",)), now=NOW)
    assert [p for p, _, _ in snap["prefixes"]] == ["fbf/home"]
    assert snap["newest_reading_at"] == NOW - timedelta(seconds=1)
    ops_metrics.collect(conn, None, now=NOW)
    assert sum("point_registry" in s for s in conn.sql) == 1  # the second call used the cache
    ops_metrics.prefix_cache.clear()

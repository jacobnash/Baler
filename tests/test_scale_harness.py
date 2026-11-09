"""
scripts/scale/ (Baler#190): the load generator speaks Windrow's exact
envelope and the listener routes each message kind where it should, the
isolation guard refuses anything that looks like live, and the ladder's
pass conditions say pass/fail/inconclusive for the right reasons. No
services: the real-broker runs are scripts/scale/ladder.py itself.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCALE = Path(__file__).resolve().parent.parent / "scripts" / "scale"
sys.path.insert(0, str(SCALE))
import common
import ladder
import loadgen

from baler import mqtt_listener


def test_envelope_is_windrows_bytes():
    # windrow/mqtt_sink._publish: json.dumps({"point", "value", "ts"})
    assert common.envelope("sat", 71.5, 1.5) == b'{"point": "sat", "value": 71.5, "ts": 1.5}'


def test_shape_counts_and_topics():
    shape = common.Shape(1005, points_per_equip=10, equips_per_site=50)
    assert (shape.equips, shape.sites) == (101, 3)
    assert shape.equip_prefix(51) == "fbf/scale/s00001/e00051"  # global equip index (#273)
    last = shape.equip_points(100)
    assert [p[0] for p in last] == [1000, 1001, 1002, 1003, 1004]  # partial last equip
    assert sum(len(shape.equip_points(g)) for g in range(shape.equips)) == 1005
    labels = [p[1] for p in common.Shape(25, points_per_equip=25).equip_points(0)]
    assert len(set(labels)) == 25  # templates repeat with a suffix, never a duplicate topic


def test_his_group_markers():
    shape = common.Shape(2000)
    t = common.TEMPLATES[0]
    assert {k for k in common.point_tags(shape, 0, 0, "sat", t) if k.startswith("scaleHis")} == {
        "scaleHis1", "scaleHis100", "scaleHis1000"}
    assert "scaleHis100" not in common.point_tags(shape, 10, 100, "sat", t)
    assert "scaleHis1000" in common.point_tags(shape, 99, 999, "sat", t)


def test_values_match_point_kind():
    for pattern in common.PATTERNS[1:]:
        v = common.value_at(pattern, 3, 7)
        assert isinstance(v, bool) if pattern == "bool" else isinstance(v, float)


def test_generator_messages_take_the_listeners_real_paths(monkeypatch):
    """The generator's equip, tags and reading messages land where Windrow's
    do: the equip links its points, a point's tags are stored, a reading
    goes to history (here without a batcher, so all inline)."""
    from rdflib import URIRef

    from baler.store import BRICK, HAYSTACK, Store

    shape = common.Shape(10)
    prefix = shape.equip_prefix(0)
    pts = shape.equip_points(0)
    store = Store()
    written = []
    monkeypatch.setattr(mqtt_listener, "ingest_reading",
                        lambda store, conn, topic, value, ts, quality=None: written.append((topic, value, ts)))
    on_message = mqtt_listener.make_on_message(store=store, ts_conn=None)
    equip = common.envelope("equip/tags", json.dumps(common.equip_tags(shape, 0, [p[1] for p in pts])), 1.0)
    on_message(None, None, SimpleNamespace(topic=f"{prefix}/equip/tags", payload=equip))
    equip_uri = URIRef(f"urn:equip:{prefix}")
    assert len(set(store.graph.objects(equip_uri, BRICK.hasPoint))) == 10

    idx, label, t = pts[0]
    tags = common.envelope(f"{label}/tags", json.dumps(common.point_tags(shape, 0, idx, label, t)), 1.0)
    on_message(None, None, SimpleNamespace(topic=f"{prefix}/{label}/tags", payload=tags))
    point = URIRef(f"urn:point:{prefix}/{label}")
    assert "scale" in {str(o) for o in store.graph.objects(point, HAYSTACK.hasTag)}

    on_message(None, None, SimpleNamespace(topic=f"{prefix}/{label}", payload=common.envelope(label, 70.0, 2.0)))
    assert written == [(f"{prefix}/{label}", 70.0, 2.0)]


def _config(project="scale", prefix="scale", ports=((1883, 41883),)):
    return {"name": project,
            "volumes": {"postgres-data": {"name": f"{prefix}_postgres-data"}},
            "services": {"mosquitto": {"ports": [{"target": t, "published": str(h)} for t, h in ports]}}}


def test_guard_accepts_the_scale_stack(tmp_path):
    assert common.guard_problems(_config(), env_file=tmp_path / "scale.env", checkout_name="baler") == []


@pytest.mark.parametrize("config, needle", [
    (_config(project="baler"), "live install's default name"),
    (_config(project="timberdoodle"), "live install's default name"),
    (_config(project="mystack"), "must contain 'scale'"),
    (_config(prefix="baler"), "not under the scale_ prefix"),
    (_config(prefix="timberdoodle"), "not under the scale_ prefix"),
    (_config(ports=((1883, 1883),)), "live default port"),
    (_config(ports=((80, 8080),)), "live default port"),
])
def test_guard_refuses_anything_live(tmp_path, config, needle):
    problems = common.guard_problems(config, env_file=tmp_path / "scale.env", checkout_name="baler")
    assert any(needle in p for p in problems), problems


def test_guard_refuses_the_checkout_dir_name_and_live_env(tmp_path):
    assert common.guard_problems(_config(project="scale-co"), env_file=tmp_path / "x", checkout_name="scale-co")
    problems = common.guard_problems(_config(), env_file=common.REPO / ".env", checkout_name="baler")
    assert any("live secrets" in p for p in problems)


def test_override_moves_every_published_port_off_live():
    text = common.OVERRIDE.read_text()
    assert text.count("ports: !override") >= 6
    for port in common.LIVE_PORTS:
        assert f":{port}\"" in text or f"{port}:" not in text.split("ports: !override")[0]


def test_cpu_caps_clamp_to_the_host(monkeypatch):
    for k in list(ladder.os.environ):
        if k.startswith("SCALE_CPUS_"):
            monkeypatch.delenv(k)
    caps = ladder.cpu_caps(1, {})
    assert caps["SCALE_CPUS_POSTGRES"] == "1"  # default 2.0, host has 1
    assert caps["SCALE_CPUS_GRAFANA"] == "0.25"
    assert ladder.cpu_caps(8, {"SCALE_CPUS_POSTGRES": "6"})["SCALE_CPUS_POSTGRES"] == "6"


def test_steps():
    assert ladder.parse_count("2.5k") == 2500 and ladder.parse_count("1M") == 1_000_000
    assert ladder.default_steps(1)[:4] == [1_000, 2_500, 5_000, 10_000]
    assert ladder.default_steps(12)[0] == 10_000 and ladder.default_steps(12)[-1] == 1_000_000


def test_split_covers_every_equip_once():
    parts = loadgen.split(10, 3)
    assert [len(p) for p in parts] == [4, 3, 3] and sorted(i for p in parts for i in p) == list(range(10))
    assert loadgen.split(2, 5) == [range(1), range(1, 2)]


def _good_step(**over):
    step = {"points": 10_000,
            "generator": {"offered_ratio": 1.0},
            "ingest": {"stored_ratio": 1.0, "stored_rate_ratio": 1.0, "stored_rps": 2000, "offered_rps": 2000,
                       "broker_dropped_delta": 0},
            "lag": {"lost": 0, "p95_s": 0.5, "first_third_median_s": 0.2, "last_third_median_s": 0.3},
            "probes": [{"name": "p", "status": 200, "median_s": 0.1, "budget_s": 0.5, "bytes": 10, "source": "#178"}],
            "containers": {"mqtt_listener": {"restarts_delta": 0, "status": "running"},
                           "migrate": {"restarts_delta": 0, "status": "exited"}}}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(step.get(k), dict):
            step[k] = {**step[k], **v}
        else:
            step[k] = v
    return step


def test_evaluate_pass():
    assert ladder.evaluate(_good_step()) == ("pass", [])


@pytest.mark.parametrize("over, needle", [
    ({"ingest": {"stored_ratio": 0.95}}, "data loss"),
    ({"ingest": {"broker_dropped_delta": 12}}, "broker dropped 12"),
    ({"lag": {"p95_s": 31.0}}, "p95 ingest lag"),
    ({"lag": {"lost": 2}}, "never stored"),
    ({"lag": {"first_third_median_s": 1.0, "last_third_median_s": 20.0}}, "growing backlog"),
    ({"probes": [{"name": "p", "status": 200, "median_s": 0.9, "budget_s": 0.5, "bytes": 1, "source": "#178"}]}, "> budget"),
    ({"probes": [{"name": "p", "status": 502, "median_s": 0.1, "budget_s": 0.5, "bytes": 1, "source": "#178"}]}, "HTTP 502"),
    ({"containers": {"mqtt_listener": {"restarts_delta": 1, "oom_killed": True, "status": "running"}}}, "OOM"),
    ({"graph_timeout": 600}, "graph ingest"),
    ({"rules": {"fault_detector": {"feed": {"missed_readings_delta": 5000, "lag_s_last": 0.3}}}}, "fault_detector missed 5000"),
    ({"rules": {"derivation_engine": {"feed": {"missed_readings_delta": 0, "lag_s_last": 45.0}}}}, "behind on the reading feed"),
])
def test_evaluate_fail(over, needle):
    status, reasons = ladder.evaluate(_good_step(**over))
    assert status == "fail" and any(needle in r for r in reasons), reasons


@pytest.mark.parametrize("over, needle", [
    ({"generator": {"offered_ratio": 0.9}}, "generator limit"),
    ({"live_safety_tripped": {"load_per_cpu": 9}}, "live-safety"),
    ({"disk_low": 1.2}, "disk low"),
])
def test_evaluate_inconclusive_is_never_a_baler_ceiling(over, needle):
    status, reasons = ladder.evaluate(_good_step(**over))
    assert status == "inconclusive" and needle in reasons[0]


def test_acked_window():
    stats = {"acked_by_second": {"100": 5, "101": 7, "102": 9}}
    assert ladder.acked_in(stats, 101, 103) == 16


# --- #233: per-worker listener figures, child restarts, tags storm ----------


def test_summarize_listener_per_worker_restarts_and_drops():
    def beat(rps, cpu, blocked=0.0):
        return {"readings_per_s": rps, "cpu_pct": cpu, "blocked_s_delta": blocked}

    samples = [{"listener": {"mqtt_listener/0": beat(9000, 80), "mqtt_listener/1": beat(11000, 90, 1.5),
                             "mqtt_listener/tags": {"feed": {"lag_s": 0.2}}}},
               {"listener": {"mqtt_listener/0": beat(11000, 100), "mqtt_listener/1": beat(9000, 70)}}]
    before = {"mqtt_listener": {"workers": 2, "restarts": {"worker-1": 1}, "broker_dropped_total": 10}}
    after = {"mqtt_listener": {"workers": 2, "restarts": {"worker-1": 3, "tags": 0}, "broker_dropped_total": 25},
             "mqtt_listener/tags": {"messages": 7, "feed": {"lag_s": 0.4, "lag_batches": 1}}}
    out = ladder.summarize_listener(samples, before, after)
    assert out["workers"]["mqtt_listener/0"] == {"readings_per_s_mean": 10000, "cpu_pct_mean": 90, "cpu_pct_max": 100,
                                                 "blocked_s": 0}
    assert out["workers"]["mqtt_listener/1"]["blocked_s"] == 1.5
    assert "mqtt_listener/tags" not in out["workers"]
    assert out["readings_per_s_total"] == 20000 and out["worker_count"] == 2
    assert out["restarts_delta"] == {"worker-1": 2} and out["broker_dropped_delta"] == 15
    assert out["tag_path"] == {"lag_s": 0.4, "lag_batches": 1, "messages": 7}


def test_a_pre_233_listener_summarizes_to_nothing():
    assert ladder.summarize_listener([{}], {}, {})["workers"] == {}


@pytest.mark.parametrize("over, needle", [
    ({"listener": {"restarts_delta": {"worker-2": 1}}}, "mqtt_listener worker-2 restarted 1x"),
    ({"tags_storm": {"points": 100000, "lag_p95_s": 45.0}}, "tags storm of 100000 points"),
])
def test_evaluate_fails_on_listener_restarts_and_tags_storm_lag(over, needle):
    status, reasons = ladder.evaluate(_good_step(**over))
    assert status == "fail" and any(needle in r for r in reasons), reasons


def test_batch_envelope_is_windrows_bytes():
    # Windrow's mqtt_sink.batch_payload for one poll tick (Baler#234)
    assert common.batch_envelope(1.5, {"sat": 71.5, "fan": True}) == b'{"ts": 1.5, "points": {"sat": 71.5, "fan": true}}'


def test_a_generator_batch_stores_the_same_rows_as_per_point(monkeypatch):
    """--envelope batch: the same equip tick, as one message, lands as the
    same (topic, value, ts) rows the per-point messages do (Baler#234)."""
    shape = common.Shape(10)
    prefix = shape.equip_prefix(0)
    pts = shape.equip_points(0)
    values = {label: common.value_at(t[4], idx, 3) for idx, label, t in pts}
    per_point, batched = [], []
    monkeypatch.setattr(mqtt_listener, "ingest_reading",
                        lambda store, conn, topic, value, ts, quality=None: per_point.append((topic, value, ts)))
    monkeypatch.setattr(mqtt_listener, "ingest_readings",
                        lambda store, conn, rows: batched.extend((r[0], r[1], r[2]) for r in rows))
    on_message = mqtt_listener.make_on_message(store=None, ts_conn=None)
    for label, v in values.items():
        on_message(None, None, SimpleNamespace(topic=f"{prefix}/{label}", payload=common.envelope(label, v, 9.0)))
    on_message(None, None, SimpleNamespace(topic=f"{prefix}/batch", payload=common.batch_envelope(9.0, values)))
    assert batched == [(t, mqtt_listener.binary_state(v), ts) for t, v, ts in per_point] and len(batched) == 10


def test_loadgen_counts_a_batch_puback_as_its_readings():
    acks = loadgen.Acks()
    acks.sent(1, 100, readings=10)
    acks.on_publish(None, None, 1)
    acks.on_publish(None, None, 2)  # acked before sent() returned
    acks.sent(2, 101, readings=5)
    assert acks.acked == 15 and acks.take_buckets() == {100: 10, 101: 5}

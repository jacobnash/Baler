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
    assert shape.equip_prefix(51) == "fbf/scale/s00001/e00001"
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


def _route(monkeypatch, topic, payload):
    calls = []
    for name in ("ingest_equip_tags", "ingest_tags", "ingest_reading", "link_point_to_equip"):
        monkeypatch.setattr(mqtt_listener, name, lambda *a, _n=name: calls.append((_n, a)) or "urn:x")
    monkeypatch.setattr(mqtt_listener, "link_equip_ref", lambda *a: None)
    monkeypatch.setattr(mqtt_listener, "_classify", lambda *a: None)
    on_message = mqtt_listener.make_on_message(store=object(), ts_conn=object())
    on_message(None, None, SimpleNamespace(topic=topic, payload=payload))
    return calls


def test_generator_messages_take_the_listeners_real_paths(monkeypatch):
    shape = common.Shape(10)
    prefix = shape.equip_prefix(0)
    pts = shape.equip_points(0)
    equip = common.envelope("equip/tags", json.dumps(common.equip_tags(shape, 0, [p[1] for p in pts])), 1.0)
    calls = _route(monkeypatch, f"{prefix}/equip/tags", equip)
    assert calls[0][0] == "ingest_equip_tags" and calls[0][1][1] == prefix
    assert sum(1 for c in calls if c[0] == "link_point_to_equip") == 10

    idx, label, t = pts[0]
    tags = common.envelope(f"{label}/tags", json.dumps(common.point_tags(shape, 0, idx, label, t)), 1.0)
    calls = _route(monkeypatch, f"{prefix}/{label}/tags", tags)
    assert calls[0][0] == "ingest_tags" and calls[0][1][2]["scale"] is True

    calls = _route(monkeypatch, f"{prefix}/{label}", common.envelope(label, 70.0, 2.0))
    assert calls == [("ingest_reading", calls[0][1])] and calls[0][1][2:] == (f"{prefix}/{label}", 70.0, 2.0)


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

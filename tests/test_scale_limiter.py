"""
scripts/scale/limiter.py and evidence.py (Baler#269): every ladder step
names its limiter from the evidence sampled during its soak. The verdict
rules run on fixtures of samples in exactly the shape evidence.Sampler
records them (tests/fixtures/scale_limiter/: recorded_*.json from real
local runs, trimmed to the services that matter; the other overload cases
built in the same shape), and the parsers on real
cgroup, /proc and diskstats text. No services.
"""

import json
import sys
from pathlib import Path

import pytest

SCALE = Path(__file__).resolve().parent.parent / "scripts" / "scale"
sys.path.insert(0, str(SCALE))
import common
import evidence
import ladder
import limiter

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "scale_limiter"


def _load(path: Path) -> tuple[dict, dict]:
    case = json.loads(path.read_text())
    step = case["step"]
    step["evidence"] = evidence.summarize(case["ticks"])
    return step, case["expect"]


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_verdict_on_recorded_samples(path):
    step, expect = _load(path)
    v = limiter.verdict(step)
    assert expect["contains"] in v["limiter"], (v["limiter"], v["evidence"])
    if "component" in expect:
        assert v["component"] == expect["component"], v
    if "confidence" in expect:
        assert v["confidence"] == expect["confidence"], v
    if "next_suspect" in expect:
        assert expect["next_suspect"] in (v.get("next_suspect") or ""), v
    else:
        assert "next_suspect" not in v  # only a step with no limiter has one
    assert v["evidence"] or v["limiter"] == "none found"
    json.dumps(v)  # goes into the result JSON as is


def test_acceptance_case_names_the_ingest_listener_with_evidence():
    step, _ = _load(FIXTURES / "listener_cpu_capped.json")
    v = limiter.verdict(step)
    assert v["limiter"].startswith("ingest listener")
    assert any("broker dropped 1,204" in e for e in v["evidence"])
    assert any("throttled 38%" in e for e in v["evidence"])
    assert "[high]" in limiter.short(v)


def test_postgres_cpu_capped_outranks_its_busy_listener():
    step, _ = _load(FIXTURES / "postgres_write_bound.json")
    soak = step["evidence"]["phases"]["soak"]
    soak["containers"]["postgres"].update(throttled=0.5, cpu_p90=1.9)
    soak["pg"]["waits"] = {"CPU:running": 1.0}
    v = limiter.verdict(step)
    assert v["limiter"] == "postgres CPU-capped (throttled 50%)", v


def test_several_saturated_parts_lower_the_confidence():
    step, _ = _load(FIXTURES / "listener_cpu_capped.json")
    step["evidence"]["phases"]["soak"]["containers"]["mosquitto"].update(throttled=0.3, cpu_p90=0.95)
    v = limiter.verdict(step)
    assert v["confidence"] == "medium" and any("mosquitto" in a for a in v["also"]), v


def test_pass_without_evidence_still_has_a_next_suspect():
    step, _ = _load(FIXTURES / "pass_next_suspect.json")
    del step["evidence"]
    v = limiter.verdict(step)
    assert v["limiter"] == "none found" and v["next_suspect"].startswith("read?filter=point 0.9 s of 1.5 s")
    assert limiter.short(v).startswith("none found; next: ")


def test_unattributed_failure_is_still_named():
    v = limiter.verdict({"points": 10, "status": "fail", "reasons": ["cx run failed"]})
    assert v["limiter"] == "unattributed: cx run failed" and v["confidence"] == "low"


# ---------------------------------------------------------------- evidence parsing

def test_cgroup_v1_and_v2_parsing():
    v1 = "1:cpu:/docker/abc\n2:cpuacct:/docker/abc\n4:memory:/docker/abc\n9:name=systemd:/docker/abc\n0::/docker/abc\n"
    assert evidence.parse_proc_cgroup(v1)["cpu"] == "/docker/abc"
    assert evidence.parse_proc_cgroup("0::/system.slice/docker-abc.scope\n") == {"": "/system.slice/docker-abc.scope"}
    assert evidence.parse_cpu_stat("nr_periods 100\nnr_throttled 38\nthrottled_time 2500000000\n") == {
        "nr_periods": 100, "nr_throttled": 38, "throttled_s": 2.5}
    v2 = evidence.parse_cpu_stat("usage_usec 5000000\nuser_usec 1\nnr_periods 10\nnr_throttled 1\nthrottled_usec 200000\n")
    assert v2 == {"nr_periods": 10, "nr_throttled": 1, "throttled_s": 0.2, "usage_s": 5.0}
    assert evidence.parse_io_write_bytes("254:0 Read 10\n254:0 Write 4096\n254:0 Total 4106\nTotal 4106\n", v2=False) == 4096
    assert evidence.parse_io_write_bytes("254:0 rbytes=1 wbytes=8192 rios=1 wios=2\n253:0 rbytes=0 wbytes=8192\n", v2=True) == 16384


def test_task_stat_with_odd_comm():
    text = "4242 (python (x) y) S 1 4242 4242 0 -1 4194560 100 0 0 0 750 250 0 0 20 0 3 0 100 1000 200\n"
    assert evidence.parse_task_stat(text) == ("python (x) y", 1000)


def test_process_labels():
    assert evidence.process_label("postgres", "postgres: baler baler 172.18.0.9(45678) COPY") == "postgres client baler"
    assert evidence.process_label("postgres", "postgres: checkpointer        ") == "postgres: checkpointer"
    assert evidence.process_label("postgres", "postgres -c log_statement=all") == "postgres (postmaster)"
    assert evidence.process_label("mqtt_listener", "python -m baler.mqtt_listener ") == "python -m baler.mqtt_listener"


def test_diskstats():
    line = " 254       0 vda 97535 35786 3413138 1057625 206643 41034 6572784 272806 0 193964 1443741 0 0 0 0\n"
    assert evidence.parse_diskstats(line, "vda") == {"writes": 206643, "sectors_w": 6572784, "write_ms": 272806,
                                                     "io_ms": 193964, "reads": 97535, "sectors_r": 3413138}
    assert evidence.parse_diskstats(line, "sda") is None


def test_counter_delta():
    a = {"wal": {"wal_bytes": 100.0, "wal_write": 3}, "io": {"client backend/relation/normal": {"write_bytes": 8192}}}
    b = {"wal": {"wal_bytes": 900.5, "wal_write": 10}, "io": {"client backend/relation/normal": {"write_bytes": 24576},
                                                              "checkpointer/relation/normal": {"write_bytes": 8192}}}
    assert evidence.counter_delta(a, b) == {"wal": {"wal_bytes": 800.5, "wal_write": 7},
                                            "io": {"client backend/relation/normal": {"write_bytes": 16384},
                                                   "checkpointer/relation/normal": {"write_bytes": 8192}}}


def test_span_breakdown_self_time():
    def span(trace, sid, parent, name, start, end):
        return {"trace": trace, "id": sid, "parent": parent, "name": name, "start": start, "end": end}

    spans = []
    for i, (t0, enc) in enumerate(((0.0, 0.5), (10.0, 0.7), (20.0, 0.6))):
        tr = f"t{i}"
        spans += [span(tr, "r", None, "haystack.read", t0, t0 + 1.0),
                  span(tr, "m", "r", "haystack.read.match_entities", t0, t0 + 0.2),
                  span(tr, "e", "r", "haystack.encode", t0 + 0.3, t0 + 0.3 + enc)]
    bd = evidence.span_breakdown(spans)
    assert bd["requests"] == 3 and bd["root_median_s"] == 1.0
    assert bd["spans"][0] == {"span": "haystack.encode", "self_s": 0.6, "share": 0.6}
    assert {s["span"] for s in bd["spans"]} == {"haystack.encode", "haystack.read.match_entities", "haystack.read"}
    assert evidence.span_breakdown([]) == {}


def test_summarize_throttled_share_is_weighted_by_periods():
    ticks = [{"t": 0, "containers": {"x": {"cpu": 1.0, "cap": 1.0, "throttled": 0.5, "periods": 100}}},
             {"t": 5, "containers": {"x": {"cpu": 0.2, "cap": 1.0, "throttled": 0.0, "periods": 300}}}]
    c = evidence.summarize(ticks)["phases"]["soak"]["containers"]["x"]
    assert c["throttled"] == 0.125 and c["cpu_mean"] == 0.6


# ---------------------------------------------------------------- the defects #269 fixed

def test_disk_guard_watches_dockers_data_root(monkeypatch):
    """Filling Docker's data root below --min-free-gb stops the soak, even
    when the checkout's disk has room (it used to watch the checkout's)."""
    monkeypatch.setattr(common, "docker_root", lambda: "/docker-data")
    seen = []

    def free_gb(path):
        seen.append(str(path))
        return 1.5 if str(path) == "/docker-data" else 500.0

    monkeypatch.setattr(common, "free_gb", free_gb)
    stop, info = ladder.disk_check(5.0)
    assert stop and info["docker_root_free_gb"] == 1.5 and info["repo_free_gb"] == 500.0
    assert "/docker-data" in seen
    monkeypatch.setattr(common, "free_gb", lambda p: 50.0 if str(p) == "/docker-data" else 0.1)
    assert ladder.disk_check(5.0)[0] is False  # a full checkout disk alone doesn't stop it


def test_disk_guard_falls_back_to_the_checkout_disk(monkeypatch):
    monkeypatch.setattr(common, "docker_root", lambda: "/not-visible")
    monkeypatch.setattr(common, "free_gb", lambda p: None if str(p) == "/not-visible" else 2.0)
    assert ladder.disk_check(5.0)[0] is True


def test_disk_low_names_the_disk_and_is_inconclusive():
    step = {"points": 1000, "disk_low": 1.5, "status": "inconclusive"}
    assert ladder.evaluate(step)[0] == "inconclusive"
    assert limiter.verdict(step)["limiter"] == "disk space (Docker data root 1.5 GB free)"


def test_summary_says_none_in_this_run():
    result = {"steps": [{"points": 50_000, "status": "fail", "limiter": {"limiter": "reads: x", "confidence": "high"}}],
              "max_passing_points": None}
    text = ladder.summary_text(result)
    assert "max passing step: none in this run" in text and "reads: x [high]" in text
    result["max_passing_points"] = 25_000
    assert "max passing step: 25,000 points" in ladder.summary_text(result)


def test_table_has_the_limiter_column(capsys, tmp_path):
    step, _ = _load(FIXTURES / "listener_cpu_capped.json")
    step["limiter"] = limiter.verdict(step)
    result = {"run_id": "r1", "commits": {"baler": "abcdef0123"}, "durability": {"comparable": True},
              "hardware": {"hostname": "h", "nproc": 4, "cpu_model": "cpu", "mem_total_gib": 16}, "steps": [step]}
    f = tmp_path / "r.json"
    f.write_text(json.dumps(result))
    ladder.cmd_table(type("A", (), {"files": [str(f)]})())
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith("| Limiter (or next suspect) |")
    assert "ingest listener: mqtt_listener CPU-capped (throttled 38%) [high]" in out[2]
    assert out[2].count("|") == out[0].count("|")

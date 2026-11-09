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
from types import SimpleNamespace

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


# ---------------------------------------------------------------- fault_detector periodic work (#268)


def _with_fd_heartbeat(quality: dict, his: dict) -> dict:
    step, _ = _load(FIXTURES / "pass_next_suspect.json")
    step.setdefault("rules", {}).setdefault("fault_detector", {})["heartbeat"] = {
        "interval_seconds": 30.0, "quality": quality, "his": his}
    return step


def test_a_quality_sweep_over_its_interval_is_the_limiter():
    step = _with_fd_heartbeat({"interval_s": 300.0, "sweep_s": 412.0, "rows": 9_000_000, "span_s": 600.0,
                               "watermark_lag_s": 900.0},
                              {"interval_s": 30.0, "sweep_s": 0.4, "gap_s_max": 30.2})
    v = limiter.verdict(step)
    assert v["limiter"].startswith("fault_detector quality watermark over its interval"), v
    assert v["component"] == "fault_detector"
    assert "fault_detector quality sweep" in v["also"]
    assert any("412.0 s of its 300 s interval (9,000,000 rows" in e for e in v["evidence"])


def test_his_cycles_that_wait_are_the_limiter():
    step = _with_fd_heartbeat({"interval_s": 300.0, "sweep_s": 40.0, "watermark_lag_s": 310.0},
                              {"interval_s": 30.0, "sweep_s": 0.4, "gap_s_max": 170.0})
    v = limiter.verdict(step)
    assert v["limiter"].startswith("fault_detector his cycle over its interval: His cycles up to 170.0 s apart"), v


def test_a_quality_sweep_within_its_interval_is_headroom_only():
    step = _with_fd_heartbeat({"interval_s": 300.0, "sweep_s": 40.0, "rows": 6_000_000, "span_s": 330.0,
                               "watermark_lag_s": 305.0},
                              {"interval_s": 30.0, "sweep_s": 0.4, "gap_s_max": 30.4})
    v = limiter.verdict(step)
    assert v["limiter"] == "none found"
    parts = {h["part"]: h["ratio"] for h in limiter.headroom(step)}
    assert parts["fault_detector quality sweep"] == round(40 / 300, 3)
    assert parts["fault_detector quality watermark"] == round(305 / 600, 3)


# ---------------------------------------------------------------- N listener workers (#233)

WORKER = "/usr/local/bin/python -m baler.mqtt_listener --child worker --index {}"


def test_listener_process_roles():
    assert limiter._listener_role(WORKER.format(2)) == "worker 2"
    assert limiter._listener_role("/usr/local/bin/python -m baler.mqtt_listener --child tags --index 0") == "tag path"
    assert limiter._listener_role("/usr/local/bin/python -m baler.mqtt_listener") is None  # supervisor, or pre-#233


def test_a_gil_bound_worker_is_named_with_the_worker_count():
    """With N workers the busiest process is one worker: "at one core"
    means that worker is GIL-bound and more workers help, so the verdict
    says which worker and how many there are - still the ingest listener."""
    step, _ = _load(FIXTURES / "listener_gil_bound.json")
    soak = step["evidence"]["phases"]["soak"]["containers"]["mqtt_listener"]
    soak.update(cap=4.0, cpu_p90=1.9, throttled=0.0)  # 2 workers under a 4-core cap: not capped
    soak["procs"] = [{"label": WORKER.format(1), "cpu_mean": 0.97, "cpu_max": 1.0, "hot_thread_mean": 0.9,
                      "hot_thread_max": 0.95},
                     {"label": WORKER.format(0), "cpu_mean": 0.95, "cpu_max": 1.0, "hot_thread_mean": 0.88,
                      "hot_thread_max": 0.93}]
    step["listener"] = {"worker_count": 2}
    v = limiter.verdict(step)
    assert v["limiter"].startswith("ingest listener: mqtt_listener CPU-bound (worker 1"), v["limiter"]
    assert "2 workers" in v["limiter"] and v["component"] == "ingest listener"


def test_without_per_process_figures_the_listener_cpu_is_spread_over_its_workers():
    """Three workers at 0.6 cores each is 1.8 cores of container CPU: not
    one process at its one-core limit."""
    c = {"cap": 4.0, "cpu_p90": 1.8}
    one = limiter.cpu_saturation("mqtt_listener", c)
    three = limiter.cpu_saturation("mqtt_listener", c, workers=3)
    assert one[0] > 1.0 and three[0] < 1.0 and "over 3 workers" in three[1]


def test_a_worker_restarted_by_its_supervisor_is_the_limiter():
    step, _ = _load(FIXTURES / "pass_next_suspect.json")
    step["listener"] = {"worker_count": 3, "restarts_delta": {"worker-2": 1}}
    v = limiter.verdict(step)
    assert v["limiter"] == "ingest listener: mqtt_listener worker-2 restarted 1x" and v["component"] == "ingest listener"


def test_the_writer_busy_share_is_per_worker(monkeypatch):
    """The supervisor's heartbeat sums flush_s over its workers (#233):
    two workers that each wrote half of a 10 s tick are 50% busy, not
    100%. A heartbeat without `writers` (one process) reads as before."""
    beats = []

    class Conn:
        def execute(self, sql):
            return SimpleNamespace(fetchone=lambda: ([], 0, beats[-1]))

    clock = [1000.0]
    monkeypatch.setattr(evidence.time, "time", lambda: clock[0])
    sampler = evidence.Sampler([], pg_connect=None, docker_root="/nonexistent-docker-root")
    sampler.device = None
    busy = []
    for writers, flush_s in ((2, 0.0), (2, 10.0), (None, 18.0)):
        beats.append({"queue": 0, "queue_peak": 0, "queue_max": 50000, "rows": 0, "flush_s": flush_s,
                      **({"writers": writers} if writers else {})})
        tick = sampler.tick(Conn())
        if tick is not None:
            busy.append(tick["listener"]["flush_busy"])
        clock[0] += 10.0
    # 10 s written by 2 workers in 10 s; then 8 s by one process in 10 s
    assert busy == [0.5, 0.8]


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

class _Statvfs:
    """os.statvfs over fake filesystems: {path prefix: free GB}; a path
    under none of them raises (not visible from here)."""

    def __init__(self, filesystems: dict[str, float]):
        self.filesystems, self.calls = filesystems, []

    def __call__(self, path):
        path = str(path)
        self.calls.append(path)
        for prefix, free in sorted(self.filesystems.items(), key=lambda kv: -len(kv[0])):
            if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
                return SimpleNamespace(f_bavail=int(free * 1e9 / 4096), f_frsize=4096)
        raise FileNotFoundError(path)


@pytest.fixture
def two_disks(monkeypatch):
    """nash-srv's layout: the checkout on the root disk, Docker's data root
    on a separate one."""
    def setup(docker_free, repo_free, reported="/data/docker"):
        monkeypatch.setattr(common, "docker_root_reported", lambda: reported)
        monkeypatch.setattr(ladder, "_DISK_FALLBACK_WARNED", False)
        fs = _Statvfs({"/data": docker_free, str(common.REPO): repo_free} if docker_free is not None
                      else {str(common.REPO): repo_free})
        monkeypatch.setattr(common.os, "statvfs", fs)
        return fs
    return setup


def test_disk_guard_watches_dockers_data_root_on_its_own_filesystem(two_disks):
    """Docker's disk below --min-free-gb stops the step while the checkout's
    disk has plenty: the old guard (on the checkout) would never fire."""
    fs = two_disks(docker_free=1.5, repo_free=500.0)
    stop, info = ladder.disk_check(5.0)
    assert stop and info == {"docker_root": "/data/docker", "docker_root_free_gb": 1.5, "repo_free_gb": 500.0,
                             "watched": "docker_root"}
    assert "/data/docker" in fs.calls


def test_a_full_checkout_disk_alone_does_not_stop_it(two_disks):
    two_disks(docker_free=80.0, repo_free=0.5)
    stop, info = ladder.disk_check(5.0)
    assert not stop and info["watched"] == "docker_root"


@pytest.mark.parametrize("reported", ["", "/var/lib/docker-remote"])
def test_disk_guard_falls_back_to_the_checkout_disk_with_a_warning(two_disks, capsys, reported):
    """No DockerRootDir from `docker info`, or one not visible from here
    (remote/rootless Docker): the checkout's disk, and a warning once."""
    two_disks(docker_free=None, repo_free=2.0, reported=reported)
    stop, info = ladder.disk_check(5.0)
    assert stop and info["watched"] == "checkout" and info["docker_root_free_gb"] is None
    assert "WARNING: disk guard" in capsys.readouterr().err
    ladder.disk_check(5.0)
    assert capsys.readouterr().err == ""  # once per process


def test_run_and_soak_both_use_the_docker_root_guard():
    """`ladder.py run` and `ladder.py soak` stop on the same check."""
    import inspect

    import soak

    for src in (inspect.getsource(ladder.run_step), inspect.getsource(soak)):
        assert "disk_check(args.min_free_gb)" in src
        assert "disk_usage(common.REPO)" not in src


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

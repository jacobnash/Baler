"""
scripts/scale/soak.py (Baler#245): the long soak's verdict. A soak passes
only when disk plateaus (hot window bounded, growth at the compressed rate),
compression keeps up, lag and reads stay in budget through compression
cycles, the aggregates keep refreshing and late writes into compressed
chunks land once each. Pure: no services.
"""

import sys
from pathlib import Path

import pytest

SCALE = Path(__file__).resolve().parent.parent / "scripts" / "scale"
sys.path.insert(0, str(SCALE))
import soak

CHUNK, AFTER, SCHED = 300.0, 600.0, 150.0
RAW_B, COMP_B, RPS = 250, 10, 400  # bytes/row raw and compressed, rows/s


def _result(n=240, every=15.0, hot_growth=0.0, comp_b=COMP_B, backlog_end=0, lag=1.0, late_ok=True,
            probe_s=0.2, runs_ok=True, steady_cycles=True):
    """A synthetic soak: hot bytes a sawtooth (one chunk's worth filling up
    above compress_after's worth), compressed bytes growing per row."""
    samples = []
    for i in range(1, n + 1):
        t = i * every
        stored = int(RPS * t)
        hot_rows = RPS * (AFTER + (t % CHUNK)) * (1 + hot_growth * t / (n * every))
        compressed_rows = max(0, stored - hot_rows)
        disk = {"hot_bytes": int(hot_rows * RAW_B), "compressed_bytes": int(compressed_rows * comp_b),
                "hot_chunks": 3, "compressed_chunks": int(compressed_rows // (RPS * CHUNK)),
                "before_compression_bytes": int(compressed_rows * RAW_B), "after_compression_bytes": int(compressed_rows * comp_b)}
        disk["total_bytes"] = disk["hot_bytes"] + disk["compressed_bytes"]
        samples.append({"t": t, "stored_total": stored, "disk": disk, "oldest_hot_end_age_s": AFTER + SCHED / 2,
                        "backlog": backlog_end if i > n * 0.8 else 0, "compressing": i % 20 == 0,
                        "watermark_age_s": {"point_history_5m": 400.0, "point_history_1h": 4000.0},
                        "lag": {"p95_s": lag}})
    runs = [{"t": t, "duration_s": 3.0, "chunks": 1, "rows": int(RPS * CHUNK), "rows_per_s": int(RPS * CHUNK / 3),
             "status": "Success" if runs_ok else "Failed"}
            for t in range(int(AFTER + CHUNK), n * int(every), int(CHUNK))] if steady_cycles else []
    rounds = [{"t": t, "during_compression": t == 1200,
               "probes": [{"name": "p", "status": 200, "median_s": probe_s, "budget_s": 0.5}]} for t in (600, 1200, 1800)]
    late = [{"t": 1500, "points": 2000, "replayed": 2000 if late_ok else 1990, "backfilled": 2000, "duplicates": 0,
             "ok": late_ok}]
    return {"settings": {"chunk_interval_s": CHUNK, "compress_after_s": AFTER, "schedule_s": SCHED},
            "samples": samples, "runs": runs, "probe_rounds": rounds, "late": late,
            "ingest": {"stored_ratio": 1.0, "offered_rps": RPS, "stored_rps": RPS}, "lag": {"p95_s": lag, "lost": 0},
            "cagg_check": {"checked": True, "equal": True},
            "containers": {"mqtt_listener": {"restarts_delta": 0, "status": "running"}}}


def _verdict(r):
    r["summary"] = soak.summarize(r)
    return soak.evaluate_soak(r)


def test_plateau_passes_and_reports_the_numbers():
    r = _result()
    status, reasons = _verdict(r)
    assert (status, reasons) == ("pass", [])
    s = r["summary"]
    assert s["compressed_bytes_per_row"] == pytest.approx(COMP_B, rel=0.15)
    assert s["growth_bytes_per_row"] == pytest.approx(COMP_B, rel=0.3)  # phase-aligned: the compressed rate, not raw
    assert s["compression_rows_per_s_median"] == 40_000 and s["compression_margin"] == 100.0
    assert s["compression_ratio"] == 25.0


@pytest.mark.parametrize("over, needle", [
    ({"hot_growth": 4.0}, "hot window still growing"),
    ({"backlog_end": 3}, "backlog growing"),
    ({"lag": 45.0}, "p95 ingest lag"),
    ({"late_ok": False}, "late writes"),
    ({"probe_s": 0.9}, "> budget"),
    ({"runs_ok": False}, "compression run"),
])
def test_failures(over, needle):
    status, reasons = _verdict(_result(**over))
    assert status == "fail" and any(needle in r for r in reasons), reasons


def test_disk_growing_at_the_raw_rate_fails():
    r = _result()
    for s in r["samples"]:  # nothing ever shrinks: total grows at the raw rate
        s["disk"]["total_bytes"] = s["stored_total"] * RAW_B
    status, reasons = _verdict(r)
    assert status == "fail" and any("B per stored row" in x for x in reasons)


def test_short_soak_is_inconclusive():
    status, reasons = _verdict(_result(n=60))  # 15 min: warm-up only
    assert status == "inconclusive" and "too short" in reasons[0]


def test_cagg_mismatch_and_restart_fail():
    r = _result()
    r["cagg_check"]["equal"] = False
    r["containers"]["mqtt_listener"]["restarts_delta"] = 2
    status, reasons = _verdict(r)
    assert status == "fail"
    assert any("point_history_5m disagrees" in x for x in reasons) and any("restarted 2x" in x for x in reasons)


def test_soak_probes_fill_in_the_windows():
    budgets = {"runs": 3, "probes": [{"name": "a", "path": "/x", "params": {}, "warm_median_s": 1, "source": "s"}],
               "soak_probes": [{"name": "b", "path": "/ingest/history", "params": {"point": "{point0}", "start": "{start_boundary}"},
                                "warm_median_s": 1, "source": "s"}]}
    out = soak.soak_probes(budgets, soak.common.Shape(100), {"chunk_interval_s": 3600, "compress_after_s": 10800}, 100_000.0)
    assert out["runs"] == 3 and [p["name"] for p in out["probes"]] == ["a", "b"]
    assert out["probes"][1]["params"] == {"point": "fbf/scale/s00000/e00000/sat", "start": str(100_000 - 10800 - 7200)}


def test_live_safety_trips_only_on_a_sustained_breach():
    """The soak's live-safety (ladder.LiveSafety, sustain_s): a breach must
    last the whole window, with no good check in between, to stop it."""
    clock = [0.0]
    safety = soak.ladder.LiveSafety(None, 1.5, 3.0, sustain_s=300.0, clock=lambda: clock[0])
    verdicts = []
    for t, bad in ((0, True), (120, True), (240, False), (270, True), (450, True), (560, True), (570, True)):
        clock[0] = t
        verdicts.append(safety.judge(bad))
    assert verdicts == [False, False, False, False, False, False, True]  # 270 -> 570: 300 s bad
    assert safety.bad_for_s() == 300.0
    three = soak.ladder.LiveSafety(None, 1.5, 3.0)  # the ladder's default: three checks in a row
    assert [three.judge(b) for b in (True, True, False, True, True, True)] == [False, False, False, False, False, True]


def test_a_live_safety_stop_is_recorded_and_inconclusive():
    r = _result()
    r["live_safety_tripped"] = {"load_per_cpu": 4.2, "live_latency_s": 3.1, "degraded_for_s": 312.0, "at_t": 7200.0}
    status, reasons = _verdict(r)
    assert status == "inconclusive"
    assert any("live-safety" in x and "312.0 s" in x and "not a Baler ceiling" in x for x in reasons), reasons


def test_soak_has_the_ladders_live_safety_options():
    import argparse

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers()
    soak.add_parser(sub, lambda p: None)
    args = ap.parse_args(["soak", "--points", "1000"])
    assert (args.live_sustain, args.live_load_factor, args.live_latency_factor) == (300.0, 1.5, 3.0)
    args = ap.parse_args(["soak", "--points", "1000", "--live-sustain", "60", "--no-live-check"])
    assert args.live_sustain == 60.0 and args.no_live_check

"""
scripts/scale/soak.py (Baler#245): the long soak's verdict. A soak passes
only when disk plateaus (hot window bounded, growth at the compressed rate),
compression keeps up, lag and reads stay in budget through compression
cycles, the aggregates keep refreshing and late writes into compressed
chunks land once each. Pure: no services.
"""

import sys
import time
from collections import deque
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


# ------------------------------------------------- late-write pacing (fake paho)

ERR_QUEUE_SIZE = 15


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, d):
        self.t += max(0.0, d)


class RealClock:
    __call__ = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)


class FakeInfo:
    def __init__(self, client, mid, rc=0):
        self.client, self.mid, self.rc, self.ack_at = client, mid, rc, None

    def is_published(self):
        if self.rc == ERR_QUEUE_SIZE:  # as paho: a refused message raises here
            raise ValueError("Message is not queued due to ERR_QUEUE_SIZE")
        return self.ack_at is not None and self.client.clock() >= self.ack_at

    def wait_for_publish(self, timeout=None):
        if self.rc == ERR_QUEUE_SIZE:
            raise ValueError("Message is not queued due to ERR_QUEUE_SIZE")
        if self.ack_at is None:
            self.client.clock.sleep(timeout)
        elif self.ack_at > self.client.clock():
            self.client.clock.sleep(min(self.ack_at - self.client.clock(), timeout))


class FakePaho:
    """paho's QoS 1 outgoing queue as it matters here: 16-bit message ids,
    a reused id while its message is still unacknowledged gets
    ERR_QUEUE_SIZE (whatever max_queued_messages says), and a broker that
    acks at `ack_rate`/s (None: never)."""

    def __init__(self, clock, ack_rate=None):
        self.clock, self.ack_rate = clock, ack_rate
        self.mid, self.out, self.last_ack, self.sent = 0, {}, 0.0, []
        self.order = deque()
        self.max_outstanding = 0

    def publish(self, topic, payload, qos=0, retain=False):
        assert qos == 1 and not retain
        self.mid = self.mid % 65535 + 1
        while self.order and self.order[0].is_published():  # acks come in order
            del self.out[self.order.popleft().mid]
        if self.mid in self.out:
            return FakeInfo(self, self.mid, rc=ERR_QUEUE_SIZE)
        info = FakeInfo(self, self.mid)
        if self.ack_rate:
            self.last_ack = info.ack_at = max(self.clock(), self.last_ack) + 1 / self.ack_rate
        self.out[self.mid] = info
        self.order.append(info)
        self.sent.append((topic, payload))
        self.max_outstanding = max(self.max_outstanding, len(self.out))
        return info

    def loop_stop(self):
        pass

    def disconnect(self):
        pass


def _msgs(n):
    return [(f"t/{i}", "{}") for i in range(n)]


def test_unpaced_publishing_reproduces_the_nash_srv_crash():
    """The model is faithful: 200k messages (100k points x 2) faster than
    the broker acks overflows the id space, as the old late round did."""
    clock = FakeClock()
    c = FakePaho(clock, ack_rate=1000)
    infos = []
    for topic, payload in _msgs(200_000):
        infos.append(c.publish(topic, payload, qos=1))
        clock.sleep(1 / 20_000)  # the soak's own rate
    with pytest.raises(ValueError, match="ERR_QUEUE_SIZE"):
        for info in infos:
            info.wait_for_publish(timeout=60)


def test_paced_publish_never_exceeds_the_window_on_a_slow_broker():
    clock = FakeClock()
    c = FakePaho(clock, ack_rate=1000)  # the broker acks at half the asked rate
    st = soak.paced_publish(c, _msgs(200_000), rate=2000, window=500, ack_timeout=60, clock=clock, sleep=clock.sleep)
    assert st["published"] == len(c.sent) == 200_000
    assert st["max_outstanding"] <= 500 and c.max_outstanding <= 500
    assert st["ack_waits"] > 0
    assert 199 <= st["publish_s"] <= 201  # held to the broker's 1000/s


def test_paced_publish_keeps_to_its_rate_when_the_broker_keeps_up():
    clock = FakeClock()
    c = FakePaho(clock, ack_rate=1_000_000)
    st = soak.paced_publish(c, _msgs(10_000), rate=1000, window=500, ack_timeout=60, clock=clock, sleep=clock.sleep)
    assert 9.9 <= st["publish_s"] <= 10.1
    assert st["max_outstanding"] <= 2


def test_paced_publish_fails_the_round_when_acks_stop():
    clock = FakeClock()
    c = FakePaho(clock, ack_rate=None)
    with pytest.raises(soak.LateWriteError, match="no PUBACK within 5 s after 50 of 1000"):
        soak.paced_publish(c, _msgs(1000), rate=1e6, window=50, ack_timeout=5, clock=clock, sleep=clock.sleep)
    assert len(c.sent) == 50


def test_paced_publish_fails_the_round_on_a_refused_message():
    clock = FakeClock()
    c = FakePaho(clock, ack_rate=1e6)
    c.publish = lambda *a, **k: FakeInfo(c, 1, rc=ERR_QUEUE_SIZE)
    with pytest.raises(soak.LateWriteError, match="refused message 1 of 3"):
        soak.paced_publish(c, _msgs(3), rate=10, window=5, ack_timeout=1, clock=clock, sleep=clock.sleep)


def test_paced_publish_rejects_a_window_past_the_id_space():
    with pytest.raises(ValueError):
        soak.paced_publish(FakePaho(FakeClock()), [], rate=1, window=65535, ack_timeout=1)


def test_late_rate_defaults_to_a_tenth_of_the_load():
    from types import SimpleNamespace as NS

    assert soak.late_rate(NS(late_rate=None, points=100_000, interval=5.0)) == 2000
    assert soak.late_rate(NS(late_rate=None, points=1000, interval=5.0)) == 200
    assert soak.late_rate(NS(late_rate=750.0, points=100_000, interval=5.0)) == 750


class FakeConn:
    """Answers LateWriter.run's two catalog/history reads; nothing lands."""

    def __init__(self, n_points):
        from datetime import datetime, timezone

        self.end = datetime(2026, 11, 9, 4, tzinfo=timezone.utc)
        self.rows = [(f"urn:point:scale/s0/e{i}/temp", self.end) for i in range(n_points)]

    def execute(self, sql, params=None):
        class R:
            def __init__(self, one=None, all_=None):
                self.one, self.all_ = one, all_

            def fetchone(self):
                return self.one

            def fetchall(self):
                return self.all_

        if "is_compressed" in sql:
            return R(one=("_timescaledb_internal._hyper_1_1_chunk", self.end, self.end))
        return R(all_=self.rows)


def test_a_failed_late_round_is_recorded_and_the_writer_recovers():
    """The nash-srv failure mode end to end: the broker stops acking, the
    round fails as a result entry, not an exception, and the next round
    gets a fresh client."""
    made = []

    def factory():
        made.append(FakePaho(RealClock(), ack_rate=None))
        return made[-1]

    late = soak.LateWriter(None, 5.0, rate=1e6, window=10, ack_timeout=0.05, client_factory=factory)
    entry = soak.guarded_round("late-write", lambda: late.run(FakeConn(100)), 3600.0, on_error=late.reset)
    assert entry["harness_error"] and entry["ok"] is False and entry["t"] == 3600.0
    assert "LateWriteError: no PUBACK" in entry["error"]
    assert len(made) == 2 and late.c is made[1]
    assert len(made[0].sent) == 10  # stopped at the window, not 200 messages queued


def test_guarded_round_passes_a_good_round_through():
    assert soak.guarded_round("probe", lambda: {"probes": []}, 12.34) == {"probes": [], "t": 12.3}


# ------------------------------------------------- windowed lag, broker drops

def test_lag_window_takes_canaries_stored_in_the_window():
    samples = [(0.0, 1.0), (10.0, 1.5), (20.0, 25.0), (30.0, 2.0), (50.0, 1.0)]
    w = soak.lag_window(samples, pending=[55.0], t0=20.0, t1=60.0)
    # stored at 32 (30 + 2), 45 (20 + 25) and 51: not the ones stored before 20
    assert w["n"] == 3 and w["p50_s"] == 2.0 and w["max_s"] == 25.0 and w["p95_s"] == 25.0
    assert w["pending"] == 1 and w["oldest_pending_s"] == 5.0
    assert soak.lag_window([], [], 0, 30) == {"n": 0, "pending": 0, "oldest_pending_s": None}


def test_broker_drops_from_the_supervisor_heartbeat():
    hb = {"mqtt_listener": {"broker_dropped_total": 120}}
    first = soak.broker_drops(hb, None, None)
    assert first == {"dropped_total": 120, "since_start": None, "delta": None}
    hb2 = {"mqtt_listener": {"broker_dropped_total": 150}}
    assert soak.broker_drops(hb2, first, first) == {"dropped_total": 150, "since_start": 30, "delta": 30}
    assert soak.broker_drops({}, first, first)["dropped_total"] is None


def test_broker_drops_during_the_soak_fail_it():
    r = _result()
    for s in r["samples"]:
        s["broker"] = {"dropped_total": 10, "since_start": 0, "delta": 0}
    r["samples"][-1]["broker"] = {"dropped_total": 1010, "since_start": 1000, "delta": 1000}
    status, reasons = _verdict(r)
    assert status == "fail" and any("broker dropped 1,000" in x for x in reasons)
    assert r["summary"]["broker_dropped"] == 1000


def test_windowed_p95_replaces_the_cumulative_one_during_compression():
    """A burst early on holds the cumulative p95 up; compression is judged
    on its own windows."""
    r = _result(lag=35.0)  # cumulative p95 high in every sample
    r["lag"]["p95_s"] = 5.0
    for s in r["samples"]:
        s["lag_window"] = {"n": 15, "p50_s": 1.0, "p95_s": 2.0, "max_s": 3.0}
    r["samples"][10]["lag_window"]["p95_s"] = 40.0
    status, reasons = _verdict(r)
    assert status == "pass", reasons
    assert r["summary"]["lag_p95_during_compression_max"] == 2.0
    assert r["summary"]["lag_window_p95_max"]["t"] == r["samples"][10]["t"]
    assert [w["t"] for w in r["summary"]["lag_windows_over_budget"]] == [r["samples"][10]["t"]]


def test_a_failed_late_round_does_not_fail_a_soak_with_a_good_one():
    r = _result()
    r["late"].append({"t": 3300, "ok": False, "harness_error": True, "error": "LateWriteError: no PUBACK"})
    status, reasons = _verdict(r)
    assert status == "pass", reasons
    assert r["summary"]["late_round_errors"] == [{"t": 3300, "error": "LateWriteError: no PUBACK"}]
    assert r["summary"]["late_rounds_ok"] == 1


def test_only_failed_late_rounds_leave_late_data_untested():
    r = _result()
    r["late"] = [{"t": 1500, "ok": False, "harness_error": True, "error": "ValueError: ERR_QUEUE_SIZE"}]
    status, reasons = _verdict(r)
    assert status == "inconclusive"
    assert any("no late-write round completed" in x and "ERR_QUEUE_SIZE" in x for x in reasons)


def test_a_failed_probe_round_is_skipped_not_fatal():
    r = _result()
    r["probe_rounds"].append({"t": 2400, "ok": False, "harness_error": True, "error": "ConnectionError",
                              "during_compression": False})
    status, reasons = _verdict(r)
    assert status == "pass", reasons
    assert r["summary"]["probe_rounds"] == 3 and len(r["summary"]["probe_round_errors"]) == 1


def test_soak_has_the_late_pacing_options():
    import argparse

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers()
    soak.add_parser(sub, lambda p: None)
    a = ap.parse_args(["soak", "--points", "100", "--late-rate", "50", "--late-window", "100"])
    assert (a.late_rate, a.late_window, a.late_ack_timeout) == (50.0, 100, 60.0)

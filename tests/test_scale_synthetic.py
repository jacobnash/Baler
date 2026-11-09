"""
scripts/scale/synthetic.py (Baler#273): the ladder's realistic profiles and
the synthetic load - how N items split into derivations and rules, how a
step's total points split into real and synthetic, the derivation and rule
definitions it creates, and how a step is summarized and judged. No
services; the definitions' functions run in Baler's own sandbox.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

SCALE = Path(__file__).resolve().parent.parent / "scripts" / "scale"
sys.path.insert(0, str(SCALE))
import common
import synthetic

from baler import sandbox


def _args(**kw):
    a = SimpleNamespace(profile=None, interval=None, synthetic_per_equip=None, synthetic_mix=None, fault_share=None,
                        warmup=None, soak=None, points_per_equip=10)
    a.__dict__.update(kw)
    return a


def test_profiles_fill_only_what_was_left_unset():
    a = _args(profile="typical-5min", fault_share=0.05)
    synthetic.apply_profile(a)
    assert (a.interval, a.synthetic_per_equip, a.fault_share, a.soak) == (300.0, 5, 0.05, 1800.0)
    b = _args()
    synthetic.apply_profile(b)  # no profile: the stress test, as before #273
    assert (b.profile, b.interval, b.synthetic_per_equip, b.fault_share) == ("stress-5s", 5.0, 0, 0.0)


@pytest.mark.parametrize("n,mix,want", [(5, "60:20:20", (3, 1, 1)), (10, "60:20:20", (6, 2, 2)),
                                        (5, "1:0:0", (5, 0, 0)), (7, "1:1:1", (3, 2, 2)), (0, "60:20:20", (0, 0, 0))])
def test_items_split_by_the_mix(n, mix, want):
    assert synthetic.split_items(n, synthetic.parse_mix(mix)) == want


def test_bad_mix_and_too_few_points_per_equip_are_refused():
    with pytest.raises(SystemExit):
        synthetic.parse_mix("1:2")
    with pytest.raises(SystemExit):
        synthetic.apply_profile(_args(profile="typical-1min", points_per_equip=5))


def test_step_size_counts_synthetic_points():
    plan = synthetic.plan_for(_args(interval=60.0, synthetic_per_equip=5, synthetic_mix="60:20:20", fault_share=0.01))
    pts = plan.points(100_000)
    assert pts == {"real": 76_920, "synthetic": 23_076, "total": 99_996, "equips": 7_692}
    assert plan.real_points(0) == 0
    stress = synthetic.plan_for(_args(interval=5.0, synthetic_per_equip=0, synthetic_mix="60:20:20", fault_share=0))
    assert stress.points(25_000)["real"] == 25_000  # exactly the ladder before #273


def test_expected_load_scales_with_equips():
    plan = synthetic.plan_for(_args(interval=60.0, synthetic_per_equip=10, synthetic_mix="60:20:20", fault_share=0.0))
    exp = plan.expected(16_000)  # 1,000 equips of 10 real + 6 synthetic
    assert exp["readings_per_s"] == pytest.approx(10_000 / 60)
    assert exp["cur_rule_evaluations_per_s"] == pytest.approx((10_000 + 1_000 * 2) / 60)
    assert exp["derivation_rows_per_s"] == pytest.approx(1_000 * 6 / 60)
    assert exp["cur_rows_per_s"] == pytest.approx(1_000 * 4 / 60)  # L1 and L2 of two chains
    assert plan.chains == 2 and plan.levels() == [(0, 1), (0, 2), (0, 3), (1, 1), (1, 2), (1, 3)]


def test_fault_share_picks_numeric_points_near_the_share():
    shape = common.Shape(100_000)
    n = synthetic.faulting_points(shape)(0.01)
    assert 900 <= n <= 1100
    for g in range(50):
        for idx, _label, t in shape.equip_points(g):
            if common.is_faulting(idx, t, 0.5):
                assert t[3] == "Number"
    faults = [common.fault_value(7, tick) for tick in range(4 * common.FAULT_HALF_PERIOD)]
    assert faults.count(common.FAULT_VALUE) == faults.count(None) == 2 * common.FAULT_HALF_PERIOD


def test_equip_names_are_unique_across_sites():
    shape = common.Shape(10_000, equips_per_site=50)
    names = [shape.equip_prefix(g).rsplit("/", 1)[-1] for g in range(shape.equips)]
    assert len(set(names)) == len(names)  # the engine names outputs after this segment


def test_derivation_definitions_chain_and_pass_their_own_tests():
    plan = synthetic.plan_for(_args(interval=60.0, synthetic_per_equip=5, synthetic_mix="60:20:20", fault_share=0.0))
    ids = {}
    bodies = []
    for chain, level in plan.levels():
        body = synthetic.derivation_body(plan, chain, level, ids)
        ids[(chain, level)] = f"id{level}"
        bodies.append(body)
        fn = sandbox.compile_fn(body["fn_source"])
        assert all(r["passed"] for r in sandbox.run_test_cases(fn, body["test_cases"])), body["name"]
    l1, l2, l3 = bodies
    assert l1["applies_to_topic_glob"] and l2["applies_to_topic_glob"] and "applies_to_topic_glob" not in l3
    assert l3["interval_seconds"] == 60.0 and l3["depends_on"] == ["id2"] and l2["depends_on"] == ["id1"]
    assert l1["input_vars"] == ["a", "b"]  # multi-input
    assert 'urn:point:computed/id1/' in l2["select"] and 'urn:point:computed/id2/' in l3["select"]  # chained


def test_the_function_returns_the_newest_origin_timestamp():
    fn = sandbox.compile_fn(synthetic.FN_SOURCE)
    t = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
    # raw input: its sample's ts; derived input: its value (an origin ts)
    assert fn({"a": [(t, 70.0)], "d1": [(t, t.timestamp() + 5)]}, {}) == t.timestamp() + 5
    assert fn({"a": [], "b": []}, {}) is None


def test_rules_never_fire_on_generated_values():
    plan = synthetic.plan_for(_args(interval=300.0, synthetic_per_equip=10, synthetic_mix="60:20:20", fault_share=0.0))
    rules = synthetic.rule_bodies(plan)
    assert [r["mode"] for r in rules] == ["cur", "cur", "his", "his"]
    assert {r["type"] for r in rules if r["mode"] == "his"} == {"stuck", "stale"}
    for r in rules:
        label = r["applies_to"]["topic_glob"].rsplit("/", 1)[-1]
        assert next(t for t in common.TEMPLATES if t[0] == label)[4] in ("sine", "sine-sat", "walk")  # always moving


def _hb(cur_rows, tick_rows, passes, busy, evals, cur_busy_de=0.0, cur_busy_fd=0.0):
    return {"derivation_engine": {"cur_rows": cur_rows, "cur_busy_s": cur_busy_de,
                                  "tick": {"rows": tick_rows, "passes": passes, "busy_s": busy, "interval_s": 60.0}},
            "fault_detector": {"cur_evaluations": evals, "cur_busy_s": cur_busy_fd, "interval_seconds": 30.0,
                               "his": {"rules": 3}}}


def test_summary_and_reasons():
    plan = synthetic.plan_for(_args(interval=60.0, synthetic_per_equip=5, synthetic_mix="60:20:20", fault_share=0.01))
    samples = [{"synthetic": {"derivation_engine": {"tick": {"last_s": x, "late_s": y}},
                              "fault_detector": {"his": {"sweep_s": 0.5}}}} for x, y in ((1.0, 0.1), (2.0, 0.3))]
    exp = plan.expected(10_000)
    dt = 600.0
    h0 = _hb(0, 0, 0, 0.0, 0)
    h1 = _hb(exp["cur_rows_per_s"] * dt, exp["tick_rows_per_s"] * dt, 10, 15.0, exp["cur_rule_evaluations_per_s"] * dt,
             60.0, 30.0)
    lag = {"L1": {"n": 10, "p50_s": 0.5, "p95_s": 1.0, "max_s": 1.2}, "L3": {"n": 10, "p50_s": 30.0, "p95_s": 58.0,
                                                                          "max_s": 60.0}}
    syn = synthetic.summarize(plan, 10_000, samples, h0, h1, dt, {"opened": 3, "resolved": 3, "open_at_end": 2},
                              {"received": 6}, lag, {"p50_s": 0.3})
    d = syn["derivations"]
    assert d["rows_ratio"] == pytest.approx(1.0, abs=0.01) and d["cur_busy"] == 0.1
    assert d["tick"]["mean_s"] == 1.5 and d["tick"]["busy"] == 0.025 and d["tick"]["late_s_max"] == 0.3
    assert syn["rules"]["cur_ratio"] == pytest.approx(1.0, abs=0.01) and syn["rules"]["cur_busy"] == 0.05
    assert syn["chain_lag"]["top"]["p95_s"] == 58.0 and syn["chain_lag"]["top_minus_ingest_p50_s"] == 29.7
    assert syn["faults"]["transitions_per_s"] == 0.01 and syn["faults"]["webhooks"] == {"received": 6}
    assert synthetic.reasons({"synthetic": syn}) == []

    syn["derivations"]["tick"]["late_s_max"] = 75.0
    syn["chain_lag"]["top"]["p95_s"] = 200.0
    syn["rules"]["cur_ratio"], syn["rules"]["cur_evaluations_per_s"] = 0.5, 10.0
    why = synthetic.reasons({"synthetic": syn})
    assert any(w.startswith("derivation ticks behind") for w in why)
    assert any(w.startswith("derivation chain top lags") for w in why)
    assert any(w.startswith("Cur-mode rule evaluations") for w in why)
    assert synthetic.reasons({}) == []

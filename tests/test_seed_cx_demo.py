"""The data side of scripts/seed_cx_demo.py: pure functions, no server."""

import importlib.util
import random
from pathlib import Path

# scripts/ isn't a package, so load the script by path (CI's import roots differ
# from a local checkout's).
_spec = importlib.util.spec_from_file_location("seed_cx_demo", Path(__file__).resolve().parent.parent / "scripts" / "seed_cx_demo.py")
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)


def test_ahu_has_the_seven_template_points():
    ahu = next(e for e in seed.equips("newbuild", 1) if e[2] == "Air_Handling_Unit")
    assert {p[1] for p in ahu[3]} == {
        "Supply_Air_Temperature_Sensor", "Supply_Air_Temperature_Setpoint", "Fan_Status",
        "Fan_Command", "Return_Air_Temperature_Sensor", "Mixed_Air_Temperature_Sensor",
        "Outside_Air_Temperature_Sensor"}


def test_keys_are_topic_shaped_under_demo_root():
    for key, *_ in seed.equips("campus-01", 1):
        assert key.startswith(seed.ROOT + "/campus-01/")


def test_planted_defects_show_up_in_the_data():
    rng = random.Random(1)
    rows = seed.readings("newbuild", 2, 0, 24 * 3600, {"ahu-2/sat": "stuck", "ahu-2/mat": "stale", "ahu-1/oat": "range"}, rng)
    by = lambda suffix: [r for r in rows if r["point"].endswith(suffix)]
    assert {r["value"] for r in by("ahu-2/sat")} == {55.0}                  # stuck: one value
    assert max(r["ts"] for r in by("ahu-2/mat")) < 0                        # stale: nothing inside the window
    assert min(r["value"] for r in by("ahu-1/oat")) == 140.0                # out of range
    assert max(r["ts"] for r in by("ahu-2/rat")) == 24 * 3600               # healthy: runs to the end


def test_same_seed_same_data():
    a = seed.readings("newbuild", 1, 0, 3600, {}, random.Random(3))
    assert a == seed.readings("newbuild", 1, 0, 3600, {}, random.Random(3))


def test_project_prefix_covers_exactly_its_own_equipment():
    for slug in ("newbuild", "campus-07"):
        assert all(key.startswith(seed.prefix(slug) + "/") for key, *_ in seed.equips(slug, 2))
    assert not any(k.startswith(seed.prefix("campus-1") + "/") for k, *_ in seed.equips("campus-10", 1))


def test_vav_points_arrive_only_with_the_fix():
    vav = lambda fixed: next(e for e in seed.equips("newbuild", 1, fixed) if e[2] == "Variable_Air_Volume_Box")
    assert {p[1] for p in vav(False)[3]} == {"Zone_Air_Temperature_Sensor", "Zone_Air_Temperature_Setpoint"}
    assert {"Damper_Position_Command", "Air_Flow_Sensor"} <= {p[1] for p in vav(True)[3]}
    rows = seed.readings("newbuild", 1, 0, 60, {}, random.Random(2), fixed=True, step=15)
    assert {r["ts"] for r in rows if r["point"].endswith("vav-1/airflow")} == {0, 15, 30, 45, 60}


def test_gate_text_lists_each_unmet_rule():
    gate = {"unmet": [{"rule": "no_open_issues", "message": "2 issue(s) not closed"}]}
    assert seed.gate_text(gate) == "  - no_open_issues: 2 issue(s) not closed"
    assert seed.gate_text({"unmet": []}) == "  (met)"

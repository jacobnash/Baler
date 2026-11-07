"""The data side of scripts/seed_cx_demo.py: pure functions, no server."""

import random

from scripts import seed_cx_demo as seed


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
    assert max(r["ts"] for r in by("ahu-2/mat")) < 24 * 3600 * 0.6          # stale: stops early
    assert min(r["value"] for r in by("ahu-1/oat")) == 140.0                # out of range
    assert max(r["ts"] for r in by("ahu-2/rat")) == 24 * 3600               # healthy: runs to the end


def test_same_seed_same_data():
    a = seed.readings("newbuild", 1, 0, 3600, {}, random.Random(3))
    assert a == seed.readings("newbuild", 1, 0, 3600, {}, random.Random(3))

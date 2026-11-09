"""baler.sizing (#195): the deployment sizing calculator. Pure arithmetic over
the scale work's measured numbers, so every test is a unit test."""

import json

import pytest

from baler import sizing


def _line(s, label):
    return next(ln for ln in s.lines if ln.label == label)


def test_load_counts_synthetics_in_total_points():
    s = sizing.size(1_000_000, 300, 10)  # Jacob's target shape, 10 points per equip
    assert s.equips == 100_000 and s.synthetic_points == 1_000_000 and s.total_points == 2_000_000
    assert s.readings_per_s == pytest.approx(1_000_000 / 300)  # the listener sees field points only
    assert s.rows_per_s == pytest.approx(2_000_000 / 300)  # derivations write history too
    assert s.servers_target == 2  # one per ~1M total points


def test_synthetic_history_share_only_moves_history_rows():
    a = sizing.size(100_000, 60, 10, synthetic_history_share=0.0)
    assert a.rows_per_s == pytest.approx(a.readings_per_s)
    assert a.total_points == 200_000  # rules still count toward graph and split


def test_raw_disk_uses_measured_bytes_per_row():
    s = sizing.size(100_000, 5)  # the 100k stress step: 20,000 rows/s
    assert s.raw_bytes_per_day == pytest.approx(20_000 * 86400 * 250)
    assert _line(s, "raw (uncompressed) per day").kind == "measured"


def test_chunk_settings_match_history_chunks_recommend():
    s = sizing.size(100_000, 5, pg_memory_bytes=32 * 10**9)
    assert (s.chunk_interval, s.compress_after, s.compress_schedule) == ("1 hour", "3 hours", "30 minutes")
    small = sizing.size(50, 5, pg_memory_bytes=8 * 10**9)  # history-compression.mdx's small site
    assert (small.chunk_interval, small.compress_after) == ("7 days", "7 days")
    assert "BALER_HISTORY_CHUNK_INTERVAL=\n" in sizing.render(small)


def test_compressed_bytes_per_row_follows_batch_size():
    assert sizing.compressed_bytes_per_row(2016) == (8.2, "measured")  # full batches
    assert sizing.compressed_bytes_per_row(60) == (15.7, "measured")  # the local soak's batch
    v, kind = sizing.compressed_bytes_per_row(12)
    assert v == 15.7 and kind == "extrapolated"  # below anything measured: a lower bound
    mid, kind = sizing.compressed_bytes_per_row(300)
    assert 8.2 < mid < 15.7 and kind == "extrapolated"


def test_slow_reporting_with_short_chunks_is_flagged():
    s = sizing.size(1_000_000, 300, 10)
    assert s.rows_per_point_per_chunk < 60
    assert _line(s, "compressed per day").kind == "extrapolated"
    assert _line(s, "chunk for full compressed batches").value == ">= 84 h"


def test_listener_workers_from_measured_per_worker_rate():
    assert sizing.size(10_000, 5).listener_workers == 1  # 2,000/s
    w = sizing.size(100_000, 5)  # 20,000/s
    assert w.listener_workers == 4
    assert _line(w, "BALER_LISTENER_WORKERS").kind == "extrapolated"  # past 18,232/s measured
    assert _line(sizing.size(50_000, 5), "BALER_LISTENER_WORKERS").kind == "measured"


def test_envelope_measured_only_inside_a_passing_step():
    assert _line(sizing.size(8_000, 5), "measured envelope").kind == "measured"
    assert "25,000 points" in _line(sizing.size(25_000, 5), "measured envelope").value
    assert _line(sizing.size(100_000, 300), "measured envelope").kind == "pending"
    assert "beyond" in _line(sizing.size(1_000_000, 60), "measured envelope").value


def test_pending_results_are_placeholders_until_filled(monkeypatch):
    s = sizing.size(500_000, 60, 5)
    assert _line(s, "measured safe points per server").value == "pending"
    monkeypatch.setitem(sizing.PENDING, "safe_points_typical_1min_n5", 750_000)
    filled = _line(sizing.size(500_000, 60, 5), "measured safe points per server")
    assert (filled.value, filled.kind) == ("750,000", "measured")


def test_every_line_is_labelled():
    s = sizing.size(250_000, 120, 7, buildings=10)
    kinds = {ln.kind for ln in s.lines}
    assert kinds <= {"measured", "extrapolated", "rule", "target", "pending", "assumption"}
    assert all(ln.basis for ln in s.lines)


def test_read_probe_extrapolation_crosses_budget():
    assert sizing.size(100_000, 5).read_point_s == pytest.approx(1.08)
    assert sizing.size(1_000_000, 300).read_point_s > sizing.READ_POINT_BUDGET_S


def test_spool_flags_the_default_cap():
    s = sizing.size(100_000, 60, buildings=1)  # 1,667 readings/s for 24 h
    assert s.spool_rows_per_building == int(100_000 / 60 * 24 * 3600 * 1.1) > 5_000_000
    assert "WINDROW_SPOOL_MAX_ROWS" in _line(s, "Windrow spool per building").basis
    assert "WINDROW_SPOOL_MAX_ROWS" not in _line(sizing.size(500, 60), "Windrow spool per building").basis


@pytest.mark.parametrize("args", [(0, 60), (10, 0), (10, 60, -1)])
def test_rejects_bad_input(args):
    with pytest.raises(ValueError):
        sizing.size(*args)


def test_cli_text_and_json(capsys):
    assert sizing.main(["--points", "20000", "--interval", "60", "--synthetic-per-equip", "5"]) == 0
    out = capsys.readouterr().out
    assert "BALER_LISTENER_WORKERS=1" in out and "(pending)" in out
    assert sizing.main(["--points", "20000", "--json", "--pg-memory", "16g"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["pg_memory_bytes"] == 16 * 10**9 and d["total_points"] == 20_000

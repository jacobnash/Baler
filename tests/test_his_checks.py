"""his_checks: one meaning of stale/stuck for fault_detector and the
commissioning ladder (#38), documented edge cases included."""

from datetime import datetime, timedelta, timezone

from baler import his_checks

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)


def test_stale():
    assert his_checks.is_stale(NOW - timedelta(seconds=120), NOW, 120)
    assert not his_checks.is_stale(NOW - timedelta(seconds=119), NOW, 120)
    assert not his_checks.is_stale(None, NOW, 120)  # never reported is not stale


def test_stuck():
    assert his_checks.is_stuck([72.0, 72.0, 72])  # same value, any numeric spelling
    assert not his_checks.is_stuck([72.0, 72.1])
    assert not his_checks.is_stuck([])  # silence is not stuck
    assert his_checks.is_stuck(["on", "on"])
    assert not his_checks.is_stuck([True, 1.0])  # a bool is not the number 1 (SQL keys differ too)
    assert his_checks.stuck_verdict(5, 1) and not his_checks.stuck_verdict(0, 0)

"""Phases are first-class and editable per project (Jacob: explicit "but
make them manageable"), #37."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from baler.commissioning import phases

T0 = datetime(2026, 11, 6, tzinfo=timezone.utc)


def test_defaults_cover_the_cx_lifecycle():
    keys = [p["key"] for p in phases.default_phases()]
    assert keys == ["design", "construction", "startup", "functional", "acceptance", "warranty", "operations"]
    assert all(p["status"] == "pending" for p in phases.default_phases())


def test_forward_completes_back_reopens():
    ps = phases.set_current(phases.default_phases(), "startup", T0)
    assert phases.current(ps) == "startup"
    ps = phases.set_current(ps, "functional", T0 + timedelta(days=1))
    by = {p["key"]: p for p in ps}
    assert by["startup"]["status"] == "complete" and by["startup"]["completed_at"] == T0 + timedelta(days=1)
    assert by["functional"]["started_at"] == T0 + timedelta(days=1)
    ps = phases.set_current(ps, "startup", T0 + timedelta(days=2))  # failed functional test sends it back
    by = {p["key"]: p for p in ps}
    assert by["functional"]["status"] == "pending"
    assert by["startup"]["status"] == "active" and by["startup"]["started_at"] == T0 and by["startup"]["completed_at"] is None


def test_unknown_phase_rejected():
    with pytest.raises(ValueError):
        phases.set_current(phases.default_phases(), "nope", T0)


def test_normalize_renames_reorders_and_keeps_timestamps():
    ps = phases.set_current(phases.default_phases(), "construction", T0)
    edited = phases.normalize(
        [{"key": "construction", "name": "Install"}, {"key": "power-on", "name": "Power-on testing"}, {"key": "design", "status": "skipped"}],
        ps,
    )
    assert [p["key"] for p in edited] == ["construction", "power-on", "design"]
    assert [p["position"] for p in edited] == [0, 1, 2]
    assert edited[0]["name"] == "Install" and edited[0]["status"] == "active" and edited[0]["started_at"] == T0
    assert edited[2]["name"] == "Design" and edited[2]["status"] == "skipped"


@pytest.mark.parametrize("bad", [
    [], "x", [{"key": "Bad Key"}], [{"key": "a"}, {"key": "a"}], [{"key": "a", "status": "done"}],
    [{"key": "a", "status": "active"}, {"key": "b", "status": "active"}], [1],
])
def test_normalize_rejects(bad):
    with pytest.raises((ValueError, TypeError)):
        phases.normalize(bad)

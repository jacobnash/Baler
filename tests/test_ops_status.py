"""Ops status assembly unit tests (no live stack required)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from baler import ops_status


def test_rank_summary_prefers_down_over_degraded():
    assert ops_status._rank_summary(["ok", "quiet", "down"]) == "down"
    assert ops_status._rank_summary(["ok", "degraded"]) == "degraded"
    assert ops_status._rank_summary(["ok", "ok"]) == "ok"


def test_infer_ingest_quiet_when_no_points():
    state, detail = ops_status._infer_ingest({"last_ingest_at": None}, platform_ok=True)
    assert state == "quiet"
    assert "no points" in detail


def test_infer_ingest_ok_when_recent():
    recent = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    state, detail = ops_status._infer_ingest({"last_ingest_at": recent}, platform_ok=True)
    assert state == "ok"
    assert "last ingest" in detail


def test_infer_derivations_degraded_when_targets_disabled():
    state, detail = ops_status._infer_derivations(
        {"disabled_derivation_targets": 2}, platform_ok=True
    )
    assert state == "degraded"
    assert "2" in detail


@patch("baler.ops_status._building_signals")
@patch("baler.ops_status._probe_one")
def test_collect_status_shape(mock_probe, mock_building):
    mock_building.return_value = {
        "last_ingest_at": None,
        "points_seen_last_hour": 0,
        "open_faults": 0,
        "disabled_derivation_targets": 0,
        "org_count": 0,
    }

    def fake_probe(comp, building, platform_states):
        return {
            "id": comp["id"],
            "label": comp["label"],
            "layer": comp["layer"],
            "service": comp["service"],
            "state": "ok",
            "detail": None,
        }

    mock_probe.side_effect = fake_probe
    doc = ops_status.collect_status()
    assert doc["summary"] == "ok"
    assert doc["building"]["needs_first_org"] is True
    assert {c["id"] for c in doc["platform"]} >= {"historian", "live_bus", "sign_in"}
    assert {c["id"] for c in doc["jobs"]} >= {"ingest_listener", "fault_detector"}
    assert len(doc["first_actions"]) >= 2

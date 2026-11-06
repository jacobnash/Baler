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
        {"disabled_derivation_targets": 2, "heartbeats": {"derivation_engine": _now()}}, platform_ok=True
    )
    assert state == "degraded"
    assert "2" in detail


def _now(seconds_ago: int = 0) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def test_daemons_are_down_without_a_fresh_heartbeat():
    """Was "heartbeat not wired yet" - always ok, even with the daemon dead."""
    assert ops_status._infer_faults({}, platform_ok=True)[0] == "down"
    stale = {"heartbeats": {"fault_detector": _now(600), "derivation_engine": _now(600)}}
    assert ops_status._infer_faults(stale, platform_ok=True) == ("down", "last heartbeat 600s ago")
    assert ops_status._infer_derivations(stale, platform_ok=True)[0] == "down"
    fresh = {"open_faults": 3, "heartbeats": {"fault_detector": _now(20), "derivation_engine": _now(2)}}
    assert ops_status._infer_faults(fresh, platform_ok=True) == ("ok", "running; 3 open fault(s)")
    assert ops_status._infer_derivations(fresh, platform_ok=True)[0] == "ok"


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


def test_building_cards_judge_faults_and_disabled_targets():
    cards = {c["id"]: c for c in ops_status.building_cards({
        "last_ingest_at": "2026-11-06T21:00:00+00:00", "points_seen_last_hour": 12,
        "open_faults": 91, "disabled_derivation_targets": 0,
    })}
    assert cards["open_faults"]["state"] == "degraded"
    assert cards["open_faults"]["detail"] == "91"
    assert cards["open_faults"]["href"] == "alarms.html"
    assert cards["disabled_targets"]["state"] == "ok"
    assert cards["last_ingest"]["state"] == "ok"
    assert cards["points_last_hour"]["state"] == "ok"
    assert all(c["layer"] == "building" for c in cards.values())


@patch("baler.ops_status._building_signals")
@patch("baler.ops_status._probe_one")
def test_open_faults_are_not_all_green(mock_probe, mock_building):
    """Baler#64: a DEGRADED Building card shows under Needs attention and
    the summary isn't ok, even with every service healthy."""
    mock_building.return_value = {
        "last_ingest_at": datetime.now(timezone.utc).isoformat(),
        "points_seen_last_hour": 40,
        "open_faults": 91,
        "disabled_derivation_targets": 0,
        "org_count": 1,
    }
    mock_probe.side_effect = lambda comp, building, platform_states: {
        "id": comp["id"], "label": comp["label"], "layer": comp["layer"],
        "service": comp["service"], "state": "ok", "detail": None,
    }
    doc = ops_status.collect_status()
    assert doc["summary"] == "degraded"
    assert [c["id"] for c in doc["attention"]] == ["open_faults"]
    card = next(c for c in doc["building"]["cards"] if c["id"] == "open_faults")
    assert card["state"] == "degraded"


@patch("baler.ops_status._building_signals")
@patch("baler.ops_status._probe_one")
def test_healthy_building_is_all_green(mock_probe, mock_building):
    mock_building.return_value = {
        "last_ingest_at": datetime.now(timezone.utc).isoformat(),
        "points_seen_last_hour": 40,
        "open_faults": 0,
        "disabled_derivation_targets": 0,
        "org_count": 1,
    }
    mock_probe.side_effect = lambda comp, building, platform_states: {
        "id": comp["id"], "label": comp["label"], "layer": comp["layer"],
        "service": comp["service"], "state": "ok", "detail": None,
    }
    doc = ops_status.collect_status()
    assert doc["summary"] == "ok"
    assert doc["attention"] == []


class _RowsConn:
    def __init__(self, rows):
        self.rows = rows
        self.sql = None

    def execute(self, sql):
        self.sql = sql
        rows = self.rows

        class _Cur:
            def fetchall(self):
                return rows

            def fetchone(self):
                return (len(rows),)

        return _Cur()


def test_building_counts_only_what_the_caller_may_see():
    """Ops' open-fault count used to cover every org while Alarms shows only
    your own - a mismatch, and a leak of other orgs' numbers."""
    from baler.ops_status import _count_visible
    from baler.scope import Scope

    rows = [("urn:point:fbf/mine/a",), ("urn:point:fbf/mine/b",), ("urn:point:fbf/theirs/c",), (None,)]
    assert _count_visible(_RowsConn(rows), "SELECT point_uri FROM faults", Scope(("fbf/mine",))) == 2
    assert _count_visible(_RowsConn(rows), "SELECT point_uri FROM faults", Scope(None, ("fbf/theirs",))) == 2
    conn = _RowsConn(rows)
    assert _count_visible(conn, "SELECT point_uri FROM faults", Scope.everything()) == 4
    assert conn.sql.startswith("SELECT COUNT(*)")  # unscoped: counted in SQL, no rows shipped

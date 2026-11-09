"""Ops status assembly unit tests (no live stack required; the one integration-marked test needs postgres)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

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


def test_infer_ingest_degraded_after_broker_drops():
    """#233: overload is never silent - drops the broker counted turn the
    ingest card degraded for an hour."""
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(seconds=30)).isoformat()
    lst = {"workers": 2, "broker_dropped_since_start": 4741, "broker_last_drop_at": (now - timedelta(minutes=5)).isoformat()}
    state, detail = ops_status._infer_ingest({"last_ingest_at": recent, "listener": lst}, platform_ok=True)
    assert state == "degraded" and "4741" in detail and "BALER_LISTENER_WORKERS" in detail
    lst["broker_last_drop_at"] = (now - timedelta(hours=2)).isoformat()
    assert ops_status._infer_ingest({"last_ingest_at": recent, "listener": lst}, platform_ok=True)[0] == "ok"


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


def _fake_probe(comp, building, platform_states):
    return {"id": comp["id"], "label": comp["label"], "layer": comp["layer"], "service": comp["service"],
            "state": "ok", "detail": None}


def test_a_hung_probe_is_down_after_the_deadline_not_waited_for():
    """Name resolution has no timeout: right after a deploy one lookup hung
    /ops/status for 8.8 s on nash-srv (Baler#106)."""
    import threading
    import time

    release = threading.Event()

    def probe(comp, building, platform_states):
        if comp["id"] == "data_api":
            release.wait(10)
        return _fake_probe(comp, building, platform_states)

    direct = [c for c in ops_status.COMPONENTS if not c["probe"].startswith("inferred_")]
    with patch("baler.ops_status._probe_one", side_effect=probe):
        t0 = time.monotonic()
        results = ops_status._run_probes(direct, deadline=0.2)
        elapsed = time.monotonic() - t0
    release.set()
    assert elapsed < 1
    assert results["data_api"]["state"] == "down" and "no answer" in results["data_api"]["detail"]
    assert results["sign_in"]["state"] == "ok"
    assert set(results) == {c["id"] for c in direct}


@patch("baler.ops_status._building_signals")
def test_collect_status_answers_from_the_background_round(mock_building):
    mock_building.return_value = {"last_ingest_at": None, "points_seen_last_hour": 0, "open_faults": 0,
                                  "disabled_derivation_targets": 0, "org_count": 1}
    cache = ops_status._ProbeCache()
    with patch("baler.ops_status._probe_one", side_effect=_fake_probe):
        cache.refresh()
    with patch("baler.ops_status._probes", cache), patch("baler.ops_status._probe_one", side_effect=_fake_probe) as probe:
        doc = ops_status.collect_status()
    # only the inferred (no I/O) components were judged per request
    assert {c[0][0]["id"] for c in probe.call_args_list} == {"ingest_listener", "fault_detector", "derivation_engine",
                                                             "backups", "restore_drill"}
    assert {c["id"] for c in doc["platform"]} >= {"historian", "data_api"}


def test_a_stale_background_round_is_not_used():
    cache = ops_status._ProbeCache()
    with patch("baler.ops_status._probe_one", side_effect=_fake_probe):
        cache.refresh()
    assert cache.latest() is not None
    cache._at -= 4 * cache._interval
    assert cache.latest() is None


def test_a_primed_prober_has_a_round_before_start_returns():
    """The first /status after a deploy found no round yet and probed for
    itself (2.3 s, Baler#172): ops_api primes the first round before it
    listens."""
    cache = ops_status._ProbeCache()
    with patch("baler.ops_status._probe_one", side_effect=_fake_probe):
        cache.start(interval=3600, prime=True)
        assert cache.latest() is not None


@pytest.mark.integration
def test_points_seen_last_hour_from_the_registry_matches_the_history_scan():
    """Baler#244: the count comes from point_registry, whose last_ts trails
    a point's newest sample by up to REGISTRY_GRANULARITY_S; the points in
    that band at the hour's edge are confirmed against point_history, so
    the answer is the old scan's exactly. Needs docker compose's postgres."""
    from baler import timeseries

    now = datetime.now(timezone.utc)
    hour = now - timedelta(hours=1)
    cases = {  # point: (registry last_ts, newest sample)
        "seen": (now - timedelta(minutes=10), now - timedelta(minutes=10)),
        "edge-seen": (hour - timedelta(seconds=10), hour + timedelta(seconds=15)),
        "edge-quiet": (hour - timedelta(seconds=20), hour - timedelta(seconds=15)),
        "quiet": (now - timedelta(hours=2), now - timedelta(hours=2)),
    }
    with timeseries.connect() as conn:
        conn.execute("DELETE FROM point_history WHERE point_uri LIKE 'urn:test-ops:%'")
        conn.execute("DELETE FROM point_registry WHERE point_uri LIKE 'urn:test-ops:%'")
        for name, (last_ts, newest) in cases.items():
            conn.execute("INSERT INTO point_history (point_uri, ts, value) VALUES (%s, %s, 1)", (f"urn:test-ops:{name}", newest))
            conn.execute("INSERT INTO point_registry (point_uri, last_ts) VALUES (%s, %s)", (f"urn:test-ops:{name}", last_ts))
        conn.commit()
        try:
            mine = " WHERE point_uri LIKE 'urn:test-ops:%'"
            new = {r[0] for r in conn.execute(f"SELECT point_uri FROM ({ops_status.SEEN_LAST_HOUR_SQL}) q{mine}")}
            old = {r[0] for r in conn.execute(
                "SELECT point_uri FROM point_history WHERE ts > now() - interval '1 hour'"
                " AND point_uri LIKE 'urn:test-ops:%' GROUP BY point_uri")}
            assert new == old == {"urn:test-ops:seen", "urn:test-ops:edge-seen"}
        finally:
            conn.execute("DELETE FROM point_history WHERE point_uri LIKE 'urn:test-ops:%'")
            conn.execute("DELETE FROM point_registry WHERE point_uri LIKE 'urn:test-ops:%'")
            conn.commit()


# --- the listener's backlog (#235)


def _recent():
    return (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()


def test_infer_ingest_judges_the_listeners_backlog():
    def state(**lst):
        return ops_status._infer_ingest({"last_ingest_at": _recent(), "listener": lst}, platform_ok=True)

    assert state(queue=10, queue_max=50_000, oldest_age_s=0.4)[0] == "ok"
    assert "queue 10" in state(queue=10, queue_max=50_000, oldest_age_s=0.4)[1]
    assert state(queue=10, queue_max=50_000, oldest_age_s=45)[0] == "degraded"
    assert state(queue=10, queue_max=50_000, oldest_age_s=400)[0] == "down"
    full = state(queue=45_000, queue_max=50_000, oldest_age_s=1, workers=2)
    assert full[0] == "degraded" and "BALER_LISTENER_WORKERS" in full[1]


def test_a_stale_listener_heartbeat_is_down_but_a_missing_one_is_not_judged():
    old = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    b = {"last_ingest_at": _recent(), "heartbeats": {"mqtt_listener": old}}
    assert ops_status._infer_ingest(b, platform_ok=True)[0] == "down"
    assert ops_status._infer_ingest({"last_ingest_at": _recent(), "heartbeats": {}}, platform_ok=True)[0] == "ok"


def test_ingest_summary_is_what_metrics_export():
    now = datetime(2026, 11, 9, 12, 0, 30, tzinfo=timezone.utc)
    b = {"last_ingest_at": "2026-11-09T12:00:00+00:00",
         "listener": {"queue": 3, "queue_max": 100, "oldest_age_s": 0.5, "workers": 2, "broker_dropped_since_start": 0}}
    s = ops_status.ingest_summary(b, now)
    assert s["lag_s"] == 30.0 and s["queue"] == 3 and s["queue_oldest_age_s"] == 0.5 and s["workers"] == 2
    assert ops_status.ingest_summary({}, now)["lag_s"] is None

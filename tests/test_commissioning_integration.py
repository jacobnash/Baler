"""commissioning PgRepo against real Postgres (#37). Needs `docker
compose up -d`: run with `pytest -m integration`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from baler import auth
from baler.commissioning import db, engine, phases
from baler.scope import Scope
from baler.store import Store
from baler.timeseries import connect_pool, write_point_value

pytestmark = pytest.mark.integration


@pytest.fixture
def repo():
    pool = connect_pool(min_size=1, max_size=2)
    with pool.connection() as conn:
        db.ensure_schema(conn)
        db.ensure_schema(conn)  # idempotent
    yield db.PgRepo(pool, Store())
    pool.close()


def test_project_phase_event_run_round_trip(repo):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with repo.pool.connection() as conn:
        org = auth.create_org(conn, f"cx-it-{now.timestamp()}", f"cx-{now.timestamp()}@example.com", "pw-123456789")
    org_id = org["id"]
    pid = db.new_id()
    plist = phases.set_current(phases.default_phases(), "startup", now)
    repo.create_project({"id": pid, "org_id": org_id, "site_id": None, "name": "IT", "settings": {"window_hours": 6},
                         "created_by": "it", "created_at": now, "updated_at": now, "phases": plist})
    p = repo.get_project(pid)
    assert p["name"] == "IT" and p["settings"] == {"window_hours": 6} and phases.current(p["phases"]) == "startup"
    assert [x["id"] for x in repo.list_projects(org_id)] == [pid]

    repo.save_phases(pid, phases.set_current(p["phases"], "functional", now), now)
    assert {x["key"]: x["status"] for x in repo.get_project(pid)["phases"]}["startup"] == "complete"
    repo.add_event(pid, "phase.set", "it", None, {"to": "functional"}, now)
    assert repo.list_events(pid)[0]["detail"] == {"to": "functional"}

    with repo.pool.connection() as conn:
        write_point_value(conn, "urn:point:cx-it/x", 1.0, now - timedelta(minutes=1))
    with repo.history() as h:
        assert h.latest(["urn:point:cx-it/x"])["urn:point:cx-it/x"][1] == 1.0
        summary, _results = engine.run(repo.store, h, [Scope.everything()], now)
    rid = db.new_id()
    fake = [{"equip_uri": "urn:equip:cx-it", "check_id": "exists", "subject": "urn:point:cx-it/x", "status": "pass",
             "severity": None, "symptom": None, "candidates": [], "evidence": {"last_ts": now.isoformat()}}]
    repo.save_run({"id": rid, "project_id": pid, "phase": "functional", "started_at": now, "finished_at": now,
                   "window_start": now - timedelta(hours=6), "window_end": now, "triggered_by": "it",
                   "scope": {"caller": {"prefixes": None, "exclude": []}, "site_prefixes": []}, "summary": summary}, fake)
    assert repo.get_run(rid)["phase"] == "functional"
    assert repo.get_results(rid) == fake
    assert [r["id"] for r in repo.list_runs(pid)] == [rid]

    repo.update_project(pid, now, archived_at=now)
    assert repo.list_projects(org_id) == []

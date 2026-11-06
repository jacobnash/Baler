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


def _org(repo, tag):
    stamp = f"{tag}-{datetime.now(timezone.utc).timestamp()}"
    with repo.pool.connection() as conn:
        return auth.create_org(conn, f"cx-it-{stamp}", f"cx-{stamp}@example.com", "pw-123456789")["id"]


def _new_project(repo, org_id, now, phase="startup"):
    pid = db.new_id()
    repo.create_project({"id": pid, "org_id": org_id, "site_id": None, "name": "IT", "settings": {}, "created_by": "it",
                         "created_at": now, "updated_at": now,
                         "phases": phases.set_current(phases.default_phases(), phase, now)})
    return pid


def _queued(pid, now, rid=None):
    return {"id": rid or db.new_id(), "project_id": pid, "phase": "startup", "status": "queued", "started_at": now,
            "window_start": now - timedelta(hours=1), "window_end": now, "triggered_by": "it",
            "scope": {"caller": {"prefixes": None, "exclude": []}, "site_prefixes": []}}


def test_async_run_lifecycle(repo):
    """#57: queued -> running (progress) -> completed, one active run per
    project enforced by the partial unique index, and restart recovery."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    pid = _new_project(repo, _org(repo, "async"), now)

    first = _queued(pid, now)
    repo.create_run(first)
    got = repo.get_run(first["id"])
    assert got["status"] == "queued" and got["finished_at"] is None and got["summary"] is None
    with pytest.raises(db.RunActive) as exc:
        repo.create_run(_queued(pid, now))
    assert exc.value.run_id == first["id"]

    repo.set_run_status(first["id"], "running", {"stage": "starting"})
    repo.set_run_progress(first["id"], {"stage": "checks", "equipment_done": 3, "equipment_total": 9})
    assert repo.get_run(first["id"])["progress"]["equipment_done"] == 3
    res = [{"equip_uri": "urn:equip:cx-it", "check_id": "exists", "subject": "urn:point:cx-it/y", "status": "pass",
            "severity": None, "symptom": None, "candidates": [], "evidence": {}}]
    repo.complete_run(first["id"], now, {"by_status": {"pass": 1}}, res)
    done = repo.get_run(first["id"])
    assert done["status"] == "completed" and done["progress"] is None and done["summary"] == {"by_status": {"pass": 1}}
    assert repo.get_results(first["id"]) == res

    second = _queued(pid, now + timedelta(seconds=1))
    repo.create_run(second)  # the completed one doesn't block
    repo.set_run_status(second["id"], "running", {"stage": "history"})
    hit = repo.fail_interrupted_runs(now)
    assert {"id": second["id"], "project_id": pid} in hit
    lost = repo.get_run(second["id"])
    assert lost["status"] == "failed" and lost["error"] == db.INTERRUPTED and lost["finished_at"] is not None
    assert repo.get_run(first["id"])["status"] == "completed"
    repo.create_run(_queued(pid, now + timedelta(seconds=2)))  # unblocked


def test_run_through_the_api_on_postgres(repo, monkeypatch):
    """The #57 report end to end on PgRepo: 202 at once, run and project
    phase non-null, poll to completed."""
    import time
    from http.server import ThreadingHTTPServer
    from threading import Thread

    import jwt as pyjwt
    import requests

    from baler.commissioning_api import make_handler

    monkeypatch.setenv("BALER_GATEWAY_SECRET", "gw-it")
    monkeypatch.setenv("BALER_JWT_SECRET", "j" * 32)
    org_id = _org(repo, "api")
    tok = pyjwt.encode({"sub": "it", "org_id": org_id, "site_ids": [], "role": "operator", "kind": "session"},
                       "j" * 32, algorithm="HS256")
    headers = {"X-Gateway-Secret": "gw-it", "Authorization": f"Bearer {tok}"}
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(repo))
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        p = requests.post(f"{base}/projects", json={"name": "IT", "phase": "startup"}, headers=headers)
        assert p.status_code == 201 and p.json()["phase"] == "startup"
        r = requests.post(f"{base}/projects/{p.json()['id']}/runs", json={"window_hours": 1}, headers=headers)
        assert r.status_code == 202 and r.json()["status"] == "queued" and r.json()["phase"] == "startup"
        deadline = time.monotonic() + 30
        while (run := requests.get(f"{base}/runs/{r.json()['id']}", headers=headers).json()["run"])["status"] in db.ACTIVE:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert run["status"] == "completed", run
        assert run["phase"] == "startup" and run["summary"]["phase"] == "startup"
    finally:
        server.shutdown()

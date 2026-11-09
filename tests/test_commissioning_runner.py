"""Background commissioning runs (#57): the worker, progress, failure and
restart recovery, against MemoryRepo (no services)."""

from __future__ import annotations

from datetime import timedelta

import cx_fixtures as fx
import pytest

from baler.commissioning import db, engine, phases, runner
from baler.scope import Scope


@pytest.fixture
def repo():
    r = db.MemoryRepo(store=fx.store(), history=fx.history())
    r.create_project({"id": "p1", "org_id": "o", "site_id": None, "name": "P", "settings": {}, "created_by": "u",
                      "created_at": fx.NOW, "updated_at": fx.NOW,
                      "phases": phases.set_current(phases.default_phases(), "functional", fx.NOW)})
    return r


def _queue(repo, rid="r1", status="queued"):
    repo.create_run({"id": rid, "project_id": "p1", "phase": "functional", "status": status, "started_at": fx.NOW,
                     "window_start": fx.NOW - timedelta(hours=24), "window_end": fx.NOW, "triggered_by": "u",
                     "scope": {}})
    return runner.RunJob(rid, "p1", "functional", [Scope.everything()], fx.NOW, 24.0, "u", "u@example.com")


def test_execute_completes_with_results_and_event(repo):
    runner.execute(repo, _queue(repo), lambda: fx.NOW + timedelta(seconds=5))
    run = repo.get_run("r1")
    assert run["status"] == "completed" and run["finished_at"] == (fx.NOW + timedelta(seconds=5)).isoformat()
    assert run["summary"]["phase"] == "functional" and run["summary"]["equipment"] == 4
    assert run["progress"] is None and run["error"] is None
    assert repo.get_results("r1") and repo.list_events("p1")[0]["action"] == "run.complete"


def test_execute_failure_is_recorded_not_raised(repo):
    def boom(*a, **k):
        raise ValueError("x" * 5000)

    runner.execute(repo, _queue(repo), lambda: fx.NOW, run_fn=boom)
    run = repo.get_run("r1")
    assert run["status"] == "failed" and run["error"].startswith("ValueError: xxx")
    assert len(run["error"]) == runner.MAX_ERROR and repo.get_results("r1") == []
    assert repo.list_events("p1")[0]["action"] == "run.fail"


def test_execute_survives_a_repo_that_cannot_record_failure(repo):
    def boom(*a, **k):
        raise RuntimeError("down")

    def no_db(*a, **k):
        raise ConnectionError("db gone")

    repo.fail_run = no_db
    runner.execute(repo, _queue(repo), lambda: fx.NOW, run_fn=boom)  # no exception escapes the worker
    assert repo.get_run("r1")["status"] == "running"  # left for startup recovery


def test_one_active_run_per_project(repo):
    _queue(repo, "r1")
    with pytest.raises(db.RunActive) as exc:
        _queue(repo, "r2")
    assert exc.value.run_id == "r1"
    repo.fail_run("r1", fx.NOW, "x")
    _queue(repo, "r2")  # failed runs don't count


def test_interrupted_runs_fail_at_startup(repo):
    _queue(repo, "r1")
    repo.set_run_status("r1", "running", {"stage": "checks"})
    repo.save_run({"id": "old", "project_id": "p1", "phase": None, "started_at": fx.NOW, "finished_at": fx.NOW,
                   "window_start": fx.NOW, "window_end": fx.NOW, "scope": {}, "summary": {}}, [])
    hit = repo.fail_interrupted_runs(fx.NOW)
    assert hit == [{"id": "r1", "project_id": "p1"}]
    r1 = repo.get_run("r1")
    assert r1["status"] == "failed" and r1["error"] == db.INTERRUPTED and r1["finished_at"] == fx.NOW.isoformat()
    assert repo.get_run("old")["status"] == "completed"
    assert repo.fail_interrupted_runs(fx.NOW) == []


def test_engine_reports_progress():
    seen = []
    engine.run(fx.store(), fx.history(), [Scope.everything()], fx.NOW, progress=seen.append)
    stages = [p["stage"] for p in seen]
    assert stages[:2] == ["inventory", "history"] and set(stages[2:]) == {"checks"}
    assert seen[-1]["equipment_total"] == 4 and seen[-1]["equipment_done"] == 3 and seen[1]["points"] == 8


def test_progress_writes_are_throttled(repo, monkeypatch):
    writes = []
    monkeypatch.setattr(repo, "set_run_progress", lambda rid, p: writes.append(p["stage"]))
    runner.execute(repo, _queue(repo), lambda: fx.NOW)
    assert writes == ["inventory"]  # the rest land within PROGRESS_EVERY_S of it


def test_worker_runs_jobs_in_the_background(repo):
    w = runner.RunWorker(repo, lambda: fx.NOW, workers=1)
    w.submit(_queue(repo))
    w.join()
    assert repo.get_run("r1")["status"] == "completed"


class _ScopeRepo:
    """Just what project_scopes reads."""

    def __init__(self, caller, sites=None):
        self.caller = caller
        self.sites = sites or {}

    def scope_for(self, claims):
        return self.caller

    def get_site(self, site_id):
        return self.sites.get(site_id)


def test_project_prefixes_narrow_an_org_wide_project():
    """#75: an operator's org-wide project can cover one building by prefix."""
    from baler.commissioning.runner import project_scopes
    from baler.scope import Scope

    project = {"site_id": None, "settings": {"prefixes": ["fbf/demo-cx/campus-07"]}}
    scopes, prefixes = project_scopes(_ScopeRepo(Scope.everything()), project, {})
    assert prefixes == ["fbf/demo-cx/campus-07"]
    visible = lambda uri: all(s.allows(uri) for s in scopes)
    assert visible("urn:equip:fbf/demo-cx/campus-07/ahu-1")
    assert not visible("urn:equip:fbf/demo-cx/campus-08/ahu-1")
    assert not visible("urn:equip:fbf/hospital-a/ahu-1")


def test_project_prefixes_never_widen_the_callers_scope():
    from baler.commissioning.runner import project_scopes
    from baler.scope import Scope

    project = {"site_id": None, "settings": {"prefixes": ["fbf/other-org"]}}
    scopes, _ = project_scopes(_ScopeRepo(Scope(("fbf/mine",))), project, {})
    assert not all(s.allows("urn:equip:fbf/other-org/ahu-1") for s in scopes)


@pytest.mark.parametrize("bad", ["fbf/x", ["fbf/#"], [""], ["urn:equip:x"], [1]])
def test_bad_prefix_settings_are_refused(bad):
    from baler.commissioning_api import _check_settings

    with pytest.raises((TypeError, ValueError)):
        _check_settings({"prefixes": bad})


def test_prefix_settings_are_normalized():
    from baler.commissioning_api import _check_settings

    assert _check_settings({"prefixes": [" fbf/a/ ", "fbf/a", "fbf/b"]})["prefixes"] == ["fbf/a", "fbf/b"]


def test_the_scheduler_tick_runs_every_sweeper_even_if_one_fails():
    from datetime import datetime, timezone
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    calls = []

    def boom(now):
        raise RuntimeError("one sweeper failing")
    s = runner.Scheduler(db.MemoryRepo(), None, lambda: t0, every_s=3600, extra=[boom, calls.append])
    s.stop()
    s.tick()
    assert calls == [t0]

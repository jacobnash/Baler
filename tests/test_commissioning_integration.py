"""commissioning PgRepo against real Postgres (#37). Needs `docker
compose up -d`: run with `pytest -m integration`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from baler import auth
from baler.commissioning import db, engine, phases, report
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


def test_issues_and_risks_round_trip(repo):
    """Cx 4 (#40) on PgRepo: the one-active-issue index, upsert lookup by
    source, events and risks, and the reconcile the runner applies."""
    from datetime import date

    from baler.commissioning import runner

    now = datetime.now(timezone.utc).replace(microsecond=0)
    pid = _new_project(repo, _org(repo, "issues"), now)
    issue = {"id": db.new_id(), "project_id": pid, "equip_uri": "urn:equip:cx-it", "check_id": "exists",
             "subject": "urn:point:cx-it/x", "status": "open", "title": "stale", "severity": None, "assignee": None,
             "detail": {"symptom": "s"}, "source": "baler", "source_id": None, "opened_run": "r1",
             "last_seen_run": "r1", "closed_run": None, "created_by": "it", "created_at": now, "updated_at": now,
             "closed_at": None}
    repo.create_issue(issue)
    with pytest.raises(db.IssueExists) as exc:
        repo.create_issue({**issue, "id": db.new_id()})
    assert exc.value.issue_id == issue["id"]
    ext = {**issue, "id": db.new_id(), "check_id": None, "subject": None, "source": "cxalloy", "source_id": "A-1"}
    repo.create_issue(ext)
    assert repo.find_issue_by_source(pid, "cxalloy", "A-1")["id"] == ext["id"]
    assert {i["id"] for i in repo.list_issues(pid, ("open",))} == {issue["id"], ext["id"]}
    repo.update_issue(ext["id"], {"status": "closed", "closed_at": now, "detail": {"a": 1}})
    assert repo.get_issue(ext["id"])["detail"] == {"a": 1}
    repo.add_issue_event(issue["id"], "create", "it", None, {"x": 1}, now)
    assert repo.list_issue_events(issue["id"])[0]["detail"] == {"x": 1}

    rid = db.new_id()
    repo.create_risk({"id": rid, "issue_id": issue["id"], "accepted_by": "it", "accepted_email": None, "reason": "r",
                      "review_by": date(2000, 1, 1), "created_at": now, "revoked_at": None, "revoked_by": None})
    repo.update_issue(issue["id"], {"status": "risk_accepted"})
    assert repo.get_risk(rid)["review_by"] == date(2000, 1, 1)
    assert [r["id"] for r in repo.list_risks(pid)] == [rid]
    # a waiver from before #121 (no name, no source) still reads, as typed/none
    old = repo.get_risk(rid)
    assert old["accepted_name"] is None and old["accepted_name_source"] is None
    assert report.accepted_by(old, "Jacob Nash") == {"name": None, "email": "it", "source": None, "cxa": False}
    # review_by long past: the risk lapses, then the passing result resolves the issue (#86: never closes it)
    changes = runner.reconcile_issues(repo, pid, "r2", [{"check_id": "exists", "subject": "urn:point:cx-it/x", "status": "pass"}], now)
    assert [c[2] for c in changes] == ["risk.expired", "resolve.evidence"]
    got = repo.get_issue(issue["id"])
    assert got["status"] == "ready_for_retest" and got["resolved_by"] == {"run_id": "r2"} and got["closed_run"] is None
    assert repo.list_risks(pid) == [] and repo.get_risk(rid)["revoked_at"] is not None
    # a later run sees the point report after the fix: a retest, stored as JSONB
    later = (now + timedelta(minutes=1)).isoformat()
    changes = runner.reconcile_issues(repo, pid, "r3", [{"check_id": "exists", "subject": "urn:point:cx-it/x", "status": "pass",
                                                        "evidence": {"last_ts": later}}], now)
    assert [c[2] for c in changes] == ["retest.passed"]
    assert repo.get_issue(issue["id"])["retest"]["run_id"] == "r3"
    assert repo.list_issue_events(issue["id"])[0]["actor"] == "system"
    # the report reads every issue's events in one query (#150)
    assert repo.list_issue_events_many([issue["id"], ext["id"]]) == {
        issue["id"]: repo.list_issue_events(issue["id"]), ext["id"]: repo.list_issue_events(ext["id"])}
    repo.update_issue(issue["id"], {"status": "closed", "closed_at": now, "closed_run": "r3"})  # the person's close
    # closed: a new issue on the same check is allowed again
    repo.create_issue({**issue, "id": db.new_id()})


def test_procedures_and_test_runs_round_trip(repo):
    """Cx 6 (#42) on PgRepo: JSON columns, the one-active-test-run index,
    scheduled-project listing."""
    from baler.commissioning import procedures

    now = datetime.now(timezone.utc).replace(microsecond=0)
    pid = _new_project(repo, _org(repo, "proc"), now)
    proc = {"id": db.new_id(), "project_id": pid, **procedures.from_seed("boiler_startup"), "created_by": "it",
            "created_at": now, "updated_at": now}
    repo.create_procedure(proc)
    got = repo.get_procedure(proc["id"])
    assert got["steps"] == proc["steps"] and got["schedule"] is None and got["template"] == "boiler_startup"
    repo.update_procedure(proc["id"], {"schedule": {"every_hours": 6}, "updated_at": now})
    assert repo.get_procedure(proc["id"])["schedule"] == {"every_hours": 6}
    assert proc["id"] in {p["id"] for p in repo.list_procedures(None)}
    tr = {"id": db.new_id(), "project_id": pid, "procedure_id": proc["id"], "phase": "startup", "status": "queued",
          "started_at": now - timedelta(hours=2), "window_end": now, "requested_at": now, "finished_at": None,
          "triggered_by": "it", "scope": {}, "procedure": {"id": proc["id"]}, "equipment": None, "summary": None,
          "previous_id": None, "diff": None, "error": None}
    repo.create_test_run(tr)
    with pytest.raises(db.RunActive):
        repo.create_test_run({**tr, "id": db.new_id()})
    repo.update_test_run(tr["id"], {"status": "completed", "equipment": [{"equip_uri": "e", "steps": []}], "finished_at": now})
    assert repo.get_test_run(tr["id"])["equipment"] == [{"equip_uri": "e", "steps": []}]
    assert [r["id"] for r in repo.list_test_runs(proc["id"])] == [tr["id"]]
    # the report's one-query read: the newest completed run among the newest 20 (#150)
    newer = {**tr, "id": db.new_id(), "status": "failed", "requested_at": now + timedelta(seconds=1)}
    repo.create_test_run(newer)
    other = {"id": db.new_id(), "project_id": pid, **procedures.from_seed("boiler_startup"), "created_by": "it",
             "created_at": now, "updated_at": now}
    repo.create_procedure(other)
    latest = repo.latest_completed_test_runs([proc["id"], other["id"]])
    assert latest[proc["id"]] == next(t for t in repo.list_test_runs(proc["id"], 20) if t["status"] == "completed")
    assert latest[other["id"]] is None
    assert repo.latest_completed_test_runs([proc["id"]], window=1) == {proc["id"]: None}
    repo.update_project(pid, now, settings={"schedule": {"every_hours": 12}})
    assert pid in {p["id"] for p in repo.list_scheduled_projects()}
    repo.update_project(pid, now, archived_at=now)
    assert pid not in {p["id"] for p in repo.list_scheduled_projects()}


def test_spec_rows_round_trip(repo):
    """Cx 7 (#43) on PgRepo: documents, spec items and mappings through the
    generic row helpers, JSON columns intact."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    pid = _new_project(repo, _org(repo, "spec"), now)
    doc = {"id": db.new_id(), "project_id": pid, "filename": "M-601.pdf", "media_type": "application/pdf", "size": 3,
           "sha256": "x", "method": "llm", "status": "queued", "items": 0, "info": None, "error": None, "created_by": "it",
           "created_at": now, "finished_at": None}
    repo.put_rows("cx_spec_documents", [doc])
    repo.update_row("cx_spec_documents", doc["id"], {"status": "completed", "info": {"model": "m", "notes": ""}, "items": 1})
    got = repo.get_row("cx_spec_documents", doc["id"])
    assert got["status"] == "completed" and got["info"] == {"model": "m", "notes": ""}
    item = {"id": db.new_id(), "project_id": pid, "document_id": doc["id"], "kind": "equipment", "tag": "AHU-1", "name": "AHU",
            "equip_tag": None, "brick_class": "Air_Handling_Unit", "properties": {"cfm": "12000"},
            "source": {"document": "M-601.pdf", "page": 3}, "confidence": 0.9, "status": "proposed", "created_by": "llm",
            "created_at": now, "reviewed_by": None, "reviewed_at": None}
    repo.put_rows("cx_spec_items", [item])
    assert repo.find_rows("cx_spec_items", project_id=pid, status="proposed")[0]["source"] == {"document": "M-601.pdf", "page": 3}
    m = {"id": db.new_id(), "project_id": pid, "spec_item_id": item["id"], "equip_uri": "urn:equip:x", "confidence": "confirmed",
         "basis": [], "alternatives": [], "status": "confirmed", "decided_by": "it", "decided_at": now,
         "written_back": {"set": {"cxSpecTag": "AHU-1"}}, "created_at": now}
    repo.put_rows("cx_mappings", [m])
    assert repo.find_rows("cx_mappings", spec_item_id=item["id"])[0]["written_back"] == {"set": {"cxSpecTag": "AHU-1"}}
    assert repo.delete_rows("cx_mappings", id=m["id"]) == 1
    with pytest.raises(ValueError):
        repo.find_rows("cx_spec_items", nope=1)


def test_mirrored_faults_round_trip(repo):
    """Cx 8 (#44) on PgRepo: open/close in the shared faults table, the
    per-project mirror lookup, ids. Removes its own fault rows."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    pid = _new_project(repo, _org(repo, "mirror"), now)
    uri = f"urn:point:cx-it-mirror/{pid}/sat"
    try:
        fid = repo.open_fault("cx:exists", uri, now, {"project_id": pid, "source": "commissioning"}, "warning")
        assert fid is not None
        assert repo.open_fault("cx:exists", uri, now, {"project_id": pid}, "warning") is None  # already open
        assert repo.open_mirrored_faults(pid) == [("cx:exists", uri)]
        assert [f["id"] for f in repo.list_faults(True, [str(fid)])] == [fid]
        assert repo.close_fault("cx:exists", uri, now) == fid
        assert repo.open_mirrored_faults(pid) == []
        assert repo.list_faults(False, [fid])[0]["ended_at"] is not None
    finally:
        with repo.pool.connection() as conn:
            conn.execute("DELETE FROM faults WHERE point_uri = %s", (uri,))


def test_cx_levels_in_point_write_levels(repo):
    """Cx 9 (#45) on PgRepo: the sweeper's view of Cx-owned levels in the
    shared point_write_levels, a person's untouched. Removes its rows."""
    from baler import point_write

    now = datetime.now(timezone.utc).replace(microsecond=0)
    uri = f"urn:point:cx-it-active/{now.timestamp()}"
    try:
        with repo.pool.connection() as conn:
            point_write.record_level(conn, uri, 8, 1, "cx:run-1", "it", now)
            point_write.record_level(conn, uri, 10, 0, "alice@example.com", "it")
        mine = [x for x in repo.cx_levels() if x[0] == uri]
        assert mine == [(uri, 8, "cx:run-1", now)]
        assert set(repo.point_levels(uri)) == {"8", "10"}
    finally:
        with repo.pool.connection() as conn:
            conn.execute("DELETE FROM point_write_levels WHERE point_uri = %s", (uri,))


def test_agent_rows_round_trip(repo):
    """Cx 10 (#46) on PgRepo: a session's transcript and a proposal, JSON intact."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    pid = _new_project(repo, _org(repo, "agent"), now)
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": "ok"}]}]
    sess = {"id": db.new_id(), "project_id": pid, "created_by": "it", "claims": {"org_id": "o", "site_ids": []}, "status": "idle",
            "messages": msgs, "last_reply": "ok", "error": None, "created_at": now, "updated_at": now}
    repo.put_rows("cx_agent_sessions", [sess])
    assert repo.get_row("cx_agent_sessions", sess["id"])["messages"] == msgs
    prop = {"id": db.new_id(), "project_id": pid, "session_id": sess["id"], "kind": "phase", "payload": {"key": "functional"},
            "reason": "r", "status": "proposed", "result": None, "decided_by": None, "decided_at": None, "created_at": now}
    repo.put_rows("cx_agent_proposals", [prop])
    repo.update_row("cx_agent_proposals", prop["id"], {"status": "accepted", "result": {"phase": "functional"}})
    assert repo.find_rows("cx_agent_proposals", project_id=pid, status="accepted")[0]["result"] == {"phase": "functional"}


def test_a_point_shared_by_two_equipment_and_a_repeat_dont_abort_the_run(repo):
    """#72: a computed point attached to two pieces of equipment is checked
    under each; the (run, check, subject) key made the second a
    UniqueViolation that cost the run every result."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    pid = _new_project(repo, _org(repo, "dup"), now)
    rid = db.new_id()
    shared = "urn:point:computed/abc/ahu-1"

    def res(equip):
        return {"equip_uri": equip, "check_id": "exists", "subject": shared, "status": "pass", "severity": None,
                "symptom": None, "candidates": [], "evidence": {}}

    results = [res("urn:equip:cx-it/ahu-1"), res("urn:equip:cx-it/building"), res("urn:equip:cx-it/ahu-1")]
    repo.save_run({"id": rid, "project_id": pid, "phase": "startup", "started_at": now, "finished_at": now,
                   "window_start": now - timedelta(hours=1), "window_end": now, "triggered_by": "it",
                   "scope": {"caller": {"prefixes": None, "exclude": []}, "site_prefixes": []}, "summary": {}}, results)
    stored = repo.get_results(rid)
    assert sorted(r["equip_uri"] for r in stored) == ["urn:equip:cx-it/ahu-1", "urn:equip:cx-it/building"]


def test_availability_on_postgres_is_availability_pct(repo, monkeypatch):
    """repo.availability folds in Postgres what procedures.availability_pct
    folds in Python (Baler#150): same samples, same percentage - gaps longer
    than max_gap, a late first sample, empty rows, a point with nothing."""
    from baler.commissioning.procedures import availability_pct

    start = datetime(2026, 3, 2, 8, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    gap = timedelta(minutes=5)
    series = {
        "urn:point:test-cxint-avail/steady": [start + timedelta(seconds=30 * i) for i in range(120)],
        "urn:point:test-cxint-avail/gappy": [start + timedelta(minutes=m) for m in (3, 4, 20, 21, 22, 50, 59.5)],
        "urn:point:test-cxint-avail/late": [start + timedelta(minutes=40, seconds=7)],
    }
    with repo.pool.connection() as conn:
        conn.execute("DELETE FROM point_history WHERE point_uri LIKE 'urn:point:test-cxint-avail/%'")
        for uri, times in series.items():
            for t in times:
                write_point_value(conn, uri, 1.5, t)
        write_point_value(conn, "urn:point:test-cxint-avail/gappy", None, start + timedelta(minutes=35))  # no value: not a sample
        write_point_value(conn, "urn:point:test-cxint-avail/steady", 2.0, end)  # end is exclusive
    uris = [*series, "urn:point:test-cxint-avail/silent"]
    from baler.commissioning import history

    with repo.pool.connection() as conn:
        conn.autocommit = True
        assert history.ensure_empty_rows_index(conn) in ("present", "built", "another process is building it")
    # with the empty-rows index (index-only scans, the heap for `gappy`) and without it (heap only)
    for valid_sql in (history._EMPTY_ROWS_VALID, "SELECT false"):
        monkeypatch.setattr(history, "_EMPTY_ROWS_VALID", valid_sql)
        got = repo.availability(uris, start, end, gap)
        for uri in uris:
            times = series.get(uri, [])
            assert got[uri] == (len(times), availability_pct(times, start, end, gap)), (uri, valid_sql)
        assert got["urn:point:test-cxint-avail/silent"] == (0, 0.0)


def test_override_inputs_round_trip(repo):
    """Baler#212: schedule snapshots, the priority-array change log and
    Baler's own writes, on Postgres."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    tag = f"ov-it-{int(now.timestamp() * 1000)}"
    prefix = f"fbf/{tag}"
    sp, occ = f"urn:point:{prefix}/zsp", f"urn:point:{prefix}/occ"
    office = {"object": "schedule,1", "name": "Office", "default": "inactive", "weekly": {}, "exceptions": [],
              "controls": [{"object": "binary-value,1", "property": "present-value", "label": "occ"}]}
    cal = {"object": "calendar,1", "name": "Holidays", "dates": [{"date": "*-12-25"}]}
    rows = [{"id": f"s/{tag}/c1/{b['object']}", "site": "s", "instance": tag, "connection_id": "c1", "topic_prefix": prefix,
             "kind": kind, "object": b["object"], "name": b["name"], "body": b, "points": [occ] if kind == "schedule" else [],
             "read_at": now} for kind, b in (("schedule", office), ("calendar", cal))]
    repo.put_schedules(rows)
    repo.put_schedules(rows)  # a refresh replaces, never duplicates
    got = repo.schedules_for_points([occ])
    assert [s["name"] for s in got] == ["Office"] and got[0]["calendars"] == {"calendar,1": cal}
    assert repo.schedules_for_points([sp]) == []
    assert sum(1 for r in repo.list_schedules() if r["topic_prefix"] == prefix) == 2

    a = {"8": {"value": 68.0, "baler": False}}
    assert repo.record_priority_arrays({sp: a}, now - timedelta(hours=2)) == 1
    assert repo.record_priority_arrays({sp: a}, now - timedelta(hours=1)) == 0  # unchanged: no row
    assert repo.record_priority_arrays({sp: {}}, now) == 1
    obs = repo.priority_observations([sp], now - timedelta(minutes=90), now)[sp]
    assert [(o["observed_at"], o["levels"]) for o in obs] == [(now - timedelta(hours=2), a), (now, {})]

    with repo.pool.connection() as conn:
        orgs = auth.create_org(conn, f"{tag}-org", f"{tag}@example.com", "pw-123456789")
    pid = db.new_id()
    repo.create_project({"id": pid, "org_id": orgs["id"], "site_id": None, "name": tag, "settings": {"overrides": {"enabled": True}},
                         "created_by": "it", "created_at": now, "updated_at": now, "phases": phases.default_phases()})
    assert pid in [p["id"] for p in repo.list_projects_with_setting("overrides")]
    run = {"id": db.new_id(), "project_id": pid, "equip_uri": f"urn:equip:{prefix}", "kind": "custom", "steps": [], "plan": [],
           "status": "completed", "window_start": now - timedelta(hours=1), "window_end": now, "created_by": "it",
           "created_at": now, "confirmed_by": None, "confirmed_at": None, "started_at": now - timedelta(minutes=50),
           "finished_at": now - timedelta(minutes=40), "before": None, "results": [], "error": None, "abort_requested": False,
           "writes": [{"action": "write", "point": sp, "at": (now - timedelta(minutes=45)).isoformat()}]}
    repo.put_rows("cx_active_runs", [run])
    assert repo.baler_writes([sp], now - timedelta(hours=1), now) == {sp: [now - timedelta(minutes=45)]}

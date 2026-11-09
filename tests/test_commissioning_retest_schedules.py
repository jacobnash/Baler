"""Scheduled active retests (Baler#275 slice e): a fleet run that repeats -
seasonally - only under an admin approval that names its scope and window,
never with writes enabled by default. Once approved a schedule can only
narrow; an expired approval stops scheduling; every run and skip is
audited. Mock terminal units and a fake clock only (test_commissioning_fleet's)."""

from __future__ import annotations

from datetime import timedelta

import pytest
import requests
import test_commissioning_fleet as ff
import test_commissioning_openapi as api
from conftest import assert_matches_schema, load_spec

from baler.commissioning import fleet
from baler.commissioning import retest_schedules as rs
from baler.commissioning_api import OPENAPI_SPEC_PATH

T0 = ff.T0
DAY = timedelta(days=1)


def _proposal(**over):
    return {"equip_class": "Variable_Air_Volume_Box", "first_window_start": (T0 + timedelta(hours=1)).isoformat(),
            "window_minutes": 240, "every_days": 90, "valid_until": (T0 + timedelta(days=365)).isoformat(),
            "concurrency_per_system": 2, "max_concurrent": 6, "stagger_seconds": 0, "max_failures": 10} | over


def _setup(bad=()):
    site, repo, ex, fr = ff.setup(2, 3, bad=bad)
    repo.create_project({"id": "p", "org_id": "org-a", "site_id": None, "name": "B", "settings": {}, "phases": [],
                         "created_at": T0, "updated_at": T0, "archived_at": None})
    return site, repo, ex, fr


def _approved(repo, **over):
    scope, timing = rs.parse(_proposal(**over), ff.equips(repo), T0)
    s = rs.new("p", "Seasonal", scope, timing, "admin", T0)
    s.update(status="approved", approval={"scope": scope, "timing": timing, "hash": s["approval_hash"]}, approved_by="admin",
             approved_at=T0, approver_claims={"org_id": "org-a", "site_ids": []})
    repo.put_rows(rs.TABLE, [s])
    return s


def _actions(repo):
    return [e["action"] for e in repo.events]


def _finish(fr, ex, fid):
    fr.threads[fid].join(30)
    for t in list(ex.threads.values()):
        t.join(30)


# ------------------------------------------------------------------- pure


def test_occurrences_and_next_window():
    timing = {"first_window_start": T0.isoformat(), "window_minutes": 60, "every_days": 90,
              "valid_until": (T0 + timedelta(days=200)).isoformat()}
    assert rs.occurrence(timing, T0 - timedelta(minutes=1)) is None
    assert rs.occurrence(timing, T0 + timedelta(minutes=30))[0] == 0
    assert rs.occurrence(timing, T0 + timedelta(minutes=61)) is None
    assert rs.occurrence(timing, T0 + 90 * DAY + timedelta(minutes=5))[0] == 1
    assert rs.next_window(timing, T0 + DAY) == (T0 + 90 * DAY).isoformat()
    assert rs.next_window(timing, T0 + 181 * DAY) is None  # 270 days is past valid_until


def test_an_approved_schedule_only_narrows():
    _, repo, _, _ = _setup()
    s = _approved(repo)
    some = s["scope"]["equip_uris"][:2]
    for widen in ({"equip_uris": [*s["scope"]["equip_uris"], "urn:equip:fbf/a/other"]},
                  {"valid_until": (T0 + timedelta(days=366)).isoformat()},
                  {"window_minutes": 241}, {"concurrency_per_system": 3}, {"max_concurrent": 7}, {"max_failures": 11},
                  {"stagger_seconds": -1}):
        with pytest.raises((PermissionError, ValueError)):
            rs.patch(s, widen, T0)
    for fixed in ({"every_days": 30}, {"first_window_start": T0.isoformat()}, {"kind": "library"}, {"test": "vav_reheat"},
                  {"response_minutes": 5}):
        with pytest.raises(PermissionError, match="propose a new one"):
            rs.patch(s, fixed, T0)
    narrowed = rs.patch(s, {"equip_uris": some, "window_minutes": 120, "valid_until": (T0 + timedelta(days=100)).isoformat(),
                            "concurrency_per_system": 1, "stagger_seconds": 60, "mode": "failures"}, T0)
    assert narrowed["scope"]["equip_uris"] == sorted(some) and narrowed["timing"]["window_minutes"] == 120
    # and from narrowed, back out to the original approval is still allowed - it's within the envelope - but no wider
    s2 = {**s, **narrowed}
    with pytest.raises(PermissionError):
        rs.patch(s2, {"mode": "all", "window_minutes": 300}, T0)
    assert "approval_hash" not in narrowed  # the approval itself is untouched


def test_proposals_are_bounded():
    _, repo, _, _ = _setup()
    equips = ff.equips(repo)
    for bad in ({"window_minutes": 481}, {"every_days": 0}, {"valid_until": (T0 + timedelta(days=500)).isoformat()},
                {"valid_until": (T0 - DAY).isoformat()}, {"first_window_start": "soon"}, {"mode": "sometimes"},
                {"kind": "custom"}, {"equip_class": None}, {"equip_uris": ["urn:equip:nope"]}):
        with pytest.raises(ValueError):
            rs.parse(_proposal(**bad), equips, T0)


# ---------------------------------------------------------------- ticking


def test_a_proposed_schedule_never_runs(monkeypatch):
    monkeypatch.setenv("BALER_CX_ALLOW_WRITES", "1")
    _, repo, _, fr = _setup()
    scope, timing = rs.parse(_proposal(), ff.equips(repo), T0)
    repo.put_rows(rs.TABLE, [rs.new("p", "x", scope, timing, "admin", T0)])
    assert rs.Ticker(repo, fr)(T0 + timedelta(hours=2)) == []
    assert repo.find_rows(fleet.TABLE) == [] and repo.commands == []


def test_writes_off_skips_the_window_and_audits_it(monkeypatch):
    monkeypatch.delenv("BALER_CX_ALLOW_WRITES", raising=False)
    _, repo, _, fr = _setup()
    s = _approved(repo)
    assert rs.Ticker(repo, fr)(T0 + timedelta(hours=2)) == []
    assert repo.find_rows(fleet.TABLE) == [] and repo.commands == []
    skip = [e for e in repo.events if e["action"] == "active_schedule.skip"]
    assert len(skip) == 1 and "BALER_CX_ALLOW_WRITES" in skip[0]["detail"]["reason"]
    assert skip[0]["detail"]["approval_hash"] == s["approval_hash"]
    # turning the switch on later in the same window doesn't run a window already handled
    monkeypatch.setenv("BALER_CX_ALLOW_WRITES", "1")
    assert rs.Ticker(repo, fr)(T0 + timedelta(hours=3)) == []


def test_an_approved_window_runs_once_audited_then_failures_only_next_season(monkeypatch):
    monkeypatch.setenv("BALER_CX_ALLOW_WRITES", "1")
    bad = [ff.vav(1, 2)]
    site, repo, ex, fr = _setup(bad=bad)
    s = _approved(repo, mode="failures")
    tick = rs.Ticker(repo, fr)
    assert tick(T0) == []  # before the first window
    site.now = T0 + timedelta(hours=1, minutes=1)
    [fid] = tick(site.now)
    assert tick(site.now) == []  # once per window
    _finish(fr, ex, fid)
    f = repo.get_row(fleet.TABLE, fid)
    assert f["status"] == "completed" and f["schedule_id"] == s["id"] and f["confirmed_by"] == "admin"
    assert f["created_by"] == f"schedule:{s['id']}" and len(f["members"]) == 6
    assert fleet._dt(f["window_end"]) <= T0 + timedelta(hours=5)  # inside the approved window
    run = [e for e in repo.events if e["action"] == "active_schedule.run"]
    assert len(run) == 1 and run[0]["detail"]["fleet_run_id"] == fid and run[0]["detail"]["plan_hash"] == f["plan_hash"]
    assert run[0]["detail"]["approval_hash"] == s["approval_hash"] and run[0]["user_sub"] == "admin"
    assert repo.levels == {}
    # next season: only what failed, and only that is commanded
    before = len(repo.commands)
    site.now = T0 + 90 * DAY + timedelta(hours=1, minutes=1)
    [fid2] = tick(site.now)
    _finish(fr, ex, fid2)
    assert [m["equip_uri"] for m in repo.get_row(fleet.TABLE, fid2)["members"]] == bad
    assert {c["point"] for c in repo.commands[before:]} == {ff.cmd(bad[0])}
    assert repo.levels == {}


def test_expired_approval_stops_scheduling(monkeypatch):
    monkeypatch.setenv("BALER_CX_ALLOW_WRITES", "1")
    site, repo, _, fr = _setup()
    s = _approved(repo, valid_until=(T0 + timedelta(days=100)).isoformat())
    site.now = T0 + 90 * DAY + timedelta(hours=2)
    assert rs.Ticker(repo, fr)(site.now) != []  # season 2 is still inside
    for t in list(fr.threads.values()):
        t.join(30)
        assert not t.is_alive()
    assert rs.Ticker(repo, fr)(T0 + 101 * DAY) == []
    assert repo.get_row(rs.TABLE, s["id"])["status"] == "expired" and "active_schedule.expire" in _actions(repo)
    n = len(repo.find_rows(fleet.TABLE))
    assert rs.Ticker(repo, fr)(T0 + 180 * DAY + timedelta(hours=2)) == [] and len(repo.find_rows(fleet.TABLE)) == n


def test_a_schedule_edited_outside_its_approval_is_refused(monkeypatch):
    """Defense in depth: even a schedule row changed behind the API's back
    never runs outside what was approved."""
    monkeypatch.setenv("BALER_CX_ALLOW_WRITES", "1")
    _, repo, _, fr = _setup()
    s = _approved(repo, equip_uris=[ff.vav(0, 0)])
    repo.update_row(rs.TABLE, s["id"], {"scope": {**s["scope"], "equip_uris": [ff.vav(0, 0), ff.vav(1, 0)]}})
    assert rs.Ticker(repo, fr)(T0 + timedelta(hours=2)) == []
    skip = [e for e in repo.events if e["action"] == "active_schedule.skip"]
    assert "outside its approval" in skip[0]["detail"]["reason"] and repo.commands == []


# ------------------------------------------------------------------- api


@pytest.fixture(scope="module")
def oas():
    return load_spec(OPENAPI_SPEC_PATH)


@pytest.fixture
def served(monkeypatch):
    server, base, repo, fr, site = ff._served(monkeypatch, 2, 3)
    yield base, repo, fr, site
    server.shutdown()


def _key_hdr():
    import jwt as pyjwt
    tok = pyjwt.encode({"sub": "u-a", "org_id": "org-a", "site_ids": [], "role": "admin", "kind": "api_key"}, api.JWT,
                       algorithm="HS256")
    return {"X-Gateway-Secret": api.GATEWAY, "Authorization": f"Bearer {tok}"}


def test_propose_approve_narrow_and_revoke_over_the_api(served, oas, monkeypatch):
    base, repo, fr, site = served
    monkeypatch.delenv("BALER_CX_ALLOW_WRITES", raising=False)
    admin, operator = ff._hdr("admin"), ff._hdr("operator")
    p = api._project(base, headers=admin)
    url = f"{base}/projects/{p['id']}/active-schedules"
    assert requests.post(url, json=_proposal(), headers=operator).status_code == 403
    assert requests.post(url, json=_proposal(window_minutes=1), headers=admin).status_code == 400  # the fleet can't fit
    r = requests.post(url, json=_proposal(), headers=admin)
    assert r.status_code == 201, r.text
    assert_matches_schema(r.json(), api._schema(oas, "/projects/{id}/active-schedules", "post", 201), oas)
    s = r.json()
    assert s["status"] == "proposed" and s["writes_allowed"] is False and len(s["scope"]["equip_uris"]) == 6
    assert len(s["preview"]["plan"]) == 12 and repo.commands == []  # a dry run of the next window, nothing commanded
    sid, code = s["id"], s["approval_hash"]
    approve = lambda body, h=admin: requests.post(f"{base}/active-schedules/{sid}/approve", json=body, headers=h)
    assert approve({"confirm": code}, operator).status_code == 403
    assert approve({"confirm": code}, _key_hdr()).status_code == 403  # a person approves, not an API key
    assert approve({"confirm": sid}).status_code == 400
    assert approve({"confirm": code}, ff._hdr("admin", "org-b")).status_code == 404
    a = approve({"confirm": code})
    assert a.status_code == 200 and a.json()["status"] == "approved" and a.json()["approval"]["hash"] == code
    assert approve({"confirm": code}).status_code == 409
    # approving didn't turn writes on: the window comes and is skipped, audited
    site.now = T0 + timedelta(hours=2)
    assert rs.Ticker(repo, fr)(site.now) == [] and repo.find_rows(fleet.TABLE) == []
    # an approved schedule can't widen
    patch = lambda body, h=admin: requests.patch(f"{base}/active-schedules/{sid}", json=body, headers=h)
    for widen in ({"valid_until": (T0 + timedelta(days=366)).isoformat()}, {"window_minutes": 300}, {"every_days": 30},
                  {"equip_uris": [*s["scope"]["equip_uris"], "urn:equip:fbf/a/x"]}, {"max_concurrent": 50}):
        w = patch(widen)
        assert w.status_code == 409, (widen, w.text)
    assert patch({"window_minutes": 120}, operator).status_code == 403
    n = patch({"window_minutes": 120, "name": "Spring retest"})
    assert n.status_code == 200 and n.json()["timing"]["window_minutes"] == 120 and n.json()["approval"]["hash"] == code
    got = requests.get(f"{base}/active-schedules/{sid}", headers=ff._hdr("viewer"))
    assert_matches_schema(got.json(), api._schema(oas, "/active-schedules/{id}", "get", 200), oas)
    assert [x["id"] for x in requests.get(url, headers=ff._hdr("viewer")).json()] == [sid]
    # revoke (operator): nothing runs any more, even with the switch on
    rv = requests.post(f"{base}/active-schedules/{sid}/revoke", json={}, headers=operator)
    assert rv.status_code == 200 and rv.json()["status"] == "revoked"
    monkeypatch.setenv("BALER_CX_ALLOW_WRITES", "1")
    site.now = T0 + 90 * DAY + timedelta(hours=2)
    assert rs.Ticker(repo, fr)(site.now) == [] and repo.commands == []
    assert patch({"window_minutes": 60}).status_code == 409
    want = {"active_schedule.propose", "active_schedule.approve", "active_schedule.skip", "active_schedule.edit",
            "active_schedule.revoke"}
    assert want <= set(_actions(repo))


def test_revoking_stops_a_running_scheduled_fleet(served, monkeypatch):
    base, repo, fr, site = served
    monkeypatch.setenv("BALER_CX_ALLOW_WRITES", "1")
    site.bad = {ff.vav(a, i) for a in range(2) for i in range(3)}  # every member holds for its whole response time
    admin = ff._hdr("admin")
    p = api._project(base, headers=admin)
    s = requests.post(f"{base}/projects/{p['id']}/active-schedules", headers=admin,
                      json=_proposal(response_minutes=30, concurrency_per_system=1, max_concurrent=2)).json()
    requests.post(f"{base}/active-schedules/{s['id']}/approve", json={"confirm": s["approval_hash"]}, headers=admin)
    site.now = T0 + timedelta(hours=1, minutes=1)
    [fid] = rs.Ticker(repo, fr)(site.now)
    ff._wait(lambda: ff._held(repo, fid))
    rv = requests.post(f"{base}/active-schedules/{s['id']}/revoke", json={}, headers=ff._hdr("operator"))
    assert rv.status_code == 200 and ff._held(repo, fid) == []
    fr.threads[fid].join(30)
    assert repo.get_row(fleet.TABLE, fid)["status"] == "aborted" and repo.levels == {}

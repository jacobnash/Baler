"""Cx 10 (#46): the commissioning agent - its tools, scoping, proposals a
person accepts, and the loop, against a scripted fake Claude client (no
network) on MemoryRepo."""

from __future__ import annotations

import time
from datetime import timedelta
from types import SimpleNamespace

import cx_fixtures as fx
import pytest
import requests
import test_commissioning_openapi as api
from conftest import assert_matches_schema, load_spec

from baler.commissioning import agent, db, phases, runner
from baler.commissioning_api import OPENAPI_SPEC_PATH

RAT = fx.pt(fx.AHU, "RAT")


class Block(dict):
    def model_dump(self, exclude_none=False):
        return dict(self)


def _resp(stop, *blocks):
    return SimpleNamespace(stop_reason=stop, content=[Block(b) for b in blocks])


def _use(i, name, **inp):
    return {"type": "tool_use", "id": f"tu{i}", "name": name, "input": inp}


class FakeClient:
    """Plays back scripted responses; records every request."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.requests.append(kw)
        step = self.script.pop(0)
        return step(kw) if callable(step) else step


def _repo():
    return db.MemoryRepo(store=fx.store(), history=fx.history(), sites=api.SITES, scope_fn=api._scope, users={"u-a": "a@example.com"})


def _ctx(repo, claims=None, site_id="site-a"):
    p = {"id": "p1", "org_id": "org-a", "site_id": site_id, "name": "P", "settings": {}, "created_by": "u-a", "created_at": fx.NOW,
         "updated_at": fx.NOW, "phases": phases.set_current(phases.default_phases(), "startup", fx.NOW)}
    if repo.get_project("p1") is None:
        repo.create_project(p)
    worker = runner.RunWorker(repo, lambda: fx.NOW)
    return agent.Ctx(repo, worker, repo.get_project("p1"), claims or {"sub": "u-a", "org_id": "org-a", "site_ids": [], "role": "operator"},
                     lambda: fx.NOW, "s1")


def test_tools_are_declared_and_implemented():
    assert {t["name"] for t in agent.TOOLS} == set(agent.IMPL)
    for t in agent.TOOLS:
        assert t["input_schema"]["type"] == "object" and t["description"]


def test_tools_are_scoped_like_a_person():
    repo = _repo()
    ctx = _ctx(repo)
    inv = {e["id"] for e in agent.IMPL["inventory_summary"](ctx, {})}
    assert inv == {fx.AHU, fx.VAV, fx.MTR}  # never org B's AHU-9
    text, err = agent.call_tool(ctx, "equipment_points", {"equip_uri": fx.AHU_B})
    assert err and "no such equipment" in text
    gaps = agent.IMPL["template_gaps"](ctx, {})
    assert {g["equip_uri"] for g in gaps} <= inv
    # a site-restricted starter on another site sees nothing of site A
    other = _ctx(repo, {"sub": "u-a2", "org_id": "org-a", "site_ids": ["site-a2"], "role": "operator"})
    assert agent.IMPL["inventory_summary"](other, {}) == []


def test_every_call_is_an_event_and_errors_come_back_as_results():
    repo = _repo()
    ctx = _ctx(repo)
    agent.call_tool(ctx, "list_runs", {})
    text, err = agent.call_tool(ctx, "nope", {})
    assert err
    text, err = agent.call_tool(ctx, "run_results", {"run_id": "missing"})
    assert err and "no such run" in text
    actions = [e["action"] for e in repo.list_events("p1")]
    assert actions[:2] == ["agent.run_results", "agent.list_runs"]
    assert repo.list_events("p1")[0]["detail"]["session_id"] == "s1"


def test_writes_are_only_proposals():
    repo = _repo()
    ctx = _ctx(repo)
    out = agent.IMPL["propose"](ctx, {"kind": "issue", "payload": {"title": "Label AHU-1", "equip_uri": fx.AHU}, "reason": "no nameplate tag"})
    assert out["status"].startswith("proposed")
    assert repo.list_issues("p1") == []  # nothing happened yet
    [prop] = repo.find_rows("cx_agent_proposals", project_id="p1")
    assert prop["kind"] == "issue" and prop["status"] == "proposed" and prop["session_id"] == "s1"
    for bad in ({"kind": "issue", "payload": {"title": "x", "equip_uri": fx.AHU_B}, "reason": "r"},
                {"kind": "phase", "payload": {"key": "nope"}, "reason": "r"},
                {"kind": "procedure", "payload": {"template": "nope"}, "reason": "r"},
                {"kind": "active_test", "payload": {"equip_uri": fx.AHU, "window_start": "2026-11-06T13:00:00+00:00",
                                                    "window_end": "2026-11-06T23:00:00+00:00"}, "reason": "r"}):
        _, err = agent.call_tool(ctx, "propose", bad)
        assert err, bad


def test_accepting_runs_as_the_person():
    repo = _repo()
    ctx = _ctx(repo)
    project = repo.get_project("p1")
    op = {"sub": "u-a", "org_id": "org-a", "site_ids": [], "role": "operator"}

    def prop(kind, payload):
        agent.IMPL["propose"](ctx, {"kind": kind, "payload": payload, "reason": "because"})
        return repo.find_rows("cx_agent_proposals", project_id="p1", kind=kind)[-1]

    assert "issue_id" in agent.accept(repo, prop("issue", {"title": "Label AHU-1"}), project, op, fx.NOW)
    assert "procedure_id" in agent.accept(repo, prop("procedure", {"template": "ahu_power_on"}), project, op, fx.NOW)
    assert agent.accept(repo, prop("phase", {"key": "functional"}), project, op, fx.NOW) == {"phase": "functional"}
    assert phases.current(repo.get_project("p1")["phases"]) == "functional"
    item = agent.accept(repo, prop("spec_item", {"kind": "equipment", "tag": "AHU-1", "name": "AHU", "brick_class": "AHU"}), project, op, fx.NOW)
    assert repo.get_row("cx_spec_items", item["spec_item_id"])["status"] == "accepted"
    assert "mapping_id" in agent.accept(repo, prop("mapping", {"spec_item_id": item["spec_item_id"], "equip_uri": fx.AHU}), project, op, fx.NOW)
    win = {"equip_uri": fx.AHU, "window_start": fx.NOW.isoformat(), "window_end": (fx.NOW + timedelta(hours=1)).isoformat()}
    at = prop("active_test", win)
    with pytest.raises(PermissionError):
        agent.accept(repo, at, project, op, fx.NOW)


def test_run_turn_loops_through_tools_and_replays_history():
    repo = _repo()
    ctx = _ctx(repo)
    seen = []

    def second(kw):
        seen.append(kw["messages"][-1])
        return _resp("tool_use", {"type": "thinking", "thinking": "", "signature": "sig"},
                     _use(2, "propose", kind="issue", payload={"title": "RAT stale", "equip_uri": fx.AHU}, reason="exists failed"))
    client = FakeClient([
        _resp("tool_use", {"type": "text", "text": "Looking."}, _use(1, "inventory_summary"), _use(9, "list_runs")),
        second,
        _resp("end_turn", {"type": "text", "text": "I proposed one issue."}),
    ])
    messages, reply = agent.run_turn(ctx, [], "What should I do next?", client)
    assert reply == "I proposed one issue."
    req = client.requests[0]
    assert req["tools"] == agent.TOOLS and req["system"] == agent.SYSTEM and req["fallbacks"] == "default"
    results = seen[0]["content"]
    assert [r["tool_use_id"] for r in results] == ["tu1", "tu9"]  # all results of a turn in one message
    assert messages[3]["content"][0]["type"] == "thinking"  # replayed as returned
    assert len(repo.find_rows("cx_agent_proposals", project_id="p1")) == 1
    # the next turn replays the whole transcript unchanged
    client2 = FakeClient([_resp("end_turn", {"type": "text", "text": "ok"})])
    agent.run_turn(ctx, messages, "thanks", client2)
    assert client2.requests[0]["messages"][: len(messages)] == messages


def test_run_turn_stops_on_refusal_and_runaway_loops(monkeypatch):
    repo = _repo()
    ctx = _ctx(repo)
    _, reply = agent.run_turn(ctx, [], "x", FakeClient([_resp("refusal")]))
    assert "declined" in reply
    monkeypatch.setattr(agent, "MAX_TOOL_ROUNDS", 2)
    _, reply = agent.run_turn(ctx, [], "x", FakeClient([_resp("tool_use", _use(1, "list_runs")), _resp("tool_use", _use(2, "list_runs"))]))
    assert "too many tool calls" in reply


# ------------------------------------------------------------------- api


@pytest.fixture(scope="module")
def oas():
    return load_spec(OPENAPI_SPEC_PATH)


@pytest.fixture
def served(monkeypatch):
    from http.server import ThreadingHTTPServer
    from threading import Thread

    from baler.commissioning_api import make_handler

    repo = _repo()
    client = FakeClient([])
    monkeypatch.setenv("BALER_GATEWAY_SECRET", api.GATEWAY)
    monkeypatch.setenv("BALER_JWT_SECRET", api.JWT)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(repo, clock=lambda: fx.NOW, agent_client=client))
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", repo, client
    server.shutdown()


def _wait_idle(base, sid, headers=api.A):
    deadline = time.monotonic() + 10
    while (s := requests.get(f"{base}/agent-sessions/{sid}", headers=headers).json())["status"] == "working":
        assert time.monotonic() < deadline
        time.sleep(0.02)
    return s


def test_agent_api(served, oas):
    base, _, client = served
    p = api._project(base)
    client.script += [
        _resp("tool_use", _use(1, "propose", kind="issue", payload={"title": "Investigate stale RAT", "equip_uri": fx.AHU},
                               reason="RAT last reported 3 days ago")),
        _resp("end_turn", {"type": "text", "text": "Proposed an issue for the stale return air sensor."}),
    ]
    r = requests.post(f"{base}/projects/{p['id']}/agent/sessions", json={"message": "Review the AHU"}, headers=api.A)
    assert r.status_code == 202, r.text
    assert_matches_schema(r.json(), api._schema(oas, "/projects/{id}/agent/sessions", "post", 202), oas)
    s = _wait_idle(base, r.json()["id"])
    assert_matches_schema(s, api._schema(oas, "/agent-sessions/{id}", "get", 200), oas)
    assert s["status"] == "idle" and s["last_reply"].startswith("Proposed")
    assert [t.get("tool") for t in s["transcript"] if t["role"] == "tool"] == ["propose"]
    props = requests.get(f"{base}/projects/{p['id']}/agent/proposals", params={"status": "proposed"}, headers=api.A)
    assert_matches_schema(props.json(), api._schema(oas, "/projects/{id}/agent/proposals", "get", 200), oas)
    [prop] = props.json()
    assert requests.post(f"{base}/agent-proposals/{prop['id']}/accept", headers=api.B).status_code == 404
    acc = requests.post(f"{base}/agent-proposals/{prop['id']}/accept", headers=api.A)
    assert acc.status_code == 200 and acc.json()["status"] == "accepted" and acc.json()["result"]["issue_id"]
    assert requests.post(f"{base}/agent-proposals/{prop['id']}/accept", headers=api.A).status_code == 409
    [issue] = requests.get(f"{base}/projects/{p['id']}/issues", headers=api.A).json()
    assert issue["title"] == "Investigate stale RAT" and issue["source"] == "agent"
    actions = [e["action"] for e in requests.get(f"{base}/projects/{p['id']}/events", headers=api.A).json()]
    assert {"agent.session", "agent.propose", "agent.proposal.accept"} <= set(actions)
    # a follow-up turn; someone else can read but not drive this session
    client.script.append(_resp("end_turn", {"type": "text", "text": "Nothing else."}))
    other = api._h(sub="u-other")
    assert requests.post(f"{base}/agent-sessions/{s['id']}/messages", json={"message": "hi"}, headers=other).status_code == 403
    assert requests.post(f"{base}/agent-sessions/{s['id']}/messages", json={"message": "anything else?"}, headers=api.A).status_code == 202
    assert _wait_idle(base, s["id"])["last_reply"] == "Nothing else."
    assert requests.post(f"{base}/agent-sessions/{s['id']}/messages", json={}, headers=api.A).status_code == 400
    listed = requests.get(f"{base}/projects/{p['id']}/agent/sessions", headers=api.A).json()
    assert [x["id"] for x in listed] == [s["id"]] and "messages" not in listed[0]


def test_agent_failure_is_recorded_and_rejects(served):
    base, repo, client = served
    p = api._project(base)

    def boom(kw):
        raise RuntimeError("overloaded")
    client.script.append(boom)
    r = requests.post(f"{base}/projects/{p['id']}/agent/sessions", json={"message": "go"}, headers=api.A).json()
    s = _wait_idle(base, r["id"])
    assert s["status"] == "failed" and "overloaded" in s["error"]
    repo.put_rows("cx_agent_proposals", [{"id": "x1", "project_id": p["id"], "session_id": r["id"], "kind": "phase",
                                          "payload": {"key": "warranty"}, "reason": "", "status": "proposed", "result": None,
                                          "decided_by": None, "decided_at": None, "created_at": fx.NOW}])
    rej = requests.post(f"{base}/agent-proposals/x1/reject", json={"note": "not yet"}, headers=api.A).json()
    assert rej["status"] == "rejected" and rej["result"] == {"note": "not yet"}

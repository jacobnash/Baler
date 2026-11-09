"""Assistant tools across the building record (#220): read-only, audited,
scoped, citing; offered only for the parts of the record this install has."""

from __future__ import annotations

import importlib.util
import json

import cx_fixtures as fx
import pytest
from test_commissioning_agent import _ctx, _repo

from baler.commissioning import agent, record_tools


@pytest.fixture
def repo():
    return _repo()


def _call(ctx, name, args):
    text, err = agent.call_tool(ctx, name, args)
    return json.loads(text), err


def test_always_offered_and_read_only():
    names = {t["name"] for t in agent.TOOLS}
    assert {"search_documents", "asset_history", "point_history"} <= names
    assert record_tools.PROMPT in agent.SYSTEM
    # nothing new writes: the only path to a change is still `propose`
    assert not any(n.startswith(("create", "update", "delete", "set_")) for n in names)


def test_tools_for_parts_this_install_lacks_are_not_offered():
    names = {t["name"] for t in agent.TOOLS}
    for needs, tool, _ in record_tools.GROUPS:
        present = all(importlib.util.find_spec(f"baler.commissioning.{m}") is not None for m in needs)
        assert (tool["name"] in names) == present, tool["name"]


def test_search_documents_cites_page_and_quote(repo):
    ctx = _ctx(repo)
    repo.put_rows("cx_spec_documents", [{"id": "d1", "project_id": "p1", "filename": "M-601 schedules.pdf", "sha256": "ab" * 32}])
    repo.put_rows("cx_spec_items", [{"id": "i1", "project_id": "p1", "document_id": "d1", "kind": "equipment", "tag": "P-2",
                                     "name": "Chilled water pump", "properties": {"seal": "mechanical, type 21"},
                                     "source": {"page": 4, "quote": "P-2 CHW PUMP 450 GPM, MECH SEAL TYPE 21"},
                                     "status": "accepted", "created_at": fx.NOW}])
    out, err = _call(ctx, "search_documents", {"query": "seal"})
    assert not err and out["hits"] == [{"document": "M-601 schedules.pdf", "sha256": "ab" * 32, "page": 4,
                                        "quote": "P-2 CHW PUMP 450 GPM, MECH SEAL TYPE 21", "item": "Chilled water pump",
                                        "tag": "P-2", "status": "accepted"}]
    assert "#201" in out["note"]
    assert repo.list_events("p1")[0]["action"] == "agent.search_documents"  # every call audited


def test_asset_history_says_when_the_record_doesnt_hold_it(repo):
    ctx = _ctx(repo)
    out, _ = _call(ctx, "asset_history", {"equip_uri": fx.AHU})
    assert out["events"] == [] and "#197" in out["note"]
    record_tools.ASSET_HISTORY.append(lambda repo, project, uri: [{"event": "install", "make": "WEG", "at": "2031-07-14"}])
    try:
        out, _ = _call(ctx, "asset_history", {"equip_uri": fx.AHU})
        assert out["events"][0]["make"] == "WEG" and "note" not in out
    finally:
        record_tools.ASSET_HISTORY.clear()
    out, err = _call(ctx, "asset_history", {"equip_uri": fx.AHU_B})  # another org's equipment
    assert err


def test_point_history_reports_what_it_read(repo):
    ctx = _ctx(repo)
    out, err = _call(ctx, "point_history", {"point_uri": fx.pt(fx.AHU, "SAT"), "hours": 1})
    assert not err and out["samples"] == 1 and out["mean"] == 55.0 and out["window"][1] == fx.NOW.isoformat()
    _, err = _call(ctx, "point_history", {"point_uri": fx.pt(fx.AHU_B, "SAT")})
    assert err


@pytest.mark.skipif(importlib.util.find_spec("baler.commissioning.spaces_api") is None,
                    reason="spaces (#209) aren't in this install")
def test_space_tools_when_spaces_exist(repo):
    from baler.commissioning import spaces_api

    ctx = _ctx(repo)
    repo.put_rows(spaces_api.SPACES, [{"id": "or3", "project_id": "p1", "name": "OR 3", "kind": "room",
                                       "space_type": "operating_room", "parent_id": None, "status": "active",
                                       "point_uris": [], "attributes": {}, "created_at": fx.NOW, "updated_at": fx.NOW}])
    out, err = _call(ctx, "list_spaces", {"space_type": "operating_room"})
    assert not err and out[0]["name"] == "OR 3"

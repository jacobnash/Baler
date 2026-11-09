"""Spaces (#209): the model's rules, and the API on MemoryRepo."""

from __future__ import annotations

import cx_api_harness as hx
import cx_fixtures as fx
import pytest
import requests
from rdflib import URIRef

from baler import brick_vocab
from baler.commissioning import spaces
from baler.store import BRICK

CHW = "urn:equip:fbf/a/ch-1"


def test_builtin_brick_classes_exist_in_the_vendored_brick():
    vocab = brick_vocab.load()
    for slug, t in spaces.SPACE_TYPES.items():
        assert vocab.canonical_class(t["brick_class"]) == t["brick_class"], slug


def test_normalize_defaults_and_rejects():
    s = spaces.normalize({"name": "OR 3", "space_type": "operating_room", "attributes": {"volume_ft3": 4000}})
    assert s["kind"] == "room" and s["brick_class"] == "Room" and s["point_uris"] == []
    with pytest.raises(ValueError):
        spaces.normalize({"name": "x", "kind": "planet"})
    with pytest.raises(ValueError):
        spaces.normalize({"name": "x", "space_type": "Operating Room"})
    with pytest.raises(ValueError):
        spaces.normalize({"name": "x", "attributes": {"volume_ft3": -1}})
    with pytest.raises(ValueError):
        spaces.normalize({"name": "x", "point_uris": ["urn:equip:nope"]})
    # PATCH keeps what isn't sent
    assert spaces.normalize({"name": "OR 3b"}, s)["space_type"] == "operating_room"


def test_parent_cycles_refused_and_descendants():
    by_id = {"suite": {"id": "suite", "parent_id": None}, "or3": {"id": "or3", "parent_id": "suite"},
             "ante": {"id": "ante", "parent_id": "or3"}}
    spaces.check_parent("x", "suite", by_id)
    with pytest.raises(ValueError):
        spaces.check_parent("suite", "ante", by_id)
    with pytest.raises(ValueError):
        spaces.check_parent("x", "nope", by_id)
    assert [s["id"] for s in spaces.descendants("suite", list(by_id.values()))] == ["or3", "ante"]


def test_serving_chain_walks_feeds_in_scope():
    store = fx.store()
    g = store.graph
    g.add((URIRef(fx.AHU), BRICK.feeds, URIRef(fx.VAV)))
    g.add((URIRef(CHW), BRICK.feeds, URIRef(fx.AHU)))
    g.add((URIRef(fx.VAV), BRICK.feeds, URIRef(CHW)))  # a cycle in bad data must not loop
    chain = spaces.serving_chain(store, [fx.VAV])
    assert [(c["equip_uri"], c["level"]) for c in chain] == [(fx.VAV, 1), (fx.AHU, 2), (CHW, 3)]
    hidden = spaces.serving_chain(store, [fx.VAV], allows=lambda u: u != fx.AHU)
    assert [c["equip_uri"] for c in hidden] == [fx.VAV]


@pytest.fixture
def api(monkeypatch):
    store = fx.store()
    store.graph.add((URIRef(fx.AHU), BRICK.feeds, URIRef(fx.VAV)))
    repo = hx.repo(store=store)
    base, server = hx.serve(monkeypatch, repo)
    yield base, repo
    server.shutdown()


def test_spaces_api_end_to_end(api):
    base, repo = api
    p = hx.project(base)
    pid = p["id"]
    suite = hx.check(requests.post(f"{base}/projects/{pid}/spaces", json={"name": "Surgery suite", "kind": "suite"}, headers=hx.A),
                     "/projects/{id}/spaces", "post", 201)
    or3 = hx.check(requests.post(f"{base}/projects/{pid}/spaces", headers=hx.A, json={
        "name": "OR 3", "space_type": "operating_room", "parent_id": suite["id"],
        "point_uris": ["urn:point:fbf/a/vav-1/ZNT"], "attributes": {"volume_ft3": 4200}}), "/projects/{id}/spaces", "post", 201)
    corr = requests.post(f"{base}/projects/{pid}/spaces", json={"name": "Corridor 3", "space_type": "corridor"}, headers=hx.A).json()
    assert corr["brick_class"] == "Hallway"

    got = hx.check(requests.put(f"{base}/spaces/{or3['id']}/adjacent", json={"adjacent": [{"space_id": corr["id"], "relation": "door"}]},
                                headers=hx.A), "/spaces/{id}/adjacent", "put", 200)
    assert got["adjacent"] == [{"space_id": corr["id"], "relation": "door"}]
    # symmetric: the corridor sees the OR
    other = requests.get(f"{base}/spaces/{corr['id']}", headers=hx.A).json()
    assert other["adjacent"] == [{"space_id": or3["id"], "relation": "door"}]

    got = hx.check(requests.put(f"{base}/spaces/{or3['id']}/serving", json={"equip_uris": [fx.VAV]}, headers=hx.A),
                   "/spaces/{id}/serving", "put", 200)
    assert [(c["equip_uri"], c["level"]) for c in got["serving_chain"]] == [(fx.VAV, 1), (fx.AHU, 2)]
    # another org's equipment can't be attached
    r = requests.put(f"{base}/spaces/{or3['id']}/serving", json={"equip_uris": [fx.AHU_B]}, headers=hx.A)
    assert r.status_code == 400

    s = hx.check(requests.get(f"{base}/spaces/{suite['id']}", headers=hx.A), "/spaces/{id}", "get", 200)
    assert [c["id"] for c in s["children"]] == [or3["id"]]
    listed = hx.check(requests.get(f"{base}/projects/{pid}/spaces?space_type=operating_room", headers=hx.A),
                      "/projects/{id}/spaces", "get", 200)
    assert [x["id"] for x in listed] == [or3["id"]]
    hx.check(requests.get(f"{base}/space-types", headers=hx.A), "/space-types", "get", 200)

    r = requests.patch(f"{base}/spaces/{suite['id']}", json={"parent_id": or3["id"]}, headers=hx.A)
    assert r.status_code == 400 and "cycle" in r.text
    hx.check(requests.patch(f"{base}/spaces/{or3['id']}", json={"name": "OR 3 (hybrid)"}, headers=hx.A), "/spaces/{id}", "patch", 200)

    # tenancy: another org sees nothing
    assert requests.get(f"{base}/spaces/{or3['id']}", headers=hx.B).status_code == 404
    assert requests.delete(f"{base}/spaces/{corr['id']}", headers=hx.A).status_code == 204
    assert [x["name"] for x in requests.get(f"{base}/projects/{pid}/spaces", headers=hx.A).json()] == ["OR 3 (hybrid)", "Surgery suite"]
    actions = [e["action"] for e in repo.list_events(pid)]
    for a in ("space.create", "space.adjacency", "space.serving", "space.update", "space.archive"):
        assert a in actions

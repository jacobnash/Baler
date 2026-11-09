"""The shipped worked-example pack (#208): ASHRAE 170 operating room,
structure and citations only, every value blank, nothing active until a
person confirms it for their edition."""

from __future__ import annotations

import json
import os
import re

import cx_api_harness as hx
import requests

from baler.commissioning import packs

OR = "ashrae-170-or"


def test_library_loads_and_every_value_is_blank():
    lib = packs.library()
    pk = lib[OR]
    assert pk["source"]["edition"] == "2021" and "verify" in pk["title"].lower()
    rows = {r["id"]: r for r in pk["requirements"]}
    assert set(rows) == {"or-pressure-to-adjacent", "or-total-ach", "or-outdoor-ach", "or-temperature",
                         "or-relative-humidity", "or-final-filter"}
    for r in pk["requirements"]:
        assert packs.blanks(r), f"{r['id']} ships a value"
        assert r["verified_by"] is None
        assert "verify against your edition" in (r["note"] or "").lower()
        assert r["applies_to"]["space_type"] == ["operating_room"]
    assert packs.active_rows({**pk, "status": "draft"}) == []
    assert rows["or-final-filter"]["check"] == {"engine": "attested"}


def test_no_numbers_from_the_standard_in_the_file():
    """A guard on what we ship: the only digits allowed are the edition,
    table/section numbers in citations, and the evidence window settings."""
    path = os.path.join(packs.LIBRARY_DIR, "ashrae-170-operating-room.json")
    with open(path) as f:
        doc = json.load(f)
    for r in doc["requirements"]:
        c = r["criterion"]
        assert all(c.get(k) is None for k in ("value", "min", "max"))
        assert not re.search(r"\d", r["title"]), r["title"]
        assert not re.search(r"\d", r.get("note") or ""), r["note"]


def test_import_from_library_then_confirm(monkeypatch):
    base, server = hx.serve(monkeypatch, hx.repo())
    try:
        listed = hx.check(requests.get(f"{base}/pack-library", headers=hx.A), "/pack-library", "get", 200)
        assert [x["pack_id"] for x in listed] == [OR]
        draft = hx.check(requests.post(f"{base}/packs", json={"library": OR}, headers=hx.A), "/packs", "post", 201)
        assert draft["summary"] == {"rows": 6, "confirmed": 0, "blank": 6, "active": 0}
        r = requests.post(f"{base}/packs/{draft['id']}/requirements/or-relative-humidity/confirm", headers=hx.A)
        assert r.status_code == 400  # blank values are never confirmed
        assert requests.post(f"{base}/packs", json={"library": "nope"}, headers=hx.A).status_code == 400
    finally:
        server.shutdown()

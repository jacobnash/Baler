"""Requirement packs (#205): the schema's rules, confirmation, versions, API."""

from __future__ import annotations

import copy

import cx_api_harness as hx
import cx_fixtures as fx
import pytest
import requests

from baler.commissioning import packs

OWNER = {
    "pack_id": "owner-opr-ahu", "version": "2027.1", "title": "Owner design standard: AHUs",
    "source": {"standard": "Example owner design standard", "edition": "2027", "kind": "owner"},
    "jurisdiction": ["owner:example"],
    "requirements": [
        {"id": "dat-band", "title": "Discharge air temperature within band", "citation": {"ref": "Section 4.2", "page": 12},
         "applies_to": {"equip_class": ["Air_Handling_Unit"], "phase": ["acceptance", "operations"]},
         "criterion": {"quantity": "temperature", "op": "between", "min": None, "max": None, "unit": "degF"},
         "evidence": {"point_class": "Discharge_Air_Temperature_Sensor", "window_hours": 24, "coverage": 0.95},
         "check": {"engine": "procedure", "criterion": "range"}},
        {"id": "ahu-points", "title": "Required AHU points present", "citation": {"ref": "Section 4.1"},
         "applies_to": {"equip_class": ["Air_Handling_Unit"]},
         "criterion": {}, "check": {"engine": "cx", "check_id": "template:"}},
        {"id": "or-rh", "title": "Operating room humidity", "citation": {"ref": "Section 9.3"},
         "applies_to": {"space_type": ["operating_room"]},
         "criterion": {"quantity": "relative_humidity", "op": "between", "min": None, "max": None, "unit": "%RH"},
         "check": {"engine": "procedure", "criterion": "range"}},
    ],
}


def test_normalize_accepts_blank_values_and_names_engines():
    p = packs.normalize(copy.deepcopy(OWNER))
    assert [packs.blanks(r) for r in p["requirements"]] == [["min", "max"], [], ["min", "max"]]


@pytest.mark.parametrize("mutate, msg", [
    (lambda d: d["source"].pop("edition"), "edition"),
    (lambda d: d["requirements"][0].update(text="verbatim table text"), "never quoted"),
    (lambda d: d["requirements"][0]["citation"].update(quote="..."), "no quoted text"),
    (lambda d: d["requirements"][0].update(check={"engine": "magic"}), "check.engine"),
    (lambda d: d["requirements"][0].update(check={"engine": "procedure", "criterion": "vibes"}), "criterion"),
    (lambda d: d["requirements"][0].update(check={"engine": "shacl", "template": "nope"}), "cx-expected-points"),
    (lambda d: d["requirements"][0].update(check={"engine": "gl36", "item": "nope"}), "GL36"),
    (lambda d: d["requirements"][0]["criterion"].update(unit="Pa"), "entered in"),
    (lambda d: d["requirements"][0].update(applies_to={}), "space_type or an equip_class"),
    (lambda d: d["requirements"][1].update(id="dat-band"), "unique"),
    (lambda d: d["requirements"][0].update(title="x" * 400), "at most"),
])
def test_normalize_refuses(mutate, msg):
    d = copy.deepcopy(OWNER)
    mutate(d)
    with pytest.raises(ValueError, match=msg):
        packs.normalize(d)


def test_values_then_confirm_then_active():
    p = {**packs.normalize(copy.deepcopy(OWNER)), "status": "draft", "history": []}
    assert [r["id"] for r in packs.active_rows(p)] == []
    with pytest.raises(ValueError, match="blank"):
        packs.confirm(p, "dat-band", "u", None, fx.NOW)
    p.update(packs.set_values(p, "dat-band", {"min": 52, "max": 58}, "u", fx.NOW))
    p.update(packs.confirm(p, "dat-band", "u", "u@example.com", fx.NOW))
    assert p["requirements"][0]["verified_by"]["edition"] == "2027"
    p.update(packs.confirm(p, "ahu-points", "u", None, fx.NOW))  # no values to enter
    assert [r["id"] for r in packs.active_rows(p)] == ["dat-band", "ahu-points"]
    # a changed number must be confirmed again
    p.update(packs.set_values(p, "dat-band", {"max": 60}, "u", fx.NOW))
    assert [r["id"] for r in packs.active_rows(p)] == ["ahu-points"]
    with pytest.raises(ValueError, match="frozen"):
        packs.set_values({**p, "status": "published"}, "dat-band", {"max": 61}, "u", fx.NOW)


def test_applies_by_space_type_equipment_subclass_and_phase():
    p = packs.normalize(copy.deepcopy(OWNER))
    dat, _, rh = p["requirements"]
    assert packs.applies(rh, space_type="operating_room")
    assert not packs.applies(rh, space_type="corridor")
    assert packs.applies(dat, equip_classes=["Air_Handling_Unit"], phase="operations")
    assert packs.applies(dat, equip_classes=["Rooftop_Unit"])  # a subclass of AHU in Brick
    assert not packs.applies(dat, equip_classes=["Air_Handling_Unit"], phase="design")


def test_packs_api(monkeypatch):
    base, server = hx.serve(monkeypatch, hx.repo())
    try:
        draft = hx.check(requests.post(f"{base}/packs", json=OWNER, headers=hx.A), "/packs", "post", 201)
        assert draft["status"] == "draft" and draft["summary"] == {"rows": 3, "confirmed": 0, "blank": 2, "active": 0}
        assert requests.post(f"{base}/packs", json=OWNER, headers=hx.A).status_code == 400  # same version twice
        ref = draft["id"]
        r = requests.post(f"{base}/packs/{ref}/requirements/dat-band/confirm", headers=hx.A)
        assert r.status_code == 400 and "blank" in r.text
        hx.check(requests.patch(f"{base}/packs/{ref}/requirements/dat-band", json={"min": 52, "max": 58}, headers=hx.A),
                 "/packs/{id}/requirements/{rid}", "patch", 200)
        got = hx.check(requests.post(f"{base}/packs/{ref}/requirements/dat-band/confirm", headers=hx.A),
                       "/packs/{id}/requirements/{rid}/confirm", "post", 200)
        assert got["summary"]["active"] == 1 and got["requirements"][0]["verified_by"]["email"] == "a@example.com"
        assert requests.get(f"{base}/packs/{ref}", headers=hx.B).status_code == 404  # another org's library

        pid = hx.project(base)["id"]
        r = requests.post(f"{base}/projects/{pid}/packs", json={"pack": ref}, headers=hx.A)
        assert r.status_code == 400 and "published" in r.text
        hx.check(requests.post(f"{base}/packs/{ref}/publish", headers=hx.A), "/packs/{id}/publish", "post", 200)
        assert requests.patch(f"{base}/packs/{ref}/requirements/dat-band", json={"max": 60}, headers=hx.A).status_code == 400
        hx.check(requests.post(f"{base}/projects/{pid}/packs", json={"pack": ref}, headers=hx.A), "/projects/{id}/packs", "post", 201)

        # a new edition is a new version, adopted by choice; the old adoption is retired, not deleted
        v2 = requests.post(f"{base}/packs", json={**OWNER, "version": "2029.1"}, headers=hx.A).json()
        requests.post(f"{base}/packs/{v2['id']}/publish", headers=hx.A)
        requests.post(f"{base}/projects/{pid}/packs", json={"pack": v2["id"]}, headers=hx.A)
        listed = hx.check(requests.get(f"{base}/projects/{pid}/packs", headers=hx.A), "/projects/{id}/packs", "get", 200)
        assert [x["pack"]["version"] for x in listed] == ["2029.1"]
        hx.check(requests.get(f"{base}/packs", headers=hx.A), "/packs", "get", 200)
        hx.check(requests.get(f"{base}/pack-schema", headers=hx.A), "/pack-schema", "get", 200)
    finally:
        server.shutdown()

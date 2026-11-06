"""Cx 7 (#43): spec ingestion and spec<->device alignment. PR #5's
spec_model/alignment tests, re-keyed on Brick classes and urn:equip:
URIs, plus CSV/XLSX import, the Claude extraction (a fake client - no
network), and the routes on MemoryRepo."""

from __future__ import annotations

import base64
import io
import time
from types import SimpleNamespace

import cx_fixtures as fx
import pytest
import requests
import test_commissioning_openapi as api
from conftest import assert_matches_schema, load_spec
from rdflib import Literal, URIRef

from baler.commissioning import db, inventory, spec
from baler.commissioning_api import OPENAPI_SPEC_PATH
from baler.scope import Scope
from baler.store import HAYSTACK

# --------------------------------------------------------------- tags (#5)


@pytest.mark.parametrize("a,b", [("AHU-1", "AHU_1"), ("AHU-1", "ahu 1"), ("AHU-1", "AHU1"), ("RTU-01", "RTU-1"), ("VAV-2-05", "vav2_5")])
def test_normalize_tag_ignores_delimiters_case_and_leading_zeros(a, b):
    assert spec.normalize_tag(a) == spec.normalize_tag(b)


def test_tag_letters_and_number_split_a_tag():
    assert spec.tag_letters("VAV-2-05") == "VAV"
    assert spec.tag_number("VAV-2-05") == spec.tag_number("V2_5") == "25"
    assert spec.tag_number("AHU-1") == "1"
    assert spec.tag_letters("") == "" and spec.tag_number(None) == ""


def test_type_from_tag_and_text():
    assert spec.type_from_tag("AHU-3").brick == "Air_Handling_Unit"
    assert spec.type_from_tag("VAV-2-05").brick == "Variable_Air_Volume_Box"
    assert spec.type_from_tag("XYZ-1") is None
    assert spec.brick_class_for("Fan coil unit, 4-pipe", "X-1") == ("Fan_Coil_Unit", "type column says fan coil unit")
    assert spec.brick_class_for(None, "RTU-4")[0] == "Rooftop_Unit"
    assert "inferred from the tag" in spec.brick_class_for(None, "RTU-4")[1]  # flagged, not silently assumed
    assert spec.brick_class_for("thingamajig", "Q-7") == (None, "type unknown")
    assert spec.brick_class_for("Chiller", None)[0] == "Chiller"


def test_point_class_from_a_point_list_name():
    assert spec.point_class_for("Supply Air Temperature") == "Supply_Air_Temperature_Sensor"
    assert spec.point_class_for("SAT", "Supply_Air_Temperature_Setpoint") == "Supply_Air_Temperature_Setpoint"


# ------------------------------------------------------------- CSV / XLSX

SCHEDULE = "Tag,Type,Serves,Airflow (cfm)\nAHU-1,Air handling unit,Level 1,12000\nVB-1,VAV box,Room 101,800\nCH-1,Chiller,Plant,\n,,,\n"
POINTS = "Equipment,Point Name,Units\nAHU-1,Supply Air Temperature,degF\nAHU-1,Supply Fan Status,\n"


def test_items_from_a_schedule_and_a_point_list():
    items = spec.items_from_rows(spec.read_rows("csv", SCHEDULE), "M-601.csv")
    assert [(i["kind"], i["tag"], i["brick_class"]) for i in items] == [
        ("equipment", "AHU-1", "Air_Handling_Unit"), ("equipment", "VB-1", "Variable_Air_Volume_Box"), ("equipment", "CH-1", "Chiller")]
    assert items[0]["properties"]["Airflow (cfm)"] == "12000" and items[0]["properties"]["location"] == "Level 1"
    assert items[0]["source"] == {"document": "M-601.csv", "row": 2, "page": None, "method": "table"}
    pts = spec.items_from_rows(spec.read_rows("csv", POINTS), "points.csv")
    assert [(p["kind"], p["equip_tag"], p["brick_class"]) for p in pts] == [
        ("point", "AHU-1", "Supply_Air_Temperature_Sensor"), ("point", "AHU-1", "Fan_Status")]


def test_xlsx_import():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Tag", "Type"])
    ws.append(["FCU-3", "Fan Coil Unit"])
    buf = io.BytesIO()
    wb.save(buf)
    rows = spec.read_rows("xlsx", base64.b64encode(buf.getvalue()).decode())
    assert spec.items_from_rows(rows, "x.xlsx")[0]["brick_class"] == "Fan_Coil_Unit"
    with pytest.raises(ValueError):
        spec.read_rows("xlsx", "not base64!")
    with pytest.raises(ValueError):
        spec.read_rows("pdf", "x")


# ---------------------------------------------------------------- align (#5)


def _items(*rows):
    return [{"id": f"s{i}", "kind": "equipment", "status": "accepted", "tag": t, "brick_class": c} for i, (t, c) in enumerate(rows)]


@pytest.fixture(scope="module")
def equips():
    return inventory.build(fx.store(), [Scope(("fbf/a",))])


def _m(res, tag):
    return next(m for m in res["mappings"] if m["spec_tag"] == tag)


def test_tag_match_with_delimiter_variation_and_agreeing_class_is_high(equips):
    res = spec.align(_items(("AHU_1", "Air_Handling_Unit")), equips, "warranty")
    m = _m(res, "AHU_1")
    assert m["equip_uri"] == fx.AHU and m["confidence"] == "high"
    assert m["basis"][0]["evidence"] == "exact" and m["basis"][1]["agrees"] is True


def test_known_abbreviation_with_same_number_is_medium(equips):
    m = _m(spec.align(_items(("VB-1", "Variable_Air_Volume_Box")), equips, "warranty"), "VB-1")
    assert m["equip_uri"] == fx.VAV and m["confidence"] == "medium"
    assert "abbreviation" in m["basis"][0]["detail"]


def test_number_only_match_is_low_with_a_named_alternative(equips):
    m = _m(spec.align(_items(("FCU-1", None)), equips, "warranty"), "FCU-1")
    assert m["confidence"] == "low"
    assert any("not FCU-1 at all" in a["interpretation"] for a in m["alternatives"])


def test_class_contradiction_is_a_type_mismatch_deviation(equips):
    res = spec.align(_items(("AHU-1", "Chiller")), equips, "warranty")
    m = _m(res, "AHU-1")
    assert m["confidence"] == "low" and any("replaced" in a["interpretation"] for a in m["alternatives"])
    assert any(d["kind"] == "type_mismatch" and d["spec_tag"] == "AHU-1" for d in res["deviations"])


def test_spec_item_not_found_is_read_per_phase_never_bare(equips):
    for phase, words, sev in (("construction", "not yet installed", "info"), ("warranty", "contractor is still responsible", "warning"),
                              ("operations", "outage", "warning")):
        d = next(d for d in spec.align(_items(("CH-1", "Chiller")), equips, phase)["deviations"] if d["kind"] == "spec_not_found")
        assert words in d["interpretation"] and d["severity"] == sev and d["to_settle"]


def test_duplicate_claim_and_unspecified_field_equipment(equips):
    res = spec.align(_items(("AHU-1", "Air_Handling_Unit"), ("AHU_01", "Air_Handling_Unit")), equips, "operations")
    dup = next(d for d in res["deviations"] if d["kind"] == "duplicate_claim")
    assert dup["equip_uri"] == fx.AHU and sorted(dup["spec_tags"]) == ["AHU-1", "AHU_01"]
    extra = {d["equip_uri"] for d in res["deviations"] if d["kind"] == "unspecified_in_field"}
    assert fx.VAV in extra and fx.MTR in extra and fx.AHU not in extra


def test_a_persons_decisions_pin_and_exclude(equips):
    items = _items(("FCU-1", None))
    pinned = spec.align(items, equips, "warranty", {"s0": {"status": "confirmed", "equip_uri": fx.VAV}})
    assert _m(pinned, "FCU-1")["confidence"] == "confirmed" and _m(pinned, "FCU-1")["equip_uri"] == fx.VAV
    first = _m(spec.align(items, equips, "warranty"), "FCU-1")["equip_uri"]
    rejected = _m(spec.align(items, equips, "warranty", {"s0": {"status": "rejected", "equip_uri": first}}), "FCU-1")
    assert rejected["equip_uri"] != first


def test_points_present_absent_and_unspecified(equips):
    ahu = next(e for e in equips if e["id"] == fx.AHU)
    item = {"id": "s0", "tag": "AHU-1"}
    pts = [{"id": "p1", "kind": "point", "status": "accepted", "equip_tag": "AHU_1", "name": "Discharge air temp",
            "brick_class": "Supply_Air_Temperature_Sensor"},
           {"id": "p2", "kind": "point", "status": "accepted", "equip_tag": "AHU-1", "name": "DSP",
            "brick_class": "Supply_Air_Static_Pressure_Sensor"}]
    out = spec.points_alignment(item, ahu, pts)
    by = {o["spec_item_id"]: o for o in out if o["spec_item_id"]}
    assert by["p1"]["status"] == "present" and by["p1"]["point_uri"] == fx.pt(fx.AHU, "SAT")
    assert by["p2"]["status"] == "specified_absent" and by["p2"]["point_uri"] is None  # never invented
    assert {o["point_uri"] for o in out if o["status"] == "unspecified_present"} >= {fx.pt(fx.AHU, "RAT")}


# ---------------------------------------------------------- PDF extraction


def _fake_client(items, stop="end_turn", notes=""):
    from baler.commissioning import spec_llm

    calls = []

    def parse(**kw):
        calls.append(kw)
        parsed = spec_llm._Extraction(items=[spec_llm._Item(**i) for i in items], notes=notes)
        return SimpleNamespace(stop_reason=stop, parsed_output=parsed, model=kw["model"],
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5))
    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(parse=parse))), calls


EXTRACTED = [
    {"kind": "equipment", "tag": "AHU-1", "name": "Air handling unit", "brick_class": "Air_Handling_Unit",
     "properties": [{"name": "Airflow", "value": "12,000 cfm"}], "page": 3, "quote": "AHU-1 ... 12,000 CFM", "confidence": 0.95},
    {"kind": "equipment", "tag": "B-2", "name": "Boiler", "brick_class": "Not_A_Class", "page": 4, "quote": "B-2 boiler", "confidence": 0.7},
    {"kind": "point", "tag": "SAT", "name": "Supply air temperature", "equipment_tag": "AHU-1",
     "brick_class": "Supply_Air_Temperature_Sensor", "page": 7, "quote": "SAT AI-1", "confidence": 2.0},
]


def test_extract_pdf_proposes_cited_items(monkeypatch):
    pytest.importorskip("pydantic")
    from baler.commissioning import spec_llm

    monkeypatch.setenv("BALER_CX_MODEL", "test-model")
    client, calls = _fake_client(EXTRACTED, notes="page 9 illegible")
    items, info = spec_llm.extract_pdf(b"%PDF-1.4 x", "M-601.pdf", client)
    kw = calls[0]
    assert kw["model"] == "test-model" and kw["fallbacks"] == "default" and kw["output_format"] is spec_llm._Extraction
    doc = kw["messages"][0]["content"][0]
    assert doc["type"] == "document" and doc["source"]["media_type"] == "application/pdf"
    assert base64.b64decode(doc["source"]["data"]) == b"%PDF-1.4 x"
    assert [(i["tag"], i["brick_class"]) for i in items] == [("AHU-1", "Air_Handling_Unit"), ("B-2", "Boiler"), ("SAT", "Supply_Air_Temperature_Sensor")]
    assert items[0]["source"] == {"document": "M-601.pdf", "page": 3, "quote": "AHU-1 ... 12,000 CFM", "method": "llm", "model": "test-model"}
    assert items[0]["properties"] == {"Airflow": "12,000 cfm"} and items[2]["confidence"] == 1.0
    assert info["notes"] == "page 9 illegible"


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_extract_pdf_fails_loudly(stop):
    pytest.importorskip("pydantic")
    from baler.commissioning import spec_llm

    client, _ = _fake_client(EXTRACTED, stop=stop)
    with pytest.raises(spec_llm.ExtractionError):
        spec_llm.extract_pdf(b"%PDF", "x.pdf", client)


# ------------------------------------------------------------------- api


@pytest.fixture(scope="module")
def oas():
    return load_spec(OPENAPI_SPEC_PATH)


@pytest.fixture
def repo():
    return db.MemoryRepo(store=fx.store(), history=fx.history(), sites=api.SITES, scope_fn=api._scope, users={"u-a": "a@example.com"})


@pytest.fixture
def served(monkeypatch, repo):
    from baler.commissioning import runner

    worker = runner.RunWorker(repo, lambda: fx.NOW)
    server = api._serve(monkeypatch, repo, worker)
    yield f"http://127.0.0.1:{server.server_address[1]}", worker
    server.shutdown()


def test_import_review_align_confirm_write_back(served, repo, oas):
    base, _ = served
    p = api._project(base)
    r = requests.post(f"{base}/projects/{p['id']}/spec/import", json={"format": "csv", "data": SCHEDULE, "filename": "M-601.csv"}, headers=api.A)
    assert r.status_code == 201, r.text
    assert_matches_schema(r.json(), api._schema(oas, "/projects/{id}/spec/import", "post", 201), oas)
    assert (r.json()["items"], r.json()["skipped_rows"]) == (3, 1)
    requests.post(f"{base}/projects/{p['id']}/spec/import", json={"format": "csv", "data": POINTS, "kind": "points"}, headers=api.A)
    items = requests.get(f"{base}/projects/{p['id']}/spec", params={"kind": "equipment"}, headers=api.A)
    assert_matches_schema(items.json(), api._schema(oas, "/projects/{id}/spec", "get", 200), oas)
    assert {i["status"] for i in items.json()} == {"accepted"}

    al = requests.get(f"{base}/projects/{p['id']}/alignment", headers=api.A)
    assert_matches_schema(al.json(), api._schema(oas, "/projects/{id}/alignment", "get", 200), oas)
    by = {m["spec_tag"]: m for m in al.json()["mappings"]}
    assert by["AHU-1"]["confidence"] == "high" and by["VB-1"]["confidence"] == "medium" and by["CH-1"]["confidence"] == "unmatched"
    assert {x["status"] for x in by["AHU-1"]["points"]} >= {"present", "unspecified_present"}

    vb = next(i for i in items.json() if i["tag"] == "VB-1")
    d = requests.post(f"{base}/projects/{p['id']}/alignment/decisions", headers=api.A,
                      json={"spec_item_id": vb["id"], "equip_uri": fx.VAV, "status": "confirmed", "write_back": True})
    assert d.status_code == 201, d.text
    assert d.json()["written_back"] == {"set": {"cxSpecTag": "VB-1"}}  # the VAV already has a class: not overwritten
    assert (URIRef(fx.VAV), HAYSTACK["cxSpecTag"], Literal("VB-1")) in repo.store.graph
    assert repo.edits[-1]["uri"] == fx.VAV and repo.edits[-1]["user_sub"] == "u-a"
    by = {m["spec_tag"]: m for m in requests.get(f"{base}/projects/{p['id']}/alignment", headers=api.A).json()["mappings"]}
    assert by["VB-1"]["confidence"] == "confirmed" and by["VB-1"]["decision"]["status"] == "confirmed"
    # undo the decision
    assert requests.delete(f"{base}/mappings/{d.json()['id']}", headers=api.B).status_code == 404
    assert requests.delete(f"{base}/mappings/{d.json()['id']}", headers=api.A).status_code == 204
    # validation
    for bad in ({"spec_item_id": vb["id"], "equip_uri": fx.AHU_B, "status": "confirmed"},
                {"spec_item_id": vb["id"], "equip_uri": fx.VAV, "status": "maybe"},
                {"spec_item_id": "nope", "equip_uri": fx.VAV, "status": "confirmed"}):
        assert requests.post(f"{base}/projects/{p['id']}/alignment/decisions", json=bad, headers=api.A).status_code == 400
    assert requests.get(f"{base}/projects/{p['id']}/alignment", headers=api.B).status_code == 404


def test_extract_then_review(served, repo, oas):
    base, worker = served
    from baler.commissioning import spec as spec_mod

    def fake(data, filename):
        assert data.startswith(b"%PDF")
        return [{"kind": "equipment", "tag": "AHU-1", "name": "AHU", "equip_tag": None, "brick_class": "Air_Handling_Unit",
                 "properties": {}, "source": {"document": filename, "page": 2, "quote": "AHU-1", "method": "llm"},
                 "confidence": 0.9},
                {"kind": "equipment", "tag": "AHU-7", "name": "AHU", "equip_tag": None, "brick_class": None,
                 "properties": {}, "source": {"document": filename, "page": 2, "quote": "AHU-7", "method": "llm"},
                 "confidence": 0.4}], {"model": "fake", "notes": ""}
    worker.extract_fn = fake
    assert spec_mod.MAX_PDF_BYTES > 1000
    p = api._project(base)
    pdf = base64.b64encode(b"%PDF-1.7\n...").decode()
    r = requests.post(f"{base}/projects/{p['id']}/spec/extract", json={"filename": "M-601.pdf", "data": pdf}, headers=api.A)
    assert r.status_code == 202, r.text
    assert_matches_schema(r.json(), api._schema(oas, "/projects/{id}/spec/extract", "post", 202), oas)
    deadline = time.monotonic() + 10
    while (doc := requests.get(f"{base}/spec-documents/{r.json()['id']}", headers=api.A).json())["status"] not in ("completed", "failed"):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert doc["status"] == "completed" and doc["items"] == 2 and doc["sha256"]
    items = requests.get(f"{base}/projects/{p['id']}/spec", params={"status": "proposed"}, headers=api.A).json()
    assert len(items) == 2 and all(i["source"]["method"] == "llm" for i in items)
    # proposals never align until a person accepts them
    assert requests.get(f"{base}/projects/{p['id']}/alignment", headers=api.A).json()["mappings"] == []
    a1 = next(i for i in items if i["tag"] == "AHU-1")
    a7 = next(i for i in items if i["tag"] == "AHU-7")
    fixed = requests.patch(f"{base}/spec-items/{a7['id']}", json={"tag": "AHU-01", "brick_class": "air_handling_unit", "status": "accepted"}, headers=api.A)
    assert fixed.status_code == 200 and fixed.json()["source"]["corrected_from"] == {"tag": "AHU-7", "brick_class": None}
    assert fixed.json()["brick_class"] == "Air_Handling_Unit" and fixed.json()["reviewed_by"] == "u-a"
    assert requests.post(f"{base}/projects/{p['id']}/spec/review", json={"ids": [a1["id"]], "status": "rejected"}, headers=api.A).json() == {"updated": 1}
    al = requests.get(f"{base}/projects/{p['id']}/alignment", headers=api.A).json()
    assert [m["spec_tag"] for m in al["mappings"]] == ["AHU-01"]
    assert requests.patch(f"{base}/spec-items/{a7['id']}", json={"brick_class": "Nope"}, headers=api.A).status_code == 400
    assert requests.post(f"{base}/projects/{p['id']}/spec/extract", json={"data": base64.b64encode(b"hello").decode()}, headers=api.A).status_code == 400
    docs = requests.get(f"{base}/projects/{p['id']}/spec/documents", headers=api.A).json()
    assert [d["id"] for d in docs] == [r.json()["id"]]


def test_failed_extraction_is_recorded(served):
    base, worker = served

    def boom(data, filename):
        raise RuntimeError("model declined")
    worker.extract_fn = boom
    p = api._project(base)
    r = requests.post(f"{base}/projects/{p['id']}/spec/extract", json={"data": base64.b64encode(b"%PDF").decode()}, headers=api.A)
    deadline = time.monotonic() + 10
    while (doc := requests.get(f"{base}/spec-documents/{r.json()['id']}", headers=api.A).json())["status"] not in ("completed", "failed"):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert doc["status"] == "failed" and "model declined" in doc["error"]

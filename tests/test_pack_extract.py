"""Requirement rows from a user-held document, as proposals (#206). The
model is faked; quotes are checked against the pages."""

from __future__ import annotations

import base64
import copy
import json

import cx_api_harness as hx
import pytest
import requests
from test_packs import OWNER

from baler.commissioning import pack_extract, pack_extract_api, spec_pdf

PAGES = [
    spec_pdf.Page(1, "Owner design guide. Contents.", ""),
    spec_pdf.Page(7, "4.3 Operating rooms. Relative humidity shall be held between 41 and 57 percent. "
                     "Rooms shall be positive to adjacent corridors by at least 0.07 in. w.c.", ""),
]
GOOD = {"id": "guide-or-rh", "title": "OR humidity band", "ref": "4.3", "page": 7,
        "quote": "Relative humidity shall be held between 41 and 57 percent", "space_type": ["operating_room"],
        "quantity": "relative_humidity", "op": "between", "min": 41, "max": 57, "unit": "%RH", "confidence": 0.9}
MOVED = {"id": "or-dp", "title": "OR positive to corridor", "ref": "4.3", "page": 1,
         "quote": "positive to adjacent corridors by at least 0.07 in. w.c.", "space_type": ["operating_room"],
         "quantity": "pressure_to_adjacent", "op": ">=", "value": 0.07, "unit": "inH2O", "confidence": 0.8}
INVENTED = {**GOOD, "id": "or-ach", "quote": "Thirty-three air changes per hour are required", "quantity": "total_ach",
            "op": ">=", "value": 33, "unit": "1/h"}
NO_ENGINE = {**GOOD, "id": "filters", "quantity": "filtration", "value": "MERV 9"}


def fake(items):
    return lambda system, text: json.dumps({"items": items, "notes": "fake"})


def test_quotes_are_checked_and_rows_carry_citations_only():
    found, info = pack_extract.extract(PAGES, "guide.pdf", fake([GOOD, MOVED, INVENTED, NO_ENGINE]))
    assert [f["row"]["id"] for f in found] == ["guide-or-rh", "or-dp"]
    rh, dp = found
    assert rh["row"]["citation"] == {"ref": "4.3", "page": 7}
    assert "quote" not in json.dumps(rh["row"])  # the quote stays on the proposal
    assert rh["source"]["quote"].startswith("Relative humidity") and rh["source"]["quote_verified"]
    assert rh["row"]["criterion"] == {"quantity": "relative_humidity", "op": "between", "unit": "%RH", "min": 41, "max": 57}
    assert rh["row"]["verified_by"] is None
    assert dp["source"]["page"] == 7 and dp["row"]["citation"]["page"] == 7  # quote found on p7, not the cited p1
    assert info["dropped"] == 2 and "quote not in the PDF" in info["notes"] and "filtration" in info["notes"]


def test_scanned_pdf_and_no_model():
    with pytest.raises(pack_extract.ExtractionError, match="OCR"):
        pack_extract.extract([spec_pdf.Page(1, "  ", "")], "x.pdf", fake([]))


def test_extract_api_accept_and_reject(monkeypatch):
    monkeypatch.setattr(pack_extract_api, "CHAT", fake([GOOD, MOVED]))
    monkeypatch.setattr(spec_pdf, "safe_read", lambda data, name: (PAGES, None))
    base, server = hx.serve(monkeypatch, hx.repo())
    try:
        draft = requests.post(f"{base}/packs", json=copy.deepcopy(OWNER), headers=hx.A).json()
        pdf = base64.b64encode(b"%PDF-1.4 fake").decode()
        ex = hx.check(requests.post(f"{base}/packs/{draft['id']}/extract", json={"filename": "guide.pdf", "data": pdf}, headers=hx.A),
                      "/packs/{id}/extract", "post", 202)
        server.RequestHandlerClass.WORKER.join()
        got = hx.check(requests.get(f"{base}/pack-extractions/{ex['id']}", headers=hx.A), "/pack-extractions/{id}", "get", 200)
        assert got["status"] == "completed" and got["proposals"] == 2, got
        props = hx.check(requests.get(f"{base}/packs/{draft['id']}/proposals", headers=hx.A), "/packs/{id}/proposals", "get", 200)
        rh = next(p for p in props if p["row"]["id"] == "guide-or-rh")
        dp = next(p for p in props if p["row"]["id"] == "or-dp")
        # the person corrects the RH floor and accepts: the row joins the draft unconfirmed
        acc = hx.check(requests.post(f"{base}/pack-proposals/{rh['id']}/accept", json={"row": {"criterion": {
            "quantity": "relative_humidity", "op": "between", "min": 23, "max": 58, "unit": "%RH"}}}, headers=hx.A),
            "/pack-proposals/{id}/accept", "post", 200)
        assert acc["status"] == "accepted"
        pack = requests.get(f"{base}/packs/{draft['id']}", headers=hx.A).json()
        row = pack["requirements"][-1]
        assert row["id"] == "guide-or-rh" and row["criterion"]["min"] == 23 and row["verified_by"] is None
        assert pack["summary"]["active"] == 0
        assert requests.post(f"{base}/pack-proposals/{rh['id']}/accept", headers=hx.A).status_code == 400  # decided already
        hx.check(requests.post(f"{base}/pack-proposals/{dp['id']}/reject", headers=hx.A), "/pack-proposals/{id}/reject", "post", 200)
        assert requests.get(f"{base}/pack-extractions/{ex['id']}", headers=hx.B).status_code == 404
        assert requests.post(f"{base}/packs/{draft['id']}/extract", json={"data": base64.b64encode(b"hello").decode()},
                             headers=hx.A).status_code == 400
    finally:
        server.shutdown()

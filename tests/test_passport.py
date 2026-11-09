"""The building passport (#219): it reads the parts of the record this
install has, names the parts it doesn't, and takes asset history and the
like from the modules that own them (no competing model)."""

from __future__ import annotations

from datetime import timedelta

import cx_api_harness as hx
import cx_fixtures as fx
import pytest
import requests

from baler import export
from baler.commissioning import db, passport


@pytest.fixture
def api(monkeypatch):
    repo = hx.repo()
    base, server = hx.serve(monkeypatch, repo)
    yield base, repo
    server.shutdown()
    passport.SECTION_SOURCES.clear()


def test_passport_per_position(api):
    base, repo = api
    pid = hx.project(base)["id"]
    proc = requests.post(f"{base}/projects/{pid}/procedures", json={"template": "ahu_startup"}, headers=hx.A).json()
    for i, status in enumerate(("pass", "fail")):
        repo.create_test_run({"id": f"tr{i}", "procedure_id": proc["id"], "project_id": pid, "status": "completed",
                              "requested_at": fx.NOW - timedelta(days=30 - i), "finished_at": fx.NOW - timedelta(days=30 - i),
                              "equipment": [{"equip_uri": fx.AHU, "status": status, "steps": []}]})
    repo.create_issue({"id": "iss", "project_id": pid, "equip_uri": fx.AHU, "check_id": None, "subject": fx.AHU,
                       "status": "open", "title": "SAT hunting", "created_at": fx.NOW})
    repo.open_fault("r1", fx.pt(fx.AHU, "SAT"), fx.NOW, {}, "warning")

    rep = hx.check(requests.get(f"{base}/projects/{pid}/passport", headers=hx.A), "/projects/{id}/passport", "get", 200)
    ahu = next(p for p in rep["positions"] if p["equip_uri"] == fx.AHU)
    assert ahu["startup_vs_today"]["first_startup"]["status"] == "pass"
    assert ahu["startup_vs_today"]["latest"]["status"] == "fail"  # start-up baseline vs today
    assert ahu["issues"]["open"] == 1 and len(ahu["open_faults"]) == 1
    missing = rep["not_in_this_record"]
    assert missing["assets"].startswith("#197")
    # #215 (baselines), #216 (savings cases), #209/#207 (spaces, compliance log) have landed: their
    # tables are registered, so those sections are read, not listed as missing
    assert not {"efficiency", "replace_vs_keep", "compliance_by_space"} & set(missing)
    assert rep["compliance_by_space"] == [] and ahu["efficiency"] == [] and ahu["replace_vs_keep"] == []
    assert {d["key"]: d["in_baler"] for d in rep["std_202_deliverables"]}["systems_manual"] is False
    # never names equipment another org owns
    assert all(not p["equip_uri"].startswith("urn:equip:fbf/b/") for p in rep["positions"])

    page = requests.get(f"{base}/projects/{pid}/passport?format=html", headers=hx.A)
    assert page.status_code == 200 and "default-src 'none'" in page.headers["Content-Security-Policy"]
    assert "Building passport" in page.text and "#197" in page.text
    assert requests.get(f"{base}/projects/{pid}/passport", headers=hx.B).status_code == 404


def test_sections_come_from_the_modules_that_own_them(api):
    base, _repo = api
    pid = hx.project(base)["id"]
    passport.SECTION_SOURCES["assets"] = lambda repo, project, uri: (
        [{"event": "install", "make": "WEG", "at": "2031-07-14", "reason": "bearing failure"}] if uri == fx.AHU else [])
    rep = requests.get(f"{base}/projects/{pid}/passport", headers=hx.A).json()
    ahu = next(p for p in rep["positions"] if p["equip_uri"] == fx.AHU)
    assert ahu["assets"][0]["make"] == "WEG" and "assets" not in rep["not_in_this_record"]


def test_registered_tables_are_read(api, monkeypatch):
    base, repo = api
    pid = hx.project(base)["id"]
    monkeypatch.setitem(db.ROW_TABLES, "cx_performance_baselines", (("id",), frozenset()))
    repo.put_rows("cx_performance_baselines", [{"id": "b1", "project_id": pid, "equip_uri": fx.AHU, "kind": "chiller_kw_per_ton",
                                                "fit_start": fx.NOW, "fit_end": fx.NOW, "status": "active", "asset_id": None,
                                                "model": {"cvrmse_pct": 4.2, "nmbe_pct": 0.1}, "created_at": fx.NOW}])
    rep = requests.get(f"{base}/projects/{pid}/passport", headers=hx.A).json()
    ahu = next(p for p in rep["positions"] if p["equip_uri"] == fx.AHU)
    assert ahu["efficiency"][0]["cvrmse_pct"] == 4.2 and "efficiency" not in rep["not_in_this_record"]


def test_export_lists_the_record_tables_in_fk_order():
    names = [t for t, _ in export.CX_TABLES]
    for before, after in (("cx_spaces", "cx_space_adjacency"), ("cx_packs", "cx_project_packs"),
                          ("cx_compliance_runs", "cx_compliance_log"), ("cx_performance_baselines", "cx_savings_cases"),
                          ("cx_schedules", "cx_schedule_activities"), ("cx_schedule_links", "cx_evidence_signoffs")):
        assert names.index(before) < names.index(after)
    assert export._cx_where("adopted_pack", None)[0].startswith("WHERE id IN (SELECT pack_ref FROM cx_project_packs")

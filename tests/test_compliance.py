"""Continuous compliance (#207): adopted packs judged against spaces on a
schedule, a per-space log, alerts through the shared faults path, and the
readiness report's requirements criterion."""

from __future__ import annotations

from datetime import timedelta

import cx_api_harness as hx
import cx_fixtures as fx
import pytest
import requests
from rdflib import RDF, URIRef

from baler.commissioning import compliance
from baler.store import BRICK

DP = "urn:point:fbf/a/or3/dp"
RH = "urn:point:fbf/a/or3/rh"


def _row(rid, quantity, crit, point_class, phase=("operations",)):
    return {"id": rid, "title": rid, "citation": {"ref": "Table X", "page": 3},
            "applies_to": {"space_type": ["operating_room"], "phase": list(phase)},
            "criterion": {"quantity": quantity, **crit},
            "evidence": {"point_class": point_class, "window_hours": 24, "coverage": 0.9},
            "check": {"engine": "procedure", "criterion": "range"}}


PACK = {"pack_id": "test-or", "version": "1", "title": "Test OR pack", "source": {"standard": "Test standard", "edition": "2021"},
        "requirements": [
            _row("or-dp", "pressure_to_adjacent", {"op": ">=", "value": 0.01, "unit": "inH2O"}, "Differential_Pressure_Sensor"),
            _row("or-rh", "relative_humidity", {"op": "between", "min": 20, "max": 60, "unit": "%RH"}, "Zone_Air_Humidity_Sensor"),
            _row("or-ach", "total_ach", {"op": ">=", "value": 20, "unit": "1/h"}, None),
            _row("or-design-only", "temperature", {"op": "between", "min": 68, "max": 75, "unit": "degF"},
                 "Zone_Air_Temperature_Sensor", phase=("design",)),
        ]}


def test_meets_and_settings():
    assert compliance.meets(0.02, {"op": ">=", "value": 0.01})
    assert not compliance.meets(65, {"op": "between", "min": 20, "max": 60})
    with pytest.raises(ValueError):
        compliance.validate_settings({"every_hours": 0})
    assert compliance.validate_settings({"every_hours": 24, "alerts": False})


@pytest.fixture
def api(monkeypatch):
    store = fx.store()
    store.graph.add((URIRef(DP), RDF.type, BRICK.Differential_Pressure_Sensor))
    store.graph.add((URIRef(RH), RDF.type, BRICK.Zone_Air_Humidity_Sensor))
    hist = fx.history()
    for i in range(24 * 12):
        t = fx.NOW - timedelta(minutes=5 * i)
        hist.add(DP, t, 0.004 if i == 30 else 0.02)  # one dip below 0.01, 2.5 h ago
        hist.add(RH, t, 45.0)
    repo = hx.repo(store=store, history=hist)
    base, server = hx.serve(monkeypatch, repo)
    yield base, repo, server, hist
    server.shutdown()


def _setup(base):
    pid = hx.project(base, phase="operations")["id"]
    room = requests.post(f"{base}/projects/{pid}/spaces", headers=hx.A,
                         json={"name": "OR 3", "space_type": "operating_room", "point_uris": [DP, RH]}).json()
    pack = requests.post(f"{base}/packs", json=PACK, headers=hx.A).json()
    for r in PACK["requirements"]:
        assert requests.post(f"{base}/packs/{pack['id']}/requirements/{r['id']}/confirm", headers=hx.A).status_code == 200
    requests.post(f"{base}/packs/{pack['id']}/publish", headers=hx.A)
    assert requests.post(f"{base}/projects/{pid}/packs", json={"pack": pack["id"]}, headers=hx.A).status_code == 201
    return pid, room


def _run(base, server, pid):
    run = hx.check(requests.post(f"{base}/projects/{pid}/compliance/runs", headers=hx.A), "/projects/{id}/compliance/runs", "post", 202)
    server.RequestHandlerClass.WORKER.join()
    return hx.check(requests.get(f"{base}/compliance-runs/{run['id']}", headers=hx.A), "/compliance-runs/{id}", "get", 200)


def test_compliance_run_log_alerts_and_readiness(api):
    base, repo, server, hist = api
    pid, room = _setup(base)
    got = _run(base, server, pid)
    assert got["status"] == "completed", got
    by = {r["row_id"]: r for r in got["results"]}
    assert set(by) == {"or-dp", "or-rh", "or-ach"}  # the design-phase row doesn't apply in operations
    assert by["or-dp"]["status"] == "fail" and by["or-dp"]["evidence"]["points"][0]["exceedances"] == 1
    assert by["or-dp"]["evidence"]["points"][0]["worst"]["value"] == 0.004
    assert by["or-rh"]["status"] == "pass"
    assert by["or-ach"]["status"] == "inconclusive" and "airflow" in by["or-ach"]["note"]
    assert by["or-dp"]["citation"] == {"ref": "Table X", "page": 3, "standard": "Test standard", "edition": "2021"}

    # an alert through the shared faults table (the webhook path Cx 8 uses)
    open_ = [f for f in repo.faults if f["ended_at"] is None]
    assert [(f["rule_id"], f["point_uri"]) for f in open_] == [("compliance:test-or:or-dp", DP)]

    # readiness reads the latest run
    rep = requests.get(f"{base}/spaces/{room['id']}/readiness", headers=hx.A).json()
    assert rep["rooms"][0]["criteria"]["requirements"]["status"] == "fail"

    # the per-space log, JSON and printable
    log = hx.check(requests.get(f"{base}/spaces/{room['id']}/compliance?days=7", headers=hx.A), "/spaces/{id}/compliance", "get", 200)
    assert len(log["log"]) == 3
    page = requests.get(f"{base}/spaces/{room['id']}/compliance?format=html", headers=hx.A)
    assert "default-src 'none'" in page.headers["Content-Security-Policy"] and "or-dp" in page.text

    # the dip is fixed in the data: the next run passes and resolves the fault
    hist.series[DP] = [(t, 0.02) for t, _ in hist.series[DP]]
    got = _run(base, server, pid)
    assert {r["row_id"]: r["status"] for r in got["results"]}["or-dp"] == "pass"
    assert all(f["ended_at"] is not None for f in repo.faults)
    hx.check(requests.get(f"{base}/projects/{pid}/compliance/runs", headers=hx.A), "/projects/{id}/compliance/runs", "get", 200)
    assert requests.get(f"{base}/compliance-runs/{got['id']}", headers=hx.B).status_code == 404


def test_schedule_tick(api):
    base, repo, server, _ = api
    pid, _ = _setup(base)
    assert requests.patch(f"{base}/projects/{pid}", json={"settings": {"compliance": {"every_hours": 24}}},
                          headers=hx.A).status_code == 200
    worker = server.RequestHandlerClass.WORKER
    assert len(compliance.tick(repo, worker, fx.NOW)) == 1
    worker.join()
    assert compliance.tick(repo, worker, fx.NOW + timedelta(hours=1)) == []  # not due yet
    assert len(compliance.tick(repo, worker, fx.NOW + timedelta(hours=25))) == 1
    worker.join()

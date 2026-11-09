"""Hunting / oscillation (#214): the shared metrics, the per-loop report
against the start-up baseline, and the API."""

from __future__ import annotations

import math
from datetime import timedelta

import cx_api_harness as hx
import cx_fixtures as fx
import requests
from rdflib import RDF, URIRef

from baler import oscillation
from baler.commissioning import hunting
from baler.commissioning.history import InMemoryHistory
from baler.store import BRICK

AHU = fx.AHU
SAT, SATSP, VLV = fx.pt(AHU, "SAT2"), fx.pt(AHU, "SAT-SP"), fx.pt(AHU, "CHW-VLV")
NOW = fx.NOW


def wave(start, n, period_s=240, amp=22.0, step=30, mid=50.0):
    return [(start + timedelta(seconds=step * i), mid + amp * math.sin(2 * math.pi * step * i / period_s)) for i in range(n)]


def test_metrics_find_a_four_minute_oscillation():
    m = oscillation.metrics(wave(NOW, 240), deadband=1.0)
    assert m["judgeable"] and m["period_s"] == 240.0 and m["amplitude"] == 22.0
    assert 29 <= m["reversals_per_hour"] <= 31
    steady = oscillation.metrics([(NOW + timedelta(seconds=30 * i), 50 + 0.2 * (i % 2)) for i in range(240)], deadband=1.0)
    assert steady["reversals_per_hour"] == 0


def test_rollups_are_not_judged():
    m = oscillation.metrics([(NOW + timedelta(minutes=15 * i), float(i % 2)) for i in range(40)])
    assert m["judgeable"] is False and "raw samples" in m["reason"]


def test_error_crossings_with_hysteresis():
    sensor = wave(NOW, 240, amp=2.0, mid=55.0)
    sp = [(NOW - timedelta(hours=1), 55.0)]
    lm = oscillation.loop_metrics(sensor, sp, deadband=0.5)
    assert 29 <= lm["error"]["crossings_per_hour"] <= 31
    assert oscillation.crossings([(NOW, 0.1), (NOW, -0.1), (NOW, 0.2)], deadband=0.5) == 0  # noise inside the band


def _equip():
    return {"id": AHU, "dis": "AHU-1", "points": [
        {"id": SAT, "brickClass": "Supply_Air_Temperature_Sensor"},
        {"id": SATSP, "brickClass": "Supply_Air_Temperature_Setpoint"},
        {"id": VLV, "brickClass": "Valve_Command"}]}


def _history():
    h = InMemoryHistory()
    days = 4
    start = NOW - timedelta(days=days)
    onset = NOW - timedelta(days=2)
    for i in range(days * 24 * 120):  # every 30 s
        t = start + timedelta(seconds=30 * i)
        if t < onset:
            h.add(VLV, t, 40.0 + (0.3 if i % 2 else 0.0))
            h.add(SAT, t, 55.0 + (0.1 if i % 2 else 0.0))
        else:
            h.add(VLV, t, 50 + 22 * math.sin(2 * math.pi * 30 * i / 240))
            h.add(SAT, t, 55 + 2 * math.sin(2 * math.pi * 30 * i / 240))
    h.add(SATSP, start, 55.0)
    h.add(SATSP, onset - timedelta(minutes=10), 53.0)  # someone edited the setpoint
    return h


def test_loop_report_against_startup_baseline():
    eq = _equip()
    assert [lp["loop"] for lp in hunting.loops(eq)] == ["supply_air_temperature"]
    base = {AHU: (NOW - timedelta(days=4), NOW - timedelta(days=3, hours=20))}
    rows = hunting.analyse([eq], _history(), NOW, hours=6, days=3, baselines=base)
    r = rows[0]
    assert r["verdict"] == "hunting"
    vlv = r["now"]["commands"][VLV]
    assert vlv["period_s"] == 240.0 and vlv["amplitude"] == 22.0
    assert r["baseline_rate_per_hour"] == 0 and r["ratio"] >= 29
    assert r["onset"] is not None and (NOW - timedelta(days=3)).isoformat() <= r["onset"] <= NOW.isoformat()
    assert [c["kind"] for c in r["changes"]] == ["setpoint_edit"] and r["changes"][0]["to"] == 53.0


def test_asset_events_feed_the_changes():
    eq = _equip()
    swap = NOW - timedelta(days=2, hours=1)
    hunting.ASSET_EVENTS.append(lambda uri: [{"at": swap, "kind": "install", "detail": {"make": "WEG"}}] if uri == AHU else [])
    try:
        r = hunting.analyse([eq], _history(), NOW, hours=6, days=3)[0]
        assert "asset_install" in [c["kind"] for c in r["changes"]]
        assert r["baseline"] is None and r["threshold_per_hour"] == hunting.DEFAULTS["min_reversals_per_hour"]
    finally:
        hunting.ASSET_EVENTS.clear()


def test_hunting_api(monkeypatch):
    store = fx.store()
    for uri, cls in ((SAT, "Supply_Air_Temperature_Sensor"), (SATSP, "Supply_Air_Temperature_Setpoint"), (VLV, "Valve_Command")):
        store.graph.add((URIRef(uri), RDF.type, BRICK[cls]))
        store.graph.add((URIRef(AHU), BRICK.hasPoint, URIRef(uri)))
        store.graph.add((URIRef(uri), BRICK.isPointOf, URIRef(AHU)))
    base, server = hx.serve(monkeypatch, hx.repo(store=store, history=_history()))
    try:
        pid = hx.project(base)["id"]
        rep = hx.check(requests.get(f"{base}/projects/{pid}/hunting?hours=6&days=3&equip={AHU}", headers=hx.A),
                       "/projects/{id}/hunting", "get", 200)
        assert rep["hunting"] == 1 and rep["loops"][0]["verdict"] == "hunting"
        assert requests.get(f"{base}/projects/{pid}/hunting?hours=0", headers=hx.A).status_code == 400
        assert requests.get(f"{base}/projects/{pid}/hunting", headers=hx.B).status_code == 404
    finally:
        server.shutdown()

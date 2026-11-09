"""Performance baselines and degradation (#215): a synthetic chiller whose
kW/ton follows the model form; an 8 % drift after the fit season must be
found at matched conditions, and a hot spell outside the fitted range must
not be counted."""

from __future__ import annotations

import random
from datetime import timedelta

import cx_api_harness as hx
import cx_fixtures as fx
import pytest
import requests

from baler import performance
from baler.commissioning.history import InMemoryHistory

CH = "urn:equip:fbf/a/ch-1"
KW, TONS, ECWT = "urn:point:fbf/a/ch-1/kw", "urn:point:fbf/a/ch-1/tons", "urn:point:fbf/a/ch-1/ecwt"
START = fx.NOW - timedelta(days=120)
FIT_END = START + timedelta(days=60)


def truth(tons, ecwt):
    return 0.9 - 0.001 * tons + 0.0000015 * tons * tons + 0.006 * (ecwt - 75)


def chiller_history(drift=0.08):
    rnd = random.Random(7)
    h = InMemoryHistory()
    for i in range(120 * 24):
        t = START + timedelta(hours=i)
        tons = 100 + 400 * rnd.random()
        ecwt = 65 + 20 * rnd.random()
        if t >= FIT_END and i % 24 < 3:
            ecwt = 95.0  # a hot spell: outside what the model was fitted on
        kwpt = truth(tons, ecwt) * (1 + (drift if t >= FIT_END else 0)) * (1 + rnd.gauss(0, 0.01))
        h.add(KW, t, kwpt * tons)
        h.add(TONS, t, tons)
        h.add(ECWT, t, ecwt)
    return h


def _hours(h, start, end):
    raw = h.window([KW, TONS, ECWT], start, end)
    s = {u: performance.hourly_from_samples(raw[u], start, end) for u in raw}
    return performance.hours_from(s[KW], s[TONS], s[ECWT])


def test_fit_quality_and_drift():
    h = chiller_history()
    model = performance.fit(_hours(h, START, FIT_END))
    assert model["cvrmse_pct"] < 3 and abs(model["nmbe_pct"]) < 0.5  # noise was 1 %
    assert performance.quality(model)["meets_thresholds"]
    rep = performance.degradation(model, _hours(h, FIT_END, fx.NOW), alert_pct=5)
    assert 7.0 <= rep["overall"]["gap_pct"] <= 9.0
    assert rep["hours_outside_fit_range"] >= 60 * 3 - 5 and rep["alert"] is True
    assert [b["period"] for b in rep["buckets"]][0] == FIT_END.strftime("%Y-%m")


def test_too_little_data_and_flat_data_refused():
    with pytest.raises(ValueError, match="usable hours"):
        performance.fit([])
    flat = [{"ts": START + timedelta(hours=i), "tons": 300.0, "ecwt": 75.0, "kw": 180.0, "kw_per_ton": 0.6} for i in range(60)]
    with pytest.raises(ValueError, match="vary enough"):
        performance.fit(flat)


def test_load_from_flow_and_temps():
    t = START
    assert performance.load_tons({t: 1200.0}, {t: 44.0}, {t: 54.0}) == {t: 500.0}


def test_baselines_api(monkeypatch):
    base, server = hx.serve(monkeypatch, hx.repo(history=chiller_history()))
    try:
        pid = hx.project(base)["id"]
        body = {"equip_uri": CH, "points": {"kw": KW, "tons": TONS, "ecwt": ECWT},
                "fit_start": START.isoformat(), "fit_end": FIT_END.isoformat()}
        b = hx.check(requests.post(f"{base}/projects/{pid}/baselines", json=body, headers=hx.A), "/projects/{id}/baselines", "post", 201)
        assert b["model"]["quality"]["thresholds"]["note"].startswith("verify")
        rep = hx.check(requests.get(f"{base}/baselines/{b['id']}/degradation?alert_pct=5", headers=hx.A),
                       "/baselines/{id}/degradation", "get", 200)
        assert 7.0 <= rep["overall"]["gap_pct"] <= 9.0 and rep["alert"] is True
        # a refit supersedes, never deletes
        requests.post(f"{base}/projects/{pid}/baselines", json=body, headers=hx.A)
        rows = hx.check(requests.get(f"{base}/projects/{pid}/baselines", headers=hx.A), "/projects/{id}/baselines", "get", 200)
        assert sorted(r["status"] for r in rows) == ["active", "superseded"]
        bad = {**body, "points": {"kw": KW}}
        assert requests.post(f"{base}/projects/{pid}/baselines", json=bad, headers=hx.A).status_code == 400
        assert requests.post(f"{base}/projects/{pid}/baselines", json={**body, "equip_uri": "urn:equip:fbf/b/x"},
                             headers=hx.A).status_code == 400
        assert requests.get(f"{base}/baselines/{b['id']}/degradation", headers=hx.B).status_code == 404
    finally:
        server.shutdown()

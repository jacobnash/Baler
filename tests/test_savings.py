"""Replace-vs-keep savings case (#216): the arithmetic, every assumption
visible with its source, edits recomputed and recorded."""

from __future__ import annotations

import cx_api_harness as hx
import pytest
import requests
from test_baselines import CH, ECWT, FIT_END, KW, START, TONS, chiller_history

from baler import savings

USER = {"replacement_kw_per_ton": 0.5, "tariff_per_kwh": 0.10, "replacement_cost": 100000, "remaining_life_years": 8}


def test_compute_is_plain_arithmetic():
    a = savings.normalize({**USER, "current_kw_per_ton": 0.7, "annual_ton_hours": 1_000_000})
    r = savings.compute(a)
    assert r["annual_kwh_saved"] == 200_000 and r["annual_savings"] == 20_000.0
    assert r["simple_payback_years"] == 5.0 and r["savings_over_remaining_life"] == 160_000.0
    assert r["pays_back_within_remaining_life"] is True
    mid = next(x for x in r["sensitivity"] if x["tariff"] == "+0%" and x["ton_hours"] == "+0%")
    assert mid["simple_payback_years"] == 5.0
    assert {x["source"] for x in a.values()} == {"user", "default"}


def test_nothing_is_guessed():
    with pytest.raises(ValueError, match="none is guessed"):
        savings.normalize({"replacement_kw_per_ton": 0.5})
    with pytest.raises(ValueError):
        savings.normalize({"unknown": 1})


def test_worse_replacement_saves_nothing():
    r = savings.compute(savings.normalize({**USER, "current_kw_per_ton": 0.45, "annual_ton_hours": 1000}))
    assert r["simple_payback_years"] is None and "isn't more efficient" in r["note"]


def test_savings_api_measured_then_edited(monkeypatch):
    base, server = hx.serve(monkeypatch, hx.repo(history=chiller_history()))
    try:
        pid = hx.project(base)["id"]
        b = requests.post(f"{base}/projects/{pid}/baselines", headers=hx.A, json={
            "equip_uri": CH, "points": {"kw": KW, "tons": TONS, "ecwt": ECWT},
            "fit_start": START.isoformat(), "fit_end": FIT_END.isoformat()}).json()
        c = hx.check(requests.post(f"{base}/projects/{pid}/savings-cases", json={"baseline_id": b["id"], "assumptions": USER},
                                   headers=hx.A), "/projects/{id}/savings-cases", "post", 201)
        a = c["assumptions"]
        assert a["current_kw_per_ton"]["source"] == "measured" and a["annual_ton_hours"]["source"] == "measured"
        assert "hours" in a["annual_ton_hours"]["note"] and a["tariff_per_kwh"]["source"] == "user"
        first = c["result"]["annual_savings"]
        assert first > 0
        # the user disagrees with the measured ton-hours and the tariff: both editable, recomputed, recorded
        e = hx.check(requests.patch(f"{base}/savings-cases/{c['id']}", json={"assumptions": {
            "tariff_per_kwh": 0.20, "annual_ton_hours": {"value": 500000, "note": "metered last year"}}}, headers=hx.A),
            "/savings-cases/{id}", "patch", 200)
        assert e["assumptions"]["annual_ton_hours"]["source"] == "user"
        assert e["assumptions"]["current_kw_per_ton"]["source"] == "measured"  # untouched stays measured
        assert e["history"][-1]["changed"] == ["annual_ton_hours", "tariff_per_kwh"]
        hx.check(requests.get(f"{base}/projects/{pid}/savings-cases", headers=hx.A), "/projects/{id}/savings-cases", "get", 200)
        assert requests.get(f"{base}/savings-cases/{c['id']}", headers=hx.B).status_code == 404
    finally:
        server.shutdown()

"""
Replace-vs-keep savings case with visible assumptions (Baler#216, epic #221
§8.3): "replacing CH-2 saves about $X a year" is only as good as its stated
tariff and run hours, so every input is an assumption with a value, a
unit, where it came from, and a note, and every one can be edited.

Inputs (ASSUMPTIONS): the existing machine's kW/ton as measured (#215's
degradation report: actual kW/ton at matched conditions), the cooling
delivered per year (ton-hours, measured from the load point over the last
year where history allows), the replacement's rated kW/ton (from its
submittal: the user enters it), the tariff, an optional demand charge,
the replacement's installed cost, and the existing machine's remaining
service life (the user's judgement; ASHRAE's public service-life database
is a reference with a date, never a copied table).

Outputs: kWh and dollars saved per year, simple payback, savings over the
remaining life, and a sensitivity table (tariff and ton-hours +/-20 %),
so the reader sees how much the answer leans on each assumption. The
arithmetic is plain: kWh/yr = ton-hours x (kW/ton now - kW/ton new).
Nothing here is a price list or an estimate of anyone's costs: cost and
tariff are always the user's numbers.
"""

from __future__ import annotations

ASSUMPTIONS: dict[str, dict] = {
    "current_kw_per_ton": {"unit": "kW/ton", "label": "Existing chiller, measured kW/ton"},
    "replacement_kw_per_ton": {"unit": "kW/ton", "label": "Replacement, rated kW/ton at comparable load (from its submittal)"},
    "annual_ton_hours": {"unit": "ton-h/yr", "label": "Cooling delivered per year"},
    "tariff_per_kwh": {"unit": "$/kWh", "label": "Energy tariff"},
    "demand_charge_per_kw_month": {"unit": "$/kW-month", "label": "Demand charge (0 if not billed)"},
    "peak_tons": {"unit": "ton", "label": "Load at the billing peak (for the demand saving)"},
    "replacement_cost": {"unit": "$", "label": "Replacement installed cost"},
    "remaining_life_years": {"unit": "yr", "label": "Existing chiller's remaining service life"},
}
REQUIRED = ("current_kw_per_ton", "replacement_kw_per_ton", "annual_ton_hours", "tariff_per_kwh", "replacement_cost")
SOURCES = ("measured", "user", "default")


def normalize(assumptions: dict, measured: dict | None = None) -> dict:
    """{key: {value, unit, label, source, note}}: the user's values over
    measured ones over defaults. Raises naming any required value missing."""
    if not isinstance(assumptions, dict) or set(assumptions) - set(ASSUMPTIONS):
        raise ValueError(f"assumptions are any of {', '.join(ASSUMPTIONS)}")
    out: dict[str, dict] = {}
    for key, meta in ASSUMPTIONS.items():
        given = assumptions.get(key)
        if given is not None:
            v = given.get("value") if isinstance(given, dict) else given
            note = given.get("note") if isinstance(given, dict) else None
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise ValueError(f"{key} must be a number >= 0")
            out[key] = {**meta, "value": v, "source": "user", "note": (str(note)[:300] if note else None)}
        elif measured and key in measured:
            out[key] = {**meta, **measured[key], "source": "measured"}
        elif key in ("demand_charge_per_kw_month", "peak_tons"):
            out[key] = {**meta, "value": 0, "source": "default", "note": "no demand saving counted"}
        elif key == "remaining_life_years":
            out[key] = {**meta, "value": None, "source": "default", "note": "not given: no lifetime total"}
    missing = [k for k in REQUIRED if out.get(k, {}).get("value") is None]
    if missing:
        raise ValueError(f"needs {', '.join(missing)}: every number in a savings case is stated, none is guessed")
    return out


def compute(a: dict) -> dict:
    v = {k: a[k]["value"] for k in a}
    delta = v["current_kw_per_ton"] - v["replacement_kw_per_ton"]
    kwh = v["annual_ton_hours"] * delta
    energy = kwh * v["tariff_per_kwh"]
    demand = 12 * v["demand_charge_per_kw_month"] * v["peak_tons"] * delta
    annual = energy + demand
    payback = v["replacement_cost"] / annual if annual > 0 else None
    life = v.get("remaining_life_years")

    def pb(tariff_x: float, tonh_x: float) -> float | None:
        s = v["annual_ton_hours"] * tonh_x * delta * v["tariff_per_kwh"] * tariff_x + demand
        return round(v["replacement_cost"] / s, 1) if s > 0 else None

    out = {"kw_per_ton_saved": round(delta, 4), "annual_kwh_saved": round(kwh), "annual_energy_savings": round(energy, 2),
           "annual_demand_savings": round(demand, 2), "annual_savings": round(annual, 2),
           "simple_payback_years": round(payback, 1) if payback is not None else None,
           "savings_over_remaining_life": round(annual * life, 2) if life else None,
           "pays_back_within_remaining_life": (payback <= life) if payback is not None and life else None,
           "sensitivity": [{"tariff": f"{tx:+.0%}", "ton_hours": f"{hx:+.0%}", "simple_payback_years": pb(1 + tx, 1 + hx)}
                           for tx in (-0.2, 0.0, 0.2) for hx in (-0.2, 0.0, 0.2)],
           "formula": "annual kWh saved = ton-hours x (current kW/ton - replacement kW/ton); "
                      "$ = kWh x tariff + 12 x demand charge x peak tons x kW/ton saved; payback = cost / annual $"}
    if delta <= 0:
        out["note"] = "the replacement isn't more efficient than the existing chiller as measured: no energy saving"
    return out

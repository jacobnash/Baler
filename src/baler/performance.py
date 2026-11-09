"""
Equipment performance baselines and degradation since install (Baler#215,
epic #221 §8.2). Chiller kW/ton first; the same pattern fits boilers
(efficiency vs load and return water), fans (kW vs flow and static) and
pumps (kW vs flow and head).

A baseline is a regression fitted on a period the equipment was known
good (normally the first cooling season after acceptance):

    kW/ton = b0 + b1*tons + b2*tons^2 + b3*ECWT

so that a hot week, which raises the condenser water temperature, isn't
mistaken for a worn chiller. Degradation is then the gap between actual
and predicted kW/ton at matched conditions: only hours whose load and
ECWT fall inside the range the model was fitted on are compared (never
extrapolated), and the report says how many hours that was.

Model quality is ASHRAE Guideline 14's calibration statistics, CV(RMSE)
and NMBE. The thresholds are settings (THRESHOLDS), shown with the model,
and labelled "verify against the current edition of Guideline 14": they
are not quoted here as the standard's numbers. Rules of thumb such as
"inspect at 10 % drift" are vendor claims: `alert_pct` is a setting the
user chooses, not a standard.

Inputs are hourly averages of existing points (timeseries.read_rollup over
the continuous aggregates; raw history stays raw). The load can be a
point in tons (for example a derivation's output) or computed from chilled
water flow (gpm) and supply/return temperatures (degF): tons = gpm x dT / 24.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

KINDS = {"chiller_kw_per_ton": "Chiller kW/ton vs load and entering condenser water temperature"}
FEATURES = ("1", "tons", "tons^2", "ecwt")
# Settings, not the standard: verify against the current edition of Guideline 14.
THRESHOLDS = {"cvrmse_pct": 30.0, "nmbe_pct": 10.0, "note": "verify against the current edition of ASHRAE Guideline 14"}
MIN_HOURS = 48
GPM_TONS = 24.0  # tons = gpm * degF / 24 (water, 500 BTU/h per gpm-degF / 12,000 BTU/h per ton)


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting; raises on a singular system."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-12:
            raise ValueError("the fit period doesn't vary enough in load or condenser water temperature to fit a model")
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c], strict=True)]
    return [m[i][n] / m[i][i] for i in range(n)]


def _x(tons: float, ecwt: float) -> list[float]:
    return [1.0, tons, tons * tons, ecwt]


def predict(coef: list[float], tons: float, ecwt: float) -> float:
    return sum(c * x for c, x in zip(coef, _x(tons, ecwt), strict=True))


def hours_from(kw: dict, load: dict, ecwt: dict, min_tons: float | None = None) -> list[dict]:
    """[{ts, kw, tons, ecwt, kw_per_ton}] for the hours with all three,
    and a load above `min_tons` (default 10 % of the hours' peak load:
    kW/ton at near-zero load is noise, not efficiency)."""
    rows = [{"ts": t, "kw": kw[t], "tons": load[t], "ecwt": ecwt[t]} for t in sorted(set(kw) & set(load) & set(ecwt))]
    rows = [r for r in rows if all(isinstance(r[k], (int, float)) and math.isfinite(r[k]) for k in ("kw", "tons", "ecwt"))]
    if not rows:
        return []
    floor = min_tons if min_tons is not None else 0.1 * max(r["tons"] for r in rows)
    out = [r for r in rows if r["tons"] > max(floor, 0.0) and r["kw"] >= 0]
    for r in out:
        r["kw_per_ton"] = r["kw"] / r["tons"]
    return out


def fit(hours: list[dict]) -> dict:
    """Least squares on the hours; the coefficients, the fitted ranges and
    Guideline 14's CV(RMSE) and NMBE."""
    n, p = len(hours), len(FEATURES)
    if n < MIN_HOURS:
        raise ValueError(f"{n} usable hours in the fit period: at least {MIN_HOURS} needed")
    xs = [_x(h["tons"], h["ecwt"]) for h in hours]
    ys = [h["kw_per_ton"] for h in hours]
    xtx = [[sum(x[i] * x[j] for x in xs) for j in range(p)] for i in range(p)]
    xty = [sum(x[i] * y for x, y in zip(xs, ys, strict=True)) for i in range(p)]
    coef = _solve(xtx, xty)
    pred = [predict(coef, h["tons"], h["ecwt"]) for h in hours]
    ybar = sum(ys) / n
    resid = [y - f for y, f in zip(ys, pred, strict=True)]
    dof = max(n - p, 1)
    cvrmse = 100 * math.sqrt(sum(r * r for r in resid) / dof) / ybar
    nmbe = 100 * sum(resid) / (dof * ybar)
    return {"coefficients": coef, "features": list(FEATURES), "n_hours": n, "mean_kw_per_ton": round(ybar, 4),
            "cvrmse_pct": round(cvrmse, 2), "nmbe_pct": round(nmbe, 2),
            "ranges": {"tons": [min(h["tons"] for h in hours), max(h["tons"] for h in hours)],
                       "ecwt": [min(h["ecwt"] for h in hours), max(h["ecwt"] for h in hours)]}}


def quality(model: dict, thresholds: dict | None = None) -> dict:
    t = {**THRESHOLDS, **(thresholds or {})}
    ok = model["cvrmse_pct"] <= t["cvrmse_pct"] and abs(model["nmbe_pct"]) <= t["nmbe_pct"]
    return {"meets_thresholds": ok, "thresholds": t}


def in_range(model: dict, h: dict) -> bool:
    r = model["ranges"]
    return r["tons"][0] <= h["tons"] <= r["tons"][1] and r["ecwt"][0] <= h["ecwt"] <= r["ecwt"][1]


def degradation(model: dict, hours: list[dict], bucket: str = "month", alert_pct: float | None = None) -> dict:
    """Actual vs predicted kW/ton at matched conditions, per bucket and
    overall. Positive = using more energy per ton than the baseline."""
    matched = [h for h in hours if in_range(model, h)]
    rows: dict[str, list[tuple[float, float]]] = {}
    for h in matched:
        key = h["ts"].strftime("%Y-%m") if bucket == "month" else h["ts"].strftime("%G-W%V")
        rows.setdefault(key, []).append((h["kw_per_ton"], predict(model["coefficients"], h["tons"], h["ecwt"])))

    def gap(pairs):
        a = sum(x for x, _ in pairs) / len(pairs)
        p = sum(y for _, y in pairs) / len(pairs)
        return {"hours": len(pairs), "actual_kw_per_ton": round(a, 4), "predicted_kw_per_ton": round(p, 4),
                "gap_pct": round(100 * (a - p) / p, 2) if p else None}

    buckets = [{"period": k, **gap(v)} for k, v in sorted(rows.items())]
    overall = gap([pair for v in rows.values() for pair in v]) if rows else None
    out = {"hours_total": len(hours), "hours_matched": len(matched), "hours_outside_fit_range": len(hours) - len(matched),
           "buckets": buckets, "overall": overall, "alert_pct": alert_pct, "alert": None}
    if alert_pct is not None and overall and overall["gap_pct"] is not None:
        out["alert"] = overall["gap_pct"] >= alert_pct
    return out


def load_tons(flow_gpm: dict, supply_f: dict, return_f: dict) -> dict:
    """Hourly tons from chilled-water flow and temperatures."""
    return {t: flow_gpm[t] * (return_f[t] - supply_f[t]) / GPM_TONS
            for t in set(flow_gpm) & set(supply_f) & set(return_f)}


def hourly_from_samples(samples: list, start: datetime, end: datetime) -> dict[datetime, float]:
    """Hourly averages of raw samples (for callers without the rollup views)."""
    acc: dict[datetime, list[float]] = {}
    for t, v in samples:
        if start <= t < end and isinstance(v, (int, float)) and not isinstance(v, bool):
            acc.setdefault(t.replace(minute=0, second=0, microsecond=0), []).append(float(v))
    return {k: sum(v) / len(v) for k, v in acc.items()}


def hour_range(start: datetime, end: datetime) -> list[datetime]:
    t = start.replace(minute=0, second=0, microsecond=0)
    out = []
    while t < end:
        out.append(t)
        t += timedelta(hours=1)
    return out

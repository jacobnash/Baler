"""
HTTP routes for performance baselines and degradation (Baler#215), under
/cx/. A person fits a baseline over a period they choose; the degradation
report compares any later period against it at matched conditions.

Baselines attach to the position (equip_uri) and, once the asset registry
lands (#197), to the asset installed there (asset_id): a replaced chiller
gets a new baseline instead of being compared against the old machine.
"""

from __future__ import annotations

import re
from datetime import datetime

from baler import performance, timeseries
from baler.commissioning import db
from baler.commissioning.routes import Route, owned_row, query
from baler.http_handler_base import respond_json

BASELINES = "cx_performance_baselines"
db.ROW_TABLES[BASELINES] = (("id", "project_id", "equip_uri", "asset_id", "kind", "points", "fit_start", "fit_end",
                             "model", "status", "note", "created_by", "created_at"), frozenset({"points", "model"}))

POINT_KEYS = ("kw", "ecwt", "tons", "chw_flow_gpm", "chw_supply_f", "chw_return_f")


def hourly(repo, uris: list[str], start: datetime, end: datetime) -> dict[str, dict[datetime, float]]:
    """Hourly averages: the continuous aggregates where Postgres has them
    (timeseries.read_rollup), else folded from raw samples."""
    if hasattr(repo, "run_pool"):
        with repo.run_pool.connection() as conn:
            got = timeseries.read_rollup(conn, uris, start, end, "avg", 3600, None, "UTC")
        return {u: {t: v for t, v in got.get(u, []) if v is not None} for u in uris}
    with repo.history() as h:
        raw = h.window(uris, start, end)
    return {u: performance.hourly_from_samples(raw.get(u, []), start, end) for u in uris}


def check_points(points) -> dict:
    if not isinstance(points, dict) or set(points) - set(POINT_KEYS):
        raise ValueError(f"points is an object of {', '.join(POINT_KEYS)}")
    if not all(isinstance(v, str) and v.startswith("urn:point:") for v in points.values()):
        raise ValueError("points are urn:point: ids")
    if "kw" not in points or "ecwt" not in points:
        raise ValueError("points needs kw (chiller power, kW) and ecwt (entering condenser water, degF)")
    if "tons" not in points and not {"chw_flow_gpm", "chw_supply_f", "chw_return_f"} <= set(points):
        raise ValueError("points needs tons, or chw_flow_gpm + chw_supply_f + chw_return_f to compute it")
    return points


def hours_for(repo, points: dict, start: datetime, end: datetime) -> list[dict]:
    series = hourly(repo, sorted(set(points.values())), start, end)
    load = (series[points["tons"]] if "tons" in points else
            performance.load_tons(series[points["chw_flow_gpm"]], series[points["chw_supply_f"]], series[points["chw_return_f"]]))
    return performance.hours_from(series[points["kw"]], load, series[points["ecwt"]])


def _ts(v, what: str) -> datetime:
    try:
        t = datetime.fromisoformat(str(v))
    except ValueError as exc:
        raise ValueError(f"{what} is an ISO timestamp with a zone") from exc
    if t.tzinfo is None:
        raise ValueError(f"{what} needs a zone (e.g. ...Z or +00:00)")
    return t


def _create(h, span, claims, pid) -> None:
    """Fit a baseline: {equip_uri, kind, points, fit_start, fit_end, asset_id?, note?}."""
    if h._project(pid, claims) is None:
        return
    b = h.read_json_body()
    kind = b.get("kind", "chiller_kw_per_ton")
    if kind not in performance.KINDS:
        raise ValueError(f"kind must be one of {', '.join(performance.KINDS)}")
    equip = b.get("equip_uri")
    if not isinstance(equip, str) or not h.REPO.scope_for(claims).allows(equip):
        raise ValueError("equip_uri must be equipment you can see")
    points = check_points(b.get("points"))
    scope = h.REPO.scope_for(claims)
    if not all(scope.allows(u) for u in points.values()):
        raise ValueError("every point must be one you can see")
    start, end = _ts(b.get("fit_start"), "fit_start"), _ts(b.get("fit_end"), "fit_end")
    if end <= start:
        raise ValueError("fit_end must be after fit_start")
    model = performance.fit(hours_for(h.REPO, points, start, end))
    model["quality"] = performance.quality(model)
    row = {"id": db.new_id(), "project_id": pid, "equip_uri": equip, "asset_id": b.get("asset_id"), "kind": kind,
           "points": points, "fit_start": start, "fit_end": end, "model": model, "status": "active",
           "note": str(b.get("note") or "")[:500] or None, "created_by": claims.get("sub"), "created_at": h.NOW()}
    for old in h.REPO.find_rows(BASELINES, project_id=pid, equip_uri=equip, kind=kind):
        if old["status"] == "active" and old.get("asset_id") == row["asset_id"]:
            h.REPO.update_row(BASELINES, old["id"], {"status": "superseded"})
    h.REPO.put_rows(BASELINES, [row])
    h._event(pid, "baseline.fit", claims, {"baseline_id": row["id"], "equip_uri": equip, "kind": kind,
                                          "cvrmse_pct": model["cvrmse_pct"], "nmbe_pct": model["nmbe_pct"]})
    respond_json(h, 201, db.public_row(row))


def _list(h, span, claims, pid) -> None:
    if h._project(pid, claims) is None:
        return
    scope = h.REPO.scope_for(claims)
    respond_json(h, 200, [db.public_row(r) for r in h.REPO.find_rows(BASELINES, project_id=pid) if scope.allows(r["equip_uri"])])


def _degradation(h, span, claims, bid) -> None:
    """?start&end (default: from the end of the fit to now), bucket=month|week, alert_pct."""
    hit = owned_row(h, BASELINES, bid, claims, "baseline")
    if not hit:
        return
    row, _ = hit
    q = query(h)
    fit_end = row["fit_end"] if isinstance(row["fit_end"], datetime) else datetime.fromisoformat(row["fit_end"])
    start = _ts(q["start"][0], "start") if q.get("start") else fit_end
    end = _ts(q["end"][0], "end") if q.get("end") else h.NOW()
    bucket = q.get("bucket", ["month"])[0]
    if bucket not in ("month", "week"):
        raise ValueError("bucket is month or week")
    alert = float(q["alert_pct"][0]) if q.get("alert_pct") else None
    rep = performance.degradation(row["model"], hours_for(h.REPO, row["points"], start, end), bucket, alert)
    respond_json(h, 200, {"baseline": db.public_row(row), "start": start.isoformat(), "end": end.isoformat(), **rep})


ROUTES: list[Route] = [
    ("POST", re.compile(r"/projects/([^/]+)/baselines"), "fit_baseline", _create),
    ("GET", re.compile(r"/projects/([^/]+)/baselines"), "list_baselines", _list),
    ("GET", re.compile(r"/baselines/([^/]+)/degradation"), "baseline_degradation", _degradation),
]

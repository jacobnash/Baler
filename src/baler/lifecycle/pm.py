"""
Preventive maintenance schedules (#199, design §8.3).

A PM task applies to one asset, or to every installed asset of a type
("all supply fan motors"). Its trigger is one of:

  - **calendar**: every `interval_days`;
  - **run hours**: every `interval_hours` of run time, counted from a
    status point's *raw* history (a filter change every 2,000 fan hours).
    Every stored sample is used, never an aggregate: run time is the sum
    of the spans where the status is on, each capped at MAX_GAP so a gap
    in the data is never counted as running;
  - **condition**: when a point's latest value crosses a limit (filter
    differential pressure >= 1.0 inH2O).

Points can be absolute URIs, or a name relative to the asset's position:
`sf-status` under `urn:equip:fbf/s/ahu-1` is `urn:point:fbf/s/ahu-1/sf-status`,
so one type-wide task works for every asset of that type.

The count starts from the latest completion for that task and asset, or
from the asset's install into its current position. Due work exports as
CSV or as rows shaped like a COBie Job sheet. Baler is not a CMMS: it says
what's due and why, and the work order lives elsewhere.

Intervals typed here are the user's. Intervals read from O&M manuals are
proposals (#202) until a person confirms them. Schema: migration 0014.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta
from typing import Any

from baler.lifecycle import assets
from baler.lifecycle import rows as rows_mod
from baler.lifecycle.routes import (
    HTTPError,
    Raw,
    Request,
    as_dt,
    need,
    number,
    route,
    text,
    when,
)

TASK_COLS = ("id", "org_id", "title", "description", "asset_id", "asset_type", "trigger", "interval_days",
             "interval_hours", "run_point", "condition_point", "condition_op", "condition_value", "condition_unit",
             "duration_hours", "source", "status", "created_by", "created_at", "updated_at")
DONE_COLS = ("id", "org_id", "task_id", "asset_id", "done_at", "run_hours", "note", "work_ref", "created_by", "created_at")
rows_mod.register("lc_pm_tasks", TASK_COLS, ("source",))
rows_mod.register("lc_pm_completions", DONE_COLS, (), order="done_at")

TRIGGERS = ("calendar", "run_hours", "condition")
OPS = {">=": lambda a, b: a >= b, ">": lambda a, b: a > b, "<=": lambda a, b: a <= b, "<": lambda a, b: a < b}
MAX_GAP = timedelta(minutes=15)
DUE_SOON_DAYS = 14
DUE_SOON_FRACTION = 0.9
CONDITION_STALE = timedelta(hours=24)
ON_TEXT = ("on", "true", "run", "running", "active", "start")


def _uri(task_id: str) -> str:
    return rows_mod.entity_uri("pm_task", task_id)


# ------------------------------------------------------------ run hours


def is_on(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v > 0.5
    return str(v).strip().lower() in ON_TEXT


def run_seconds(samples: list[tuple[datetime, Any]], start: datetime, end: datetime, before: Any = None,
                max_gap: timedelta = MAX_GAP) -> float:
    """Seconds the status was on in [start, end). `samples` are the raw
    samples in the window, oldest first; `before` is the value of the last
    sample before `start` (it holds from `start` until the first sample, up
    to max_gap). Each sample's value holds until the next sample, for at
    most max_gap."""
    cap = max_gap.total_seconds()
    total = 0.0
    points = ([(start, before)] if before is not None else []) + [(t, v) for t, v in samples if start <= t < end]
    for i, (t, v) in enumerate(points):
        nxt = points[i + 1][0] if i + 1 < len(points) else end
        if is_on(v):
            total += min((nxt - t).total_seconds(), cap)
    return total


_RUN_SQL = """
WITH prior AS (
    SELECT %(start)s::timestamptz AS ts, value, value_text, value_bool FROM (
        SELECT value, value_text, value_bool FROM point_history
         WHERE point_uri = %(p)s AND ts < %(start)s AND ts >= %(start)s::timestamptz - %(gap)s * interval '1 second'
           AND (quality IS NULL OR quality = 'ok')
         ORDER BY ts DESC LIMIT 1) x
), s AS (
    SELECT ts, value, value_text, value_bool FROM prior
    UNION ALL
    SELECT ts, value, value_text, value_bool FROM point_history
     WHERE point_uri = %(p)s AND ts >= %(start)s AND ts < %(end)s AND (quality IS NULL OR quality = 'ok')
), spans AS (
    SELECT COALESCE(value_bool, value > 0.5, lower(value_text) = ANY(%(on)s)) AS on_,
           LEAST(EXTRACT(EPOCH FROM (LEAD(ts, 1, %(end)s::timestamptz) OVER (ORDER BY ts) - ts)), %(gap)s) AS secs
      FROM s
)
SELECT COALESCE(SUM(secs) FILTER (WHERE on_), 0) FROM spans"""


def run_hours(req: Request, point_uri: str, start: datetime, end: datetime) -> float:
    """Run hours of a status point over [start, end), from every raw sample."""
    if end <= start:
        return 0.0
    hist = req.services.get("history")
    if hist is not None:  # tests: a commissioning.history.InMemoryHistory
        samples = hist.window([point_uri], start, end).get(point_uri, [])
        prior = hist.latest_raw([point_uri], before=start - timedelta(microseconds=1)).get(point_uri)
        before = prior[1] if prior and start - prior[0] <= MAX_GAP else None
        return run_seconds(samples, start, end, before) / 3600.0
    if not req.rows.pg:
        return 0.0
    with req.rows.conn() as conn:
        row = conn.execute(_RUN_SQL, {"p": point_uri, "start": start, "end": end, "gap": MAX_GAP.total_seconds(),
                                      "on": list(ON_TEXT)}).fetchone()
    return float(row[0] or 0.0) / 3600.0


def latest_value(req: Request, point_uri: str) -> tuple[datetime, Any] | None:
    hist = req.services.get("history")
    if hist is not None:
        return hist.latest([point_uri]).get(point_uri)
    if not req.rows.pg:
        return None
    from baler.commissioning.history import PostgresHistory

    with req.rows.conn() as conn:
        return PostgresHistory(conn).latest([point_uri]).get(point_uri)


# ---------------------------------------------------------------- tasks


def resolve_point(ref: str | None, position_uri: str | None) -> str | None:
    """An absolute URI as is; a relative name under the position's key."""
    if not ref:
        return None
    if ":" in ref:
        return ref
    if not position_uri or not position_uri.startswith("urn:equip:"):
        return None
    return f"urn:point:{position_uri[len('urn:equip:'):]}/{ref.strip('/')}"


def _task_fields(body: dict, existing: dict | None = None) -> dict:
    t = dict(existing or {})
    for k, limit in (("title", 300), ("description", 4000), ("run_point", 1000), ("condition_point", 1000),
                     ("condition_unit", 40)):
        if k in body:
            t[k] = text(body, k, limit)
    if "asset_type" in body:
        t["asset_type"] = text(body, "asset_type", 200)
    if "asset_id" in body:
        t["asset_id"] = text(body, "asset_id", 100)
    if "trigger" in body:
        t["trigger"] = body["trigger"]
    for k in ("interval_hours", "condition_value", "duration_hours"):
        if k in body:
            t[k] = number(body, k)
    if "interval_days" in body:
        v = body.get("interval_days")
        if v is not None and (not isinstance(v, int) or isinstance(v, bool)):
            raise ValueError("interval_days is a whole number")
        t["interval_days"] = v
    if "condition_op" in body:
        t["condition_op"] = body["condition_op"]
    if "status" in body:
        if body["status"] not in ("active", "paused"):
            raise ValueError("status is active or paused")
        t["status"] = body["status"]
    if "source" in body:
        src = body["source"]
        if src is not None and not isinstance(src, dict):
            raise ValueError("source is {document_id, page, quote}")
        t["source"] = src
    if not t.get("title"):
        raise ValueError("title is required")
    if bool(t.get("asset_id")) == bool(t.get("asset_type")):
        raise ValueError("give asset_id (one asset) or asset_type (every installed asset of that type), not both")
    trig = t.get("trigger")
    if trig not in TRIGGERS:
        raise ValueError(f"trigger is one of {', '.join(TRIGGERS)}")
    if trig == "calendar" and not (t.get("interval_days") or 0) > 0:
        raise ValueError("a calendar task needs interval_days > 0")
    if trig == "run_hours" and not ((t.get("interval_hours") or 0) > 0 and t.get("run_point")):
        raise ValueError("a run-hours task needs interval_hours > 0 and run_point (the status point)")
    if trig == "condition" and not (t.get("condition_point") and t.get("condition_op") in OPS
                                    and t.get("condition_value") is not None):
        raise ValueError(f"a condition task needs condition_point, condition_op ({', '.join(OPS)}) and condition_value")
    return t


def _get_task(req: Request, task_id: str) -> dict:
    t = req.rows.get("lc_pm_tasks", task_id)
    if t is None or t["org_id"] != req.org_id:
        raise HTTPError(404, "no such task")
    return t


def targets(req: Request, task: dict, assets_by_id: dict[str, dict], open_iv: dict[str, dict]) -> list[dict]:
    """The installed assets a task applies to, each with its interval."""
    if task.get("asset_id"):
        a = assets_by_id.get(task["asset_id"])
        return [a] if a and a["id"] in open_iv else []
    want = (task.get("asset_type") or "").lower()
    return [a for a in assets_by_id.values() if a["id"] in open_iv and not a.get("retired_at")
            and want in ((a.get("asset_type") or "").lower(), (a.get("brick_class") or "").lower())]


# ------------------------------------------------------------------ due


def due_item(req: Request, task: dict, asset: dict, iv: dict, last: dict | None) -> dict:
    now = req.now
    since = as_dt(last["done_at"]) if last else iv["installed_at"]
    item: dict[str, Any] = {
        "task_id": task["id"], "title": task["title"], "trigger": task["trigger"], "asset_id": asset["id"],
        "asset": {k: asset.get(k) for k in ("tag", "asset_type", "manufacturer", "model", "serial")},
        "position_uri": iv["position_uri"], "since": since.isoformat(),
        "since_what": "last completion" if last else "install",
    }
    if task["trigger"] == "calendar":
        due_at = since + timedelta(days=task["interval_days"])
        item.update(due_at=due_at.isoformat(), basis=f"every {task['interval_days']} days")
        item["status"] = ("overdue" if now >= due_at else "due_soon" if now >= due_at - timedelta(days=DUE_SOON_DAYS)
                          else "ok")
    elif task["trigger"] == "run_hours":
        point = resolve_point(task["run_point"], iv["position_uri"])
        if point is None or not req.allows(point):
            item.update(status="unknown", basis=f"run point {task['run_point']!r} doesn't resolve for this position")
            return item
        hours = run_hours(req, point, since, now)
        limit = float(task["interval_hours"])
        item.update(point=point, run_hours=round(hours, 2), interval_hours=limit,
                    remaining_hours=round(max(limit - hours, 0.0), 2), basis=f"every {limit:g} run hours")
        item["status"] = "overdue" if hours >= limit else "due_soon" if hours >= DUE_SOON_FRACTION * limit else "ok"
    else:
        point = resolve_point(task["condition_point"], iv["position_uri"])
        latest = latest_value(req, point) if point and req.allows(point) else None
        op, limit = task["condition_op"], float(task["condition_value"])
        item.update(point=point, basis=f"{point} {op} {limit:g}{(' ' + task['condition_unit']) if task.get('condition_unit') else ''}")
        if latest is None or not isinstance(latest[1], (int, float)) or isinstance(latest[1], bool):
            item.update(status="unknown", value=None)
        else:
            ts, v = latest
            item.update(value=float(v), value_at=ts.isoformat())
            if now - ts > CONDITION_STALE:
                item["status"] = "unknown"
                item["basis"] += " (latest value is over a day old)"
            else:
                item["status"] = "overdue" if OPS[op](float(v), limit) else "ok"
    return item


ORDER = {"overdue": 0, "due_soon": 1, "unknown": 2, "ok": 3}


def due(req: Request, statuses: set[str] | None = None) -> list[dict]:
    tasks = [t for t in req.rows.find("lc_pm_tasks", org_id=req.org_id) if t["status"] == "active"]
    if not tasks:
        return []
    assets_by_id = {a["id"]: a for a in req.rows.find("lc_assets", org_id=req.org_id)}
    open_iv = {iv["asset_id"]: iv for iv in assets.intervals(req.rows.find("lc_asset_events", org_id=req.org_id))
               if iv["removed_at"] is None and req.allows(iv["position_uri"])}
    done = req.rows.find("lc_pm_completions", org_id=req.org_id)
    last: dict[tuple[str, str], dict] = {}
    for d in done:  # oldest first, so the last write wins
        iv = open_iv.get(d["asset_id"])
        if iv is None or as_dt(d["done_at"]) >= iv["installed_at"]:
            last[(d["task_id"], d["asset_id"])] = d
    out = []
    for t in tasks:
        for a in targets(req, t, assets_by_id, open_iv):
            item = due_item(req, t, a, open_iv[a["id"]], last.get((t["id"], a["id"])))
            if not statuses or item["status"] in statuses:
                out.append(item)
    return sorted(out, key=lambda i: (ORDER[i["status"]], i.get("due_at") or "", i["title"]))


# --------------------------------------------------------------- export

CSV_COLUMNS = ("status", "title", "trigger", "basis", "asset_tag", "manufacturer", "model", "serial", "position_uri",
               "since", "due_at", "run_hours", "interval_hours", "value", "task_id", "asset_id")
# Column names of the COBie Job worksheet (NIBS NBIMS-US V3 COBie, Annex A).
# Field names only; see docs-site/pages/pm-schedules.mdx.
COBIE_JOB_COLUMNS = ("Name", "CreatedBy", "CreatedOn", "Category", "Status", "TypeName", "Description", "Duration",
                     "DurationUnit", "Start", "TaskStartUnit", "Frequency", "FrequencyUnit", "ExtSystem", "ExtObject",
                     "ExtIdentifier", "TaskNumber", "Priors", "ResourceNames")


def to_csv(items: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for i in items:
        a = i["asset"]
        row = {**i, "asset_tag": a.get("tag"), "manufacturer": a.get("manufacturer"), "model": a.get("model"),
               "serial": a.get("serial")}
        w.writerow(["" if row.get(c) is None else row[c] for c in CSV_COLUMNS])
    return buf.getvalue().encode("utf-8")


def cobie_jobs(req: Request) -> list[dict]:
    """One COBie Job row per active task (Job is per Type in COBie; a task
    on one asset uses the asset's type, or its tag)."""
    assets_by_id = {a["id"]: a for a in req.rows.find("lc_assets", org_id=req.org_id)}
    out = []
    for n, t in enumerate(req.rows.find("lc_pm_tasks", org_id=req.org_id), start=1):
        if t["status"] != "active":
            continue
        a = assets_by_id.get(t.get("asset_id") or "", {})
        freq, unit = {"calendar": (t.get("interval_days"), "day"), "run_hours": (t.get("interval_hours"), "hour"),
                      "condition": ("", "")}[t["trigger"]]
        desc = t.get("description") or t["title"]
        if t["trigger"] == "run_hours":
            desc += f" (every {t['interval_hours']:g} run hours of {t['run_point']})"
        elif t["trigger"] == "condition":
            desc += f" (when {t['condition_point']} {t['condition_op']} {t['condition_value']:g})"
        out.append({"Name": t["title"], "CreatedBy": t.get("created_by") or "", "CreatedOn": rows_mod.public(t)["created_at"],
                    "Category": "PM", "Status": "Active", "TypeName": t.get("asset_type") or a.get("asset_type") or a.get("tag") or "",
                    "Description": desc, "Duration": t.get("duration_hours") or "",
                    "DurationUnit": "hour" if t.get("duration_hours") else "", "Start": "", "TaskStartUnit": "",
                    "Frequency": freq if freq is not None else "", "FrequencyUnit": unit, "ExtSystem": "Baler",
                    "ExtObject": "lc_pm_tasks", "ExtIdentifier": t["id"], "TaskNumber": str(n), "Priors": "",
                    "ResourceNames": ""})
    return out


# --------------------------------------------------------------- routes


@route("GET", r"/pm/tasks")
def _tasks(req: Request):
    return [rows_mod.public(t) for t in req.rows.find("lc_pm_tasks", org_id=req.org_id)]


@route("POST", r"/pm/tasks")
def _create(req: Request):
    body = req.json()
    t = _task_fields(body, {"status": "active"})
    if t.get("asset_id"):
        assets.asset_detail(req, t["asset_id"])
    row = {**t, "id": rows_mod.new_id(), "org_id": req.org_id, "created_by": req.actor(), "created_at": req.now,
           "updated_at": req.now}
    req.rows.insert("lc_pm_tasks", row)
    req.rows.audit(_uri(row["id"]), "pm.create", req.who, {k: t.get(k) for k in TASK_COLS if k in t})
    return 201, rows_mod.public(row)


@route("PATCH", r"/pm/tasks/([^/]+)")
def _patch(req: Request, task_id: str):
    old = _get_task(req, task_id)
    body = req.json()
    unknown = set(body) - set(TASK_COLS) | ({"id", "org_id", "created_by", "created_at", "updated_at"} & set(body))
    if unknown:
        raise ValueError(f"not editable: {', '.join(sorted(unknown))}")
    new = _task_fields(body, old)
    if new.get("asset_id") and new.get("asset_id") != old.get("asset_id"):
        assets.asset_detail(req, new["asset_id"])
    diff = {k: {"from": old.get(k), "to": new.get(k)} for k in TASK_COLS if old.get(k) != new.get(k)}
    if diff:
        req.rows.update("lc_pm_tasks", task_id, {**{k: new.get(k) for k in diff}, "updated_at": req.now})
        req.rows.audit(_uri(task_id), "pm.edit", req.who, diff)
    return rows_mod.public(_get_task(req, task_id))


@route("POST", r"/pm/tasks/([^/]+)/complete")
def _complete(req: Request, task_id: str):
    t = _get_task(req, task_id)
    body = req.json()
    asset_id = need(body, "asset_id")
    detail = assets.asset_detail(req, asset_id)
    if t.get("asset_id") and t["asset_id"] != asset_id:
        raise ValueError("this task is for another asset")
    done_at = when(body.get("done_at"), "done_at") or req.now
    if done_at > req.now + assets.FUTURE_SLACK:
        raise ValueError("done_at is in the future")
    hours = None
    if t["trigger"] == "run_hours" and detail.get("position_uri"):
        point = resolve_point(t["run_point"], detail["position_uri"])
        installed = as_dt(detail["installed_at"])
        if point and req.allows(point) and installed:
            hours = round(run_hours(req, point, installed, done_at), 2)  # meter reading since install
    row = {"id": rows_mod.new_id(), "org_id": req.org_id, "task_id": task_id, "asset_id": asset_id, "done_at": done_at,
           "run_hours": hours, "note": text(body, "note", 2000), "work_ref": text(body, "work_ref", 200),
           "created_by": req.actor(), "created_at": req.now}
    req.rows.insert("lc_pm_completions", row)
    req.rows.audit(_uri(task_id), "pm.complete", req.who, {"asset_id": asset_id, "done_at": done_at})
    return 201, rows_mod.public(row)


@route("GET", r"/pm/tasks/([^/]+)/completions")
def _completions(req: Request, task_id: str):
    _get_task(req, task_id)
    return [rows_mod.public(d) for d in req.rows.find("lc_pm_completions", org_id=req.org_id, task_id=task_id)]


@route("GET", r"/pm/due")
def _due(req: Request):
    st = req.query.get("status")
    return due(req, set(st.split(",")) if st else None)


@route("GET", r"/pm/due/export")
def _export(req: Request):
    fmt = req.query.get("format") or "csv"
    if fmt == "csv":
        st = req.query.get("status") or "overdue,due_soon"
        return Raw(to_csv(due(req, set(st.split(",")))), "text/csv; charset=utf-8", "pm-due.csv")
    if fmt == "cobie-job":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=COBIE_JOB_COLUMNS)
        w.writeheader()
        w.writerows(cobie_jobs(req))
        return Raw(buf.getvalue().encode("utf-8"), "text/csv; charset=utf-8", "cobie-job.csv")
    raise ValueError("format is csv or cobie-job")

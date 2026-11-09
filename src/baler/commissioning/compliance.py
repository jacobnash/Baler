"""
Continuous compliance: the packs that proved a space at acceptance keep
running in operations (Baler#207, epic #221 §3.3).

On the Cx 6 scheduler, every project with settings.compliance.every_hours
runs its adopted packs' active rows (#205: confirmed by a person, every
value filled) against its spaces (#209) and records one log row per space
and requirement. That is the daily temperature / humidity / pressure log a
surveyor asks for (Joint Commission EC.02.05.01 EP 15 leaves the frequency
to the facility to define and document: `every_hours` is that definition,
and the docs say so).

- An exceedance opens a fault in the shared faults table, through the
  same repo.open_fault and webhook notify path Cx 8 mirroring uses, with
  rule id compliance:<pack_id>:<row_id> and the exceeding point. A later
  passing run resolves it. Alerts go wherever fault webhooks already go.
- The per-space history stays (cx_compliance_log), for surveys:
  GET /cx/spaces/{id}/compliance, JSON or a printable log.
- The latest run feeds the readiness report's `requirements` criterion
  (readiness.REQUIREMENT_SOURCES).

What is judged in this slice: rows that apply to a space type, with the
`procedure` engine's `range` criterion, over the space's own points of the
row's evidence class (subclass-aware). A row is:

  pass          every sample in the window meets the criterion and the
                points reported for at least evidence.coverage of it
  fail          any sample doesn't (each exceedance's first/last time and
                worst value are recorded)
  inconclusive  no point of that class on the space, coverage too low,
                or the row needs something not wired yet (another engine,
                an equipment-class row, ACH from airflow and volume)

Values are compared as stored: the row's unit must be the point's unit
(Baler converts nothing; a pack's unit is part of what a person confirmed).
Raw history is read as it is, never a rollup.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from baler.commissioning import db, packs, procedures

log = logging.getLogger(__name__)

RUNS = "cx_compliance_runs"
LOG = "cx_compliance_log"
MAX_GAP = timedelta(minutes=15)  # a sample vouches for its point this long (coverage)
MAX_EXCEEDANCES = 20

db.ROW_TABLES[RUNS] = (("id", "project_id", "status", "triggered_by", "window_end", "started_at", "finished_at", "summary",
                        "error", "created_at"), frozenset({"summary"}))
db.ROW_TABLES[LOG] = (("id", "run_id", "project_id", "space_id", "pack_ref", "pack_id", "version", "row_id", "title",
                       "citation", "status", "window_start", "window_end", "evidence", "note", "created_at"),
                      frozenset({"citation", "evidence"}))


def validate_settings(cfg) -> dict:
    if cfg is None:
        return {}
    if not isinstance(cfg, dict) or set(cfg) - {"every_hours", "alerts"}:
        raise ValueError("compliance is {every_hours: 1..744, alerts: true|false}")
    every = cfg.get("every_hours")
    if every is not None and (isinstance(every, bool) or not isinstance(every, (int, float)) or not 1 <= every <= 744):
        raise ValueError("compliance.every_hours is 1..744: how often the facility defines its compliance log")
    if "alerts" in cfg and not isinstance(cfg["alerts"], bool):
        raise ValueError("compliance.alerts is true or false")
    return cfg


def meets(v: float, c: dict) -> bool:
    op = c["op"]
    if op == "between":
        return c["min"] <= v <= c["max"]
    target = c["value"]
    return {">=": v >= target, "<=": v <= target, ">": v > target, "<": v < target, "==": v == target}[op]


def point_classes(store, uris: list[str]) -> dict[str, set[str]]:
    """Each point's Brick classes, from the graph."""
    if not uris or store is None:
        return {}
    values = " ".join(f"<{u}>" for u in uris)
    out: dict[str, set[str]] = {u: set() for u in uris}
    for row in store.query(f"SELECT ?p ?c WHERE {{ VALUES ?p {{ {values} }} ?p a ?c }}"):
        c = str(row.c)
        if "#" in c:
            out.setdefault(str(row.p), set()).add(c.rsplit("#", 1)[1])
    return out


def judge(row: dict, space: dict, classes: dict[str, set[str]], series: dict[str, list], start: datetime,
          end: datetime) -> tuple[str, dict, str | None]:
    """(status, evidence, note) for one active row on one space."""
    chk, crit, ev = row["check"], row["criterion"], row["evidence"]
    if chk["engine"] != "procedure" or chk.get("criterion") != "range":
        return "inconclusive", {}, f"the {chk['engine']} engine isn't run continuously yet"
    if crit.get("op") is None or isinstance(crit.get("value"), str):
        return "inconclusive", {}, "this row has no numeric criterion to judge from data"
    if not ev.get("point_class"):
        return "inconclusive", {}, (f"no point class evidences {crit.get('quantity')} directly "
                                    "(air changes need airflow and room volume: not derived yet)")
    fam = procedures._family(ev["point_class"])
    pts = [u for u in space.get("point_uris") or [] if classes.get(u, set()) & fam]
    if not pts:
        return "inconclusive", {"point_class": ev["point_class"]}, f"the space has no {ev['point_class']} point"
    per = []
    status = "pass"
    for u in pts:
        rows = [(t, procedures._value(v)) for t, v in series.get(u, [])]
        vals = [(t, v) for t, v in rows if v is not None]
        cov = procedures.availability_pct([t for t, _ in vals], start, end, MAX_GAP)
        bad = [(t, v) for t, v in vals if not meets(v, crit)]
        item = {"point_uri": u, "samples": len(vals), "coverage_pct": cov,
                "min": min((v for _, v in vals), default=None), "max": max((v for _, v in vals), default=None),
                "exceedances": len(bad)}
        if bad:
            worst = max(bad, key=lambda tv: abs(tv[1] - (crit.get("value") if crit["op"] != "between"
                                                          else (crit["min"] + crit["max"]) / 2)))
            item.update(first_exceedance=bad[0][0].isoformat(), last_exceedance=bad[-1][0].isoformat(),
                        worst={"ts": worst[0].isoformat(), "value": worst[1]})
            status = "fail"
        elif cov < 100.0 * ev["coverage"] and status != "fail":
            status = "inconclusive"
        per.append(item)
    note = None
    if status == "inconclusive":
        note = f"points reported for less than {ev['coverage']:.0%} of the window"
    return status, {"points": per, "criterion": crit, "point_class": ev["point_class"]}, note


def evaluate(repo, project: dict, now: datetime) -> list[dict]:
    """Every adopted pack's active space rows against every active space."""
    from baler.commissioning import packs_api, spaces_api

    phase = next((p["key"] for p in project.get("phases", []) if p.get("status") == "active"), None)
    adopted = packs_api.project_packs(repo, project["id"])
    sps = [s for s in spaces_api.project_spaces(repo, project["id"]) if s.get("space_type")]
    work = []  # (space, adoption, pack, row)
    for _a, pk in adopted:
        for row in packs.active_rows(pk):
            for s in sps:
                if packs.applies(row, space_type=s["space_type"], phase=phase):
                    work.append((s, pk, row))
    if not work:
        return []
    uris = sorted({u for s, _, _ in work for u in s.get("point_uris") or []})
    classes = point_classes(repo.store, uris)
    longest = max(row["evidence"]["window_hours"] for _, _, row in work)
    with repo.history() as h:
        series = h.window(uris, now - timedelta(hours=longest), now + timedelta(seconds=1)) if uris else {}
    out = []
    for s, pk, row in work:
        start = now - timedelta(hours=row["evidence"]["window_hours"])
        win = {u: [(t, v) for t, v in series.get(u, []) if start <= t <= now] for u in s.get("point_uris") or []}
        status, evidence, note = judge(row, s, classes, win, start, now)
        out.append({"space_id": s["id"], "space_name": s["name"], "pack_ref": pk["id"], "pack_id": pk["pack_id"],
                    "version": pk["version"], "edition": pk["source"]["edition"], "row_id": row["id"], "title": row["title"],
                    "citation": {**row["citation"], "standard": pk["source"]["standard"], "edition": pk["source"]["edition"]},
                    "status": status, "window_start": start, "window_end": now, "evidence": evidence, "note": note})
    return out


def rule_id(r: dict) -> str:
    return f"compliance:{r['pack_id']}:{r['row_id']}"


def alert(repo, project: dict, results: list[dict], now: datetime, notify=None) -> dict:
    """Open a fault per exceeding point; resolve it when the row passes."""
    opened = closed = 0
    for r in results:
        rid = rule_id(r)
        for p in r["evidence"].get("points") or []:
            if r["status"] == "fail" and p["exceedances"]:
                detail = {"source": "compliance", "project_id": project["id"], "space": r["space_name"], "space_id": r["space_id"],
                          "requirement": r["title"], "citation": r["citation"], "worst": p.get("worst"),
                          "exceedances": p["exceedances"]}
                started = datetime.fromisoformat(p["first_exceedance"])
                if repo.open_fault(rid, p["point_uri"], started, detail, "high") is not None:
                    opened += 1
                    if notify:
                        notify("fault.opened", rid, p["point_uri"], "open", detail)
            elif r["status"] == "pass" and repo.close_fault(rid, p["point_uri"], now) is not None:
                closed += 1
                if notify:
                    notify("fault.resolved", rid, p["point_uri"], "resolved", {"source": "compliance", "project_id": project["id"]})
    return {"opened": opened, "closed": closed}


def summarize(results: list[dict]) -> dict:
    by = {s: sum(1 for r in results if r["status"] == s) for s in ("pass", "fail", "inconclusive")}
    return {"results": len(results), "by_status": by, "spaces": len({r["space_id"] for r in results})}


def execute(repo, run: dict, now_fn, notify=None) -> None:
    """One compliance run to completion. Never raises."""
    try:
        project = repo.get_project(run["project_id"])
        repo.update_row(RUNS, run["id"], {"status": "running"})
        results = evaluate(repo, project, run["window_end"])
        now = now_fn()
        stored = [{k: v for k, v in r.items() if k not in ("space_name", "edition")} for r in results]
        repo.put_rows(LOG, [{"id": db.new_id(), "run_id": run["id"], "project_id": project["id"], **r, "created_at": now}
                            for r in stored])
        summary = summarize(results)
        if ((project.get("settings") or {}).get("compliance") or {}).get("alerts", True):
            summary["alerts"] = alert(repo, project, results, now, notify)
        repo.update_row(RUNS, run["id"], {"status": "completed", "finished_at": now, "summary": summary})
        repo.add_event(project["id"], "compliance.run", None, None, {"run_id": run["id"], **summary}, now)
    except Exception as exc:
        log.exception("compliance run %s failed", run["id"])
        repo.update_row(RUNS, run["id"], {"status": "failed", "finished_at": now_fn(), "error": str(exc)[:2000]})


def new_run(repo, project_id: str, now: datetime, triggered_by: str | None) -> dict:
    run = {"id": db.new_id(), "project_id": project_id, "status": "queued", "triggered_by": triggered_by, "window_end": now,
           "started_at": now, "finished_at": None, "summary": None, "error": None, "created_at": now}
    repo.put_rows(RUNS, [run])
    return run


def tick(repo, worker, now: datetime) -> list[str]:
    """Queue a run for each project whose compliance log is due."""
    queued = []
    for p in repo.list_projects_with_setting("compliance"):
        every = ((p.get("settings") or {}).get("compliance") or {}).get("every_hours")
        if p.get("archived_at") or not every:
            continue
        runs = repo.find_rows(RUNS, order="started_at", project_id=p["id"])
        if any(r["status"] in ("queued", "running") for r in runs):
            continue
        last = runs[-1]["started_at"] if runs else None
        if isinstance(last, str):
            last = datetime.fromisoformat(last)
        if last is not None and now - last < timedelta(hours=float(every)):
            continue
        run = new_run(repo, p["id"], now, "schedule")
        worker.submit(lambda run=run: execute(repo, run, worker.clock, worker.notify))
        queued.append(run["id"])
    return queued


def latest_for_space(repo, project: dict, space: dict, now: datetime) -> list[dict]:
    """readiness.REQUIREMENT_SOURCES: the latest completed run's rows for this space."""
    runs = [r for r in repo.find_rows(RUNS, order="started_at", project_id=project["id"]) if r["status"] == "completed"]
    if not runs:
        return []
    rows = repo.find_rows(LOG, run_id=runs[-1]["id"], space_id=space["id"])
    return [{"id": r["row_id"], "title": r["title"],
             "citation": f'{r["citation"].get("standard")} {r["citation"].get("edition")}, {r["citation"].get("ref")}',
             "status": r["status"], "evidence": r["evidence"], "pack": f'{r["pack_id"]} {r["version"]}',
             "window_end": db.public_row(r)["window_end"]} for r in rows]


def space_history(repo, space_id: str, since: datetime) -> list[dict]:
    return [db.public_row(r) for r in repo.find_rows(LOG, order="window_end", space_id=space_id)
            if (r["window_end"] if not isinstance(r["window_end"], str) else datetime.fromisoformat(r["window_end"])) >= since]


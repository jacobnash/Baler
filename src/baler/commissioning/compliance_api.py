"""
HTTP routes for continuous compliance (Baler#207), under /cx/. Runs go on
the commissioning worker (202; poll GET /compliance-runs/{id}); the
scheduler queues them by settings.compliance.every_hours.
"""

from __future__ import annotations

import re
from datetime import timedelta
from html import escape

from baler.commissioning import compliance, db, readiness, spaces_api
from baler.commissioning.report import _CSS, _a11y_tables
from baler.commissioning.routes import Route, owned_row, query
from baler.http_handler_base import respond_json

# The latest run answers the readiness report's requirements criterion.
if compliance.latest_for_space not in readiness.REQUIREMENT_SOURCES:
    readiness.REQUIREMENT_SOURCES.append(compliance.latest_for_space)


def _start(h, span, claims, pid) -> None:
    if h._project(pid, claims) is None:
        return
    if any(r["status"] in ("queued", "running") for r in h.REPO.find_rows(compliance.RUNS, project_id=pid)):
        raise ValueError("a compliance run is already queued or running for this project")
    run = compliance.new_run(h.REPO, pid, h.NOW(), claims.get("sub"))
    repo, now_fn, notify = h.REPO, h.NOW, getattr(h.WORKER, "notify", None)
    h.WORKER.submit(lambda: compliance.execute(repo, run, now_fn, notify))
    h._event(pid, "compliance.start", claims, {"run_id": run["id"]})
    respond_json(h, 202, db.public_row(run))


def _list(h, span, claims, pid) -> None:
    if h._project(pid, claims) is not None:
        rows = h.REPO.find_rows(compliance.RUNS, order="started_at", project_id=pid)
        respond_json(h, 200, [db.public_row(r) for r in reversed(rows[-100:])])


def _get(h, span, claims, rid) -> None:
    hit = owned_row(h, compliance.RUNS, rid, claims, "compliance run")
    if hit:
        rows = h.REPO.find_rows(compliance.LOG, order="space_id", run_id=rid)
        respond_json(h, 200, {**db.public_row(hit[0]), "results": [db.public_row(r) for r in rows]})


def _space(h, span, claims, sid) -> None:
    """The space's compliance log over the last `days` (default 30), for a
    survey: JSON or a printable page."""
    hit = owned_row(h, spaces_api.SPACES, sid, claims, "space")
    if not hit:
        return
    space, project = hit
    q = query(h)
    days = int(q.get("days", ["30"])[0])
    if not 1 <= days <= 3660:
        raise ValueError("days is 1..3660")
    fmt = q.get("format", ["json"])[0]
    if fmt not in ("json", "html"):
        raise ValueError("format must be json or html")
    rows = compliance.space_history(h.REPO, sid, h.NOW() - timedelta(days=days))
    every = ((project.get("settings") or {}).get("compliance") or {}).get("every_hours")
    out = {"space": {"id": sid, "name": space["name"], "space_type": space.get("space_type")}, "days": days,
           "every_hours": every, "log": rows}
    if fmt == "json":
        respond_json(h, 200, out)
        return
    trs = "".join(f'<tr><td>{escape(r["window_start"])} to {escape(r["window_end"])}</td><td>{escape(r["title"])}</td>'
                  f'<td>{escape(str(r["citation"].get("standard")))} {escape(str(r["citation"].get("edition")))}, '
                  f'{escape(str(r["citation"].get("ref")))}</td><td>{escape(r["status"])}</td>'
                  f'<td>{escape(_ev(r))}</td></tr>' for r in reversed(rows))
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Compliance log - {escape(space["name"])}</title>
<style>{_CSS}</style></head><body>
<h1>Compliance log: {escape(space["name"])}</h1>
<p class="muted">Project {escape(project["name"])} · last {days} days · the facility's defined frequency:
{escape(f"every {every:g} h" if every else "not scheduled (runs on demand)")}</p>
<p class="muted">Inconclusive means there was no evidence either way; it is never counted as a pass.</p>
<table><caption>One row per requirement per run</caption><tr><th>Window</th><th>Requirement</th><th>Citation</th>
<th>Status</th><th>Evidence</th></tr>{trs}</table>
</body></html>
"""
    h._send_html(_a11y_tables(page).encode("utf-8"))


def _ev(r: dict) -> str:
    pts = (r.get("evidence") or {}).get("points") or []
    if not pts:
        return r.get("note") or ""
    return "; ".join(f'{p["point_uri"].rsplit("/", 1)[-1]}: {p["min"]}..{p["max"]}, {p["exceedances"]} exceedances, '
                     f'{p["coverage_pct"]}% covered' for p in pts)


ROUTES: list[Route] = [
    ("POST", re.compile(r"/projects/([^/]+)/compliance/runs"), "start_compliance_run", _start),
    ("GET", re.compile(r"/projects/([^/]+)/compliance/runs"), "list_compliance_runs", _list),
    ("GET", re.compile(r"/compliance-runs/([^/]+)"), "get_compliance_run", _get),
    ("GET", re.compile(r"/spaces/([^/]+)/compliance"), "space_compliance", _space),
]

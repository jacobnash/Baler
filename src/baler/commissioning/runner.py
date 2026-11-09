"""
Background execution of commissioning runs (#57).

A run over a real building (93 equipment, 618 points) took 55 s inside
the request, against a 60 s gateway timeout. So POST .../runs only
records a queued run and hands it here; a small pool of worker threads
inside commissioning_api executes it, and clients poll GET /runs/{id}.

The queue is in memory. A run still queued or running when the process
stops can never finish, so commissioning_api marks those failed at
startup (repo.fail_interrupted_runs) rather than leaving them `running`
for ever - and the one-active-run-per-project rule would otherwise
block that project until someone edited the database.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from baler.commissioning import (
    db,
    engine,
    gates,
    inventory,
    issues,
    manual,
    mirror,
    phases,
    procedures,
)
from baler.scope import Scope

log = logging.getLogger(__name__)

WORKERS = 2  # runs are I/O-bound (SPARQL, Postgres); two keeps one project from starving the rest
PROGRESS_EVERY_S = 2.0  # at most one progress write per run this often
MAX_ERROR = 2000


@dataclass
class RunJob:
    run_id: str
    project_id: str
    phase: str | None
    scopes: list[Scope]
    now: datetime  # the window ends here
    window_hours: float
    user_sub: str | None = None
    user_email: str | None = None
    engine_kwargs: dict = field(default_factory=dict)


def error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR]


def execute(repo, job: RunJob, clock: Callable[[], datetime], run_fn=engine.run, notify=None) -> None:
    """Run one job to completion or failure. Never raises: every outcome
    lands on the run row and as a cx_events row."""
    last = [0.0]

    def progress(p: dict) -> None:
        t = time.monotonic()
        if t - last[0] >= PROGRESS_EVERY_S:
            last[0] = t
            try:
                repo.set_run_progress(job.run_id, p)
            except Exception:  # progress is best effort; the run itself carries on
                log.warning("cx run %s: progress write failed", job.run_id, exc_info=True)

    try:
        repo.set_run_status(job.run_id, "running", {"stage": "starting"})
        with repo.history() as history:
            summary, results = run_fn(repo.store, history, job.scopes, job.now, job.window_hours,
                                      progress=progress, **_with_source(repo, job.engine_kwargs))
        summary["phase"] = job.phase
        summary["window_hours"] = job.window_hours
        repo.complete_run(job.run_id, clock(), summary, results)
        detail = {"run_id": job.run_id, "phase": job.phase, "by_status": summary["by_status"]}
        repo.add_event(job.project_id, "run.complete", job.user_sub, job.user_email, detail, clock())
        try:
            reconcile_issues(repo, job.project_id, job.run_id, results, clock(),
                             judged_from=job.now - timedelta(hours=job.window_hours))
        except Exception:  # the run itself stands; the next run reconciles again
            log.exception("cx run %s: issue reconcile failed", job.run_id)
        try:
            project = repo.get_project(job.project_id)
            if project and ((project.get("settings") or {}).get("fault_mirroring") or {}).get("enabled"):
                got = mirror.apply(repo, project, job.run_id, results, clock(), notify)
                if got["opened"] or got["closed"]:
                    repo.add_event(job.project_id, "faults.mirror", None, None, {"run_id": job.run_id, **got}, clock())
        except Exception:
            log.exception("cx run %s: fault mirroring failed", job.run_id)
    except Exception as exc:  # noqa: BLE001 - a failing run is recorded on its row, never fatal to the worker
        log.error("cx run %s failed:\n%s", job.run_id, traceback.format_exc())
        try:
            repo.fail_run(job.run_id, clock(), error_text(exc))
            repo.add_event(job.project_id, "run.fail", job.user_sub, job.user_email,
                           {"run_id": job.run_id, "error": error_text(exc)}, clock())
        except Exception:
            # The DB is the likely culprit; the next startup marks it failed.
            log.exception("cx run %s: could not record the failure", job.run_id)


def reconcile_issues(repo, project_id: str, run_id: str | None, results: list[dict], now: datetime,
                     judged_from: datetime | None = None, kind: str = "run") -> list[tuple]:
    """Apply a completed run or test run to the issues log (Cx 4, #86):
    passing evidence resolves (ready_for_retest) and records retests,
    failures reopen failed retests, overdue risks expire. Nothing here
    closes an issue - a person does. With no run (run_id None, no results)
    it only expires risks - the issues API does that on read, so an
    overdue risk reopens without waiting for a run."""
    active = repo.list_issues(project_id, issues.ACTIVE)
    if not active:
        return []
    risks = {r["issue_id"]: r for r in repo.list_risks(project_id)}
    changes = issues.reconcile(active, risks, results, run_id or "", now, now.date(), judged_from, kind)
    for issue_id, change, action, detail in changes:
        repo.update_issue(issue_id, change)
        if action == "risk.expired":
            repo.revoke_risk(detail["risk_id"], now, None)
        if action != "still.failing":
            repo.add_issue_event(issue_id, action, None, None, detail, now)
    resolved = [c[0] for c in changes if c[2] == "resolve.evidence"]
    retested = [c[0] for c in changes if c[2] == "retest.passed"]
    reopened = [c[0] for c in changes if c[2] in ("retest.failed", "risk.expired")]
    if resolved or retested or reopened:
        repo.add_event(project_id, "issues.reconcile", None, None,
                       {("test_run_id" if kind == "test_run" else "run_id"): run_id, "resolved": resolved,
                        "retested": retested, "reopened": reopened}, now)
    return changes


def phase_gate(repo, project: dict, key: str, now: datetime) -> dict | None:
    """The gate for moving `project` into phase `key` (#86), or None when
    the move isn't gated (backwards, sideways into an ungated phase, or
    phase_gates "off"). Shared by POST .../phase and the agent's accepted
    phase proposals, so neither path can skip it."""
    if gates.mode(project.get("settings")) == "off":
        return None
    by_key = {ph["key"]: ph for ph in project["phases"]}
    cur = by_key.get(phases.current(project["phases"]) or "")
    if key not in by_key or not gates.rules_for(key) or (cur and by_key[key]["position"] <= cur["position"]):
        return None
    pid = project["id"]
    run = next((r for r in repo.list_runs(pid, 20) if r.get("status") == "completed"), None)
    results = repo.get_results(run["id"]) if run else []
    procs = repo.list_procedures(pid)
    latest: dict[str, dict | None] = {}
    for pr in procs:
        got = next((t for t in repo.list_test_runs(pr["id"], 20) if t["status"] == "completed"), None)
        latest[pr["id"]] = refresh_test_run(repo, got, now) if got else None
    reconcile_issues(repo, pid, None, [], now)  # an overdue waiver lapses first
    active = repo.list_issues(pid, issues.ACTIVE)
    risks = {r["issue_id"]: r for r in repo.list_risks(pid)}
    return gates.evaluate(key, run, results, procs, latest, active, risks)


def project_scopes(repo, project: dict, claims: dict) -> tuple[list[Scope], list[str]]:
    """The caller's own scope, plus the project site's topic prefixes, plus
    the project's own `settings.prefixes` (#75: a building's project sees
    only that building, without an admin creating a site for it). Each one
    only narrows: the caller's scope always applies on top."""
    scopes = [repo.scope_for(claims)]
    prefixes: list[str] = []
    if project.get("site_id"):
        site = repo.get_site(project["site_id"])
        prefixes = list((site or {}).get("topic_prefixes") or [])
        scopes.append(inventory.site_scope(prefixes))
    own = list(((project.get("settings") or {}).get("prefixes")) or [])
    if own:
        scopes.append(inventory.site_scope(own))
        prefixes = prefixes + [p for p in own if p not in prefixes]
    return scopes, prefixes


def scope_json(s: Scope) -> dict:
    return {"prefixes": None if s.prefixes is None else list(s.prefixes), "exclude": list(s.exclude)}


def engine_kwargs(settings: dict | None) -> dict:
    """The per-project knobs a run takes from settings."""
    s = settings or {}
    out = {}
    if s.get("gl36"):
        out["gl36_settings"] = s["gl36"]
    if s.get("ladder"):
        out["ladder_config"] = s["ladder"]
    if s.get("economizer"):
        out["economizer_settings"] = s["economizer"]
    if (s.get("overrides") or {}).get("enabled"):
        out["override_settings"] = s["overrides"]
    return out


def _with_source(repo, kwargs: dict) -> dict:
    """The override tests read schedules, priority arrays and Baler's own
    writes from the repo (Baler#212); nothing else needs it."""
    return {**kwargs, "override_source": repo} if kwargs.get("override_settings") else kwargs


def queue_run(repo, worker, project: dict, claims: dict, now: datetime, window_hours: float,
              user_sub: str | None, user_email: str | None) -> dict:
    """Record a queued verification run and hand it to the worker.
    RunActive if the project already has one in flight."""
    scopes, prefixes = project_scopes(repo, project, claims)
    phase = phases.current(project["phases"])
    run = {
        "id": db.new_id(), "project_id": project["id"], "phase": phase, "status": "queued", "started_at": now,
        "finished_at": None, "window_start": now - timedelta(hours=window_hours), "window_end": now,
        "triggered_by": user_sub, "scope": {"caller": scope_json(scopes[0]), "site_prefixes": prefixes},
        "summary": None, "progress": None, "error": None,
    }
    repo.create_run(run)
    repo.add_event(project["id"], "run.create", user_sub, user_email,
                   {"run_id": run["id"], "phase": phase, "window_hours": window_hours}, now)
    worker.submit(RunJob(run["id"], project["id"], phase, scopes, now, window_hours, user_sub, user_email,
                         engine_kwargs(project.get("settings"))))
    return run


# ------------------------------------------------------------ test runs (Cx 6)


@dataclass
class TestJob:
    run_id: str
    project_id: str
    procedure: dict
    scopes: list[Scope]
    start: datetime
    end: datetime
    user_sub: str | None = None
    user_email: str | None = None
    engine_kwargs: dict = field(default_factory=dict)


def queue_test_run(repo, worker, project: dict, proc: dict, claims: dict, start: datetime, end: datetime,
                   now: datetime, user_sub: str | None, user_email: str | None, methods: dict | None = None) -> dict:
    """`methods` picks a method per step for this run (Baler#275);
    procedures.choose_methods validates it and fills in the defaults."""
    scopes, prefixes = project_scopes(repo, project, claims)
    snapshot = {k: proc[k] for k in ("id", "name", "kind", "equip_class", "equip_uris", "steps", "window_hours")}
    snapshot["methods"] = procedures.choose_methods(proc, methods)
    tr = {"id": db.new_id(), "project_id": project["id"], "procedure_id": proc["id"], "phase": phases.current(project["phases"]),
          "status": "queued", "started_at": start, "window_end": end, "requested_at": now, "finished_at": None,
          "triggered_by": user_sub, "scope": {"caller": scope_json(scopes[0]), "site_prefixes": prefixes},
          "procedure": snapshot, "equipment": None, "summary": None, "previous_id": None, "diff": None, "error": None}
    repo.create_test_run(tr)
    repo.add_event(project["id"], "test_run.create", user_sub, user_email,
                   {"test_run_id": tr["id"], "procedure_id": proc["id"], "procedure": proc["name"], "started_at": start.isoformat()}, now)
    worker.submit(TestJob(tr["id"], project["id"], snapshot, scopes, start, end, user_sub, user_email,
                          engine_kwargs(project.get("settings"))))
    return tr


def _range_points(proc: dict, equips: list[dict]) -> list[str]:
    out: set[str] = set()
    for st in proc["steps"]:
        for c in st["criteria"]:
            out |= procedures.series_points(c, equips)
    return sorted(out)


def execute_test(repo, job: TestJob, clock: Callable[[], datetime], run_fn=engine.run) -> None:
    """One passive test run to completion or failure. Never raises."""
    try:
        repo.update_test_run(job.run_id, {"status": "running"})
        with repo.history() as h:
            hist = procedures.ClippedHistory(h, job.end)
            site = inventory.build(repo.store, job.scopes)
            equips = [e for e in site if procedures.applies(job.procedure, e)]
            hours = max((job.end - job.start).total_seconds() / 3600, 1 / 60)
            _, results = run_fn(repo.store, hist, job.scopes, job.end, hours, equips=equips, site_equips=site,
                                **_with_source(repo, job.engine_kwargs))
            ids = _range_points(job.procedure, site)
            truncated: set[str] = set()
            series = hist.window(ids, job.start, job.end + timedelta(seconds=1), truncated=truncated) if ids else {}
        per = procedures.evaluate(job.procedure, equips, results, series, truncated, (job.start, job.end))
        per = manual.apply(per, job.procedure, job.procedure.get("methods") or procedures.choose_methods(job.procedure),
                           repo.find_rows(manual.RESULTS, order="recorded_at", test_run_id=job.run_id),
                           repo.find_rows("cx_active_runs", project_id=job.project_id), job.start)
        prev = next((r for r in repo.list_test_runs(job.procedure["id"], 20)
                     if r["id"] != job.run_id and r["status"] == "completed"), None)
        summary = procedures.summarize(per)
        summary["window_hours"] = round(hours, 3)
        summary["partial"] = job.end > clock()
        delta = procedures.diff(prev["equipment"] if prev else None, per)
        repo.update_test_run(job.run_id, {"status": "completed", "finished_at": clock(), "equipment": per, "summary": summary,
                                          "previous_id": prev["id"] if prev else None, "diff": delta})
        repo.add_event(job.project_id, "test_run.complete", job.user_sub, job.user_email,
                       {"test_run_id": job.run_id, "procedure": job.procedure["name"], "by_status": summary["by_status"],
                        "diff": delta["counts"]}, clock())
        if not summary["partial"]:  # a window still open can't retest anything yet
            try:
                reconcile_issues(repo, job.project_id, job.run_id, results, clock(), judged_from=job.start, kind="test_run")
            except Exception:  # the test run stands; the next run reconciles again
                log.exception("cx test run %s: issue reconcile failed", job.run_id)
        try:
            method_issues(repo, repo.get_test_run(job.run_id) or {}, clock())
        except Exception:
            log.exception("cx test run %s: method issues failed", job.run_id)
    except Exception as exc:  # noqa: BLE001 - recorded on the row, never fatal to the worker
        log.error("cx test run %s failed:\n%s", job.run_id, traceback.format_exc())
        try:
            repo.update_test_run(job.run_id, {"status": "failed", "finished_at": clock(), "error": error_text(exc)})
            repo.add_event(job.project_id, "test_run.fail", job.user_sub, job.user_email,
                           {"test_run_id": job.run_id, "error": error_text(exc)}, clock())
        except Exception:
            log.exception("cx test run %s: could not record the failure", job.run_id)


def refresh_test_run(repo, tr: dict, now: datetime) -> dict:
    """Re-apply a completed test run's methods with the manual results and
    active runs recorded since (Baler#275): its statuses, summary and
    issues follow a person's entry, or an active test finishing, without
    re-reading history. Returns the run as stored."""
    if not tr or tr.get("status") != "completed" or not tr.get("equipment"):
        return tr
    proc = tr.get("procedure") or {}
    methods = proc.get("methods") or procedures.choose_methods(proc)
    if set(methods.values()) == {"passive"} and not repo.find_rows(manual.RESULTS, order="recorded_at", test_run_id=tr["id"]):
        return tr  # nothing but history behind it: unchanged since it ran
    start = tr["started_at"] if isinstance(tr["started_at"], datetime) else datetime.fromisoformat(str(tr["started_at"]))
    per = manual.apply(tr["equipment"], proc, methods, repo.find_rows(manual.RESULTS, order="recorded_at", test_run_id=tr["id"]),
                       repo.find_rows("cx_active_runs", project_id=tr["project_id"]), start)
    summary = {**(tr.get("summary") or {}), **procedures.summarize(per)}
    if per != tr.get("equipment") or summary != tr.get("summary"):
        repo.update_test_run(tr["id"], {"equipment": per, "summary": summary})
        tr = {**tr, "equipment": per, "summary": summary}
    method_issues(repo, tr, now)
    return repo.get_test_run(tr["id"]) or tr


def method_issues(repo, tr: dict, now: datetime) -> list[tuple]:
    """Open an issue for each manual or active fail and each contradiction
    the test run carries, and reconcile the rest (a later pass resolves,
    a pass after the fix is the retest). Only results whose verdict
    changed since the last time are applied, so re-reading a run is
    idempotent."""
    if not tr or not tr.get("equipment"):
        return []
    proc_id = tr["procedure_id"]
    results = manual.issue_results(tr["equipment"], proc_id)
    seen = dict((tr.get("summary") or {}).get("issue_signatures") or {})
    fresh = []
    for r in results:
        sig = f"{r['status']}|{r['evidence'].get('manual_result_id') or r['evidence'].get('active_run_id')}"
        key = f"{r['check_id']}|{r['subject']}"
        if seen.get(key) != sig:
            fresh.append(r)
            seen[key] = sig
    if not fresh:
        return []
    for r in fresh:
        if r["status"] != "fail":
            continue
        try:
            issue = issues.from_result(r, tr["id"], tr["project_id"], db.new_id, now, None)
            issue["title"] = issues.title_for(r)
            repo.create_issue(issue)
            repo.add_issue_event(issue["id"], "open.method", None, None,
                                 {"test_run_id": tr["id"], "check_id": r["check_id"], "evidence": r["evidence"]}, now)
        except db.IssueExists:
            pass
    changes = reconcile_issues(repo, tr["project_id"], tr["id"], fresh, now, judged_from=None, kind="test_run")
    repo.update_test_run(tr["id"], {"summary": {**(tr.get("summary") or {}), "issue_signatures": seen}})
    return changes


# ------------------------------------------------------- spec extraction (Cx 7)


@dataclass
class ExtractJob:
    document_id: str
    project_id: str
    filename: str
    data: bytes  # in memory only, never written anywhere
    user_sub: str | None = None
    user_email: str | None = None


def execute_extract(repo, job: ExtractJob, clock: Callable[[], datetime], extract_fn=None) -> None:
    """PDF -> proposed spec items. Never raises."""
    try:
        repo.update_row("cx_spec_documents", job.document_id, {"status": "running"})
        if extract_fn is None:
            from baler.commissioning import spec_llm

            extract_fn = spec_llm.extract_pdf
        items, info = extract_fn(job.data, job.filename)
        now = clock()
        rows = [{"id": db.new_id(), "project_id": job.project_id, "document_id": job.document_id, "status": "proposed",
                 "created_by": "llm", "created_at": now, "reviewed_by": None, "reviewed_at": None, **it} for it in items]
        repo.put_rows("cx_spec_items", rows)
        repo.update_row("cx_spec_documents", job.document_id,
                        {"status": "completed", "items": len(rows), "info": info, "finished_at": now})
        repo.add_event(job.project_id, "spec.extract", job.user_sub, job.user_email,
                       {"document_id": job.document_id, "filename": job.filename, "proposed": len(rows), "model": info.get("model")}, now)
    except Exception as exc:  # noqa: BLE001 - recorded on the row
        log.error("cx extraction %s failed:\n%s", job.document_id, traceback.format_exc())
        try:
            repo.update_row("cx_spec_documents", job.document_id, {"status": "failed", "error": error_text(exc), "finished_at": clock()})
            repo.add_event(job.project_id, "spec.extract_fail", job.user_sub, job.user_email,
                           {"document_id": job.document_id, "error": error_text(exc)}, clock())
        except Exception:
            log.exception("cx extraction %s: could not record the failure", job.document_id)
    finally:
        job.data = b""


class RunWorker:
    def __init__(self, repo, clock: Callable[[], datetime], workers: int = WORKERS, run_fn=engine.run):
        self.repo = repo
        self.clock = clock
        self.run_fn = run_fn
        self._queue: queue.Queue = queue.Queue()
        self.extract_fn = None  # tests swap the Claude call
        self.notify = None  # fault webhooks for mirrored faults (commissioning_api sets fault_detector's)
        for i in range(workers):
            threading.Thread(target=self._loop, name=f"cx-run-{i}", daemon=True).start()

    def submit(self, job) -> None:
        """A RunJob, TestJob or ExtractJob, or any callable (run as is)."""
        self._queue.put(job)

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if callable(job):  # e.g. an agent turn (Cx 10)
                    job()
                elif isinstance(job, TestJob):
                    execute_test(self.repo, job, self.clock, self.run_fn)
                elif isinstance(job, ExtractJob):
                    execute_extract(self.repo, job, self.clock, self.extract_fn)
                else:
                    execute(self.repo, job, self.clock, self.run_fn, self.notify)
            finally:
                self._queue.task_done()

    def join(self) -> None:
        """Block until everything submitted so far has finished (tests)."""
        self._queue.join()


# ------------------------------------------------------------ scheduling


def due(last: datetime | None, every_hours: float, now: datetime) -> bool:
    return last is None or now - last >= timedelta(hours=every_hours)


def schedule_tick(repo, worker, now: datetime) -> list[str]:
    """Queue whatever is due: a procedure with schedule.every_hours, and a
    project with settings.schedule.every_hours (a verification run). Runs
    as the project's org (no user), on the project's site. Returns what
    was queued (ids). Never raises for one bad project."""
    queued: list[str] = []
    projects: dict[str, dict | None] = {}

    def project(pid: str) -> dict | None:
        if pid not in projects:
            projects[pid] = repo.get_project(pid)
        return projects[pid]

    for proc in repo.list_procedures(None):
        sched = proc.get("schedule") or {}
        p = project(proc["project_id"])
        if proc["status"] != "active" or not sched.get("every_hours") or p is None or p.get("archived_at"):
            continue
        runs = repo.list_test_runs(proc["id"], 1)
        last = runs[0]["requested_at"] if runs else None
        if isinstance(last, str):
            last = datetime.fromisoformat(last)
        if not due(last, float(sched["every_hours"]), now):
            continue
        try:
            tr = queue_test_run(repo, worker, p, proc, {"org_id": p["org_id"], "site_ids": []},
                                now - timedelta(hours=float(proc["window_hours"])), now, now, "schedule", None)
            queued.append(tr["id"])
        except db.RunActive:
            continue
        except Exception:
            log.exception("cx schedule: procedure %s", proc["id"])

    for p in _scheduled_projects(repo):
        sched = (p.get("settings") or {}).get("schedule") or {}
        runs = repo.list_runs(p["id"], 1)
        last = datetime.fromisoformat(runs[0]["started_at"]) if runs else None
        if not due(last, float(sched["every_hours"]), now):
            continue
        try:
            wh = engine.window_hours((p.get("settings") or {}).get("window_hours"))
            run = queue_run(repo, worker, p, {"org_id": p["org_id"], "site_ids": []}, now, wh, "schedule", None)
            queued.append(run["id"])
        except db.RunActive:
            continue
        except Exception:
            log.exception("cx schedule: project %s", p["id"])
    return queued


def _scheduled_projects(repo) -> list[dict]:
    lister = getattr(repo, "list_scheduled_projects", None)
    rows = lister() if lister else []
    return [p for p in rows if not p.get("archived_at") and ((p.get("settings") or {}).get("schedule") or {}).get("every_hours")]


class Scheduler:
    """A daemon thread in commissioning_api: once a minute, schedule_tick."""

    def __init__(self, repo, worker, clock: Callable[[], datetime], every_s: float = 60.0,
                 extra: list[Callable[[datetime], object]] | None = None):
        self.repo, self.worker, self.clock, self.every_s = repo, worker, clock, every_s
        self.extra = extra or []  # e.g. the active-test sweeper (Cx 9)
        self._stop = threading.Event()
        threading.Thread(target=self._loop, name="cx-schedule", daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.wait(self.every_s):
            try:
                schedule_tick(self.repo, self.worker, self.clock())
            except Exception:
                log.exception("cx schedule tick failed")

    def stop(self) -> None:
        self._stop.set()

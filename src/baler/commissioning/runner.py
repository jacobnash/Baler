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
from datetime import datetime

from baler.commissioning import engine, issues
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


def execute(repo, job: RunJob, clock: Callable[[], datetime], run_fn=engine.run) -> None:
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
                                      progress=progress, **job.engine_kwargs)
        summary["phase"] = job.phase
        summary["window_hours"] = job.window_hours
        repo.complete_run(job.run_id, clock(), summary, results)
        detail = {"run_id": job.run_id, "phase": job.phase, "by_status": summary["by_status"]}
        repo.add_event(job.project_id, "run.complete", job.user_sub, job.user_email, detail, clock())
        try:
            reconcile_issues(repo, job.project_id, job.run_id, results, clock())
        except Exception:  # the run itself stands; the next run reconciles again
            log.exception("cx run %s: issue reconcile failed", job.run_id)
    except Exception as exc:  # noqa: BLE001 - a failing run is recorded on its row, never fatal to the worker
        log.error("cx run %s failed:\n%s", job.run_id, traceback.format_exc())
        try:
            repo.fail_run(job.run_id, clock(), error_text(exc))
            repo.add_event(job.project_id, "run.fail", job.user_sub, job.user_email,
                           {"run_id": job.run_id, "error": error_text(exc)}, clock())
        except Exception:
            # The DB is the likely culprit; the next startup marks it failed.
            log.exception("cx run %s: could not record the failure", job.run_id)


def reconcile_issues(repo, project_id: str, run_id: str | None, results: list[dict], now: datetime) -> list[tuple]:
    """Apply a completed run to the issues log (Cx 4): close on passing
    evidence, reopen failed retests, expire overdue risks. With no run
    (run_id None, no results) it only expires risks - the issues API
    does that on read, so an overdue risk reopens without waiting for a run."""
    active = repo.list_issues(project_id, issues.ACTIVE)
    if not active:
        return []
    risks = {r["issue_id"]: r for r in repo.list_risks(project_id)}
    changes = issues.reconcile(active, risks, results, run_id or "", now, now.date())
    for issue_id, change, action, detail in changes:
        repo.update_issue(issue_id, change)
        if action == "risk.expired":
            repo.revoke_risk(detail["risk_id"], now, None)
        if action != "still.failing":
            repo.add_issue_event(issue_id, action, None, None, detail, now)
    closed = [c[0] for c in changes if c[2] == "close.evidence"]
    reopened = [c[0] for c in changes if c[2] in ("retest.failed", "risk.expired")]
    if closed or reopened:
        repo.add_event(project_id, "issues.reconcile", None, None,
                       {"run_id": run_id, "closed": closed, "reopened": reopened}, now)
    return changes


class RunWorker:
    def __init__(self, repo, clock: Callable[[], datetime], workers: int = WORKERS, run_fn=engine.run):
        self.repo = repo
        self.clock = clock
        self.run_fn = run_fn
        self._queue: queue.Queue[RunJob] = queue.Queue()
        for i in range(workers):
            threading.Thread(target=self._loop, name=f"cx-run-{i}", daemon=True).start()

    def submit(self, job: RunJob) -> None:
        self._queue.put(job)

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            try:
                execute(self.repo, job, self.clock, self.run_fn)
            finally:
                self._queue.task_done()

    def join(self) -> None:
        """Block until everything submitted so far has finished (tests)."""
        self._queue.join()

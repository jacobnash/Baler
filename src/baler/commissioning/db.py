"""
Commissioning persistence: cx_* tables in the shared Postgres, plus a
MemoryRepo with the same surface so the API and engine are testable with
no services (the pattern PR #5's openapi test used).

Tenancy is a column, not a convention: every project row carries its
org_id (and optionally a site_id), and the API checks the caller's claims
against it before anything else. Check definitions are never in a
global JSON file the way rules.json is - that file is shared by every org.
"""

from __future__ import annotations

import contextlib
import copy
import json
import threading
from datetime import datetime

import psycopg

from baler import auth, ddl, json_store
from baler import scope as scope_mod
from baler.commissioning.history import InMemoryHistory, PostgresHistory
from baler.scope import Scope

SCHEMA = """
CREATE TABLE IF NOT EXISTS cx_projects (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL REFERENCES orgs(id),
    site_id     TEXT REFERENCES sites(id),   -- NULL: org-wide project
    name        TEXT NOT NULL,
    settings    JSONB NOT NULL DEFAULT '{}',
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL,
    archived_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS cx_projects_org ON cx_projects (org_id);

CREATE TABLE IF NOT EXISTS cx_phases (
    project_id   TEXT NOT NULL REFERENCES cx_projects(id),
    key          TEXT NOT NULL,
    name         TEXT NOT NULL,
    position     INT NOT NULL,
    status       TEXT NOT NULL,             -- pending | active | complete | skipped
    started_at   TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (project_id, key)
);

-- Every human and agent action on a project: the audit trail, same shape
-- as record_edits.
CREATE TABLE IF NOT EXISTS cx_events (
    id          BIGSERIAL PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES cx_projects(id),
    ts          TIMESTAMPTZ NOT NULL,
    action      TEXT NOT NULL,
    user_sub    TEXT,
    user_email  TEXT,
    detail      JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS cx_events_project ON cx_events (project_id, ts DESC);

CREATE TABLE IF NOT EXISTS cx_runs (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES cx_projects(id),
    phase        TEXT,
    status       TEXT NOT NULL DEFAULT 'queued',  -- queued | running | completed | failed
    started_at   TIMESTAMPTZ NOT NULL,      -- when it was asked for; the window ends here
    finished_at  TIMESTAMPTZ,               -- NULL until completed or failed
    window_start TIMESTAMPTZ NOT NULL,
    window_end   TIMESTAMPTZ NOT NULL,
    triggered_by TEXT,
    scope        JSONB NOT NULL,            -- what was in scope, so a run is reproducible
    summary      JSONB,                     -- NULL until completed
    progress     JSONB,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS cx_runs_project ON cx_runs (project_id, started_at DESC);
-- Runs became asynchronous (#57). Rows from before then were only ever
-- written finished, so they backfill as completed.
ALTER TABLE cx_runs ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'completed';
ALTER TABLE cx_runs ALTER COLUMN status SET DEFAULT 'queued';
ALTER TABLE cx_runs ADD COLUMN IF NOT EXISTS progress JSONB;
ALTER TABLE cx_runs ADD COLUMN IF NOT EXISTS error TEXT;
ALTER TABLE cx_runs ALTER COLUMN finished_at DROP NOT NULL;
ALTER TABLE cx_runs ALTER COLUMN summary DROP NOT NULL;
-- One run in flight per project.
CREATE UNIQUE INDEX IF NOT EXISTS cx_runs_one_active ON cx_runs (project_id) WHERE status IN ('queued', 'running');

-- Immutable once written: later slices (issues, re-tests) cite results as evidence.
CREATE TABLE IF NOT EXISTS cx_results (
    run_id      TEXT NOT NULL REFERENCES cx_runs(id),
    equip_uri   TEXT NOT NULL,
    check_id    TEXT NOT NULL,
    subject     TEXT NOT NULL,
    status      TEXT NOT NULL,              -- pass | fail | inconclusive | not_applicable
    severity    TEXT,
    symptom     TEXT,
    candidates  JSONB NOT NULL DEFAULT '[]',
    evidence    JSONB NOT NULL,
    PRIMARY KEY (run_id, check_id, subject)
);
CREATE INDEX IF NOT EXISTS cx_results_equip ON cx_results (run_id, equip_uri);
"""


RUN_STATUSES = ("queued", "running", "completed", "failed")
ACTIVE = ("queued", "running")
INTERRUPTED = "interrupted: the commissioning API stopped before this run finished"


class RunActive(Exception):
    """The project already has a queued or running run."""

    def __init__(self, run_id: str | None):
        super().__init__("this project already has a run in progress")
        self.run_id = run_id


def ensure_schema(conn) -> None:
    auth.ensure_schema(conn)  # orgs/sites (+ sites.topic_prefixes) first: cx_projects references them
    ddl.run(conn, SCHEMA)


def new_id() -> str:
    return json_store.new_id()


def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


def _public_phase(p: dict) -> dict:
    return {**p, "started_at": _iso(p.get("started_at")), "completed_at": _iso(p.get("completed_at"))}


def public_project(p: dict) -> dict:
    phases = sorted(p.get("phases", []), key=lambda x: x["position"])
    current = next((x["key"] for x in phases if x["status"] == "active"), None)
    return {
        "id": p["id"], "org_id": p["org_id"], "site_id": p.get("site_id"), "name": p["name"],
        "settings": p.get("settings") or {}, "created_by": p.get("created_by"),
        "created_at": _iso(p["created_at"]), "updated_at": _iso(p["updated_at"]), "archived_at": _iso(p.get("archived_at")),
        # `phase` is what POST /projects takes, so it's what clients read back
        # (#57 found it null: only current_phase was returned).
        "phase": current,
        "current_phase": current,
        "phases": [_public_phase(x) for x in phases],
    }


def public_run(r: dict) -> dict:
    return {k: _iso(v) for k, v in r.items()}


# --------------------------------------------------------------- memory


class MemoryRepo:
    """In-process twin of PgRepo for tests and service-free runs."""

    def __init__(self, store=None, history: InMemoryHistory | None = None, sites: dict | None = None,
                 scope_fn=None, users: dict | None = None):
        self.store = store
        self._history = history or InMemoryHistory()
        self.sites = sites or {}  # id -> {id, org_id, name, topic_prefixes}
        self.users = users or {}  # sub -> email
        self._scope_fn = scope_fn or (lambda claims: Scope.everything())
        self.projects: dict[str, dict] = {}
        self.events: list[dict] = []
        self.runs: dict[str, dict] = {}
        self.results: dict[str, list[dict]] = {}
        self._lock = threading.Lock()  # runs are written from the worker thread

    def scope_for(self, claims) -> Scope:
        return self._scope_fn(claims)

    def get_site(self, site_id: str) -> dict | None:
        return self.sites.get(site_id)

    def user_email(self, sub: str | None) -> str | None:
        return self.users.get(sub) if sub else None

    @contextlib.contextmanager
    def history(self):
        yield self._history

    def create_project(self, project: dict) -> None:
        self.projects[project["id"]] = copy.deepcopy(project)

    def get_project(self, project_id: str) -> dict | None:
        p = self.projects.get(project_id)
        return None if p is None else copy.deepcopy(p)

    def list_projects(self, org_id: str, include_archived: bool = False) -> list[dict]:
        rows = [copy.deepcopy(p) for p in self.projects.values()
                if p["org_id"] == org_id and (include_archived or not p.get("archived_at"))]
        return sorted(rows, key=lambda p: (p["created_at"], p["id"]))

    def update_project(self, project_id: str, now: datetime, name: str | None = None, settings: dict | None = None,
                       archived_at: datetime | None = None) -> None:
        p = self.projects[project_id]
        if name is not None:
            p["name"] = name
        if settings is not None:
            p["settings"] = settings
        if archived_at is not None:
            p["archived_at"] = archived_at
        p["updated_at"] = now

    def save_phases(self, project_id: str, phases: list[dict], now: datetime) -> None:
        self.projects[project_id]["phases"] = [dict(x) for x in phases]
        self.projects[project_id]["updated_at"] = now

    def add_event(self, project_id: str, action: str, user_sub, user_email, detail: dict, now: datetime) -> None:
        self.events.append({"id": len(self.events) + 1, "project_id": project_id, "ts": now, "action": action,
                            "user_sub": user_sub, "user_email": user_email, "detail": detail})

    def list_events(self, project_id: str, limit: int = 100) -> list[dict]:
        rows = [e for e in self.events if e["project_id"] == project_id]
        return [{k: _iso(v) for k, v in e.items() if k != "project_id"} for e in reversed(rows)][:limit]

    def save_run(self, run: dict, results: list[dict]) -> None:
        """A finished run in one go (tests, imports)."""
        with self._lock:
            self.runs[run["id"]] = {"status": "completed", "progress": None, "error": None, **run}
            self.results[run["id"]] = [dict(r) for r in results]

    def create_run(self, run: dict) -> None:
        with self._lock:
            active = next((r["id"] for r in self.runs.values()
                           if r["project_id"] == run["project_id"] and r["status"] in ACTIVE), None)
            if active:
                raise RunActive(active)
            self.runs[run["id"]] = {"finished_at": None, "summary": None, "progress": None, "error": None, **run}

    def set_run_status(self, run_id: str, status: str, progress: dict | None = None) -> None:
        with self._lock:
            self.runs[run_id]["status"] = status
            self.runs[run_id]["progress"] = progress

    def set_run_progress(self, run_id: str, progress: dict) -> None:
        with self._lock:
            self.runs[run_id]["progress"] = progress

    def complete_run(self, run_id: str, finished_at: datetime, summary: dict, results: list[dict]) -> None:
        with self._lock:
            self.results[run_id] = [dict(r) for r in results]
            self.runs[run_id].update(status="completed", finished_at=finished_at, summary=summary, progress=None)

    def fail_run(self, run_id: str, finished_at: datetime, error: str) -> None:
        with self._lock:
            self.runs[run_id].update(status="failed", finished_at=finished_at, error=error)

    def fail_interrupted_runs(self, now: datetime) -> list[dict]:
        with self._lock:
            hit = [r for r in self.runs.values() if r["status"] in ACTIVE]
            for r in hit:
                r.update(status="failed", finished_at=now, error=INTERRUPTED)
            return [{"id": r["id"], "project_id": r["project_id"]} for r in hit]

    def list_runs(self, project_id: str, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = sorted((r for r in self.runs.values() if r["project_id"] == project_id),
                          key=lambda r: r["started_at"], reverse=True)
            return [public_run(r) for r in rows[:limit]]

    def get_run(self, run_id: str) -> dict | None:
        with self._lock:
            r = self.runs.get(run_id)
            return None if r is None else public_run(r)

    def get_results(self, run_id: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.results.get(run_id, [])]


# ------------------------------------------------------------- postgres


class PgRepo:
    def __init__(self, pool, store, run_pool=None):
        """`run_pool` serves the run worker's history reads: runs are
        background work, so they shouldn't inherit the request pool's
        gateway statement timeout (#57). Defaults to `pool`."""
        self.pool = pool
        self.run_pool = run_pool or pool
        self.store = store

    def scope_for(self, claims) -> Scope:
        with self.pool.connection() as conn:
            return scope_mod.scope_for(conn, claims)

    def get_site(self, site_id: str) -> dict | None:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT id, org_id, name, topic_prefixes FROM sites WHERE id = %s", (site_id,)).fetchone()
        return None if row is None else {"id": row[0], "org_id": row[1], "name": row[2], "topic_prefixes": list(row[3] or [])}

    def user_email(self, sub: str | None) -> str | None:
        if not sub:
            return None
        with self.pool.connection() as conn:
            user = auth.get_user(conn, sub)
        return user["email"] if user else None

    @contextlib.contextmanager
    def history(self):
        with self.run_pool.connection() as conn:
            yield PostgresHistory(conn)

    def create_project(self, project: dict) -> None:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                "INSERT INTO cx_projects (id, org_id, site_id, name, settings, created_by, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (project["id"], project["org_id"], project.get("site_id"), project["name"],
                 json.dumps(project.get("settings") or {}), project.get("created_by"),
                 project["created_at"], project["updated_at"]),
            )
            self._write_phases(conn, project["id"], project["phases"])

    @staticmethod
    def _write_phases(conn, project_id: str, phases: list[dict]) -> None:
        conn.execute("DELETE FROM cx_phases WHERE project_id = %s", (project_id,))
        for p in phases:
            conn.execute(
                "INSERT INTO cx_phases (project_id, key, name, position, status, started_at, completed_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (project_id, p["key"], p["name"], p["position"], p["status"], p.get("started_at"), p.get("completed_at")),
            )

    _PROJECT_COLS = "id, org_id, site_id, name, settings, created_by, created_at, updated_at, archived_at"

    def _project_rows(self, conn, where: str, args: tuple) -> list[dict]:
        rows = conn.execute(f"SELECT {self._PROJECT_COLS} FROM cx_projects WHERE {where} ORDER BY created_at, id", args).fetchall()
        out = []
        for r in rows:
            p = dict(zip(self._PROJECT_COLS.split(", "), r, strict=True))
            p["phases"] = [
                {"key": k, "name": n, "position": pos, "status": st, "started_at": sa, "completed_at": ca}
                for k, n, pos, st, sa, ca in conn.execute(
                    "SELECT key, name, position, status, started_at, completed_at FROM cx_phases"
                    " WHERE project_id = %s ORDER BY position", (p["id"],)).fetchall()
            ]
            out.append(p)
        return out

    def get_project(self, project_id: str) -> dict | None:
        with self.pool.connection() as conn:
            rows = self._project_rows(conn, "id = %s", (project_id,))
        return rows[0] if rows else None

    def list_projects(self, org_id: str, include_archived: bool = False) -> list[dict]:
        with self.pool.connection() as conn:
            return self._project_rows(conn, "org_id = %s" + ("" if include_archived else " AND archived_at IS NULL"), (org_id,))

    def update_project(self, project_id: str, now: datetime, name: str | None = None, settings: dict | None = None,
                       archived_at: datetime | None = None) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE cx_projects SET name = COALESCE(%s, name), settings = COALESCE(%s::jsonb, settings),"
                " archived_at = COALESCE(%s, archived_at), updated_at = %s WHERE id = %s",
                (name, None if settings is None else json.dumps(settings), archived_at, now, project_id),
            )

    def save_phases(self, project_id: str, phases: list[dict], now: datetime) -> None:
        with self.pool.connection() as conn, conn.transaction():
            self._write_phases(conn, project_id, phases)
            conn.execute("UPDATE cx_projects SET updated_at = %s WHERE id = %s", (now, project_id))

    def add_event(self, project_id: str, action: str, user_sub, user_email, detail: dict, now: datetime) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO cx_events (project_id, ts, action, user_sub, user_email, detail) VALUES (%s, %s, %s, %s, %s, %s)",
                (project_id, now, action, user_sub, user_email, json.dumps(detail, default=str)),
            )

    def list_events(self, project_id: str, limit: int = 100) -> list[dict]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, ts, action, user_sub, user_email, detail FROM cx_events WHERE project_id = %s"
                " ORDER BY ts DESC, id DESC LIMIT %s", (project_id, limit)).fetchall()
        return [{"id": i, "ts": _iso(ts), "action": a, "user_sub": s, "user_email": e, "detail": d} for i, ts, a, s, e, d in rows]

    _RUN_COLS = ("id, project_id, phase, status, started_at, finished_at, window_start, window_end, triggered_by,"
                 " scope, summary, progress, error")

    @staticmethod
    def _jsonb(v):
        return None if v is None else json.dumps(v, default=str)

    def _insert_run(self, conn, run: dict) -> None:
        conn.execute(
            f"INSERT INTO cx_runs ({self._RUN_COLS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (run["id"], run["project_id"], run.get("phase"), run.get("status", "queued"), run["started_at"],
             run.get("finished_at"), run["window_start"], run["window_end"], run.get("triggered_by"),
             json.dumps(run["scope"]), self._jsonb(run.get("summary")), self._jsonb(run.get("progress")), run.get("error")),
        )

    @staticmethod
    def _insert_results(conn, run_id: str, results: list[dict]) -> None:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO cx_results (run_id, equip_uri, check_id, subject, status, severity, symptom, candidates, evidence)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(run_id, r["equip_uri"], r["check_id"], r["subject"], r["status"], r.get("severity"),
                  r.get("symptom"), json.dumps(r.get("candidates") or []), json.dumps(r["evidence"], default=str))
                 for r in results],
            )

    def save_run(self, run: dict, results: list[dict]) -> None:
        """A finished run in one go (tests, imports)."""
        with self.pool.connection() as conn, conn.transaction():
            self._insert_run(conn, {"status": "completed", **run})
            self._insert_results(conn, run["id"], results)

    def create_run(self, run: dict) -> None:
        """Insert a queued run; RunActive if the project already has one
        in flight (the partial unique index decides, so two API processes
        can't both start one)."""
        try:
            with self.pool.connection() as conn, conn.transaction():
                self._insert_run(conn, run)
        except psycopg.errors.UniqueViolation as exc:
            if "cx_runs_one_active" not in str(exc):
                raise
            with self.pool.connection() as conn:
                row = conn.execute("SELECT id FROM cx_runs WHERE project_id = %s AND status IN ('queued', 'running')",
                                   (run["project_id"],)).fetchone()
            raise RunActive(row[0] if row else None) from exc

    def set_run_status(self, run_id: str, status: str, progress: dict | None = None) -> None:
        with self.pool.connection() as conn:
            conn.execute("UPDATE cx_runs SET status = %s, progress = %s WHERE id = %s",
                         (status, self._jsonb(progress), run_id))

    def set_run_progress(self, run_id: str, progress: dict) -> None:
        with self.pool.connection() as conn:
            conn.execute("UPDATE cx_runs SET progress = %s WHERE id = %s AND status = 'running'",
                         (self._jsonb(progress), run_id))

    def complete_run(self, run_id: str, finished_at: datetime, summary: dict, results: list[dict]) -> None:
        with self.run_pool.connection() as conn, conn.transaction():
            self._insert_results(conn, run_id, results)
            conn.execute("UPDATE cx_runs SET status = 'completed', finished_at = %s, summary = %s, progress = NULL"
                         " WHERE id = %s", (finished_at, self._jsonb(summary), run_id))

    def fail_run(self, run_id: str, finished_at: datetime, error: str) -> None:
        with self.pool.connection() as conn:
            conn.execute("UPDATE cx_runs SET status = 'failed', finished_at = %s, error = %s WHERE id = %s",
                         (finished_at, error, run_id))

    def fail_interrupted_runs(self, now: datetime) -> list[dict]:
        """At startup: whatever was queued or running belonged to a process
        that's gone (the queue is in memory), so it will never finish."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "UPDATE cx_runs SET status = 'failed', finished_at = %s, error = %s"
                " WHERE status IN ('queued', 'running') RETURNING id, project_id", (now, INTERRUPTED)).fetchall()
        return [{"id": i, "project_id": p} for i, p in rows]

    def _runs(self, where: str, args: tuple) -> list[dict]:
        with self.pool.connection() as conn:
            rows = conn.execute(f"SELECT {self._RUN_COLS} FROM cx_runs WHERE {where}", args).fetchall()
        return [public_run(dict(zip(self._RUN_COLS.split(", "), r, strict=True))) for r in rows]

    def list_runs(self, project_id: str, limit: int = 50) -> list[dict]:
        return self._runs("project_id = %s ORDER BY started_at DESC LIMIT %s", (project_id, limit))

    def get_run(self, run_id: str) -> dict | None:
        rows = self._runs("id = %s", (run_id,))
        return rows[0] if rows else None

    def get_results(self, run_id: str) -> list[dict]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT equip_uri, check_id, subject, status, severity, symptom, candidates, evidence FROM cx_results"
                " WHERE run_id = %s ORDER BY equip_uri, check_id, subject", (run_id,)).fetchall()
        keys = ("equip_uri", "check_id", "subject", "status", "severity", "symptom", "candidates", "evidence")
        return [dict(zip(keys, r, strict=True)) for r in rows]

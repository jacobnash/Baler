"""
Commissioning API (Baler#32, slice Cx 1 / #37) - :8009, gateway /cx/.

Same shape as the other APIs: stdlib http.server, own process/port, an
OTel span per request, OpenAPI served off disk, roles enforced by the
gateway (gateway/njs/policy.js) and the gateway secret checked here.

Tenancy is checked here, not trusted: the caller's own JWT (verified
again, as scope.py does) must belong to the project's org, and a
site-restricted login only sees projects on its own sites. Graph reads
go through Scope inside the SPARQL (commissioning.inventory), and results
read back are filtered by the reader's scope too.

Every change to a project - created, renamed, phases edited, phase
moved, run - is a cx_events row with who did it.

Runs are asynchronous (#57): POST .../runs answers 202 with a queued run
and commissioning.runner executes it in a worker thread; clients poll
GET /runs/{id} until status is completed or failed.
"""

from __future__ import annotations

import argparse
import os
import re
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from baler import docs_ui, gateway_auth, tracing
from baler.commissioning import db, engine, gl36, inventory, phases, runner
from baler.commissioning import templates as tpl
from baler.http_handler_base import BaseAPIHandler, respond_error, respond_json
from baler.remote_store import RemoteStore
from baler.scope import Scope, claims_from_header
from baler.timeseries import GATEWAY_STATEMENT_TIMEOUT, connect_pool

tracer = tracing.get_tracer(__name__)

OPENAPI_SPEC_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "commissioning-api-openapi.yaml")
PORT = 8009
RUN_STATEMENT_TIMEOUT = "10min"

_PROJECT = re.compile(r"/projects/([^/]+)")
_PROJECT_SUB = re.compile(r"/projects/([^/]+)/(phases|phase|events|inventory|runs)")
_RUN = re.compile(r"/runs/([^/]+)")


def _scope_json(s: Scope) -> dict:
    return {"prefixes": None if s.prefixes is None else list(s.prefixes), "exclude": list(s.exclude)}


def _check_settings(settings) -> dict:
    if settings is None:
        return {}
    if not isinstance(settings, dict):
        raise TypeError("settings must be an object")
    if "window_hours" in settings:
        engine.window_hours(settings["window_hours"])
    if "gl36" in settings:
        gl36.validate_settings(settings["gl36"])
    return settings


def make_handler(repo, clock=None, worker=None):
    now_fn = clock or (lambda: datetime.now(timezone.utc))
    worker = worker or runner.RunWorker(repo, now_fn)

    class CommissioningHandler(BaseAPIHandler):
        TRACER = tracer
        ALLOWED_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        ALLOWED_HEADERS = "Content-Type, Authorization"

        # ------------------------------------------------------- plumbing

        def _caller(self) -> dict | None:
            """Claims of a gateway-forwarded, verifiable caller, or None
            after answering 401."""
            if not gateway_auth.request_came_through_gateway(self):
                self.send_response(401)
                self.end_headers()
                return None
            claims = claims_from_header(self.headers.get("Authorization"))
            if not claims or not claims.get("org_id"):
                respond_error(self, 401, "a verifiable token with an org is required")
                return None
            return claims

        def _visible(self, project: dict | None, claims: dict) -> bool:
            if project is None or project["org_id"] != claims.get("org_id"):
                return False
            sites = claims.get("site_ids") or []
            return not sites or project.get("site_id") in sites

        def _project(self, project_id: str, claims: dict) -> dict | None:
            p = repo.get_project(project_id)
            if not self._visible(p, claims):
                respond_error(self, 404, "no such project")
                return None
            return p

        def _event(self, project_id: str, action: str, claims: dict, detail: dict) -> None:
            sub = claims.get("sub")
            repo.add_event(project_id, action, sub, repo.user_email(sub), detail, now_fn())

        def _route(self, method: str, path: str):
            """(span name, fn(span, claims)) or None."""
            if path == "/projects":
                return {"GET": ("list_projects", self._list_projects), "POST": ("create_project", self._create_project)}.get(method)
            if path == "/templates" and method == "GET":
                return "templates", self._templates
            m = _PROJECT_SUB.fullmatch(path)
            if m:
                pid, sub = m.group(1), m.group(2)
                table = {
                    ("PUT", "phases"): ("put_phases", self._put_phases),
                    ("POST", "phase"): ("set_phase", self._set_phase),
                    ("GET", "events"): ("events", self._events),
                    ("GET", "inventory"): ("inventory", self._inventory),
                    ("POST", "runs"): ("create_run", self._create_run),
                    ("GET", "runs"): ("list_runs", self._list_runs),
                }
                hit = table.get((method, sub))
                return None if hit is None else (hit[0], lambda span, claims: hit[1](span, claims, pid))
            m = _PROJECT.fullmatch(path)
            if m:
                pid = m.group(1)
                hit = {"GET": ("get_project", self._get_project), "PATCH": ("patch_project", self._patch_project),
                       "DELETE": ("archive_project", self._archive_project)}.get(method)
                return None if hit is None else (hit[0], lambda span, claims: hit[1](span, claims, pid))
            m = _RUN.fullmatch(path)
            if m and method == "GET":
                rid = m.group(1)
                return "get_run", lambda span, claims: self._get_run(span, claims, rid)
            return None

        def _dispatch(self, method: str) -> None:
            path = urlparse(self.path).path
            route = self._route(method, path)
            if route is None:
                respond_error(self, 404, "not found")
                return
            claims = self._caller()
            if claims is None:
                return
            name, fn = route
            self.handle_traced(f"commissioning_api.{name}", lambda span: fn(span, claims))

        def do_GET(self):
            if self.path == "/openapi.yaml":
                self.serve_openapi_spec("commissioning_api.get_openapi_spec", OPENAPI_SPEC_PATH)
                return
            if self.path == "/docs":
                docs_ui.serve(self)
                return
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

        # ------------------------------------------------------- projects

        def _list_projects(self, span, claims) -> None:
            q = parse_qs(urlparse(self.path).query)
            archived = q.get("include_archived", ["false"])[0] == "true"
            rows = [p for p in repo.list_projects(claims["org_id"], archived) if self._visible(p, claims)]
            respond_json(self, 200, [db.public_project(p) for p in rows])

        def _create_project(self, span, claims) -> None:
            body = self.read_json_body()
            name = body.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("name is required")
            site_id = body.get("site_id")
            allowed_sites = claims.get("site_ids") or []
            if site_id is not None:
                site = repo.get_site(site_id) if isinstance(site_id, str) else None
                if site is None or site["org_id"] != claims["org_id"] or (allowed_sites and site_id not in allowed_sites):
                    raise ValueError("no such site")
            elif allowed_sites:
                raise ValueError("site_id is required for a site-restricted login")
            phase = body.get("phase")
            if not isinstance(phase, str):
                raise TypeError("phase is required - absence means opposite things in each phase, so it's always stated")
            plist = phases.normalize(body["phases"]) if body.get("phases") is not None else phases.default_phases()
            now = now_fn()
            plist = phases.set_current(plist, phase, now)
            project = {
                "id": db.new_id(), "org_id": claims["org_id"], "site_id": site_id, "name": name.strip()[:200],
                "settings": _check_settings(body.get("settings")), "created_by": claims.get("sub"),
                "created_at": now, "updated_at": now, "archived_at": None, "phases": plist,
            }
            repo.create_project(project)
            self._event(project["id"], "project.create", claims, {"name": project["name"], "site_id": site_id, "phase": phase})
            span.set_attribute("project_id", project["id"])
            respond_json(self, 201, db.public_project(project))

        def _get_project(self, span, claims, pid) -> None:
            p = self._project(pid, claims)
            if p is not None:
                respond_json(self, 200, db.public_project(p))

        def _patch_project(self, span, claims, pid) -> None:
            p = self._project(pid, claims)
            if p is None:
                return
            body = self.read_json_body()
            name = body.get("name")
            if name is not None and (not isinstance(name, str) or not name.strip()):
                raise ValueError("name must be a non-empty string")
            settings = _check_settings(body["settings"]) if "settings" in body else None
            repo.update_project(pid, now_fn(), name=name.strip()[:200] if name else None, settings=settings)
            self._event(pid, "project.update", claims, {k: body[k] for k in ("name", "settings") if k in body})
            respond_json(self, 200, db.public_project(repo.get_project(pid)))

        def _archive_project(self, span, claims, pid) -> None:
            p = self._project(pid, claims)
            if p is None:
                return
            if not p.get("archived_at"):
                repo.update_project(pid, now_fn(), archived_at=now_fn())
                self._event(pid, "project.archive", claims, {})
            self.send_response(204)
            self.end_headers()

        # --------------------------------------------------------- phases

        def _put_phases(self, span, claims, pid) -> None:
            p = self._project(pid, claims)
            if p is None:
                return
            body = self.read_json_body()
            plist = phases.normalize(body.get("phases"), p["phases"])
            if phases.current(plist) is None:
                raise ValueError("exactly one phase must stay active - keep the current phase, or move it first with POST .../phase")
            repo.save_phases(pid, plist, now_fn())
            self._event(pid, "phases.update", claims, {"phases": [{"key": x["key"], "name": x["name"], "status": x["status"]} for x in plist]})
            respond_json(self, 200, db.public_project(repo.get_project(pid)))

        def _set_phase(self, span, claims, pid) -> None:
            p = self._project(pid, claims)
            if p is None:
                return
            body = self.read_json_body()
            key = body.get("key")
            if not isinstance(key, str):
                raise TypeError("key is required")
            before = phases.current(p["phases"])
            plist = phases.set_current(p["phases"], key, now_fn())
            repo.save_phases(pid, plist, now_fn())
            detail = {"from": before, "to": key}
            if isinstance(body.get("note"), str):
                detail["note"] = body["note"][:2000]
            self._event(pid, "phase.set", claims, detail)
            respond_json(self, 200, db.public_project(repo.get_project(pid)))

        def _events(self, span, claims, pid) -> None:
            if self._project(pid, claims) is not None:
                respond_json(self, 200, repo.list_events(pid))

        # ----------------------------------------------------- inventory/runs

        def _scopes(self, project: dict, claims: dict) -> tuple[list[Scope], list[str]]:
            scopes = [repo.scope_for(claims)]
            prefixes: list[str] = []
            if project.get("site_id"):
                site = repo.get_site(project["site_id"])
                prefixes = list((site or {}).get("topic_prefixes") or [])
                scopes.append(inventory.site_scope(prefixes))
            return scopes, prefixes

        def _inventory(self, span, claims, pid) -> None:
            p = self._project(pid, claims)
            if p is None:
                return
            scopes, _ = self._scopes(p, claims)
            equips = inventory.build(repo.store, scopes)
            templates = tpl.load()
            for e in equips:
                e["templates"] = [t.id for t in tpl.templates_for(e["brickClasses"], templates)]
            span.set_attribute("equipment", len(equips))
            respond_json(self, 200, {"equipment": equips})

        def _create_run(self, span, claims, pid) -> None:
            p = self._project(pid, claims)
            if p is None:
                return
            if p.get("archived_at"):
                respond_error(self, 409, "project is archived")
                return
            body = self.read_json_body()
            wh = engine.window_hours(body.get("window_hours", (p.get("settings") or {}).get("window_hours")))
            scopes, prefixes = self._scopes(p, claims)
            started = now_fn()
            phase = phases.current(p["phases"])
            run = {
                "id": db.new_id(), "project_id": pid, "phase": phase, "status": "queued", "started_at": started,
                "finished_at": None, "window_start": started - timedelta(hours=wh), "window_end": started,
                "triggered_by": claims.get("sub"), "scope": {"caller": _scope_json(scopes[0]), "site_prefixes": prefixes},
                "summary": None, "progress": None, "error": None,
            }
            try:
                repo.create_run(run)
            except db.RunActive as exc:
                respond_json(self, 409, {"error": str(exc), "run_id": exc.run_id})
                return
            sub = claims.get("sub")
            email = repo.user_email(sub)
            repo.add_event(pid, "run.create", sub, email, {"run_id": run["id"], "phase": phase, "window_hours": wh}, now_fn())
            gl36_settings = (p.get("settings") or {}).get("gl36")
            worker.submit(runner.RunJob(run["id"], pid, phase, scopes, started, wh, sub, email,
                                        {"gl36_settings": gl36_settings} if gl36_settings else {}))
            span.set_attribute("run_id", run["id"])
            respond_json(self, 202, db.public_run(run))

        def _list_runs(self, span, claims, pid) -> None:
            if self._project(pid, claims) is not None:
                respond_json(self, 200, repo.list_runs(pid))

        def _get_run(self, span, claims, rid) -> None:
            run = repo.get_run(rid)
            if run is None or not self._visible(repo.get_project(run["project_id"]), claims):
                respond_error(self, 404, "no such run")
                return
            q = parse_qs(urlparse(self.path).query)
            scope = repo.scope_for(claims)
            results = [r for r in repo.get_results(rid) if scope.allows(r["equip_uri"])]
            for key in ("status", "equip_uri", "check_id"):
                if key in q:
                    want = q[key][0]
                    results = [r for r in results if (r[key] == want if key != "check_id" else r[key].startswith(want))]
            respond_json(self, 200, {"run": run, "results": results})

        def _templates(self, span, claims) -> None:
            respond_json(self, 200, [t.as_dict() for t in tpl.load()])

    return CommissioningHandler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    tracing.init_tracing("baler-commissioning-api")

    ts_pool = connect_pool(statement_timeout=GATEWAY_STATEMENT_TIMEOUT)
    with ts_pool.connection() as conn:
        db.ensure_schema(conn)
    tpl.load()  # fail at startup, not on first request, if the shapes file is broken
    # Background runs read history on their own connections, with a bound
    # well past the gateway's (it's the request that has 60 s, not the run).
    run_pool = connect_pool(min_size=1, max_size=runner.WORKERS + 1, statement_timeout=RUN_STATEMENT_TIMEOUT)
    repo = db.PgRepo(ts_pool, RemoteStore(), run_pool=run_pool)
    now = datetime.now(timezone.utc)
    for r in repo.fail_interrupted_runs(now):
        repo.add_event(r["project_id"], "run.fail", None, None, {"run_id": r["id"], "error": db.INTERRUPTED}, now)
        print(f"commissioning run {r['id']} was interrupted by a restart; marked failed")
    server = ThreadingHTTPServer((args.host, args.port), make_handler(repo))
    print(f"commissioning API listening on {args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

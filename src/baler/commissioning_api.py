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
import json
import os
import re
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from baler import docs_ui, gateway_auth, tracing
from baler.commissioning import (
    db,
    economizer,
    engine,
    gl36,
    inventory,
    issues,
    ladder,
    phases,
    report,
    runner,
)
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
_PROJECT_SUB = re.compile(r"/projects/([^/]+)/(phases|phase|events|inventory|runs|gaps|issues|risks)")
_PROJECT_ISSUES = re.compile(r"/projects/([^/]+)/issues/(import|export)")
_RUN_ISSUES = re.compile(r"/runs/([^/]+)/issues")
_ISSUE = re.compile(r"/issues/([^/]+)")
_ISSUE_SUB = re.compile(r"/issues/([^/]+)/(events|risk)")
_RISK = re.compile(r"/risks/([^/]+)")
_RUN = re.compile(r"/runs/([^/]+)")
_RUN_REPORT = re.compile(r"/runs/([^/]+)/report")


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
    if "ladder" in settings:
        ladder.validate_settings(settings["ladder"])
    if "economizer" in settings:
        economizer.validate_settings(settings["economizer"])
    return settings


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
    return out


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

        @staticmethod
        def _bind(entry, ident: str):
            name, fn = entry
            return name, lambda span, claims: fn(span, claims, ident)

        def _route(self, method: str, path: str):
            """(span name, fn(span, claims)) or None."""
            if path == "/projects":
                return {"GET": ("list_projects", self._list_projects), "POST": ("create_project", self._create_project)}.get(method)
            if path == "/templates" and method == "GET":
                return "templates", self._templates
            if path == "/equipment-status" and method == "GET":
                return "equipment_status", self._equipment_status
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
                    ("GET", "gaps"): ("gaps", self._gaps),
                    ("GET", "issues"): ("list_issues", self._list_issues),
                    ("POST", "issues"): ("create_issue", self._create_issue),
                    ("GET", "risks"): ("list_risks", self._list_risks),
                }
                hit = table.get((method, sub))
                return None if hit is None else (hit[0], lambda span, claims: hit[1](span, claims, pid))
            m = _PROJECT.fullmatch(path)
            if m:
                pid = m.group(1)
                hit = {"GET": ("get_project", self._get_project), "PATCH": ("patch_project", self._patch_project),
                       "DELETE": ("archive_project", self._archive_project)}.get(method)
                return None if hit is None else (hit[0], lambda span, claims: hit[1](span, claims, pid))
            for rx, sub_table in (
                (_PROJECT_ISSUES, {("POST", "import"): ("import_issues", self._import_issues),
                                   ("GET", "export"): ("export_issues", self._export_issues)}),
                (_ISSUE_SUB, {("GET", "events"): ("issue_events", self._issue_events),
                              ("POST", "risk"): ("accept_risk", self._accept_risk)}),
            ):
                m = rx.fullmatch(path)
                if m:
                    found = sub_table.get((method, m.group(2)))
                    return None if found is None else self._bind(found, m.group(1))
            m = _RUN_ISSUES.fullmatch(path)
            if m and method == "POST":
                return self._bind(("issues_from_run", self._issues_from_run), m.group(1))
            m = _ISSUE.fullmatch(path)
            if m:
                found = {"GET": ("get_issue", self._get_issue), "PATCH": ("patch_issue", self._patch_issue)}.get(method)
                return None if found is None else self._bind(found, m.group(1))
            m = _RISK.fullmatch(path)
            if m and method == "DELETE":
                kid = m.group(1)
                return "revoke_risk", lambda span, claims: self._revoke_risk(span, claims, kid)
            m = _RUN_REPORT.fullmatch(path)
            if m and method == "GET":
                rid = m.group(1)
                return "run_report", lambda span, claims: self._run_report(span, claims, rid)
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
            worker.submit(runner.RunJob(run["id"], pid, phase, scopes, started, wh, sub, email,
                                        engine_kwargs(p.get("settings"))))
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

        def _visible_run(self, rid: str, claims: dict) -> tuple[dict, dict] | None:
            run = repo.get_run(rid)
            project = repo.get_project(run["project_id"]) if run else None
            if run is None or project is None or not self._visible(project, claims):
                respond_error(self, 404, "no such run")
                return None
            return run, project

        def _run_report(self, span, claims, rid) -> None:
            hit = self._visible_run(rid, claims)
            if hit is None:
                return
            run, project = hit
            fmt = parse_qs(urlparse(self.path).query).get("format", ["json"])[0]
            if fmt not in ("json", "html"):
                raise ValueError("format must be json or html")
            scope = repo.scope_for(claims)
            results = [r for r in repo.get_results(rid) if scope.allows(r["equip_uri"])]
            issues = [i for i in self._report_issues(project["id"]) if not i.get("equip_uri") or scope.allows(i["equip_uri"])]
            rep = report.build(db.public_project(project), run, results, issues)
            if fmt == "json":
                respond_json(self, 200, rep)
                return
            body = report.html(rep).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # The page is the caller's data; nothing else may run in it.
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def _report_issues(self, project_id: str) -> list[dict]:
            return [db.public_row(i) for i in repo.list_issues(project_id)]

        def _equipment_status(self, span, claims) -> None:
            """One verdict per equipment from each visible project's latest
            completed run (the equipment tree's Cx dot)."""
            scope = repo.scope_for(claims)
            out: dict[str, dict] = {}
            for p in repo.list_projects(claims["org_id"]):
                if not self._visible(p, claims):
                    continue
                run = next((r for r in repo.list_runs(p["id"], 20) if r["status"] == "completed"), None)
                if run is None:
                    continue
                results = [r for r in repo.get_results(run["id"]) if scope.allows(r["equip_uri"])]
                for equip, s in report.equipment_summary(results).items():
                    prior = out.get(equip)
                    if prior is None or prior["finished_at"] < run["finished_at"]:
                        out[equip] = {**s, "run_id": run["id"], "project_id": p["id"], "project": p["name"],
                                      "finished_at": run["finished_at"]}
            respond_json(self, 200, out)

        def _gaps(self, span, claims, pid) -> None:
            """Today's inventory against the expected-point templates,
            without a run: what's missing, per equipment."""
            p = self._project(pid, claims)
            if p is None:
                return
            scopes, _ = self._scopes(p, claims)
            templates = tpl.load()
            rows, without = [], []
            for e in inventory.build(repo.store, scopes):
                applicable = tpl.templates_for(e["brickClasses"], templates)
                if not applicable:
                    without.append({"id": e["id"], "dis": e["dis"], "brickClass": e["brickClass"]})
                    continue
                for t in applicable:
                    outcomes = tpl.evaluate(t, e["points"])
                    missing = [{"id": o["requirement"].id, "name": o["requirement"].name,
                                "classes": list(o["requirement"].classes), "severity": o["requirement"].severity,
                                "message": o["requirement"].message} for o in outcomes if not o["matched"]]
                    rows.append({"equip_uri": e["id"], "dis": e["dis"], "brickClass": e["brickClass"], "template": t.id,
                                 "matched": len(outcomes) - len(missing), "expected": len(outcomes), "missing": missing})
            respond_json(self, 200, {"equipment": rows, "without_template": without})

        # --------------------------------------------------------- issues

        def _issue_event(self, issue_id: str, action: str, claims: dict, detail: dict) -> None:
            sub = claims.get("sub")
            repo.add_issue_event(issue_id, action, sub, repo.user_email(sub), detail, now_fn())

        def _visible_issue(self, iid: str, claims: dict) -> tuple[dict, dict] | None:
            issue = repo.get_issue(iid)
            project = repo.get_project(issue["project_id"]) if issue else None
            if (issue is None or project is None or not self._visible(project, claims)
                    or (issue.get("equip_uri") and not repo.scope_for(claims).allows(issue["equip_uri"]))):
                respond_error(self, 404, "no such issue")
                return None
            return issue, project

        def _public_issue(self, issue: dict, risks: dict[str, dict] | None = None) -> dict:
            out = db.public_row(issue)
            risk = (risks or {}).get(issue["id"])
            out["risk"] = db.public_row(risk) if risk else None
            return out

        def _list_issues(self, span, claims, pid) -> None:
            if self._project(pid, claims) is None:
                return
            runner.reconcile_issues(repo, pid, None, [], now_fn())  # an overdue risk reopens on read
            q = parse_qs(urlparse(self.path).query)
            statuses = [x for v in q.get("status", []) for x in v.split(",") if x]
            for st in statuses:
                if st not in issues.STATUSES:
                    raise ValueError(f"status must be one of {', '.join(issues.STATUSES)}")
            scope = repo.scope_for(claims)
            rows = [i for i in repo.list_issues(pid, statuses or None) if not i.get("equip_uri") or scope.allows(i["equip_uri"])]
            risks = {r["issue_id"]: r for r in repo.list_risks(pid)}
            respond_json(self, 200, [self._public_issue(i, risks) for i in rows])

        def _create_issue(self, span, claims, pid) -> None:
            """A manual issue, or one opened from a run's result
            ({"from_result": {"run_id", "check_id", "subject"}})."""
            p = self._project(pid, claims)
            if p is None:
                return
            body = self.read_json_body()
            now = now_fn()
            if body.get("from_result") is not None:
                ref = body["from_result"]
                if not isinstance(ref, dict):
                    raise TypeError("from_result must be an object with run_id, check_id and subject")
                run = repo.get_run(str(ref.get("run_id")))
                if run is None or run["project_id"] != pid:
                    raise ValueError("no such run in this project")
                result = next((r for r in repo.get_results(run["id"])
                               if r["check_id"] == ref.get("check_id") and r["subject"] == ref.get("subject")), None)
                if result is None or not repo.scope_for(claims).allows(result["equip_uri"]):
                    raise ValueError("no such result in that run")
                if result["status"] != "fail":
                    raise ValueError("only a failing result opens an issue")
                issue = issues.from_result(result, run["id"], pid, db.new_id, now, claims.get("sub"))
            else:
                title = issues._text(body.get("title"), issues.MAX_TITLE)
                if not title:
                    raise ValueError("title is required")
                equip = body.get("equip_uri")
                if equip is not None and (not isinstance(equip, str) or not equip.startswith("urn:equip:")
                                          or not repo.scope_for(claims).allows(equip)):
                    raise ValueError("equip_uri must be an equipment URI in your scope")
                issue = {"id": db.new_id(), "project_id": pid, "equip_uri": equip, "check_id": None, "subject": None,
                         "status": "open", "title": title, "severity": issues.check_severity(body.get("severity")),
                         "assignee": issues._text(body.get("assignee"), 200), "source": "baler", "source_id": None,
                         "detail": {"description": issues._text(body.get("description"))}, "opened_run": None,
                         "last_seen_run": None, "closed_run": None, "created_by": claims.get("sub"),
                         "created_at": now, "updated_at": now, "closed_at": None}
            if body.get("severity") is not None:
                issue["severity"] = issues.check_severity(body["severity"])
            if body.get("assignee") is not None:
                issue["assignee"] = issues._text(body["assignee"], 200)
            try:
                repo.create_issue(issue)
            except db.IssueExists as exc:
                respond_json(self, 409, {"error": str(exc), "issue_id": exc.issue_id})
                return
            self._issue_event(issue["id"], "create", claims, {"title": issue["title"], "check_id": issue["check_id"]})
            self._event(pid, "issue.create", claims, {"issue_id": issue["id"], "title": issue["title"]})
            respond_json(self, 201, self._public_issue(issue))

        def _issues_from_run(self, span, claims, rid) -> None:
            """Open an issue for every failing result of a run that no
            active issue tracks yet. Optional filters: check_prefix,
            equip_uri, severity (template severities)."""
            hit = self._visible_run(rid, claims)
            if hit is None:
                return
            _, project = hit
            body = self.read_json_body()
            prefix = body.get("check_prefix") or ""
            equip = body.get("equip_uri")
            sev = body.get("severity")
            scope = repo.scope_for(claims)
            now = now_fn()
            opened, skipped = [], 0
            for r in repo.get_results(rid):
                if (r["status"] != "fail" or not r["check_id"].startswith(prefix) or not scope.allows(r["equip_uri"])
                        or (equip and r["equip_uri"] != equip) or (sev and r.get("severity") != sev)):
                    continue
                issue = issues.from_result(r, rid, project["id"], db.new_id, now, claims.get("sub"))
                try:
                    repo.create_issue(issue)
                except db.IssueExists:
                    skipped += 1
                    continue
                self._issue_event(issue["id"], "create", claims, {"run_id": rid, "check_id": r["check_id"]})
                opened.append(issue["id"])
            self._event(project["id"], "issues.from_run", claims, {"run_id": rid, "opened": len(opened), "already_tracked": skipped})
            respond_json(self, 200, {"opened": opened, "already_tracked": skipped})

        def _get_issue(self, span, claims, iid) -> None:
            hit = self._visible_issue(iid, claims)
            if hit is None:
                return
            issue, project = hit
            all_risks = [r for r in repo.list_risks(project["id"], include_revoked=True) if r["issue_id"] == iid]
            out = self._public_issue(issue, {r["issue_id"]: r for r in all_risks if r["revoked_at"] is None})
            out["risks"] = [db.public_row(r) for r in all_risks]
            out["events"] = repo.list_issue_events(iid)
            respond_json(self, 200, out)

        def _patch_issue(self, span, claims, iid) -> None:
            hit = self._visible_issue(iid, claims)
            if hit is None:
                return
            issue, _ = hit
            body = self.read_json_body()
            now = now_fn()
            changes: dict = {}
            if "status" in body and body["status"] != issue["status"]:
                try:
                    issues.transition(issue, body["status"])
                except issues.Conflict as exc:
                    respond_error(self, 409, str(exc))
                    return
                changes["status"] = body["status"]
                changes["closed_at"] = now if body["status"] == "closed" else None
                if body["status"] != "closed":
                    changes["closed_run"] = None
            if "assignee" in body:
                changes["assignee"] = issues._text(body["assignee"], 200)
            if "severity" in body:
                changes["severity"] = issues.check_severity(body["severity"])
            if "title" in body:
                title = issues._text(body["title"], issues.MAX_TITLE)
                if not title:
                    raise ValueError("title must not be empty")
                changes["title"] = title
            note = issues._text(body.get("note"))
            if not changes and not note:
                raise ValueError("nothing to change: status, assignee, severity, title or note")
            if changes:
                repo.update_issue(iid, {**changes, "updated_at": now})
            detail = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in changes.items()}
            if "status" in changes:
                detail["from"] = issue["status"]
            if note:
                detail["note"] = note
            self._issue_event(iid, "update" if changes else "note", claims, detail)
            respond_json(self, 200, self._public_issue(repo.get_issue(iid) or issue))

        def _issue_events(self, span, claims, iid) -> None:
            if self._visible_issue(iid, claims) is not None:
                respond_json(self, 200, repo.list_issue_events(iid))

        def _accept_risk(self, span, claims, iid) -> None:
            hit = self._visible_issue(iid, claims)
            if hit is None:
                return
            issue, project = hit
            if issue["status"] in ("closed", "risk_accepted"):
                respond_error(self, 409, f"issue is {issue['status']}")
                return
            now = now_fn()
            reason, review_by = issues.check_risk(self.read_json_body(), now.date())
            sub = claims.get("sub")
            risk = {"id": db.new_id(), "issue_id": iid, "accepted_by": sub, "accepted_email": repo.user_email(sub),
                    "reason": reason, "review_by": review_by, "created_at": now, "revoked_at": None, "revoked_by": None}
            repo.create_risk(risk)
            repo.update_issue(iid, {"status": "risk_accepted", "updated_at": now})
            self._issue_event(iid, "risk.accept", claims, {"risk_id": risk["id"], "reason": reason, "review_by": review_by.isoformat(),
                                                          "from": issue["status"]})
            self._event(project["id"], "risk.accept", claims, {"issue_id": iid, "risk_id": risk["id"]})
            respond_json(self, 201, db.public_row(risk))

        def _list_risks(self, span, claims, pid) -> None:
            if self._project(pid, claims) is None:
                return
            runner.reconcile_issues(repo, pid, None, [], now_fn())
            q = parse_qs(urlparse(self.path).query)
            scope = repo.scope_for(claims)
            out = []
            for r in repo.list_risks(pid, include_revoked=q.get("include_revoked", ["false"])[0] == "true"):
                issue = repo.get_issue(r["issue_id"]) or {}
                if issue.get("equip_uri") and not scope.allows(issue["equip_uri"]):
                    continue
                out.append({**db.public_row(r), "issue": {k: issue.get(k) for k in ("id", "title", "status", "equip_uri", "check_id")}})
            respond_json(self, 200, out)

        def _revoke_risk(self, span, claims, kid) -> None:
            risk = repo.get_risk(kid)
            hit = self._visible_issue(risk["issue_id"], claims) if risk else None
            if risk is None or hit is None:
                if risk is None:
                    respond_error(self, 404, "no such risk")
                return
            issue, project = hit
            if risk["revoked_at"] is None:
                now = now_fn()
                repo.revoke_risk(kid, now, claims.get("sub"))
                if issue["status"] == "risk_accepted":
                    repo.update_issue(issue["id"], {"status": "open", "updated_at": now})
                self._issue_event(issue["id"], "risk.revoke", claims, {"risk_id": kid})
                self._event(project["id"], "risk.revoke", claims, {"issue_id": issue["id"], "risk_id": kid})
            self.send_response(204)
            self.end_headers()

        def _import_issues(self, span, claims, pid) -> None:
            """External issues and punch items (CSV or JSON from CxAlloy,
            Facility Grid, a spreadsheet). Upserts on (source, source id)."""
            p = self._project(pid, claims)
            if p is None:
                return
            source, rows = issues.parse_import(self.read_json_body())
            equips: list[dict] = []
            if any(r["equipment"] for r in rows):
                scopes, _ = self._scopes(p, claims)
                equips = inventory.build(repo.store, scopes)
            now = now_fn()
            created = updated = 0
            unmatched = []
            for r in rows:
                equip = issues.match_equipment(r["equipment"], equips)
                if r["equipment"] and equip is None:
                    unmatched.append(r["equipment"])
                detail = {k: r[k] for k in ("description", "equipment", "location", "created", "status_raw", "extra") if r[k]}
                prior = repo.find_issue_by_source(pid, source, r["source_id"])
                if prior is None:
                    issue = {"id": db.new_id(), "project_id": pid, "equip_uri": equip, "check_id": None, "subject": None,
                             "status": r["status"], "title": r["title"], "severity": r["severity"], "assignee": r["assignee"],
                             "source": source, "source_id": r["source_id"], "detail": detail, "opened_run": None,
                             "last_seen_run": None, "closed_run": None, "created_by": claims.get("sub"),
                             "created_at": now, "updated_at": now, "closed_at": now if r["status"] == "closed" else None}
                    repo.create_issue(issue)
                    self._issue_event(issue["id"], "import", claims, {"source": source, "source_id": r["source_id"]})
                    created += 1
                    continue
                changes = {"title": r["title"], "severity": r["severity"], "assignee": r["assignee"], "detail": detail,
                           "equip_uri": equip or prior.get("equip_uri")}
                if prior["status"] != "risk_accepted" and r["status"] != prior["status"]:
                    changes["status"] = r["status"]
                    changes["closed_at"] = now if r["status"] == "closed" else None
                diff = {k: v for k, v in changes.items() if prior.get(k) != v}
                if diff:
                    repo.update_issue(prior["id"], {**diff, "updated_at": now})
                    self._issue_event(prior["id"], "import.update", claims,
                                      {"source": source, "fields": sorted(diff)})
                    updated += 1
            self._event(pid, "issues.import", claims, {"source": source, "created": created, "updated": updated,
                                                       "unchanged": len(rows) - created - updated})
            respond_json(self, 200, {"source": source, "created": created, "updated": updated,
                                     "unchanged": len(rows) - created - updated,
                                     "unmatched_equipment": sorted(set(unmatched))[:100]})

        def _export_issues(self, span, claims, pid) -> None:
            if self._project(pid, claims) is None:
                return
            q = parse_qs(urlparse(self.path).query)
            fmt = q.get("format", ["csv"])[0]
            shape = q.get("shape", ["baler"])[0]
            if fmt not in ("csv", "json"):
                raise ValueError("format must be csv or json")
            scope = repo.scope_for(claims)
            rows = issues.export_rows([i for i in repo.list_issues(pid) if not i.get("equip_uri") or scope.allows(i["equip_uri"])], shape)
            body = (json.dumps(rows) if fmt == "json" else issues.to_csv(rows, shape)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json" if fmt == "json" else "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="cx-issues-{shape}.{fmt}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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

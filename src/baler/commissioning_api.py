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
import base64
import hashlib
import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import psycopg
from opentelemetry import context as otel_context

from baler import docs_ui, gateway_auth, hisquery, timeseries, tracing, warmup
from baler.commissioning import (
    active,
    agent,
    baselines_api,
    compliance,
    compliance_api,
    coverage,
    db,
    economizer,
    engine,
    fleet,
    gates,
    gl36,
    history,
    hunting_api,
    inventory,
    issues,
    ladder,
    llm,
    manual,
    mirror,
    operator_writes,
    overrides,
    pack_extract_api,
    packs_api,
    passport_api,
    phases,
    procedures,
    readiness_api,
    report,
    retest_schedules,
    routes,
    runner,
    savings_api,
    spaces_api,
    spec,
    test_library,
)
from baler.commissioning import schedules as sched_eval
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
_PROJECT_SUB = re.compile(r"/projects/([^/]+)/(phases|phase|gate|report|events|inventory|runs|gaps|issues|risks|procedures|faults|faults/ingest|active-runs|fleet-runs|active-schedules|agent/sessions|agent/proposals|writes|schedule-writes|schedule-targets|schedules|schedules/refresh|coverage)")
_PROJECT_ISSUES = re.compile(r"/projects/([^/]+)/issues/(import|export)")
_RUN_ISSUES = re.compile(r"/runs/([^/]+)/issues")
_ISSUE = re.compile(r"/issues/([^/]+)")
_ISSUE_SUB = re.compile(r"/issues/([^/]+)/(events|risk)")
_RISK = re.compile(r"/risks/([^/]+)")
_PROCEDURE = re.compile(r"/procedures/([^/]+)")
_PROCEDURE_RUNS = re.compile(r"/procedures/([^/]+)/runs")
_TEST_RUN = re.compile(r"/test-runs/([^/]+)")
_TEST_RUN_MANUAL = re.compile(r"/test-runs/([^/]+)/manual-results")
_MANUAL_RESULT = re.compile(r"/manual-results/([^/]+)")
_MANUAL_ATTACHMENT = re.compile(r"/manual-attachments/([^/]+)")
_RUN_DIFF = re.compile(r"/runs/([^/]+)/diff")
_PROJECT_SPEC = re.compile(r"/projects/([^/]+)/(spec|spec/import|spec/extract|spec/documents|spec/review|alignment|alignment/decisions)")
_SPEC_DOC = re.compile(r"/spec-documents/([^/]+)")
_SPEC_ITEM = re.compile(r"/spec-items/([^/]+)")
_MAPPING = re.compile(r"/mappings/([^/]+)")
_ACTIVE_RUN = re.compile(r"/active-runs/([^/]+)")
_AGENT_SESSION = re.compile(r"/agent-sessions/([^/]+)")
_AGENT_SESSION_MSG = re.compile(r"/agent-sessions/([^/]+)/messages")
_PROPOSAL_SUB = re.compile(r"/agent-proposals/([^/]+)/(accept|reject)")
_WRITE_SUB = re.compile(r"/writes/([^/]+)/(relinquish|revert)")
_ACTIVE_RUN_SUB = re.compile(r"/active-runs/([^/]+)/(confirm|abort)")
_FLEET_RUN = re.compile(r"/fleet-runs/([^/]+)")
_FLEET_RUN_SUB = re.compile(r"/fleet-runs/([^/]+)/(confirm|abort)")
_ACTIVE_SCHEDULE = re.compile(r"/active-schedules/([^/]+)")
_ACTIVE_SCHEDULE_SUB = re.compile(r"/active-schedules/([^/]+)/(approve|revoke)")
_RUN = re.compile(r"/runs/([^/]+)")
_RUN_REPORT = re.compile(r"/runs/([^/]+)/report")

# Building-record features (epic #221), each a route table (commissioning.routes).
EXTENSIONS = (spaces_api, readiness_api, packs_api, pack_extract_api, compliance_api, hunting_api, baselines_api,
              savings_api, passport_api)


def _check_prefixes(value) -> list[str]:
    """settings.prefixes: topic prefixes the project covers (#75), e.g.
    ["fbf/hospital-a"]. Narrowing only - never more than the caller sees."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value) or len(value) > 50:
        raise TypeError("settings.prefixes is a list of up to 50 topic prefixes, e.g. [\"fbf/hospital-a\"]")
    out = []
    for p in value:
        p = p.strip().strip("/")
        if not p or any(c in p for c in "#+ ") or p.startswith(("urn:", "http")):
            raise ValueError(f"settings.prefixes: {p!r} isn't a topic prefix like fbf/hospital-a")
        if p not in out:
            out.append(p)
    return out


def _check_settings(settings) -> dict:
    if settings is None:
        return {}
    if not isinstance(settings, dict):
        raise TypeError("settings must be an object")
    if "window_hours" in settings:
        engine.window_hours(settings["window_hours"])
    if "prefixes" in settings:
        settings["prefixes"] = _check_prefixes(settings["prefixes"])
    if "gl36" in settings:
        gl36.validate_settings(settings["gl36"])
    if "ladder" in settings:
        ladder.validate_settings(settings["ladder"])
    if "economizer" in settings:
        economizer.validate_settings(settings["economizer"])
    if "fault_mirroring" in settings:
        mirror.validate_settings(settings["fault_mirroring"])
    if "compliance" in settings:
        compliance.validate_settings(settings["compliance"])
    if "overrides" in settings:
        overrides.validate_settings(settings["overrides"])
    if "phase_gates" in settings:
        gates.validate_mode(settings["phase_gates"])
    if "acceptance" in settings:
        acc = settings["acceptance"]
        if not isinstance(acc, dict) or set(acc) - {"min_pct", "max_gap_minutes", "settle_minutes"} or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 for v in acc.values()) \
                or not 0 < acc.get("min_pct", 95) <= 100 or acc.get("max_gap_minutes", 5) <= 0:
            raise ValueError("acceptance is {min_pct (0-100], max_gap_minutes > 0, settle_minutes >= 0}")
    if settings.get("schedule") is not None:
        sched = settings["schedule"]
        every = sched.get("every_hours") if isinstance(sched, dict) else None
        if set(sched if isinstance(sched, dict) else {"x"}) - {"every_hours"} or isinstance(every, bool) \
                or not isinstance(every, (int, float)) or not 1 <= every <= 744:
            raise ValueError("schedule is {every_hours: 1..744} or null - a verification run that often")
    return settings


engine_kwargs = runner.engine_kwargs
log = logging.getLogger(__name__)


def make_handler(repo, clock=None, worker=None, executor=None, agent_client=None, commander=None, fleet_runner=None):
    now_fn = clock or (lambda: datetime.now(timezone.utc))
    worker = worker or runner.RunWorker(repo, now_fn)
    # Active tests (Cx 9) need a point_write route; without an executor
    # (no MQTT commander configured) planning works and confirming is 503.
    # Fleet runs (Baler#275 d) likewise need `fleet_runner`
    # (fleet.FleetRunner over a fleet.ChildExecutor).
    # `commander` carries audited operator point and schedule writes
    # (Baler#213); without one those answer 503.
    # `commander` (point_write.Commander) reads schedules and priority
    # arrays from Windrow for the override tests (Baler#212); never writes.

    class CommissioningHandler(BaseAPIHandler):
        TRACER = tracer
        ALLOWED_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        ALLOWED_HEADERS = "Content-Type, Authorization"
        REPO = repo  # for the EXTENSIONS' handlers (commissioning.routes)
        NOW = staticmethod(now_fn)
        WORKER = worker

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
            for ext in EXTENSIONS:
                found_ext = routes.match(ext.ROUTES, method, path)
                if found_ext is not None:
                    ext_name, ext_fn, groups = found_ext
                    return ext_name, lambda span, claims, fn=ext_fn, g=groups: fn(self, span, claims, *g)
            if path == "/projects":
                return {"GET": ("list_projects", self._list_projects), "POST": ("create_project", self._create_project)}.get(method)
            if path == "/templates" and method == "GET":
                return "templates", self._templates
            if path == "/equipment-status" and method == "GET":
                return "equipment_status", self._equipment_status
            if path == "/procedure-templates" and method == "GET":
                return "procedure_templates", self._procedure_templates
            if path == "/active/status" and method == "GET":
                return "active_status", self._active_status
            if path == "/active/kill" and method == "POST":
                return "active_kill", self._active_kill
            if path == "/active/library" and method == "GET":
                return "active_library", self._active_library
            m = _PROJECT_SUB.fullmatch(path)
            if m:
                pid, sub = m.group(1), m.group(2)
                table = {
                    ("PUT", "phases"): ("put_phases", self._put_phases),
                    ("POST", "phase"): ("set_phase", self._set_phase),
                    ("GET", "gate"): ("phase_gate", self._phase_gate),
                    ("GET", "report"): ("project_report", self._project_report),
                    ("GET", "events"): ("events", self._events),
                    ("GET", "inventory"): ("inventory", self._inventory),
                    ("POST", "runs"): ("create_run", self._create_run),
                    ("GET", "runs"): ("list_runs", self._list_runs),
                    ("GET", "gaps"): ("gaps", self._gaps),
                    ("GET", "issues"): ("list_issues", self._list_issues),
                    ("POST", "issues"): ("create_issue", self._create_issue),
                    ("GET", "risks"): ("list_risks", self._list_risks),
                    ("GET", "procedures"): ("list_procedures", self._list_procedures),
                    ("POST", "procedures"): ("create_procedure", self._create_procedure),
                    ("GET", "faults"): ("project_faults", self._project_faults),
                    ("POST", "faults/ingest"): ("ingest_faults", self._ingest_faults),
                    ("GET", "active-runs"): ("list_active_runs", self._list_active_runs),
                    ("POST", "active-runs"): ("plan_active_run", self._plan_active_run),
                    ("GET", "fleet-runs"): ("list_fleet_runs", self._list_fleet_runs),
                    ("POST", "fleet-runs"): ("plan_fleet_run", self._plan_fleet_run),
                    ("GET", "active-schedules"): ("list_active_schedules", self._list_active_schedules),
                    ("POST", "active-schedules"): ("propose_active_schedule", self._propose_active_schedule),
                    ("GET", "agent/sessions"): ("list_agent_sessions", self._list_agent_sessions),
                    ("POST", "agent/sessions"): ("start_agent_session", self._start_agent_session),
                    ("GET", "agent/proposals"): ("list_agent_proposals", self._list_agent_proposals),
                    ("GET", "writes"): ("list_operator_writes", self._list_operator_writes),
                    ("POST", "writes"): ("operator_point_write", self._operator_point_write),
                    ("POST", "schedule-writes"): ("operator_schedule_write", self._operator_schedule_write),
                    ("GET", "schedule-targets"): ("schedule_targets", self._schedule_targets),
                    ("GET", "schedules"): ("list_schedules", self._list_schedules),
                    ("POST", "schedules/refresh"): ("refresh_schedules", self._refresh_schedules),
                    ("GET", "coverage"): ("coverage", self._coverage),
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
                (_PROPOSAL_SUB, {("POST", "accept"): ("accept_proposal", self._accept_proposal),
                                 ("POST", "reject"): ("reject_proposal", self._reject_proposal)}),
                (_WRITE_SUB, {("POST", "relinquish"): ("relinquish_operator_write", self._relinquish_operator_write),
                              ("POST", "revert"): ("revert_operator_write", self._revert_operator_write)}),
                (_ACTIVE_RUN_SUB, {("POST", "confirm"): ("confirm_active_run", self._confirm_active_run),
                                   ("POST", "abort"): ("abort_active_run", self._abort_active_run)}),
                (_FLEET_RUN_SUB, {("POST", "confirm"): ("confirm_fleet_run", self._confirm_fleet_run),
                                  ("POST", "abort"): ("abort_fleet_run", self._abort_fleet_run)}),
                (_ACTIVE_SCHEDULE_SUB, {("POST", "approve"): ("approve_active_schedule", self._approve_active_schedule),
                                        ("POST", "revoke"): ("revoke_active_schedule", self._revoke_active_schedule)}),
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
            m = _PROJECT_SPEC.fullmatch(path)
            if m:
                stable = {("GET", "spec"): ("list_spec", self._list_spec),
                          ("POST", "spec/import"): ("import_spec", self._import_spec),
                          ("POST", "spec/extract"): ("extract_spec", self._extract_spec),
                          ("GET", "spec/documents"): ("list_spec_documents", self._list_spec_documents),
                          ("POST", "spec/review"): ("review_spec", self._review_spec),
                          ("GET", "alignment"): ("alignment", self._alignment),
                          ("POST", "alignment/decisions"): ("decide_mapping", self._decide_mapping)}
                entry = stable.get((method, m.group(2)))
                return None if entry is None else self._bind(entry, m.group(1))
            for rx, ptable in (
                (_PROCEDURE_RUNS, {"POST": ("create_test_run", self._create_test_run), "GET": ("list_test_runs", self._list_test_runs)}),
                (_PROCEDURE, {"GET": ("get_procedure", self._get_procedure), "PATCH": ("patch_procedure", self._patch_procedure),
                              "DELETE": ("archive_procedure", self._archive_procedure)}),
                (_TEST_RUN_MANUAL, {"POST": ("record_manual_result", self._record_manual_result),
                                    "GET": ("list_manual_results", self._list_manual_results)}),
                (_MANUAL_RESULT, {"PATCH": ("edit_manual_result", self._edit_manual_result)}),
                (_MANUAL_ATTACHMENT, {"GET": ("get_manual_attachment", self._get_manual_attachment)}),
                (_TEST_RUN, {"GET": ("get_test_run", self._get_test_run)}),
                (_RUN_DIFF, {"GET": ("run_diff", self._run_diff)}),
                (_SPEC_DOC, {"GET": ("get_spec_document", self._get_spec_document)}),
                (_SPEC_ITEM, {"PATCH": ("patch_spec_item", self._patch_spec_item)}),
                (_MAPPING, {"DELETE": ("undo_mapping", self._undo_mapping)}),
                (_ACTIVE_RUN, {"GET": ("get_active_run", self._get_active_run)}),
                (_FLEET_RUN, {"GET": ("get_fleet_run", self._get_fleet_run)}),
                (_ACTIVE_SCHEDULE, {"GET": ("get_active_schedule", self._get_active_schedule),
                                    "PATCH": ("patch_active_schedule", self._patch_active_schedule)}),
                (_AGENT_SESSION_MSG, {"POST": ("agent_message", self._agent_message)}),
                (_AGENT_SESSION, {"GET": ("get_agent_session", self._get_agent_session)}),
            ):
                m = rx.fullmatch(path)
                if m:
                    pentry = ptable.get(method)
                    return None if pentry is None else self._bind(pentry, m.group(1))
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
            note = body["note"][:2000].strip() if isinstance(body.get("note"), str) else ""
            gate = self._gate_for(p, key)
            gate_mode = gates.mode(p.get("settings"))
            override = body.get("override") is True
            if gate is not None and not gate["met"] and gate_mode == "block" and not override:
                respond_json(self, 409, {"error": f"the {key} gate isn't met - fix what it lists, or override with a note",
                                         "gate": gate})
                return
            if override and gate is not None and not gate["met"] and not note:
                raise ValueError("an override needs a note saying why")
            plist = phases.set_current(p["phases"], key, now_fn())
            repo.save_phases(pid, plist, now_fn())
            detail = {"from": before, "to": key}
            if note:
                detail["note"] = note
            if gate is not None:
                detail["gate"] = gates.summary(gate)
                detail["gate_mode"] = gate_mode
                if override and not gate["met"]:
                    detail["override"] = True
            self._event(pid, "phase.set", claims, detail)
            out = db.public_project(repo.get_project(pid))
            out["gate"] = gate
            respond_json(self, 200, out)

        def _gate_for(self, p: dict, key: str) -> dict | None:
            """The gate for moving project `p` into `key` (runner.phase_gate)."""
            return runner.phase_gate(repo, p, key, now_fn())

        def _phase_gate(self, span, claims, pid) -> None:
            """Preview: what moving to ?to=<key> would require, without moving."""
            p = self._project(pid, claims)
            if p is None:
                return
            key = (parse_qs(urlparse(self.path).query).get("to") or [""])[0]
            if key not in {ph["key"] for ph in p["phases"]}:
                raise ValueError("?to= names one of this project's phases")
            gate = self._gate_for(p, key)
            respond_json(self, 200, {"mode": gates.mode(p.get("settings")),
                                     "gate": gate or {"target": key, "rules": [], "met": True, "unmet": []}})

        def _events(self, span, claims, pid) -> None:
            if self._project(pid, claims) is not None:
                respond_json(self, 200, repo.list_events(pid))

        # ----------------------------------------------------- inventory/runs

        def _scopes(self, project: dict, claims: dict) -> tuple[list[Scope], list[str]]:
            return runner.project_scopes(repo, project, claims)

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
            sub = claims.get("sub")
            try:
                run = runner.queue_run(repo, worker, p, claims, now_fn(), wh, sub, repo.user_email(sub))
            except db.RunActive as exc:
                respond_json(self, 409, {"error": str(exc), "run_id": exc.run_id})
                return
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
            q = parse_qs(urlparse(self.path).query)
            fmt = q.get("format", ["json"])[0]
            if fmt not in ("json", "html"):
                raise ValueError("format must be json or html")
            origin = report.check_origin(q.get("origin", [""])[0])
            scope = repo.scope_for(claims)
            results = [r for r in repo.get_results(rid) if scope.allows(r["equip_uri"])]
            issues = [i for i in self._report_issues(project["id"]) if not i.get("equip_uri") or scope.allows(i["equip_uri"])]
            active_runs = [db.public_row(a) for a in repo.find_rows("cx_active_runs", project_id=project["id"])
                           if a["status"] not in ("planned", "cancelled") and scope.allows(a["equip_uri"])]
            rep = report.build(db.public_project(project), run, results, issues, active_runs, origin)
            if fmt == "json":
                respond_json(self, 200, rep)
                return
            self._send_html(report.html(rep).encode("utf-8"))

        def _report_issues(self, project_id: str) -> list[dict]:
            """Every issue with its status history (actors and times) and
            standing risk - the report's issues log and waivers (#87)."""
            risks = {r["issue_id"]: r for r in repo.list_risks(project_id)}
            rows = repo.list_issues(project_id)
            events = repo.list_issue_events_many([i["id"] for i in rows])  # one query, not one per issue (#150)
            out = []
            for i in rows:
                row = self._public_issue(i, risks)
                row["events"] = events[i["id"]]
                out.append(row)
            return out

        def _send_html(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # The page is the caller's data; nothing else may run in it.
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def _report_inventory(self, p: dict) -> list[dict] | None:
            """The project's inventory, read in the project's own scope (its
            org, narrowed to its site and settings.prefixes), not the
            caller's, so the same report names the same equipment whoever
            generates it (#123). Built once per report (#150): the names
            and, narrowed to what the caller may read, the spec alignment
            both come from it."""
            try:
                scopes, _ = runner.project_scopes(repo, p, {"org_id": p["org_id"]})
                return inventory.build(repo.store, scopes)
            except Exception:  # the report stands without names, and says so (equipment_names_unavailable)
                log.warning("cx report %s: inventory failed", p["id"], exc_info=True)
                return None

        @staticmethod
        def _report_equipment(equips: list[dict] | None) -> dict[str, dict] | None:
            """uri -> {dis, brickClass, points}: names and types for the
            report instead of raw URIs (#99)."""
            if equips is None:
                return None
            return {e["id"]: {"dis": e.get("dis"), "brickClass": e.get("brickClass"),
                              "points": [{**pt, "equip_uri": e["id"]} for pt in e.get("points") or []]}
                    for e in equips}

        @staticmethod
        def _caller_inventory(equips: list[dict], scopes: list) -> list[dict]:
            """The project inventory narrowed to what `scopes` allow - what
            inventory.build(store, project scopes + scopes) returns, since
            each scope only narrows and points and equipment are judged by
            their own keys (Scope.allows is Scope.sparql for them)."""
            def ok(uri):
                return all(s.allows(uri) for s in scopes)
            return [{**e, "points": [pt for pt in e.get("points") or [] if ok(pt["id"])]} for e in equips if ok(e["id"])]

        def _report_acceptance(self, p: dict, run: dict | None, equipment: dict[str, dict] | None,
                               scope) -> dict | None:
            """Per-point availability over the latest verification run's
            window, after settings.acceptance.settle_minutes (#100 review),
            for the equipment the caller may read."""
            if run is None or not equipment:
                return None
            equipment = {k: v for k, v in equipment.items() if scope.allows(k)}
            cfg = {"min_pct": 95.0, "max_gap_minutes": 5.0, "settle_minutes": 0.0,
                   **((p.get("settings") or {}).get("acceptance") or {})}
            start = run["window_start"] if isinstance(run["window_start"], datetime) else datetime.fromisoformat(run["window_start"])
            end = run["window_end"] if isinstance(run["window_end"], datetime) else datetime.fromisoformat(run["window_end"])
            points = {pt["id"]: pt for e in equipment.values() for pt in e.get("points") or []}
            judged_from = start + timedelta(minutes=float(cfg["settle_minutes"]))
            measured = repo.availability(sorted(points), judged_from, end,
                                         timedelta(minutes=float(cfg["max_gap_minutes"]))) if points and end > judged_from else {}
            return report.acceptance_availability(measured, points, start, end, float(cfg["settle_minutes"]),
                                                  float(cfg["min_pct"]), float(cfg["max_gap_minutes"]))

        def _project_report(self, span, claims, pid) -> None:
            """The Cx report for the whole project (#87), as JSON or a
            printable page: scope and limitations, phase history, spec vs
            installed, latest verdicts, procedure results, issues log."""
            p = self._project(pid, claims)
            if p is None:
                return
            q = parse_qs(urlparse(self.path).query)
            fmt = q.get("format", ["json"])[0]
            if fmt not in ("json", "html"):
                raise ValueError("format must be json or html")
            origin = report.check_origin(q.get("origin", [""])[0])

            def section(name):
                return tracer.start_as_current_span(f"commissioning_api.report.{name}")

            # One child span per section, with row counts, so a slow report
            # shows where its time goes (#150).
            with section("scope"):
                scope = repo.scope_for(claims)
            with section("reconcile_issues"):
                runner.reconcile_issues(repo, pid, None, [], now_fn())
            with section("runs") as sp:
                run = next((r for r in repo.list_runs(pid, 20) if r.get("status") == "completed"), None)
                sp.set_attribute("run", run["id"] if run else "")
            with section("inventory") as sp:
                equips = self._report_inventory(p)
                sp.set_attribute("equipment", len(equips or []))
                sp.set_attribute("points", sum(len(e.get("points") or []) for e in equips or []))
            equipment = self._report_equipment(equips)
            if equipment is not None:  # never name what the caller can't read
                equipment = {k: v for k, v in equipment.items() if scope.allows(k)}
            # Availability folds every sample of the run's window (the slow
            # part, Baler#150): in the background while the rest is read.
            parent = otel_context.get_current()

            def acceptance_section():
                with tracer.start_as_current_span("commissioning_api.report.acceptance", context=parent) as sp:
                    acc = self._report_acceptance(p, run, equipment, scope)
                    sp.set_attribute("points", len((acc or {}).get("points") or []))
                    return acc

            side = ThreadPoolExecutor(max_workers=1)
            acceptance_f = side.submit(acceptance_section)
            with section("results") as sp:
                results = [r for r in repo.get_results(run["id"]) if scope.allows(r["equip_uri"])] if run else []
                sp.set_attribute("results", len(results))
            with section("procedures") as sp:
                procs = [x for x in repo.list_procedures(pid) if x["status"] != "archived"]
                latest = repo.latest_completed_test_runs([pr["id"] for pr in procs])  # one query (#150)
                # manual results / active tests recorded since a run (Baler#275)
                latest = {k: (runner.refresh_test_run(repo, v, now_fn()) if v else None) for k, v in latest.items()}
                sp.set_attribute("procedures", len(procs))
            with section("coverage"):
                cov = [{**r, "summary": coverage.sentence(r)} for r in
                       coverage.compute(procs, latest, self._caller_inventory(equips, self._scopes(p, claims)[0]))] if equips else []
            with section("issues") as sp:
                iss = [i for i in self._report_issues(pid) if not i.get("equip_uri") or scope.allows(i["equip_uri"])]
                sp.set_attribute("issues", len(iss))
            with section("active_runs") as sp:
                active_runs = [db.public_row(a) for a in repo.find_rows("cx_active_runs", project_id=pid)
                               if a["status"] not in ("planned", "cancelled") and scope.allows(a["equip_uri"])]
                sp.set_attribute("active_runs", len(active_runs))
            with section("alignment") as sp:
                try:
                    if equips is None:
                        raise RuntimeError("no inventory")
                    caller_scopes, _ = self._scopes(p, claims)
                    alignment, _, _ = self._alignment_for(p, claims, self._caller_inventory(equips, caller_scopes))
                except Exception:  # the report stands without the spec section
                    log.warning("cx report %s: spec alignment failed", pid, exc_info=True)
                    alignment = None
                docs = [db.public_row(d) for d in repo.find_rows("cx_spec_documents", project_id=pid)]
                sp.set_attribute("spec_documents", len(docs))
            with section("events") as sp:
                events = repo.list_events(pid, 500)
                sp.set_attribute("events", len(events))
            with section("acceptance_wait"):
                try:
                    acceptance = acceptance_f.result()
                finally:
                    side.shutdown(wait=False)
            with section("build"):
                rep = report.project_build(db.public_project(p), now_fn(), repo.user_email(claims.get("sub")) or claims.get("sub"),
                                           db.public_row(run) if run else None, results, procs,
                                           {k: (db.public_row(v) if v else None) for k, v in latest.items()},
                                           iss, events, alignment, docs, active_runs, active.writes_allowed(),
                                           site=repo.get_site(p["site_id"]) if p.get("site_id") else None,
                                           equipment=equipment,
                                           acceptance=acceptance, origin=origin, coverage=cov)
            with section("render") as sp:
                if fmt == "json":
                    respond_json(self, 200, rep)
                    return
                body = report.project_html(rep).encode("utf-8")
                sp.set_attribute("bytes", len(body))
                self._send_html(body)

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
                if result["status"] not in ("fail", "inconclusive"):
                    # inconclusive opens one too: it's how a check that can't
                    # be judged here (rung 3 on read-only controllers) gets an
                    # explicit waiver - accept the issue as a risk (#96)
                    raise ValueError("only a failing or inconclusive result opens an issue")
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
            equip_uri, severity (template severities); include_inconclusive
            also opens one per inconclusive result, so each can be waived
            (accepted as a risk) for the acceptance gate (#96)."""
            hit = self._visible_run(rid, claims)
            if hit is None:
                return
            _, project = hit
            body = self.read_json_body()
            prefix = body.get("check_prefix") or ""
            equip = body.get("equip_uri")
            sev = body.get("severity")
            wanted = ("fail", "inconclusive") if body.get("include_inconclusive") is True else ("fail",)
            scope = repo.scope_for(claims)
            now = now_fn()
            opened, skipped = [], 0
            for r in repo.get_results(rid):
                if (r["status"] not in wanted or not r["check_id"].startswith(prefix) or not scope.allows(r["equip_uri"])
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
                target = body["status"]
                changes["status"] = target
                changes["closed_at"] = now if target == "closed" else None
                changes["closed_by"] = claims.get("sub") if target == "closed" else None
                if target == "closed" and issue.get("retest"):
                    rt = issue["retest"]
                    changes["closed_run"] = rt.get("test_run_id") or rt.get("run_id")
                elif target != "closed":
                    changes["closed_run"] = None
                if target == "ready_for_retest":
                    # a person claims the fix: only a retest judged after now counts
                    changes.update(resolved_at=now, resolved_by={"user_sub": claims.get("sub")}, retest=None)
                elif target in ("open", "in_progress"):
                    changes.update(resolved_at=None, resolved_by=None, retest=None)
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
            detail = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in changes.items()
                      if k not in ("resolved_by", "retest")}
            if "status" in changes:
                detail["from"] = issue["status"]
                if changes["status"] == "closed" and issue.get("retest"):
                    detail["retest"] = issue["retest"]
            if note:
                detail["note"] = note
            action = "note"
            if changes.get("status") == "closed":
                action = "close"
            elif changes.get("status") == "ready_for_retest":
                action = "fix.claimed"
            elif changes:
                action = "update"
            self._issue_event(iid, action, claims, detail)
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
            body = self.read_json_body()
            reason, review_by = issues.check_risk(body, now.date())
            sub = claims.get("sub")
            risk = {"id": db.new_id(), "issue_id": iid, "accepted_by": sub, "accepted_email": repo.user_email(sub),
                    "reason": reason, "review_by": review_by, "created_at": now, "revoked_at": None, "revoked_by": None,
                    **issues.risk_extras(body, repo.user_name(sub))}
            repo.create_risk(risk)
            repo.update_issue(iid, {"status": "risk_accepted", "updated_at": now})
            self._issue_event(iid, "risk.accept", claims, {"risk_id": risk["id"], "reason": reason, "review_by": review_by.isoformat(),
                                                          "owner": risk["owner"], "retest_trigger": risk["retest_trigger"],
                                                          "accepted_name": risk["accepted_name"], "from": issue["status"]})
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

        # ------------------------------------------------- procedures (Cx 6)

        def _procedure_templates(self, span, claims) -> None:
            respond_json(self, 200, [procedures.from_seed(t) for t in procedures.SEED_BY_ID])

        def _public_proc(self, proc: dict) -> dict:
            return db.public_row(proc)

        def _visible_procedure(self, proc_id: str, claims: dict) -> tuple[dict, dict] | None:
            proc = repo.get_procedure(proc_id)
            project = repo.get_project(proc["project_id"]) if proc else None
            if proc is None or project is None or not self._visible(project, claims):
                respond_error(self, 404, "no such procedure")
                return None
            return proc, project

        def _list_procedures(self, span, claims, pid) -> None:
            if self._project(pid, claims) is None:
                return
            archived = parse_qs(urlparse(self.path).query).get("include_archived", ["false"])[0] == "true"
            rows = [x for x in repo.list_procedures(pid) if archived or x["status"] != "archived"]
            respond_json(self, 200, [self._public_proc(x) for x in rows])

        def _create_procedure(self, span, claims, pid) -> None:
            """A procedure from the body, or from a seed template
            ({"template": "ahu_startup"}), or every seed ({"seed": "all"})."""
            p = self._project(pid, claims)
            if p is None:
                return
            body = self.read_json_body()
            now = now_fn()
            if body.get("from_spec") is True:
                specs = self._procedures_from_spec(p, claims, body.get("availability", {"min_pct": 99, "max_gap_minutes": 5}))
                if not specs:
                    raise ValueError("no accepted spec equipment with points matched in the field - import and accept "
                                     "the spec, and confirm the equipment mappings first")
            elif body.get("seed") == "all":
                have = {x.get("template") for x in repo.list_procedures(pid)}
                specs = [procedures.from_seed(t) for t in procedures.SEED_BY_ID if t not in have]
            elif body.get("template"):
                specs = [procedures.from_seed(str(body["template"]))]
                overrides = {k: body[k] for k in ("name", "equip_uris", "window_hours", "schedule", "status") if k in body}
                if overrides:
                    specs = [{**procedures.normalize(overrides, specs[0]), "template": specs[0]["template"]}]
            else:
                specs = [{**procedures.normalize(body), "template": None}]
            made = []
            for i, proc_spec in enumerate(specs):
                at = now + timedelta(microseconds=i)  # keep the seeds' order (AHU first) in listings
                proc = {"id": db.new_id(), "project_id": pid, **proc_spec, "created_by": claims.get("sub"),
                        "created_at": at, "updated_at": at}
                repo.create_procedure(proc)
                made.append(proc)
            self._event(pid, "procedure.create", claims, {"procedures": [{"id": x["id"], "name": x["name"]} for x in made]})
            out = [self._public_proc(x) for x in made]
            respond_json(self, 201, out if body.get("seed") == "all" or body.get("from_spec") is True else out[0])

        def _procedures_from_spec(self, p: dict, claims: dict, availability) -> list[dict]:
            """One PFC procedure per accepted spec equipment item that maps to
            field equipment: present, in pfc range, in band, available (#86)."""
            if availability is not None:
                procedures._criterion({"availability": availability, "point_class": "x"}, "availability")
            alignment, equips, items = self._alignment_for(p, claims)
            by_uri = {e["id"]: e for e in equips}
            by_id = {i["id"]: i for i in items}
            point_items = [i for i in items if i["kind"] == "point"]
            out = []
            for m in alignment["mappings"]:
                if not m.get("equip_uri") or m["equip_uri"] not in by_uri:
                    continue
                item = by_id[m["spec_item_id"]]
                rows = spec.points_alignment(item, by_uri[m["equip_uri"]], point_items)
                made = procedures.from_spec(by_uri[m["equip_uri"]], item, rows, {i["id"]: i for i in point_items}, availability)
                if made:
                    out.append({**procedures.normalize(made), "template": None})
            return out

        def _get_procedure(self, span, claims, proc_id) -> None:
            hit = self._visible_procedure(proc_id, claims)
            if hit is not None:
                respond_json(self, 200, self._public_proc(hit[0]))

        def _patch_procedure(self, span, claims, proc_id) -> None:
            hit = self._visible_procedure(proc_id, claims)
            if hit is None:
                return
            proc, project = hit
            body = self.read_json_body()
            spec = procedures.normalize(body, proc)
            changes = {k: v for k, v in spec.items() if proc.get(k) != v}
            if changes:
                repo.update_procedure(proc_id, {**changes, "updated_at": now_fn()})
                self._event(project["id"], "procedure.update", claims, {"procedure_id": proc_id, "fields": sorted(changes)})
            respond_json(self, 200, self._public_proc(repo.get_procedure(proc_id) or proc))

        def _archive_procedure(self, span, claims, proc_id) -> None:
            hit = self._visible_procedure(proc_id, claims)
            if hit is None:
                return
            proc, project = hit
            if proc["status"] != "archived":
                repo.update_procedure(proc_id, {"status": "archived", "updated_at": now_fn()})
                self._event(project["id"], "procedure.archive", claims, {"procedure_id": proc_id})
            self.send_response(204)
            self.end_headers()

        def _create_test_run(self, span, claims, proc_id) -> None:
            """Passive: "powered on / started at T" (started_at, default
            the procedure's window before now); judged on history from T
            over hours (default the procedure's window_hours), up to now."""
            hit = self._visible_procedure(proc_id, claims)
            if hit is None:
                return
            proc, project = hit
            if project.get("archived_at") or proc["status"] == "archived":
                respond_error(self, 409, "project or procedure is archived")
                return
            body = self.read_json_body()
            now = now_fn()
            hours = float(body.get("hours", proc["window_hours"]))
            if not 0 < hours <= 744:
                raise ValueError("hours must be in (0, 744]")
            if body.get("started_at"):
                start = datetime.fromisoformat(str(body["started_at"]).replace("Z", "+00:00"))
                if start.tzinfo is None:
                    raise ValueError("started_at needs a time zone (e.g. 2026-11-06T08:00:00Z)")
            else:
                start = now - timedelta(hours=hours)
            if start >= now:
                raise ValueError("started_at must be in the past: a passive test judges history that already exists")
            end = min(start + timedelta(hours=hours), now)
            sub = claims.get("sub")
            methods = procedures.choose_methods(proc, body.get("methods"))  # 400 before anything is queued
            try:
                tr = runner.queue_test_run(repo, worker, project, proc, claims, start, end, now, sub, repo.user_email(sub),
                                           methods=methods)
            except db.RunActive as exc:
                respond_json(self, 409, {"error": "this procedure already has a test run in progress", "test_run_id": exc.run_id})
                return
            respond_json(self, 202, db.public_row(tr))

        def _list_test_runs(self, span, claims, proc_id) -> None:
            if self._visible_procedure(proc_id, claims) is None:
                return
            rows = [{k: v for k, v in db.public_row(r).items() if k not in ("equipment", "procedure")}
                    for r in repo.list_test_runs(proc_id)]
            respond_json(self, 200, rows)

        def _get_test_run(self, span, claims, tr_id) -> None:
            tr = repo.get_test_run(tr_id)
            if tr is None or not self._visible(repo.get_project(tr["project_id"]), claims):
                respond_error(self, 404, "no such test run")
                return
            tr = runner.refresh_test_run(repo, tr, now_fn())  # manual results / active tests since it ran
            scope = repo.scope_for(claims)
            out = db.public_row(tr)
            if out.get("equipment"):
                out["equipment"] = [e for e in out["equipment"] if scope.allows(e["equip_uri"])]
            if out.get("diff"):
                out["diff"] = {**out["diff"], "changes": [c for c in out["diff"]["changes"] if scope.allows(c["equip_uri"])]}
            respond_json(self, 200, out)

        # ---------------------------------------- manual results (Baler#275)

        def _visible_test_run(self, tr_id: str, claims: dict) -> tuple[dict, dict] | None:
            tr = repo.get_test_run(tr_id)
            project = repo.get_project(tr["project_id"]) if tr else None
            if tr is None or not self._visible(project, claims):
                respond_error(self, 404, "no such test run")
                return None
            assert project is not None
            return tr, project

        def _public_manual(self, row: dict) -> dict:
            return db.public_row({**row, "history": [db.public_row(h) for h in row.get("history") or []]})

        def _record_manual_result(self, span, claims, tr_id) -> None:
            """A person's pass / fail / N-A for one step on one equipment,
            recorded as the caller (never on someone else's behalf)."""
            if claims.get("kind") != "session" or not claims.get("sub"):
                respond_error(self, 403, "a manual result is recorded by a signed-in person, not an API key")
                return
            if claims.get("role") not in ("operator", "admin"):  # the gateway's row too; checked here as well
                respond_error(self, 403, "recording a result needs the operator role")
                return
            hit = self._visible_test_run(tr_id, claims)
            if hit is None:
                return
            tr, project = hit
            if project.get("archived_at"):
                respond_error(self, 409, "project is archived")
                return
            if tr["status"] != "completed":
                respond_error(self, 409, f"test run is {tr['status']}: record manual results once it has completed")
                return
            body = self.read_json_body()
            cid = body.get("client_id")
            if isinstance(cid, str) and cid:
                dup = repo.find_rows(manual.RESULTS, order="recorded_at", project_id=project["id"], client_id=cid)
                if dup:  # an offline draft sent again: stored once
                    respond_json(self, 200, self._public_manual(dup[0]))
                    return
            scope = repo.scope_for(claims)
            judged = {e["equip_uri"] for e in tr.get("equipment") or [] if scope.allows(e["equip_uri"])}
            sub = claims.get("sub")
            now = now_fn()
            row, files = manual.new_result(body, tr, tr["procedure"], judged, claims, repo.user_email(sub), repo.user_name(sub), now)
            repo.put_rows(manual.RESULTS, [row])
            if files:
                repo.put_rows(manual.ATTACHMENTS, files)
            self._event(project["id"], "manual_result.record", claims,
                        {"manual_result_id": row["id"], "test_run_id": tr_id, "equip_uri": row["equip_uri"],
                         "step_id": row["step_id"], "status": row["status"], "witness": (row["witness"] or {}).get("name")})
            runner.refresh_test_run(repo, tr, now)
            respond_json(self, 201, self._public_manual(row))

        def _list_manual_results(self, span, claims, tr_id) -> None:
            if self._visible_test_run(tr_id, claims) is None:
                return
            scope = repo.scope_for(claims)
            respond_json(self, 200, [self._public_manual(r) for r in repo.find_rows(manual.RESULTS, order="recorded_at", test_run_id=tr_id)
                                     if scope.allows(r["equip_uri"])])

        def _edit_manual_result(self, span, claims, rid) -> None:
            row = repo.get_row(manual.RESULTS, rid)
            tr = repo.get_test_run(row["test_run_id"]) if row else None
            project = repo.get_project(row["project_id"]) if row else None
            if row is None or tr is None or not self._visible(project, claims) or not repo.scope_for(claims).allows(row["equip_uri"]):
                respond_error(self, 404, "no such manual result")
                return
            assert project is not None
            now = now_fn()
            try:
                changes, files = manual.edit(row, self.read_json_body(), claims, now)
            except manual.NotYours as exc:
                respond_error(self, 403, str(exc))
                return
            repo.update_row(manual.RESULTS, rid, changes)
            if files:
                repo.put_rows(manual.ATTACHMENTS, files)
            self._event(project["id"], "manual_result.edit", claims,
                        {"manual_result_id": rid, "from": row["status"], "to": changes.get("status", row["status"]),
                         "reason": changes["history"][-1]["reason"]})
            runner.refresh_test_run(repo, tr, now)
            respond_json(self, 200, self._public_manual(repo.get_row(manual.RESULTS, rid) or row))

        def _get_manual_attachment(self, span, claims, aid) -> None:
            att = repo.get_row(manual.ATTACHMENTS, aid)
            res = repo.get_row(manual.RESULTS, att["result_id"]) if att else None
            project = repo.get_project(res["project_id"]) if res else None
            if att is None or res is None or not self._visible(project, claims) or not repo.scope_for(claims).allows(res["equip_uri"]):
                respond_error(self, 404, "no such attachment")
                return
            data = bytes(att["data"])
            self.send_response(200)
            self.send_header("Content-Type", att["media_type"])
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="{att["filename"].replace(chr(34), "")}"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("ETag", f'"{att["sha256"]}"')
            self.end_headers()
            self.wfile.write(data)

        def _coverage(self, span, claims, pid) -> None:
            """Tested vs sampled per equipment class and method (Baler#275)."""
            p = self._project(pid, claims)
            if p is None:
                return
            scopes, _ = self._scopes(p, claims)
            procs = [x for x in repo.list_procedures(pid) if x["status"] == "active"]
            now = now_fn()
            latest = {}
            for pr in procs:
                got = next((t for t in repo.list_test_runs(pr["id"], 20) if t["status"] == "completed"), None)
                latest[pr["id"]] = runner.refresh_test_run(repo, got, now) if got else None
            rows = coverage.compute(procs, latest, inventory.build(repo.store, scopes))
            respond_json(self, 200, [{**r, "summary": coverage.sentence(r)} for r in rows])

        def _run_diff(self, span, claims, rid) -> None:
            """Reconcile a run against an earlier one (default: the
            project's previous completed run)."""
            hit = self._visible_run(rid, claims)
            if hit is None:
                return
            _, project = hit
            against = parse_qs(urlparse(self.path).query).get("against", [None])[0]
            if against:
                other = repo.get_run(against)
                if other is None or other["project_id"] != project["id"]:
                    raise ValueError("against must be another run of this project")
            else:
                runs = repo.list_runs(project["id"], 200)  # newest first
                older = runs[next((i for i, r in enumerate(runs) if r["id"] == rid), len(runs)) + 1:]
                other = next((r for r in older if r["status"] == "completed"), None)
            scope = repo.scope_for(claims)
            cur = [r for r in repo.get_results(rid) if scope.allows(r["equip_uri"])]
            prev = [r for r in repo.get_results(other["id"]) if scope.allows(r["equip_uri"])] if other else []
            respond_json(self, 200, {"run_id": rid, "against": other["id"] if other else None,
                                     **procedures.diff_results(prev, cur)})

        # ------------------------------------------------------ spec (Cx 7)

        def _list_spec(self, span, claims, pid) -> None:
            if self._project(pid, claims) is None:
                return
            q = parse_qs(urlparse(self.path).query)
            eq: dict[str, str] = {"project_id": pid}
            eq.update({k: q[k][0] for k in ("kind", "status", "document_id") if k in q})
            respond_json(self, 200, [db.public_row(r) for r in repo.find_rows("cx_spec_items", **eq)])

        def _new_document(self, pid, claims, filename, media_type, raw: bytes, method: str, status: str) -> dict:
            doc = {"id": db.new_id(), "project_id": pid, "filename": filename[:300], "media_type": media_type,
                   "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "method": method, "status": status,
                   "items": 0, "info": None, "error": None, "created_by": claims.get("sub"), "created_at": now_fn(),
                   "finished_at": None}
            repo.put_rows("cx_spec_documents", [doc])
            return doc

        def _import_spec(self, span, claims, pid) -> None:
            """A schedule or point list as CSV text or XLSX (base64)."""
            if self._project(pid, claims) is None:
                return
            body = self.read_json_body()
            fmt = body.get("format", "csv")
            data = body.get("data")
            if not isinstance(data, str) or not data:
                raise ValueError("data is required: CSV text, or base64 XLSX")
            status = body.get("status", "accepted")
            if status not in ("accepted", "proposed"):
                raise ValueError("status is accepted (a person supplied the file) or proposed")
            kind = body.get("kind", "auto")
            if kind not in ("auto", "equipment", "points"):
                raise ValueError("kind is auto, equipment or points")
            filename = str(body.get("filename") or f"import.{fmt}")
            rows = spec.read_rows(fmt, data)
            items = spec.items_from_rows(rows, filename, kind)
            raw = data.encode("utf-8")
            doc = self._new_document(pid, claims, filename, "text/csv" if fmt == "csv" else
                                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", raw, "table", "completed")
            now = now_fn()
            sub = claims.get("sub")
            reviewed = status == "accepted"
            out = [{"id": db.new_id(), "project_id": pid, "document_id": doc["id"], "status": status, "created_by": sub,
                    "created_at": now, "reviewed_by": sub if reviewed else None, "reviewed_at": now if reviewed else None, **it}
                   for it in items]
            repo.put_rows("cx_spec_items", out)
            repo.update_row("cx_spec_documents", doc["id"], {"items": len(out), "finished_at": now})
            self._event(pid, "spec.import", claims, {"document_id": doc["id"], "filename": filename, "items": len(out), "status": status})
            unclassified = sum(1 for it in out if not it.get("brick_class"))
            respond_json(self, 201, {"document_id": doc["id"], "items": len(out), "rows": len(rows),
                                     "skipped_rows": len(rows) - len(out), "unclassified": unclassified})

        def _extract_spec(self, span, claims, pid) -> None:
            """A PDF read by Claude into proposed spec items, in the
            background (202; poll GET /spec-documents/{id})."""
            if self._project(pid, claims) is None:
                return
            body = self.read_json_body()
            if body.get("media_type", "application/pdf") != "application/pdf":
                raise ValueError("only PDFs are extracted; send CSV/XLSX to .../spec/import")
            try:
                raw = base64.b64decode(str(body.get("data") or ""), validate=True)
            except ValueError as exc:
                raise ValueError("data must be the PDF as base64") from exc
            if not raw.startswith(b"%PDF"):
                raise ValueError("that isn't a PDF")
            if len(raw) > spec.MAX_PDF_BYTES:
                raise ValueError(f"PDF is over {spec.MAX_PDF_BYTES // (1024 * 1024)} MB; split it")
            method = "llm"
            if worker.extract_fn is None:
                why = llm.unavailable_reason("extract")
                if why:
                    respond_error(self, 501, why)
                    return
                method = "table" if llm.provider() == "none" else "llm"
            filename = str(body.get("filename") or "document.pdf")
            doc = self._new_document(pid, claims, filename, "application/pdf", raw, method, "queued")
            sub = claims.get("sub")
            self._event(pid, "spec.extract_start", claims, {"document_id": doc["id"], "filename": filename, "size": len(raw)})
            worker.submit(runner.ExtractJob(doc["id"], pid, filename, raw, sub, repo.user_email(sub)))
            respond_json(self, 202, db.public_row(doc))

        def _list_spec_documents(self, span, claims, pid) -> None:
            if self._project(pid, claims) is not None:
                rows = repo.find_rows("cx_spec_documents", project_id=pid)
                respond_json(self, 200, [db.public_row(r) for r in reversed(rows)])

        def _get_spec_document(self, span, claims, did) -> None:
            doc = repo.get_row("cx_spec_documents", did)
            if doc is None or not self._visible(repo.get_project(doc["project_id"]), claims):
                respond_error(self, 404, "no such document")
                return
            respond_json(self, 200, db.public_row(doc))

        def _visible_spec_item(self, iid: str, claims: dict) -> dict | None:
            item = repo.get_row("cx_spec_items", iid)
            if item is None or not self._visible(repo.get_project(item["project_id"]), claims):
                respond_error(self, 404, "no such spec item")
                return None
            return item

        _SPEC_EDITABLE = ("tag", "name", "equip_tag", "brick_class", "kind", "properties")

        def _check_spec_changes(self, body: dict) -> dict:
            changes = {k: body[k] for k in self._SPEC_EDITABLE if k in body}
            if "kind" in changes and changes["kind"] not in spec.KINDS:
                raise ValueError(f"kind is one of {', '.join(spec.KINDS)}")
            if changes.get("brick_class") is not None:
                from baler import brick_vocab

                canon = brick_vocab.load().canonical_class(str(changes["brick_class"]))
                if not canon:
                    raise ValueError(f"no Brick class named {changes['brick_class']!r}")
                changes["brick_class"] = canon
            for k in ("tag", "name"):
                if k in changes and (not isinstance(changes[k], str) or not changes[k].strip()):
                    raise ValueError(f"{k} must be a non-empty string")
            if "properties" in changes and not isinstance(changes["properties"], dict):
                raise TypeError("properties is an object")
            return changes

        def _patch_spec_item(self, span, claims, iid) -> None:
            """Accept, correct or reject a spec item. A correction keeps
            what was proposed in source.corrected_from."""
            item = self._visible_spec_item(iid, claims)
            if item is None:
                return
            body = self.read_json_body()
            changes = self._check_spec_changes(body)
            status = body.get("status")
            if status is not None and status not in spec.STATUSES:
                raise ValueError(f"status is one of {', '.join(spec.STATUSES)}")
            if not changes and status is None:
                raise ValueError("nothing to change")
            now = now_fn()
            update: dict = dict(changes)
            if changes:
                before = {k: item.get(k) for k in changes if item.get(k) != changes[k]}
                if before:
                    update["source"] = {**item["source"], "corrected_from": {**item["source"].get("corrected_from", {}), **before}}
            if status is not None:
                update["status"] = status
            update.update(reviewed_by=claims.get("sub"), reviewed_at=now)
            repo.update_row("cx_spec_items", iid, update)
            self._event(item["project_id"], "spec.review", claims, {"spec_item_id": iid, "status": status, "corrected": sorted(changes)})
            respond_json(self, 200, db.public_row(repo.get_row("cx_spec_items", iid) or item))

        def _review_spec(self, span, claims, pid) -> None:
            """Accept or reject many proposals at once ({ids, status})."""
            if self._project(pid, claims) is None:
                return
            body = self.read_json_body()
            ids, status = body.get("ids"), body.get("status")
            if status not in ("accepted", "rejected") or not isinstance(ids, list) or not ids or len(ids) > 5000:
                raise ValueError("send ids (a list) and status accepted or rejected")
            now = now_fn()
            done = 0
            for iid in ids:
                item = repo.get_row("cx_spec_items", str(iid))
                if item is None or item["project_id"] != pid:
                    continue
                repo.update_row("cx_spec_items", item["id"], {"status": status, "reviewed_by": claims.get("sub"), "reviewed_at": now})
                done += 1
            self._event(pid, "spec.review", claims, {"status": status, "count": done})
            respond_json(self, 200, {"updated": done})

        def _alignment_for(self, p: dict, claims: dict, equips: list[dict] | None = None) -> tuple[dict, list[dict], list[dict]]:
            """`equips`: the caller-scoped inventory, when the caller has it."""
            if equips is None:
                scopes, _ = self._scopes(p, claims)
                equips = inventory.build(repo.store, scopes)
            items = repo.find_rows("cx_spec_items", project_id=p["id"])
            decisions = [d for d in repo.find_rows("cx_mappings", project_id=p["id"]) if d["status"] in ("confirmed", "rejected")]
            latest = {d["spec_item_id"]: d for d in decisions}  # oldest first, so the newest wins
            return spec.align(items, equips, phases.current(p["phases"]), latest), equips, items

        def _alignment(self, span, claims, pid) -> None:
            """Accepted spec equipment against today's inventory, computed
            now; a person's decisions (confirm/reject) pin it."""
            p = self._project(pid, claims)
            if p is None:
                return
            result, equips, items = self._alignment_for(p, claims)
            by_id = {e["id"]: e for e in equips}
            points = [i for i in items if i["kind"] == "point"]
            decisions = {d["spec_item_id"]: d for d in repo.find_rows("cx_mappings", project_id=pid)}
            spec_by_id = {i["id"]: i for i in items}
            for m in result["mappings"]:
                d = decisions.get(m["spec_item_id"])
                m["decision"] = db.public_row(d) if d else None
                if m["equip_uri"] in by_id and m["confidence"] in ("confirmed", "high"):
                    m["points"] = spec.points_alignment(spec_by_id[m["spec_item_id"]], by_id[m["equip_uri"]], points)
            counts: dict[str, int] = {}
            for m in result["mappings"]:
                counts[m["confidence"]] = counts.get(m["confidence"], 0) + 1
            respond_json(self, 200, {**result, "counts": counts,
                                     "proposed_items": sum(1 for i in items if i["status"] == "proposed")})

        def _decide_mapping(self, span, claims, pid) -> None:
            """Confirm or reject a spec item's mapping. Confirming with
            write_back records it in the graph through record_edit (the
            spec tag as cxSpecTag, and the spec's Brick class when the
            equipment has none, or with write_class), audited in
            record_edits."""
            p = self._project(pid, claims)
            if p is None:
                return
            body = self.read_json_body()
            item = repo.get_row("cx_spec_items", str(body.get("spec_item_id")))
            if item is None or item["project_id"] != pid or item["kind"] != "equipment":
                raise ValueError("spec_item_id must be an equipment spec item of this project")
            status = body.get("status")
            if status not in ("confirmed", "rejected"):
                raise ValueError("status is confirmed or rejected")
            uri = body.get("equip_uri")
            scopes, _ = self._scopes(p, claims)
            equips = {e["id"]: e for e in inventory.build(repo.store, scopes)}
            if uri not in equips:
                raise ValueError("equip_uri must be equipment in this project's inventory")
            now = now_fn()
            sub = claims.get("sub")
            written = None
            if status == "confirmed" and body.get("write_back"):
                edit: dict = {"set": {"cxSpecTag": item["tag"]}}
                have = equips[uri].get("brickClasses") or []
                if item.get("brick_class") and (not have or body.get("write_class")) and item["brick_class"] not in have:
                    edit["brickClass"] = item["brick_class"]
                written = repo.write_back(uri, edit, sub, repo.user_email(sub))
            m = {"id": db.new_id(), "project_id": pid, "spec_item_id": item["id"], "equip_uri": uri,
                 "confidence": "confirmed" if status == "confirmed" else "rejected", "basis": [{"kind": "person"}],
                 "alternatives": [], "status": status, "decided_by": sub, "decided_at": now, "written_back": written,
                 "created_at": now}
            repo.delete_rows("cx_mappings", spec_item_id=item["id"], equip_uri=uri)
            if status == "confirmed":  # one confirmed equipment per spec item
                for old in repo.find_rows("cx_mappings", spec_item_id=item["id"], status="confirmed"):
                    repo.delete_rows("cx_mappings", id=old["id"])
            repo.put_rows("cx_mappings", [m])
            self._event(pid, f"mapping.{status}", claims, {"spec_tag": item["tag"], "equip_uri": uri, "written_back": written})
            respond_json(self, 201, db.public_row(m))

        def _undo_mapping(self, span, claims, mid) -> None:
            m = repo.get_row("cx_mappings", mid)
            if m is None or self._project(m["project_id"], claims) is None:
                if m is None:
                    respond_error(self, 404, "no such mapping")
                return
            repo.delete_rows("cx_mappings", id=mid)
            self._event(m["project_id"], "mapping.undo", claims, {"spec_item_id": m["spec_item_id"], "equip_uri": m["equip_uri"],
                                                                  "note": "a graph write-back, if any, stays (undo it in the record editor)"})
            self.send_response(204)
            self.end_headers()

        # ----------------------------------------------------- faults (Cx 8)

        def _project_faults(self, span, claims, pid) -> None:
            """Open faults this project mirrored (rule ids cx:<check>)."""
            if self._project(pid, claims) is None:
                return
            scope = repo.scope_for(claims)
            keys = set(repo.open_mirrored_faults(pid))
            rows = [db.public_row(f) for f in repo.list_faults(True) if (f["rule_id"], f["point_uri"]) in keys
                    and scope.allows(f["point_uri"])]
            respond_json(self, 200, rows)

        def _ingest_faults(self, span, claims, pid) -> None:
            """Faults into the issues log. {"source": "fault_detector"}
            pulls the open faults on this project's equipment from the
            shared faults table (not cx: mirrors), and closes issues whose
            fault has since resolved. {"source": "<tool>", "items": [...]}
            takes an outside FDD tool's faults ({id, point_uri or
            equip_uri, title, message, severity, started_at})."""
            p = self._project(pid, claims)
            if p is None:
                return
            body = self.read_json_body()
            source = str(body.get("source") or "")
            if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,40}", source):
                raise ValueError("source names the fault source: fault_detector, or your FDD tool (lowercase)")
            scopes, _ = self._scopes(p, claims)
            equips = inventory.build(repo.store, scopes)
            equip_of = {pt["id"]: e["id"] for e in equips for pt in e["points"]}
            equip_ids = {e["id"] for e in equips}
            now = now_fn()
            tag = f"fdd:{source}"
            if source == "fault_detector":
                if body.get("items") is not None:
                    raise ValueError("fault_detector faults are read from the faults table; send no items")
                items = [f for f in repo.list_faults(True) if not f["rule_id"].startswith(mirror.RULE_PREFIX)]
            else:
                items = body.get("items")
                if not isinstance(items, list) or len(items) > 5000:
                    raise ValueError("items is a list of at most 5000 faults")
                norm = []
                for i, it in enumerate(items):
                    if not isinstance(it, dict) or not it.get("id"):
                        raise ValueError(f"item {i + 1} needs an id")
                    uri = it.get("point_uri") or it.get("equip_uri")
                    if not isinstance(uri, str):
                        raise TypeError(f"item {i + 1} needs a point_uri or equip_uri")
                    norm.append({"id": str(it["id"])[:200], "rule_id": str(it.get("rule") or it.get("rule_id") or "")[:200],
                                 "point_uri": uri, "severity": it.get("severity"), "started_at": it.get("started_at"),
                                 "title": it.get("title"), "detail": {"message": it.get("message")}})
                items = norm
            created = updated = 0
            outside = 0
            for f in items:
                uri = f["point_uri"]
                equip = equip_of.get(uri) or (uri if uri in equip_ids else None)
                if equip is None:
                    outside += 1  # not this project's equipment (or not in the caller's scope)
                    continue
                prior = repo.find_issue_by_source(pid, tag, str(f["id"]))
                if prior is None:
                    issue = mirror.issue_from_fault(f, equip, tag, pid, db.new_id, now, claims.get("sub"))
                    repo.create_issue(issue)
                    self._issue_event(issue["id"], "import", claims, {"source": tag, "source_id": str(f["id"])})
                    created += 1
                elif prior["status"] == "closed":
                    repo.update_issue(prior["id"], {"status": "open", "closed_at": None, "updated_at": now})
                    self._issue_event(prior["id"], "fault.reopened", claims, {"source": tag})
                    updated += 1
            resolved = 0
            if source == "fault_detector":
                active = [i for i in repo.list_issues(pid, ("open", "in_progress", "ready_for_retest")) if i["source"] == tag]
                if active:
                    still = {str(f["id"]) for f in items}
                    gone = [i for i in active if i["source_id"] not in still]
                    ended = {str(f["id"]): f for f in repo.list_faults(False, [i["source_id"] for i in gone])}
                    for i in gone:
                        f = ended.get(i["source_id"])
                        if f is not None and f.get("ended_at") is not None:
                            repo.update_issue(i["id"], {"status": "closed", "closed_at": now, "updated_at": now})
                            self._issue_event(i["id"], "fault.resolved", claims, {"source": tag, "fault_id": i["source_id"]})
                            resolved += 1
            self._event(pid, "faults.ingest", claims, {"source": tag, "created": created, "reopened": updated, "resolved": resolved,
                                                       "outside_project": outside})
            respond_json(self, 200, {"source": tag, "created": created, "reopened": updated, "resolved": resolved,
                                     "outside_project": outside})

        # ------------------------------------------------ active tests (Cx 9)

        def _require_admin(self, claims: dict) -> bool:
            """The gateway already requires admin on these routes; the
            backend checks the verified token too (defense in depth)."""
            if claims.get("role") != "admin":
                respond_error(self, 403, "active tests are admin only")
                return False
            return True

        def _active_status(self, span, claims) -> None:
            running = [r for p in repo.list_projects(claims["org_id"]) if self._visible(p, claims)
                       for r in repo.find_rows("cx_active_runs", project_id=p["id"], status="running")]
            respond_json(self, 200, {"writes_allowed": active.writes_allowed(), "executor": executor is not None,
                                     "level": active.CX_LEVEL, "running": [r["id"] for r in running]})

        def _visible_active(self, rid: str, claims: dict) -> tuple[dict, dict] | None:
            run = repo.get_row("cx_active_runs", rid)
            project = repo.get_project(run["project_id"]) if run else None
            if run is None or project is None or not self._visible(project, claims) or not repo.scope_for(claims).allows(run["equip_uri"]):
                respond_error(self, 404, "no such active run")
                return None
            return run, project

        def _list_active_runs(self, span, claims, pid) -> None:
            if self._project(pid, claims) is None:
                return
            scope = repo.scope_for(claims)
            rows = [db.public_row(r) for r in reversed(repo.find_rows("cx_active_runs", project_id=pid)) if scope.allows(r["equip_uri"])]
            respond_json(self, 200, rows)

        def _get_active_run(self, span, claims, rid) -> None:
            hit = self._visible_active(rid, claims)
            if hit is not None:
                respond_json(self, 200, db.public_row(hit[0]))

        def _plan_active_run(self, span, claims, pid) -> None:
            """The dry run: every write listed, nothing commanded."""
            if not self._require_admin(claims):
                return
            p = self._project(pid, claims)
            if p is None:
                return
            if p.get("archived_at"):
                respond_error(self, 409, "project is archived")
                return
            body = self.read_json_body()
            scopes, _ = self._scopes(p, claims)
            equips = {e["id"]: e for e in inventory.build(repo.store, scopes)}
            equip = equips.get(body.get("equip_uri"))
            if equip is None:
                raise ValueError("equip_uri must be equipment in this project's inventory")
            try:
                start = datetime.fromisoformat(str(body.get("window_start")).replace("Z", "+00:00"))
                end = datetime.fromisoformat(str(body.get("window_end")).replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("window_start and window_end are ISO date-times with a time zone") from exc
            active.check_window(start, end, now_fn())
            kind = body.get("kind", "command_response")
            if kind == "command_response":
                rm = body.get("response_minutes", 2)
                if isinstance(rm, bool) or not isinstance(rm, (int, float)) or not 0 < rm <= active.MAX_HOLD_MINUTES:
                    raise ValueError(f"response_minutes must be in (0, {active.MAX_HOLD_MINUTES}]")
                steps = active.command_response_steps(equip, float(rm))
                if not steps:
                    raise ValueError("no command/status pair on this equipment to test")
                steps = active.validate_steps(steps, equip)
            elif kind == "custom":
                steps = active.validate_steps(body.get("steps"), equip)
            elif kind == "library":
                # a standard test (Baler#275 slice c), compiled to concrete steps
                # against the latest values, then validated like any other
                test = body.get("test")
                if not test_library.applies(str(test), equip):
                    raise ValueError(f"test must be a library test for this equipment's class (library: {', '.join(test_library.BY_ID)})")
                with repo.history() as h:
                    latest = h.latest([p_["id"] for p_ in equip["points"]])
                steps = active.validate_steps(test_library.compile_steps(test, equip, latest), equip)
                kind = f"library:{test}"
            else:
                raise ValueError("kind is command_response, custom or library")
            run = active.new_run(pid, equip["id"], kind, steps, start, end, claims.get("sub"), now_fn())
            repo.put_rows("cx_active_runs", [run])
            self._event(pid, "active_run.plan", claims, {"active_run_id": run["id"], "equip_uri": equip["id"], "writes": len(run["plan"])})
            respond_json(self, 201, {**db.public_row(run), "writes_allowed": active.writes_allowed()})

        def _confirm_active_run(self, span, claims, rid) -> None:
            """Start it: the switch must be on, the run planned, now inside
            its window, and the body must repeat the run's id."""
            if not self._require_admin(claims):
                return
            # The deployment switch answers first: with writes off, nothing
            # about the request matters (planning, a dry run, stays allowed).
            if not active.writes_allowed():
                respond_error(self, 403, "active tests are switched off for this deployment (BALER_CX_ALLOW_WRITES=1 enables them)")
                return
            hit = self._visible_active(rid, claims)
            if hit is None:
                return
            run, project = hit
            body = self.read_json_body()
            if body.get("confirm") != rid:
                raise ValueError("confirm by sending {\"confirm\": \"<this run's id>\"} after reading the plan")
            if executor is None:
                respond_error(self, 503, "no point_write route is configured on this server")
                return
            if run["status"] != "planned":
                respond_error(self, 409, f"run is {run['status']}")
                return
            now = now_fn()
            ws = run["window_start"] if isinstance(run["window_start"], datetime) else datetime.fromisoformat(run["window_start"])
            we = run["window_end"] if isinstance(run["window_end"], datetime) else datetime.fromisoformat(run["window_end"])
            if not ws <= now < we:
                respond_error(self, 409, f"outside the scheduled window ({ws.isoformat()} - {we.isoformat()})")
                return
            points = sorted({w["point"] for st in run["steps"] for w in st["writes"]} | {e["point"] for st in run["steps"] for e in st["expect"]})
            with repo.history() as h:
                latest = h.latest(points)
            written = sorted({w["point"] for st in run["steps"] for w in st["writes"]})
            before = {"latest": {u: {"ts": ts.isoformat(), "value": v if isinstance(v, (int, float, str, bool)) else str(v)}
                                 for u, (ts, v) in latest.items()},
                      "priority_arrays": {u: repo.point_levels(u) for u in written}, "captured_at": now.isoformat()}
            repo.update_row("cx_active_runs", rid, {"status": "confirmed", "confirmed_by": claims.get("sub"), "confirmed_at": now,
                                                    "before": before})
            self._event(project["id"], "active_run.confirm", claims, {"active_run_id": rid, "writes": len(run["plan"])})
            executor.start(rid)
            respond_json(self, 202, db.public_row(repo.get_row("cx_active_runs", rid) or run))

        def _abort_active_run(self, span, claims, rid) -> None:
            """The kill switch for one run: a running run stops at its next
            poll and relinquishes; a planned one is cancelled."""
            hit = self._visible_active(rid, claims)
            if hit is None:
                return
            run, project = hit
            if run["status"] == "planned":
                repo.update_row("cx_active_runs", rid, {"status": "cancelled", "finished_at": now_fn()})
            elif run["status"] in ("confirmed", "running"):
                repo.update_row("cx_active_runs", rid, {"abort_requested": True})
            self._event(project["id"], "active_run.abort", claims, {"active_run_id": rid, "was": run["status"]})
            respond_json(self, 202, db.public_row(repo.get_row("cx_active_runs", rid) or run))

        def _active_library(self, span, claims) -> None:
            """The standard active tests, with their limits and expected
            results (Baler#275 slice c)."""
            respond_json(self, 200, [test_library.public(t) for t in test_library.TESTS])

        def _active_kill(self, span, claims) -> None:
            """Every running active run in the caller's org stops, and
            every level they hold is relinquished now (admin)."""
            if not self._require_admin(claims):
                return
            stopped, fleets = [], []
            for p in repo.list_projects(claims["org_id"], include_archived=True):
                if not self._visible(p, claims):
                    continue
                # Fleets first (Baler#275 d), so no coordinator starts another
                # member after its live children are flagged below; a member
                # started in between still stops at its first check
                # (fleet.ChildExecutor) and is relinquished with the rest.
                for f in repo.find_rows(fleet.TABLE, project_id=p["id"]):
                    if f["status"] in ("confirmed", "running"):
                        fleet.flag_stop(repo, None, f["id"], "kill switch")
                        fleets.append(f["id"])
                        self._event(p["id"], "fleet_run.kill", claims, {"fleet_run_id": f["id"]})
                for r in repo.find_rows("cx_active_runs", project_id=p["id"]):
                    if r["status"] in ("confirmed", "running"):
                        repo.update_row("cx_active_runs", r["id"], {"abort_requested": True})
                        stopped.append(r["id"])
                        self._event(p["id"], "active_run.kill", claims, {"active_run_id": r["id"]})
            relinquished = []
            if executor is not None:
                mine = set(stopped)
                for point, level, owner, _ in repo.cx_levels():
                    if owner[len(active.WHO_PREFIX):] in mine:
                        try:
                            executor.writer.relinquish(point, owner[len(active.WHO_PREFIX):], level)
                            relinquished.append(point)
                        except Exception as exc:  # noqa: BLE001 - reported; the sweeper retries
                            relinquished.append(f"FAILED {point}: {exc}")
            if fleet_runner is not None:
                for fid in fleets:
                    relinquished += fleet.relinquish_children(repo, fleet_runner.writer, repo.get_row(fleet.TABLE, fid) or {"id": fid, "members": []})
            respond_json(self, 200, {"aborted": stopped, "fleets": fleets, "relinquished": relinquished})

        # ------------------------------------------ fleet runs (Baler#275 d)

        def _visible_fleet(self, fid: str, claims: dict) -> tuple[dict, dict] | None:
            f = repo.get_row(fleet.TABLE, fid)
            project = repo.get_project(f["project_id"]) if f else None
            if f is None or project is None or not self._visible(project, claims) or \
                    not all(repo.scope_for(claims).allows(m["equip_uri"]) for m in f["members"]):
                respond_error(self, 404, "no such fleet run")
                return None
            return f, project

        def _list_fleet_runs(self, span, claims, pid) -> None:
            if self._project(pid, claims) is None:
                return
            scope = repo.scope_for(claims)
            rows = [fleet.public(f) for f in reversed(repo.find_rows(fleet.TABLE, project_id=pid))
                    if all(scope.allows(m["equip_uri"]) for m in f["members"])]
            respond_json(self, 200, rows)

        def _get_fleet_run(self, span, claims, fid) -> None:
            hit = self._visible_fleet(fid, claims)
            if hit is not None:
                respond_json(self, 200, fleet.public(hit[0]))

        def _plan_fleet_run(self, span, claims, pid) -> None:
            """The fleet dry run: every write across every member, the
            schedule, and the plan hash to confirm with. Nothing commanded."""
            if not self._require_admin(claims):
                return
            p = self._project(pid, claims)
            if p is None:
                return
            if p.get("archived_at"):
                respond_error(self, 409, "project is archived")
                return
            body = self.read_json_body()
            previous = None
            if body.get("retest_of") is not None:
                hit = self._visible_fleet(str(body["retest_of"]), claims)
                if hit is None:
                    return
                previous = hit[0]
                if previous["project_id"] != pid:
                    raise ValueError("retest_of must be a fleet run of this project")
            scopes, _ = self._scopes(p, claims)
            equips = inventory.build(repo.store, scopes)
            f = fleet.plan(repo, p, equips, body, claims.get("sub"), now_fn(), previous)
            repo.put_rows(fleet.TABLE, [f])
            span.set_attribute("members", len(f["members"]))
            self._event(pid, "fleet_run.plan", claims, {"fleet_run_id": f["id"], "members": len(f["members"]),
                                                        "writes": len(f["plan"]), "plan_hash": f["plan_hash"],
                                                        "retest_of": f["retest_of"]})
            respond_json(self, 201, {**fleet.public(f), "writes_allowed": active.writes_allowed()})

        def _confirm_fleet_run(self, span, claims, fid) -> None:
            """Start a planned fleet: the same envelope as one run (admin,
            the switch, inside its window), and the body repeats the plan
            hash - which must still be the hash of the stored plan, every
            member of which must still validate against the inventory."""
            if not self._require_admin(claims):
                return
            if not active.writes_allowed():
                respond_error(self, 403, "active tests are switched off for this deployment (BALER_CX_ALLOW_WRITES=1 enables them)")
                return
            hit = self._visible_fleet(fid, claims)
            if hit is None:
                return
            f, project = hit
            body = self.read_json_body()
            if body.get("confirm") != f["plan_hash"]:
                raise ValueError("confirm by sending {\"confirm\": \"<this fleet's plan_hash>\"} after reading the plan")
            if fleet_runner is None:
                respond_error(self, 503, "no point_write route is configured on this server")
                return
            if f["status"] != "planned":
                respond_error(self, 409, f"fleet run is {f['status']}")
                return
            now = now_fn()
            ws, we = fleet._dt(f["window_start"]), fleet._dt(f["window_end"])
            if not ws <= now < we:
                respond_error(self, 409, f"outside the scheduled window ({ws.isoformat()} - {we.isoformat()})")
                return
            scopes, _ = self._scopes(project, claims)
            problems = fleet.verify(f, inventory.build(repo.store, scopes))
            if problems:
                respond_error(self, 409, "the plan changed since it was listed - plan it again: " + "; ".join(problems[:5]))
                return
            repo.update_row(fleet.TABLE, fid, {"status": "confirmed", "confirmed_by": claims.get("sub"), "confirmed_at": now,
                                                "heartbeat_at": now})
            self._event(project["id"], "fleet_run.confirm", claims, {"fleet_run_id": fid, "plan_hash": f["plan_hash"],
                                                                     "members": len(f["members"]), "writes": len(f["plan"])})
            fleet_runner.start(fid)
            respond_json(self, 202, fleet.public(repo.get_row(fleet.TABLE, fid) or f))

        def _abort_fleet_run(self, span, claims, fid) -> None:
            """Stop a fleet (operator, the safe direction): no member starts
            again, every live member stops at its next check, and every level
            the fleet's members hold is relinquished now. A planned fleet is
            cancelled."""
            hit = self._visible_fleet(fid, claims)
            if hit is None:
                return
            f, project = hit
            relinquished: list[str] = []
            if f["status"] == "planned":
                repo.update_row(fleet.TABLE, fid, {"status": "cancelled", "finished_at": now_fn()})
            elif f["status"] in ("confirmed", "running"):
                relinquished = fleet.flag_stop(repo, fleet_runner.writer if fleet_runner else None, fid,
                                               f"aborted by {claims.get('sub')}")
            self._event(project["id"], "fleet_run.abort", claims, {"fleet_run_id": fid, "was": f["status"],
                                                                   "relinquished": len(relinquished)})
            respond_json(self, 202, fleet.public(repo.get_row(fleet.TABLE, fid) or f))

        # ------------------------------- scheduled active retests (Baler#275 e)

        def _visible_schedule(self, sid: str, claims: dict) -> tuple[dict, dict] | None:
            s = repo.get_row(retest_schedules.TABLE, sid)
            project = repo.get_project(s["project_id"]) if s else None
            if s is None or project is None or not self._visible(project, claims) or \
                    not all(repo.scope_for(claims).allows(u) for u in s["scope"]["equip_uris"]):
                respond_error(self, 404, "no such schedule")
                return None
            return s, project

        def _list_active_schedules(self, span, claims, pid) -> None:
            if self._project(pid, claims) is None:
                return
            scope = repo.scope_for(claims)
            respond_json(self, 200, [retest_schedules.public(s, now_fn())
                                     for s in reversed(repo.find_rows(retest_schedules.TABLE, project_id=pid))
                                     if all(scope.allows(u) for u in s["scope"]["equip_uris"])])

        def _get_active_schedule(self, span, claims, sid) -> None:
            hit = self._visible_schedule(sid, claims)
            if hit is not None:
                respond_json(self, 200, retest_schedules.public(hit[0], now_fn()))

        def _propose_active_schedule(self, span, claims, pid) -> None:
            """Propose a repeating fleet retest: its scope and windows, with
            a dry run of the next window. Nothing runs until an admin
            approves it, and then only with BALER_CX_ALLOW_WRITES on."""
            if not self._require_admin(claims):
                return
            p = self._project(pid, claims)
            if p is None:
                return
            if p.get("archived_at"):
                respond_error(self, 409, "project is archived")
                return
            body = self.read_json_body()
            now = now_fn()
            scopes, _ = self._scopes(p, claims)
            equips = inventory.build(repo.store, scopes)
            scope, timing = retest_schedules.parse(body, equips, now)
            s = retest_schedules.new(pid, str(body.get("name") or ""), scope, timing, claims.get("sub"), now)
            nxt = retest_schedules.next_window(timing, now)
            if nxt is None:
                raise ValueError("no scheduled window starts before valid_until")
            ws = datetime.fromisoformat(nxt)
            we = min(ws + timedelta(minutes=timing["window_minutes"]), datetime.fromisoformat(timing["valid_until"]))
            preview = fleet.plan(repo, p, equips, {"equip_uris": scope["equip_uris"], "window_start": ws.isoformat(),
                                                   "window_end": we.isoformat(), "kind": scope["kind"], "test": scope["test"],
                                                   **{k: scope[k] for k in ("response_minutes", "concurrency_per_system", "max_concurrent",
                                                                            "stagger_seconds", "max_failures")}},
                                 claims.get("sub"), now)
            repo.put_rows(retest_schedules.TABLE, [s])
            self._event(pid, "active_schedule.propose", claims, {"schedule_id": s["id"], "approval_hash": s["approval_hash"],
                                                                 "equipment": len(scope["equip_uris"]), "timing": timing})
            respond_json(self, 201, {**retest_schedules.public(s, now),
                                     "preview": {"window_start": ws.isoformat(), "window_end": we.isoformat(),
                                                 "members": len(preview["members"]), "skipped": preview["skipped"],
                                                 "plan": preview["plan"]},
                                     "writes_allowed": active.writes_allowed()})

        def _approve_active_schedule(self, span, claims, sid) -> None:
            """An admin - a signed-in person - approves exactly the proposed
            scope and windows by typing the approval hash. Approving never
            turns writes on."""
            if not self._require_admin(claims):
                return
            if claims.get("kind") != "session" or not claims.get("sub"):
                respond_error(self, 403, "a schedule is approved by a signed-in person, not an API key")
                return
            hit = self._visible_schedule(sid, claims)
            if hit is None:
                return
            s, project = hit
            body = self.read_json_body()
            if body.get("confirm") != s["approval_hash"]:
                raise ValueError("approve by sending {\"confirm\": \"<this schedule's approval_hash>\"} after reading it")
            if s["status"] != "proposed":
                respond_error(self, 409, f"schedule is {s['status']}")
                return
            now = now_fn()
            if s["approval_hash"] != retest_schedules.approval_hash(s):
                respond_error(self, 409, "the stored schedule no longer matches its approval hash - propose it again")
                return
            if retest_schedules._dt(s["timing"]["valid_until"]) <= now:
                respond_error(self, 409, "valid_until has already passed")
                return
            envelope = {"scope": s["scope"], "timing": s["timing"], "hash": s["approval_hash"]}
            repo.update_row(retest_schedules.TABLE, sid, {
                "status": "approved", "approval": envelope, "approved_by": claims.get("sub"), "approved_at": now,
                "approver_claims": {"org_id": claims.get("org_id"), "site_ids": list(claims.get("site_ids") or [])},
                "updated_at": now})
            self._event(project["id"], "active_schedule.approve", claims, {"schedule_id": sid, "approval_hash": s["approval_hash"],
                                                                           "scope": s["scope"], "timing": s["timing"]})
            respond_json(self, 200, retest_schedules.public(repo.get_row(retest_schedules.TABLE, sid) or s, now))

        def _patch_active_schedule(self, span, claims, sid) -> None:
            """Narrow a schedule: never wider than what was approved."""
            if not self._require_admin(claims):
                return
            hit = self._visible_schedule(sid, claims)
            if hit is None:
                return
            s, project = hit
            body = self.read_json_body()
            now = now_fn()
            try:
                changes = retest_schedules.patch(s, body, now)
            except PermissionError as exc:
                respond_error(self, 409, str(exc))
                return
            repo.update_row(retest_schedules.TABLE, sid, changes)
            self._event(project["id"], "active_schedule.edit", claims, {"schedule_id": sid, "changed": sorted(body)})
            respond_json(self, 200, retest_schedules.public(repo.get_row(retest_schedules.TABLE, sid) or s, now))

        def _revoke_active_schedule(self, span, claims, sid) -> None:
            """Stop scheduling (operator, the safe direction); a fleet the
            schedule has running is stopped and relinquished too."""
            hit = self._visible_schedule(sid, claims)
            if hit is None:
                return
            s, project = hit
            now = now_fn()
            if s["status"] in ("proposed", "approved"):
                repo.update_row(retest_schedules.TABLE, sid, {"status": "revoked", "revoked_by": claims.get("sub"), "revoked_at": now,
                                                              "updated_at": now})
            stopped = None
            last = repo.get_row(fleet.TABLE, s["last_fleet_id"]) if s.get("last_fleet_id") else None
            if last is not None and last["status"] in ("confirmed", "running"):
                fleet.flag_stop(repo, fleet_runner.writer if fleet_runner else None, last["id"], "its schedule was revoked")
                stopped = last["id"]
            self._event(project["id"], "active_schedule.revoke", claims, {"schedule_id": sid, "was": s["status"],
                                                                          "stopped_fleet_run_id": stopped})
            respond_json(self, 200, retest_schedules.public(repo.get_row(retest_schedules.TABLE, sid) or s, now))

        # ------------------------------------------ operator writes (Baler#213)

        def _require_person(self, claims: dict, role: str) -> bool:
            """A person's session at `role` or above: an API key can't make
            an operator write, and nobody writes on someone else's behalf."""
            rank = {"viewer": 1, "operator": 2, "admin": 3}
            if claims.get("kind") != "session" or not claims.get("sub"):
                respond_error(self, 403, "operator writes are made by a signed-in person, not an API key")
                return False
            if rank.get(str(claims.get("role")), 0) < rank[role]:
                respond_error(self, 403, f"this write needs the {role} role")
                return False
            return True

        def _writes_project(self, pid: str, claims: dict) -> dict | None:
            p = self._project(pid, claims)
            if p is None:
                return None
            if p.get("archived_at"):
                respond_error(self, 409, "project is archived")
                return None
            if commander is None:
                respond_error(self, 503, "no Windrow command channel is configured on this server")
                return None
            return p

        def _list_operator_writes(self, span, claims, pid) -> None:
            """Every operator write on the project, newest first, and with
            ?point= Baler's priority array for that point."""
            if self._project(pid, claims) is None:
                return
            scope = repo.scope_for(claims)
            rows = [db.public_row(r) for r in reversed(repo.find_rows(operator_writes.TABLE, project_id=pid))
                    if r["kind"] != "point" or scope.allows(r["target"])]
            out: dict = {"writes": rows, "levels": list(operator_writes.LEVELS),
                         "max_hold_minutes": operator_writes.MAX_HOLD.total_seconds() / 60}
            point = parse_qs(urlparse(self.path).query).get("point", [None])[0]
            if point:
                if not scope.allows(point):
                    respond_error(self, 404, "no such point")
                    return
                out["priority_array"] = repo.point_levels(point)
            respond_json(self, 200, out)

        def _visible_write(self, wid: str, claims: dict) -> tuple[dict, dict] | None:
            row = repo.get_row(operator_writes.TABLE, wid)
            project = repo.get_project(row["project_id"]) if row else None
            if row is None or project is None or not self._visible(project, claims) or (
                    row["kind"] == "point" and not repo.scope_for(claims).allows(row["target"])):
                respond_error(self, 404, "no such write")
                return None
            return row, project

        def _operator_point_write(self, span, claims, pid) -> None:
            if not self._require_person(claims, "operator"):
                return
            p = self._writes_project(pid, claims)
            if p is None:
                return
            body = self.read_json_body()
            scopes, _ = self._scopes(p, claims)
            now = now_fn()
            req = operator_writes.point_write(body, inventory.build(repo.store, scopes), now)
            with repo.history() as h:
                got = h.latest([req["point"]]).get(req["point"])
            latest = {"ts": got[0].isoformat(), "value": got[1] if isinstance(got[1], (int, float, str, bool)) else str(got[1])} if got else None
            row = operator_writes.new_row(pid, "point", req["point"], claims, repo.user_email(claims.get("sub")), req["reason"], now,
                                          equip_uri=req["equip_uri"], level=req["level"], value=req["value"],
                                          expires_at=req["expires_at"])
            try:
                row = operator_writes.write_point(repo, commander, row, req["replace"], now, latest)
            except operator_writes.Conflict as exc:
                respond_error(self, 409, str(exc))
                return
            except Exception as exc:  # noqa: BLE001 - the site refused or didn't answer; recorded on the row
                self._event(pid, "operator_write.failed", claims, {"write_id": row["id"], "point": req["point"], "error": str(exc)[:300]})
                respond_error(self, 502, f"the write wasn't applied: {exc}")
                return
            self._event(pid, "operator_write.point", claims, {"write_id": row["id"], "point": req["point"], "value": req["value"],
                                                               "level": req["level"], "reason": req["reason"],
                                                               "expires_at": req["expires_at"].isoformat()})
            respond_json(self, 201, db.public_row(row))

        def _relinquish_operator_write(self, span, claims, wid) -> None:
            if not self._require_person(claims, "operator"):
                return
            hit = self._visible_write(wid, claims)
            if hit is None:
                return
            row, project = hit
            if row["kind"] != "point":
                respond_error(self, 400, "a schedule or calendar write is reverted: POST /writes/{id}/revert")
                return
            if row["status"] != operator_writes.ACTIVE:
                respond_error(self, 409, f"write is {row['status']}")
                return
            if commander is None:
                respond_error(self, 503, "no Windrow command channel is configured on this server")
                return
            why = operator_writes.reason(self.read_json_body())
            try:
                row = operator_writes.relinquish(repo, commander, row, now_fn(), claims.get("sub"), why)
            except Exception as exc:  # noqa: BLE001 - logged on the row; the sweeper retries at expiry
                respond_error(self, 502, f"the relinquish wasn't applied (the sweeper retries): {exc}")
                return
            self._event(project["id"], "operator_write.relinquished", claims, {"write_id": wid, "reason": why})
            respond_json(self, 200, db.public_row(row))

        def _schedule_targets(self, span, claims, pid) -> None:
            """The schedule and calendar objects on the project's
            connections, read live (?site=&instance=)."""
            p = self._writes_project(pid, claims)
            if p is None:
                return
            q = parse_qs(urlparse(self.path).query)
            site, instance = q.get("site", [None])[0], q.get("instance", [None])[0]
            if not site or not instance:
                raise ValueError("site and instance name the Windrow instance")
            scopes, _ = self._scopes(p, claims)
            out = []
            for c in commander.command(site, instance, "list_connections", {}, timeout=operator_writes.COMMAND_TIMEOUT_S) or []:
                if not isinstance(c, dict) or not all(s.allows(f"urn:point:{c.get('topic_prefix')}/x") for s in scopes):
                    continue
                snap = commander.command(site, instance, operator_writes.READ_SCHEDULES, {"id": c["id"]},
                                         timeout=operator_writes.COMMAND_TIMEOUT_S) or {}
                out.append({"connection_id": c["id"], "topic_prefix": c.get("topic_prefix"),
                            "schedules": snap.get("schedules") or [], "calendars": snap.get("calendars") or []})
            respond_json(self, 200, out)

        def _operator_schedule_write(self, span, claims, pid) -> None:
            if not self._require_person(claims, "admin"):
                return
            p = self._writes_project(pid, claims)
            if p is None:
                return
            now = now_fn()
            req = operator_writes.schedule_write(self.read_json_body(), now)
            scopes, _ = self._scopes(p, claims)
            conn = operator_writes.connection(commander, req["site"], req["instance"], req["connection_id"])
            if conn is None or not all(s.allows(f"urn:point:{conn.get('topic_prefix')}/x") for s in scopes):
                respond_error(self, 404, "no such connection in this project")
                return
            target = f"{req['site']}/{req['instance']}/{req['connection_id']}/{req['object']}"
            row = operator_writes.new_row(pid, req["kind"], target, claims, repo.user_email(claims.get("sub")), req["reason"], now,
                                          site=req["site"], instance=req["instance"], connection_id=req["connection_id"],
                                          value={"kind": req["kind"], "object": req["object"], "fields": req["fields"]},
                                          expires_at=req["revert_at"])
            try:
                row = operator_writes.write_schedule(repo, commander, row, now)
            except ValueError:  # the object isn't on that connection: a 400, nothing sent
                raise
            except Exception as exc:  # noqa: BLE001 - recorded on the row
                self._event(pid, "operator_write.failed", claims, {"write_id": row["id"], "target": target, "error": str(exc)[:300]})
                respond_error(self, 502, f"the write wasn't applied: {exc}")
                return
            self._event(pid, f"operator_write.{req['kind']}", claims,
                        {"write_id": row["id"], "target": target, "fields": sorted(req["fields"]), "reason": req["reason"],
                         "revert_at": req["revert_at"].isoformat() if req["revert_at"] else None, "permanent": req["permanent"]})
            respond_json(self, 201, db.public_row(row))

        def _revert_operator_write(self, span, claims, wid) -> None:
            if not self._require_person(claims, "admin"):
                return
            hit = self._visible_write(wid, claims)
            if hit is None:
                return
            row, project = hit
            if row["kind"] == "point":
                respond_error(self, 400, "a point write is relinquished: POST /writes/{id}/relinquish")
                return
            if row["status"] not in (operator_writes.ACTIVE, "permanent"):
                respond_error(self, 409, f"write is {row['status']}")
                return
            if commander is None:
                respond_error(self, 503, "no Windrow command channel is configured on this server")
                return
            why = operator_writes.reason(self.read_json_body())
            try:
                row = operator_writes.revert_schedule(repo, commander, row, now_fn(), claims.get("sub"), why)
            except Exception as exc:  # noqa: BLE001 - logged on the row
                respond_error(self, 502, f"the revert wasn't applied: {exc}")
                return
            self._event(project["id"], f"operator_write.{row['status']}", claims, {"write_id": wid, "reason": why})
            respond_json(self, 200, db.public_row(row))

        # ------------------------------------------- override tests (Baler#212)

        def _project_tz(self, p: dict):
            cfg = overrides.effective((p.get("settings") or {}).get("overrides"))
            return ZoneInfo(cfg["tz"]) if cfg.get("tz") else ZoneInfo("UTC"), cfg

        def _list_schedules(self, span, claims, pid) -> None:
            """The BACnet schedules and calendars last read from the
            project's connections, each with what it commands now."""
            p = self._project(pid, claims)
            if p is None:
                return
            scopes, _ = self._scopes(p, claims)
            tz, cfg = self._project_tz(p)
            now = now_fn()
            rows = [r for r in repo.list_schedules() if all(s.allows(f"urn:point:{r['topic_prefix']}/x") for s in scopes)]
            cals: dict[tuple, dict] = {}
            for r in rows:
                if r["kind"] == "calendar":
                    cals.setdefault((r["site"], r["instance"], r["connection_id"]), {})[r["object"]] = r["body"]
            out = []
            for r in rows:
                item = {k: r[k] for k in ("id", "kind", "object", "name", "topic_prefix", "points")}
                item["read_at"] = db._iso(r["read_at"])
                item["body"] = r["body"]
                if r["kind"] == "schedule":
                    got = sched_eval.value_at(r["body"], now, tz, cals.get((r["site"], r["instance"], r["connection_id"])))
                    item["now"] = {**got, "unoccupied": sched_eval.is_unoccupied(got["value"], cfg["unoccupied_values"])}
                out.append(item)
            respond_json(self, 200, {"tz": str(tz), "schedules": out})

        def _refresh_schedules(self, span, claims, pid) -> None:
            """Read schedules and calendars from Windrow now (reads only -
            nothing is written to a controller)."""
            p = self._project(pid, claims)
            if p is None:
                return
            if commander is None:
                respond_error(self, 503, "no Windrow command channel is configured on this server")
                return
            scopes, _ = self._scopes(p, claims)
            got = overrides.refresh(repo, commander, scopes, now_fn())
            self._event(pid, "schedules.refresh", claims, {k: got[k] for k in ("schedules", "calendars")}
                        | {"errors": got["errors"][:10]})
            respond_json(self, 200, got)

        # ------------------------------------------------------- agent (Cx 10)

        def _agent_available(self) -> bool:
            if agent_client is not None:
                return True
            why = llm.unavailable_reason("agent")
            if why:
                respond_error(self, 501, why)
                return False
            return True

        @staticmethod
        def _transcript(messages: list[dict]) -> list[dict]:
            """What a person reads: text, the tools the agent used, and
            their results' errors - not the raw API blocks."""
            out: list[dict] = []
            for m in messages:
                if agent.history_format([m]) == "openai":
                    out += CommissioningHandler._openai_transcript(m)
                    continue
                for b in m.get("content") or []:
                    t = b.get("type")
                    if t == "text" and b.get("text"):
                        out.append({"role": m["role"], "text": b["text"]})
                    elif t == "tool_use":
                        out.append({"role": "tool", "tool": b["name"], "input": b.get("input")})
                    elif t == "tool_result" and b.get("is_error"):
                        out.append({"role": "tool", "error": str(b.get("content"))[:500]})
            return out

        @staticmethod
        def _openai_transcript(m: dict) -> list[dict]:
            out: list[dict] = []
            if m.get("role") == "tool":
                try:
                    res = json.loads(m.get("content") or "")
                except ValueError:
                    res = None
                if isinstance(res, dict) and set(res) == {"error"}:
                    out.append({"role": "tool", "error": str(res["error"])[:500]})
                return out
            if isinstance(m.get("content"), str) and m["content"]:
                out.append({"role": m["role"], "text": m["content"]})
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = fn.get("arguments")
                out.append({"role": "tool", "tool": fn.get("name"), "input": args})
            return out

        def _public_session(self, sess: dict) -> dict:
            out = {k: v for k, v in db.public_row(sess).items() if k not in ("messages", "claims")}
            out["transcript"] = self._transcript(sess.get("messages") or [])
            return out

        def _visible_session(self, sid: str, claims: dict) -> tuple[dict, dict] | None:
            sess = repo.get_row("cx_agent_sessions", sid)
            project = repo.get_project(sess["project_id"]) if sess else None
            if sess is None or project is None or not self._visible(project, claims):
                respond_error(self, 404, "no such agent session")
                return None
            return sess, project

        def _run_agent_turn(self, sess: dict, project: dict, text: str) -> None:
            sid = sess["id"]
            claims = sess["claims"]

            def turn():
                try:
                    ctx = agent.Ctx(repo, worker, project, claims, now_fn, sid)
                    messages, reply = agent.run_turn(ctx, sess.get("messages") or [], text, agent_client)
                    repo.update_row("cx_agent_sessions", sid, {"messages": messages, "last_reply": reply, "status": "idle",
                                                               "error": None, "updated_at": now_fn()})
                except Exception as exc:  # noqa: BLE001 - recorded on the session
                    repo.update_row("cx_agent_sessions", sid, {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:2000],
                                                               "updated_at": now_fn()})
            repo.update_row("cx_agent_sessions", sid, {"status": "working", "error": None, "updated_at": now_fn()})
            threading.Thread(target=turn, name=f"cx-agent-{sid[:8]}", daemon=True).start()

        def _read_message(self) -> str:
            text = self.read_json_body().get("message")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("message is required")
            return text.strip()

        def _start_agent_session(self, span, claims, pid) -> None:
            """A new conversation; the agent works with the caller's own
            org and sites (and the project's site), never more."""
            p = self._project(pid, claims)
            if p is None or not self._agent_available():
                return
            text = self._read_message()
            now = now_fn()
            sess = {"id": db.new_id(), "project_id": pid, "created_by": claims.get("sub"),
                    "claims": {k: claims.get(k) for k in ("sub", "org_id", "site_ids", "role")}, "status": "idle",
                    "messages": [], "last_reply": None, "error": None, "created_at": now, "updated_at": now}
            repo.put_rows("cx_agent_sessions", [sess])
            self._event(pid, "agent.session", claims, {"session_id": sess["id"]})
            self._run_agent_turn(sess, p, text)
            respond_json(self, 202, self._public_session(repo.get_row("cx_agent_sessions", sess["id"]) or sess))

        def _agent_message(self, span, claims, sid) -> None:
            hit = self._visible_session(sid, claims)
            if hit is None or not self._agent_available():
                return
            sess, project = hit
            if sess["status"] == "working":
                respond_error(self, 409, "the agent is still working on the last message")
                return
            if sess["created_by"] != claims.get("sub"):
                respond_error(self, 403, "a session runs with its starter's access; start your own")
                return
            self._run_agent_turn(sess, project, self._read_message())
            respond_json(self, 202, self._public_session(repo.get_row("cx_agent_sessions", sid) or sess))

        def _get_agent_session(self, span, claims, sid) -> None:
            hit = self._visible_session(sid, claims)
            if hit is not None:
                respond_json(self, 200, self._public_session(hit[0]))

        def _list_agent_sessions(self, span, claims, pid) -> None:
            if self._project(pid, claims) is not None:
                rows = repo.find_rows("cx_agent_sessions", project_id=pid)
                respond_json(self, 200, [{k: v for k, v in db.public_row(r).items() if k not in ("messages", "claims")}
                                         for r in reversed(rows)])

        def _list_agent_proposals(self, span, claims, pid) -> None:
            if self._project(pid, claims) is None:
                return
            q = parse_qs(urlparse(self.path).query)
            eq: dict[str, str] = {"project_id": pid}
            if "status" in q:
                eq["status"] = q["status"][0]
            respond_json(self, 200, [db.public_row(r) for r in reversed(repo.find_rows("cx_agent_proposals", **eq))])

        def _decide_proposal(self, claims, pid_or_id: str) -> tuple[dict, dict] | None:
            prop = repo.get_row("cx_agent_proposals", pid_or_id)
            project = repo.get_project(prop["project_id"]) if prop else None
            if prop is None or project is None or not self._visible(project, claims):
                respond_error(self, 404, "no such proposal")
                return None
            if prop["status"] != "proposed":
                respond_error(self, 409, f"already {prop['status']}")
                return None
            return prop, project

        def _accept_proposal(self, span, claims, prop_id) -> None:
            """Carry out the agent's proposal as you (your role and scope)."""
            hit = self._decide_proposal(claims, prop_id)
            if hit is None:
                return
            prop, project = hit
            now = now_fn()
            try:
                result = agent.accept(repo, prop, project, claims, now)
            except PermissionError as exc:
                respond_error(self, 403, str(exc))
                return
            except gates.GateNotMet as exc:
                # Not failed: it can be accepted once the gate is met (#86)
                respond_json(self, 409, {"error": str(exc), "gate": exc.gate})
                return
            except (ValueError, TypeError, KeyError, db.IssueExists) as exc:
                repo.update_row("cx_agent_proposals", prop_id, {"status": "failed", "result": {"error": str(exc)},
                                                                "decided_by": claims.get("sub"), "decided_at": now})
                respond_error(self, 409, f"couldn't carry it out: {exc}")
                return
            repo.update_row("cx_agent_proposals", prop_id, {"status": "accepted", "result": result,
                                                            "decided_by": claims.get("sub"), "decided_at": now})
            self._event(project["id"], "agent.proposal.accept", claims, {"proposal_id": prop_id, "kind": prop["kind"], **result})
            respond_json(self, 200, db.public_row(repo.get_row("cx_agent_proposals", prop_id) or prop))

        def _reject_proposal(self, span, claims, prop_id) -> None:
            hit = self._decide_proposal(claims, prop_id)
            if hit is None:
                return
            prop, project = hit
            note = self.read_json_body().get("note")
            repo.update_row("cx_agent_proposals", prop_id, {"status": "rejected", "decided_by": claims.get("sub"), "decided_at": now_fn(),
                                                            "result": {"note": str(note)[:2000]} if note else None})
            self._event(project["id"], "agent.proposal.reject", claims, {"proposal_id": prop_id, "kind": prop["kind"]})
            respond_json(self, 200, db.public_row(repo.get_row("cx_agent_proposals", prop_id) or prop))

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

    def empty_rows_index():
        # Lets report availability read the primary key alone (#150); in the
        # background, since on a big install the build takes a while.
        with psycopg.connect(timeseries.DEFAULT_DSN, autocommit=True) as conn:
            print(f"empty-rows index: {history.ensure_empty_rows_index(conn)}", flush=True)

    threading.Thread(target=lambda: warmup.run("empty-rows index", empty_rows_index, timeout=3600), daemon=True).start()
    # The Brick/Xeto lookup tables the inventory's specs read, built before
    # listening rather than in the first report after a deploy.
    warmup.run("cx lookup tables", hisquery.warm_caches)
    now = datetime.now(timezone.utc)
    for r in repo.fail_interrupted_runs(now):
        repo.add_event(r["project_id"], "run.fail", None, None, {"run_id": r["id"], "error": db.INTERRUPTED}, now)
        print(f"commissioning run {r['id']} was interrupted by a restart; marked failed")
    for r in repo.fail_interrupted_test_runs(now):
        repo.add_event(r["project_id"], "test_run.fail", None, None, {"test_run_id": r["id"], "error": db.INTERRUPTED}, now)
    clock = lambda: datetime.now(timezone.utc)
    worker = runner.RunWorker(repo, clock)
    # Mirrored faults (Cx 8) fire the same webhooks fault_detector's do.
    from baler import fault_detector, json_store

    webhooks_path = json_store.data_file("webhooks.json")
    worker.notify = lambda event, rule_id, uri, status, detail: fault_detector._fire_webhooks(
        webhooks_path, event, rule_id, uri, status, detail)
    # Scheduled procedures and project runs (Cx 6). One process runs the
    # scheduler; a second commissioning_api would double-queue, which the
    # one-active-run indexes turn into a skipped tick, not a duplicate.
    # Active tests (Cx 9): point_write over MQTT, and the sweeper that
    # relinquishes any Cx level whose run isn't running.
    from baler import point_write

    writer = active.Writer(repo, point_write.Commander())
    executor = active.Executor(repo, writer, clock)
    fleet_runner = fleet.FleetRunner(repo, fleet.ChildExecutor(repo, writer, clock), clock)
    # Fleets (Baler#275 d) a restart interrupted: failed, their members failed,
    # and every level they held relinquished - from the persisted fleet state.
    fleet.recover(repo, writer, now)
    for r in repo.find_rows("cx_active_runs", status="running") + repo.find_rows("cx_active_runs", status="confirmed"):
        repo.update_row("cx_active_runs", r["id"], {"status": "failed", "error": db.INTERRUPTED, "finished_at": now})
    active.sweep(repo, writer, now)
    # Operator writes (Baler#213): relinquish expired point writes and revert
    # schedule writes on time - now (whatever a restart interrupted) and
    # every tick.
    operator_writes.sweep(repo, writer.commander, now)
    # Override tests (Baler#212): re-read schedules and priority arrays for
    # projects that have them enabled, every settings.overrides.refresh_minutes.
    refresher = overrides.Refresher(repo, writer.commander,
                                    lambda p: runner.project_scopes(repo, p, {"org_id": p["org_id"], "site_ids": []})[0])
    runner.Scheduler(repo, worker, clock, extra=[lambda t: active.sweep(repo, writer, t),
                                                 lambda t: fleet.sweep(repo, writer, t),
                                                 # scheduled retests (Baler#275 e): only approved, only with the switch on
                                                 retest_schedules.Ticker(repo, fleet_runner),
                                                 lambda t: operator_writes.sweep(repo, writer.commander, t),
                                                 refresher.tick,
                                                 # continuous compliance (Baler#207): adopted packs on their schedule
                                                 lambda t: compliance.tick(repo, worker, t)])
    server = ThreadingHTTPServer((args.host, args.port), make_handler(repo, worker=worker, executor=executor,
                                                                      commander=writer.commander, fleet_runner=fleet_runner))
    print(f"commissioning API listening on {args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

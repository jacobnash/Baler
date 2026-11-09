"""
The commissioning agent (Cx 10, Baler#46): a Claude tool-use loop over
the same Cx capabilities a person uses through /cx/.

Guardrails, by construction:

  - Reads and passive work are tools that act: inventory, runs and their
    results, templates and gaps, issues, procedures and test runs, spec
    items and alignment, active-test status, phase gates, open faults;
    starting a passive
    verification run or a passive test procedure.
  - Every write is a *proposal* (cx_agent_proposals) a human accepts or
    rejects: an issue, a test procedure, a phase change, a spec item, a
    spec<->equipment mapping, an active test. Accepting runs the action
    as the accepting person, with their role checked.
  - Active tests only through Cx 9's envelope: an accepted active-test
    proposal becomes a *planned* run, which an admin still confirms
    inside its window with BALER_CX_ALLOW_WRITES=1.
  - Phase gates (#86) apply to an accepted phase proposal exactly as to
    POST .../phase: a "block" gate refuses it (the proposal stays open),
    a "warn" gate records what was unmet.
  - Per-project scope and token: every tool runs with the session
    starter's org/sites and the project's site scope - the same filters
    a person's request gets. The agent never sees raw SQL or SPARQL.
  - Every agent action lands in cx_events as `agent.<tool>`, so "what did
    the agent do on this project" is one query.

The model is llm.provider()'s (BALER_LLM_PROVIDER): the Claude API, or
any OpenAI-compatible endpoint with function calling (Ollama, Groq,
Gemini, OpenRouter, ...). Same tools, same loop, same guardrails; a model
that can't call tools is an error, never a conversation without them.

The conversation is stored on the session (cx_agent_sessions.messages)
exactly as the API returned it, thinking blocks included, and replayed
unchanged on the next turn - in that provider's message format, so a
session continues only on the provider it started on.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta

from baler.commissioning import (
    active,
    db,
    engine,
    gates,
    inventory,
    issues,
    llm,
    phases,
    procedures,
    record_tools,
    runner,
    spec,
)
from baler.commissioning import templates as tpl
from baler.commissioning.mirror import RULE_PREFIX as MIRROR_PREFIX

log = logging.getLogger(__name__)

DEFAULT_MODEL = llm.DEFAULT_ANTHROPIC_MODEL
MAX_TOOL_ROUNDS = 16
MAX_RESULT_CHARS = 30000
PROPOSAL_KINDS = ("issue", "procedure", "phase", "spec_item", "mapping", "active_test")


def model_name() -> str:
    return llm.anthropic_model()


SYSTEM = """You are the commissioning (Cx) agent for one project in Baler, a building data platform.
You drive the commissioning workflow with the tools you have: read what the building's data says,
run passive verification, and propose what a commissioning authority would do next.

How you work:
- Start from the project's current phase. In start-up, look at power-on procedures and template gaps.
  In functional testing, procedures and GL36/90.1 sequence items. In warranty and operations,
  scheduled re-runs and open faults.
- Before proposing a phase change, check its gate (phase_gate) and say what is unmet. A project can
  block an unmet gate; then only a person can override it.
- An inconclusive result that can't be settled here (e.g. a read-only controller) can still become an
  issue (from_result) - a person accepting that issue as a risk is the explicit waiver the gate needs.
- Base every statement on evidence a tool returned. Name the run, check and point you are citing.
  An inconclusive result means no evidence either way: never call it a pass.
- Reads and passive runs you may do freely. Everything that changes the project - an issue, a test
  procedure, a phase change, a spec item, a mapping, an active test - you only *propose*; a person
  accepts or rejects it. Say so plainly when you propose something.
- Active tests command real equipment. You can only propose one; an admin must still confirm it
  inside its window. Propose one only when passive evidence can't settle the question.
- When a person must decide or act, ask one question that needs one action, not a list.
- Keep answers short: what you found, what you propose, the one thing you need from the person."""


# ------------------------------------------------------------------- tools


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


_S = {"type": "string"}
_N = {"type": "number"}

TOOLS: list[dict] = [
    {"name": "get_project", "description": "The project: name, site, settings, phases and the current phase.",
     "input_schema": _obj({})},
    {"name": "inventory_summary", "description": "Equipment in scope: id, name, Brick class, point count, and which "
     "expected-point templates apply. Use equipment ids from here in other tools.", "input_schema": _obj({})},
    {"name": "equipment_points", "description": "The points of one equipment: id, name, Brick class, unit, Haystack markers.",
     "input_schema": _obj({"equip_uri": _S}, ["equip_uri"])},
    {"name": "template_gaps", "description": "Expected points each equipment is missing against the SHACL templates, now.",
     "input_schema": _obj({})},
    {"name": "list_runs", "description": "Verification runs, newest first, with status and counts.", "input_schema": _obj({})},
    {"name": "run_results", "description": "Results of one run. Filter by status (pass, fail, inconclusive), equipment, or "
     "check id prefix (exists, live, pair:, template:, gl36:, cx:). At most 200 rows.",
     "input_schema": _obj({"run_id": _S, "status": _S, "equip_uri": _S, "check_prefix": _S}, ["run_id"])},
    {"name": "start_verification_run", "description": "Start a passive verification run (reads history only). "
     "Returns the run id; it runs in the background - check it with list_runs.",
     "input_schema": _obj({"window_hours": _N})},
    {"name": "list_issues", "description": "The issues log (and accepted risks).", "input_schema": _obj({"status": _S})},
    {"name": "list_procedures", "description": "Test procedures and their last test run's result.", "input_schema": _obj({})},
    {"name": "procedure_templates", "description": "The standard power-on and start-up procedures that can be proposed.",
     "input_schema": _obj({})},
    {"name": "run_procedure", "description": "Run a test procedure passively over history from started_at (ISO date-time, "
     "when the equipment was powered on / started). Nothing is commanded. Returns the test run id.",
     "input_schema": _obj({"procedure_id": _S, "started_at": _S, "hours": _N}, ["procedure_id"])},
    {"name": "get_test_run", "description": "A test run's per-equipment, per-step verdicts and the diff against the previous one.",
     "input_schema": _obj({"test_run_id": _S}, ["test_run_id"])},
    {"name": "spec_items", "description": "Spec items from the design documents (equipment, points, sequences).",
     "input_schema": _obj({"status": _S, "kind": _S})},
    {"name": "alignment", "description": "Accepted spec equipment against the inventory: confidence, alternatives, deviations.",
     "input_schema": _obj({})},
    {"name": "active_status", "description": "Whether this deployment allows active tests, and recent active runs.",
     "input_schema": _obj({})},
    {"name": "phase_gate", "description": "What moving the project to phase `key` requires (verification run, points "
     "reporting, procedures passing, no open issues, no unwaived inconclusive results), what is unmet, and the "
     "project's gate mode (warn, block or off). Check it before proposing a phase change.",
     "input_schema": _obj({"key": _S}, ["key"])},
    {"name": "open_faults", "description": "Open faults on this project's equipment: fault_detector rules and this "
     "project's own mirrored Cx checks (rule ids cx:<check>). At most 200, newest first.",
     "input_schema": _obj({})},
    {"name": "propose", "description": "Propose a change for a person to accept. kind and payload:\n"
     "- issue: {title, description?, equip_uri?, severity? (critical|major|minor|info), "
     "from_result? {run_id, check_id, subject} (a failing or inconclusive result)}\n"
     "- procedure: {template} (a procedure_templates id) or a full procedure {name, kind, equip_class, steps}\n"
     "- phase: {key, note?}\n"
     "- spec_item: {kind (equipment|point|sequence), tag, name, brick_class?, equip_tag?, properties?}\n"
     "- mapping: {spec_item_id, equip_uri}\n"
     "- active_test: {equip_uri, window_start, window_end} (rung 4 command response; still needs an admin to confirm)\n"
     "Always give the reason: the evidence behind it.",
     "input_schema": _obj({"kind": {"type": "string", "enum": list(PROPOSAL_KINDS)}, "payload": {"type": "object"},
                           "reason": _S}, ["kind", "payload", "reason"])},
]
for _t in TOOLS:
    if _t["name"] == "propose":
        _t["input_schema"]["properties"]["payload"]["additionalProperties"] = True


class Ctx:
    """What one session's tools run with: the repo, the project, the
    starter's claims and scopes (the same as their own requests)."""

    def __init__(self, repo, worker, project: dict, claims: dict, now_fn: Callable[[], datetime], session_id: str):
        self.repo, self.worker, self.project, self.claims, self.now = repo, worker, project, claims, now_fn
        self.session_id = session_id
        self.scopes, _ = runner.project_scopes(repo, project, claims)
        self.scope = self.scopes[0]
        self._equips: list[dict] | None = None

    def equips(self) -> list[dict]:
        if self._equips is None:
            self._equips = inventory.build(self.repo.store, self.scopes)
        return self._equips

    def event(self, action: str, detail: dict) -> None:
        self.repo.add_event(self.project["id"], f"agent.{action}", self.claims.get("sub"), None,
                            {"session_id": self.session_id, **detail}, self.now())


class ToolError(Exception):
    pass


def _public(rows):
    return [db.public_row(r) for r in rows]


def t_get_project(ctx: Ctx, _):
    return db.public_project(ctx.repo.get_project(ctx.project["id"]))


def t_inventory_summary(ctx: Ctx, _):
    templates = tpl.load()
    return [{"id": e["id"], "dis": e["dis"], "brickClass": e["brickClass"], "points": len(e["points"]),
             "unclassified_points": sum(1 for p in e["points"] if not p["brickClass"]),
             "templates": [t.id for t in tpl.templates_for(e["brickClasses"], templates)]} for e in ctx.equips()]


def t_equipment_points(ctx: Ctx, a):
    e = next((x for x in ctx.equips() if x["id"] == a["equip_uri"]), None)
    if e is None:
        raise ToolError("no such equipment in this project's inventory")
    return [{k: p.get(k) for k in ("id", "dis", "brickClass", "unit", "tags")} for p in e["points"]]


def t_template_gaps(ctx: Ctx, _):
    templates = tpl.load()
    out = []
    for e in ctx.equips():
        for t in tpl.templates_for(e["brickClasses"], templates):
            missing = [o["requirement"].name + f" ({o['requirement'].severity})" for o in tpl.evaluate(t, e["points"]) if not o["matched"]]
            if missing:
                out.append({"equip_uri": e["id"], "dis": e["dis"], "template": t.id, "missing": missing})
    return out


def t_list_runs(ctx: Ctx, _):
    return [{k: r.get(k) for k in ("id", "status", "phase", "started_at", "finished_at", "error")}
            | {"by_status": (r.get("summary") or {}).get("by_status"), "freshness": (r.get("summary") or {}).get("freshness")}
            for r in ctx.repo.list_runs(ctx.project["id"], 20)]


def t_run_results(ctx: Ctx, a):
    run = ctx.repo.get_run(a["run_id"])
    if run is None or run["project_id"] != ctx.project["id"]:
        raise ToolError("no such run in this project")
    rows = [r for r in ctx.repo.get_results(a["run_id"]) if ctx.scope.allows(r["equip_uri"])]
    if a.get("status"):
        rows = [r for r in rows if r["status"] == a["status"]]
    if a.get("equip_uri"):
        rows = [r for r in rows if r["equip_uri"] == a["equip_uri"]]
    if a.get("check_prefix"):
        rows = [r for r in rows if r["check_id"].startswith(a["check_prefix"])]
    return {"total": len(rows), "results": [{k: r.get(k) for k in ("equip_uri", "check_id", "subject", "status", "severity", "symptom")}
                                            for r in rows[:200]]}


def t_start_verification_run(ctx: Ctx, a):
    wh = engine.window_hours(a.get("window_hours", (ctx.project.get("settings") or {}).get("window_hours")))
    try:
        run = runner.queue_run(ctx.repo, ctx.worker, ctx.project, ctx.claims, ctx.now(), wh, ctx.claims.get("sub"), None)
    except db.RunActive as exc:
        return {"already_running": exc.run_id}
    return {"run_id": run["id"], "status": "queued", "window_hours": wh}


def t_list_issues(ctx: Ctx, a):
    rows = ctx.repo.list_issues(ctx.project["id"], [a["status"]] if a.get("status") else None)
    risks = {r["issue_id"]: r for r in ctx.repo.list_risks(ctx.project["id"])}
    return [{k: i.get(k) for k in ("id", "status", "title", "equip_uri", "check_id", "subject", "severity", "source")}
            | {"risk": db.public_row(risks[i["id"]]) if i["id"] in risks else None}
            for i in rows if not i.get("equip_uri") or ctx.scope.allows(i["equip_uri"])]


def t_list_procedures(ctx: Ctx, _):
    out = []
    for p in ctx.repo.list_procedures(ctx.project["id"]):
        if p["status"] == "archived":
            continue
        last = next(iter(ctx.repo.list_test_runs(p["id"], 1)), None)
        out.append({"id": p["id"], "name": p["name"], "kind": p["kind"], "equip_class": p["equip_class"], "schedule": p.get("schedule"),
                    "last_test_run": None if last is None else {"id": last["id"], "status": last["status"],
                                                                "summary": last.get("summary")}})
    return out


def t_procedure_templates(ctx: Ctx, _):
    return [{"template": s["template"], "name": s["name"], "kind": s["kind"], "equip_class": s["equip_class"],
             "steps": [x["title"] for x in s["steps"]]} for s in procedures.SEEDS]


def t_run_procedure(ctx: Ctx, a):
    proc = ctx.repo.get_procedure(a["procedure_id"])
    if proc is None or proc["project_id"] != ctx.project["id"] or proc["status"] == "archived":
        raise ToolError("no such active procedure in this project")
    now = ctx.now()
    hours = float(a.get("hours") or proc["window_hours"])
    start = datetime.fromisoformat(a["started_at"].replace("Z", "+00:00")) if a.get("started_at") else now - timedelta(hours=hours)
    if start.tzinfo is None or start >= now:
        raise ToolError("started_at must be a past ISO date-time with a time zone")
    try:
        tr = runner.queue_test_run(ctx.repo, ctx.worker, ctx.project, proc, ctx.claims, start, min(start + timedelta(hours=hours), now),
                                   now, ctx.claims.get("sub"), None)
    except db.RunActive as exc:
        return {"already_running": exc.run_id}
    return {"test_run_id": tr["id"], "status": "queued"}


def t_get_test_run(ctx: Ctx, a):
    tr = ctx.repo.get_test_run(a["test_run_id"])
    if tr is None or tr["project_id"] != ctx.project["id"]:
        raise ToolError("no such test run in this project")
    out = db.public_row(tr)
    out["equipment"] = [{"equip_uri": e["equip_uri"], "status": e["status"],
                         "steps": [{"id": s["id"], "status": s["status"],
                                    "failing": [f for c in s["criteria"] for f in c["failing"]][:5]} for s in e["steps"]]}
                        for e in (out.get("equipment") or []) if ctx.scope.allows(e["equip_uri"])]
    return out


def t_spec_items(ctx: Ctx, a):
    eq = {"project_id": ctx.project["id"], **{k: a[k] for k in ("status", "kind") if a.get(k)}}
    return [{k: i.get(k) for k in ("id", "kind", "tag", "name", "equip_tag", "brick_class", "status", "confidence")}
            | {"source": i.get("source")} for i in ctx.repo.find_rows("cx_spec_items", **eq)][:300]


def t_alignment(ctx: Ctx, _):
    items = ctx.repo.find_rows("cx_spec_items", project_id=ctx.project["id"])
    decided = {d["spec_item_id"]: d for d in ctx.repo.find_rows("cx_mappings", project_id=ctx.project["id"])
               if d["status"] in ("confirmed", "rejected")}
    return spec.align(items, ctx.equips(), phases.current(ctx.project["phases"]), decided)


def t_active_status(ctx: Ctx, _):
    runs = ctx.repo.find_rows("cx_active_runs", project_id=ctx.project["id"])[-10:]
    return {"writes_allowed": active.writes_allowed(),
            "recent": [{k: r.get(k) for k in ("id", "equip_uri", "status", "results", "error")} for r in runs]}


def t_phase_gate(ctx: Ctx, a):
    project = ctx.repo.get_project(ctx.project["id"])
    if a["key"] not in {p["key"] for p in project["phases"]}:
        raise ToolError("no such phase key in this project")
    gate = runner.phase_gate(ctx.repo, project, a["key"], ctx.now())
    return {"mode": gates.mode(project.get("settings")),
            "gate": gate or {"target": a["key"], "rules": [], "met": True, "unmet": []}}


def t_open_faults(ctx: Ctx, _):
    equip_of = {p["id"]: e["id"] for e in ctx.equips() for p in e["points"]}
    mine = set(ctx.repo.open_mirrored_faults(ctx.project["id"]))
    rows = [f for f in ctx.repo.list_faults(True) if f["point_uri"] in equip_of and ctx.scope.allows(f["point_uri"])
            # another project's cx: mirror is that project's business
            and (not f["rule_id"].startswith(MIRROR_PREFIX) or (f["rule_id"], f["point_uri"]) in mine)]
    rows.sort(key=lambda f: str(f.get("started_at") or ""), reverse=True)
    return [{k: f.get(k) for k in ("id", "rule_id", "point_uri", "status", "severity", "started_at")}
            | {"equip_uri": equip_of[f["point_uri"]], "detail": f.get("detail")} for f in rows[:200]]


def _result_for_issue(repo, ref, project_id: str, scope) -> tuple[dict, dict]:
    """(run, result) a from_result issue refers to: this project's run, a
    failing or inconclusive result (inconclusive so it can be waived, #96),
    in scope. Raises ValueError (TypeError for a malformed ref)."""
    if not isinstance(ref, dict):
        raise TypeError("from_result must be an object with run_id, check_id and subject")
    run = repo.get_run(str(ref.get("run_id")))
    res = next((r for r in repo.get_results(run["id"]) if r["check_id"] == ref.get("check_id")
                and r["subject"] == ref.get("subject")), None) if run and run["project_id"] == project_id else None
    if res is None or res["status"] not in ("fail", "inconclusive") or not scope.allows(res["equip_uri"]):
        raise ValueError("that result isn't a failing or inconclusive result of this project's run")
    return run, res


def _check_proposal(ctx: Ctx, kind: str, payload: dict) -> None:
    """Catch what would certainly fail on acceptance, now, so the agent
    can correct it. Acceptance re-validates everything."""
    equip_ids = {e["id"] for e in ctx.equips()}
    if kind == "issue":
        if not payload.get("title") and not payload.get("from_result"):
            raise ToolError("an issue needs a title, or from_result")
        if payload.get("from_result"):
            _result_for_issue(ctx.repo, payload["from_result"], ctx.project["id"], ctx.scope)
        if payload.get("equip_uri") and payload["equip_uri"] not in equip_ids:
            raise ToolError("equip_uri isn't in this project's inventory")
        issues.check_severity(payload.get("severity"))
    elif kind == "procedure":
        if payload.get("template"):
            procedures.from_seed(str(payload["template"]))
        else:
            procedures.normalize(payload)
    elif kind == "phase":
        if payload.get("key") not in {p["key"] for p in ctx.project["phases"]}:
            raise ToolError("no such phase key in this project")
    elif kind == "spec_item":
        if payload.get("kind") not in spec.KINDS or not payload.get("tag") or not payload.get("name"):
            raise ToolError("a spec item needs kind, tag and name")
    elif kind == "mapping":
        item = ctx.repo.get_row("cx_spec_items", str(payload.get("spec_item_id")))
        if item is None or item["project_id"] != ctx.project["id"] or payload.get("equip_uri") not in equip_ids:
            raise ToolError("spec_item_id must be this project's, and equip_uri in its inventory")
    elif kind == "active_test":
        if payload.get("equip_uri") not in equip_ids:
            raise ToolError("equip_uri isn't in this project's inventory")
        start = datetime.fromisoformat(str(payload.get("window_start")).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(payload.get("window_end")).replace("Z", "+00:00"))
        active.check_window(start, end, ctx.now())


def t_propose(ctx: Ctx, a):
    kind, payload = a["kind"], a.get("payload") or {}
    if kind not in PROPOSAL_KINDS:
        raise ToolError(f"kind is one of {', '.join(PROPOSAL_KINDS)}")
    try:
        _check_proposal(ctx, kind, payload)
    except (ValueError, TypeError) as exc:
        raise ToolError(str(exc)) from exc
    row = {"id": db.new_id(), "project_id": ctx.project["id"], "session_id": ctx.session_id, "kind": kind, "payload": payload,
           "reason": str(a.get("reason") or "")[:4000], "status": "proposed", "result": None, "decided_by": None,
           "decided_at": None, "created_at": ctx.now()}
    ctx.repo.put_rows("cx_agent_proposals", [row])
    return {"proposal_id": row["id"], "status": "proposed - waiting for a person to accept or reject it"}


IMPL: dict[str, Callable[[Ctx, dict], object]] = {
    "get_project": t_get_project, "inventory_summary": t_inventory_summary, "equipment_points": t_equipment_points,
    "template_gaps": t_template_gaps, "list_runs": t_list_runs, "run_results": t_run_results,
    "start_verification_run": t_start_verification_run, "list_issues": t_list_issues, "list_procedures": t_list_procedures,
    "procedure_templates": t_procedure_templates, "run_procedure": t_run_procedure, "get_test_run": t_get_test_run,
    "spec_items": t_spec_items, "alignment": t_alignment, "active_status": t_active_status, "phase_gate": t_phase_gate,
    "open_faults": t_open_faults, "propose": t_propose,
}
assert set(IMPL) == {t["name"] for t in TOOLS}

# Read-only tools across the building record (#220): offered only for the
# parts this install has (record_tools.available); writes stay proposals.
for _tool, _fn in record_tools.available():
    TOOLS.append(_tool)
    IMPL[_tool["name"]] = _fn
SYSTEM += record_tools.PROMPT


def call_tool(ctx: Ctx, name: str, args) -> tuple[str, bool]:
    """(JSON text, is_error). Every call is a cx_events row."""
    fn = IMPL.get(name)
    if fn is None or not isinstance(args, dict):
        return json.dumps({"error": f"unknown tool {name!r} or arguments not an object"}), True
    try:
        out = fn(ctx, args)
        ok = True
    except (ToolError, ValueError, TypeError, KeyError) as exc:
        out, ok = {"error": str(exc)}, False
    ctx.event(name, {"input": args, "ok": ok})
    text = json.dumps(out, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + '..." (truncated: narrow the query)'
    return text, not ok


# -------------------------------------------------------------------- loop


def _block_dict(block) -> dict:
    return block.model_dump(exclude_none=True) if hasattr(block, "model_dump") else dict(block)


def history_format(messages: list[dict]) -> str | None:
    """'anthropic' (content is a list of blocks), 'openai' (string content,
    tool_calls, role "tool") or None for an empty conversation."""
    for m in messages:
        if m.get("role") == "tool" or "tool_calls" in m or isinstance(m.get("content"), str):
            return "openai"
        if isinstance(m.get("content"), list):
            return "anthropic"
    return None


def run_turn(ctx: Ctx, messages: list[dict], user_text: str, client=None) -> tuple[list[dict], str]:
    """One user turn: append it, loop the model through tools until it
    stops, return (messages, final text). `messages` are API-shaped dicts
    stored on the session and replayed unchanged. `client` (tests): an
    anthropic.Anthropic-like object, or an llm.OpenAIChat-like one."""
    kind = getattr(client, "kind", "anthropic") if client is not None else llm.provider()
    if kind == "none":
        raise llm.LLMError(llm.unavailable_reason("agent") or "no model configured")
    before = history_format(messages)
    if before is not None and before != kind:
        raise llm.LLMError(f"this conversation was started on the {before} provider and BALER_LLM_PROVIDER is now {kind}: "
                           "start a new conversation")
    if kind == "openai":
        return _run_turn_openai(ctx, messages, user_text, client)
    return _run_turn_anthropic(ctx, messages, user_text, client)


def _run_turn_anthropic(ctx: Ctx, messages: list[dict], user_text: str, client=None) -> tuple[list[dict], str]:
    if client is None:
        import anthropic

        client = anthropic.Anthropic()
    messages = [*messages, {"role": "user", "content": [{"type": "text", "text": user_text[:20000]}]}]
    final = ""
    for _ in range(MAX_TOOL_ROUNDS):
        response = client.beta.messages.create(
            model=model_name(), max_tokens=16000, system=SYSTEM, tools=TOOLS, messages=messages,
            output_config={"effort": "high"}, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        content = [_block_dict(b) for b in response.content]
        messages.append({"role": "assistant", "content": content})
        texts = [b.get("text", "") for b in content if b.get("type") == "text"]
        if texts:
            final = "\n".join(t for t in texts if t)
        if response.stop_reason == "refusal":
            final = final or "The model declined this request."
            break
        if response.stop_reason == "pause_turn":
            continue
        uses = [b for b in content if b.get("type") == "tool_use"]
        if response.stop_reason != "tool_use" or not uses:
            break
        results = []
        for u in uses:
            text, err = call_tool(ctx, u["name"], u.get("input"))
            results.append({"type": "tool_result", "tool_use_id": u["id"], "content": text, **({"is_error": True} if err else {})})
        messages.append({"role": "user", "content": results})
    else:
        final = (final + "\n\n" if final else "") + "(stopped: too many tool calls in one turn - ask me to continue)"
    return messages, final


def _strip_schema(schema):
    """JSON schema without additionalProperties, which some OpenAI-compatible
    endpoints (Gemini's) reject in function parameters."""
    if isinstance(schema, dict):
        return {k: _strip_schema(v) for k, v in schema.items() if k != "additionalProperties"}
    if isinstance(schema, list):
        return [_strip_schema(v) for v in schema]
    return schema


def openai_tools() -> list[dict]:
    """TOOLS in OpenAI function-calling format."""
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": _strip_schema(t["input_schema"])}} for t in TOOLS]


def _unsupported(model: str, detail: str) -> llm.ToolCallsUnsupported:
    return llm.ToolCallsUnsupported(
        f"the configured model ({model}) can't call tools, which the commissioning agent needs: {detail}. "
        "Pick a model with function calling (e.g. Ollama llama3.1:8b or qwen2.5:7b, Groq llama-3.3-70b-versatile, "
        "Gemini gemini-2.5-flash) in BALER_LLM_MODEL.")


def _looks_like_tool_call(text: str) -> bool:
    """A model without function calling often writes the call as JSON text."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return False
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return False
    if not isinstance(obj, dict):
        return False
    fn = obj.get("function")
    name = obj.get("name") or obj.get("tool") or (fn.get("name") if isinstance(fn, dict) else fn)
    return isinstance(name, str) and name in IMPL


def _run_turn_openai(ctx: Ctx, messages: list[dict], user_text: str, client=None) -> tuple[list[dict], str]:
    if client is None:
        client = llm.OpenAIChat()
    model = getattr(client, "model", "?")
    tools = openai_tools()
    messages = [*messages, {"role": "user", "content": user_text[:20000]}]
    final = ""
    for _ in range(MAX_TOOL_ROUNDS):
        try:
            response = client.chat([{"role": "system", "content": SYSTEM}, *messages], tools=tools, tool_choice="auto")
        except llm.LLMError as exc:
            status = getattr(exc, "status", None)
            if status in (400, 404, 422) and re.search(r"tool|function", str(exc), re.IGNORECASE):
                raise _unsupported(model, str(exc)[:300]) from exc
            raise
        choice = response["choices"][0]
        msg = choice.get("message") or {}
        calls = [c for c in msg.get("tool_calls") or [] if isinstance(c, dict)]
        text: str = msg["content"] if isinstance(msg.get("content"), str) else ""
        stored: dict = {"role": "assistant", "content": text or None}
        # Provider extras ride along unchanged: Gemini's thinking models put a
        # thought signature on each tool call (extra_content.google.
        # thought_signature) and reject the next request if the replayed
        # history drops it ("Function call is missing a thought_signature").
        if isinstance(msg.get("extra_content"), dict):
            stored["extra_content"] = msg["extra_content"]
        if calls:
            stored["tool_calls"] = [{**c, "id": c.get("id") or f"call_{i}", "type": "function",
                                     "function": {"name": (c.get("function") or {}).get("name", ""),
                                                  "arguments": (c.get("function") or {}).get("arguments") or "{}"}}
                                    for i, c in enumerate(calls)]
        elif not text:
            stored["content"] = ""
        if not calls and _looks_like_tool_call(text):
            raise _unsupported(model, "it wrote a tool call as text instead of calling the tool")
        messages.append(stored)
        if text:
            final = text
        if choice.get("finish_reason") == "content_filter":
            final = final or "The model declined this request."
            break
        if not calls:
            break
        for c in stored["tool_calls"]:
            raw = c["function"]["arguments"]
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                args = None
            if args is None and raw in ("", "null"):
                args = {}
            out, _err = call_tool(ctx, c["function"]["name"], args)
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": out})
    else:
        final = (final + "\n\n" if final else "") + "(stopped: too many tool calls in one turn - ask me to continue)"
    return messages, final


# ---------------------------------------------------------------- accepting


def accept(repo, proposal: dict, project: dict, claims: dict, now: datetime) -> dict:
    """Carry out an accepted proposal as the accepting person. Returns
    what was done. Raises ValueError/PermissionError."""
    kind, p = proposal["kind"], proposal["payload"]
    sub = claims.get("sub")
    pid = project["id"]
    scopes, _ = runner.project_scopes(repo, project, claims)
    if kind == "issue":
        if p.get("from_result"):
            run, res = _result_for_issue(repo, p["from_result"], pid, scopes[0])
            issue = issues.from_result(res, run["id"], pid, db.new_id, now, sub)
        else:
            issue = {"id": db.new_id(), "project_id": pid, "equip_uri": p.get("equip_uri"), "check_id": None, "subject": None,
                     "status": "open", "title": str(p["title"])[:300], "severity": issues.check_severity(p.get("severity")),
                     "assignee": None, "source": "agent", "source_id": proposal["id"],
                     "detail": {"description": p.get("description"), "agent_reason": proposal.get("reason")},
                     "opened_run": None, "last_seen_run": None, "closed_run": None, "created_by": sub,
                     "created_at": now, "updated_at": now, "closed_at": None}
        repo.create_issue(issue)
        repo.add_issue_event(issue["id"], "create", sub, None, {"via": "agent proposal", "proposal_id": proposal["id"]}, now)
        return {"issue_id": issue["id"]}
    if kind == "procedure":
        body = procedures.from_seed(str(p["template"])) if p.get("template") else {**procedures.normalize(p), "template": None}
        proc = {"id": db.new_id(), "project_id": pid, **body, "created_by": sub, "created_at": now, "updated_at": now}
        repo.create_procedure(proc)
        return {"procedure_id": proc["id"]}
    if kind == "phase":
        # The same gate POST .../phase applies (#86): accepting a proposal
        # is not a way around a project's "block". An override stays a
        # person's explicit move with a note, not a click on a proposal.
        key = str(p["key"])
        before = phases.current(project["phases"])
        gate = runner.phase_gate(repo, project, key, now)
        mode = gates.mode(project.get("settings"))
        if gate is not None and not gate["met"] and mode == "block":
            raise gates.GateNotMet(gate)
        repo.save_phases(pid, phases.set_current(project["phases"], key, now), now)
        detail = {"from": before, "to": key, "note": p.get("note"), "via": "agent proposal"}
        if gate is not None:
            detail["gate"], detail["gate_mode"] = gates.summary(gate), mode
        repo.add_event(pid, "phase.set", sub, None, detail, now)
        out: dict = {"phase": key}
        if gate is not None and not gate["met"]:
            out["gate_unmet"] = ", ".join(u["rule"] for u in gate["unmet"])
        return out
    if kind == "spec_item":
        cls = p.get("brick_class")
        if cls:
            from baler import brick_vocab

            cls = brick_vocab.load().canonical_class(str(cls))
        item = {"id": db.new_id(), "project_id": pid, "document_id": None, "kind": p["kind"], "tag": str(p["tag"])[:200],
                "name": str(p["name"])[:500], "equip_tag": p.get("equip_tag"), "brick_class": cls,
                "properties": p.get("properties") if isinstance(p.get("properties"), dict) else {},
                "source": {"method": "agent", "proposal_id": proposal["id"], "reason": proposal.get("reason")},
                "confidence": None, "status": "accepted", "created_by": "agent", "created_at": now, "reviewed_by": sub, "reviewed_at": now}
        repo.put_rows("cx_spec_items", [item])
        return {"spec_item_id": item["id"]}
    if kind == "mapping":
        equips = {e["id"] for e in inventory.build(repo.store, scopes)}
        item = repo.get_row("cx_spec_items", str(p["spec_item_id"]))
        if item is None or item["project_id"] != pid or p.get("equip_uri") not in equips:
            raise ValueError("the spec item or equipment is no longer in this project")
        m = {"id": db.new_id(), "project_id": pid, "spec_item_id": item["id"], "equip_uri": p["equip_uri"], "confidence": "confirmed",
             "basis": [{"kind": "agent proposal", "reason": proposal.get("reason")}], "alternatives": [], "status": "confirmed",
             "decided_by": sub, "decided_at": now, "written_back": None, "created_at": now}
        for old in repo.find_rows("cx_mappings", spec_item_id=item["id"], status="confirmed"):
            repo.delete_rows("cx_mappings", id=old["id"])
        repo.put_rows("cx_mappings", [m])
        return {"mapping_id": m["id"]}
    if kind == "active_test":
        if claims.get("role") != "admin":
            raise PermissionError("an active test proposal is accepted by an admin")
        equip = next((e for e in inventory.build(repo.store, scopes) if e["id"] == p.get("equip_uri")), None)
        if equip is None:
            raise ValueError("that equipment is no longer in this project")
        start = datetime.fromisoformat(str(p["window_start"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(p["window_end"]).replace("Z", "+00:00"))
        active.check_window(start, end, now)
        steps = active.validate_steps(active.command_response_steps(equip, float(p.get("response_minutes", 2))), equip)
        run = active.new_run(pid, equip["id"], "command_response", steps, start, end, sub, now)
        repo.put_rows("cx_active_runs", [run])
        return {"active_run_id": run["id"], "status": "planned - an admin still confirms it inside its window"}
    raise ValueError(f"unknown proposal kind {kind!r}")

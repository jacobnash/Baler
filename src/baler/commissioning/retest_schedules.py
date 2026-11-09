"""
Scheduled active retests (Baler#275 slice e): a fleet run that repeats -
seasonal retesting - but only under an admin's approval that names its
scope and its window, and never with writes enabled by default.

A schedule names:

  scope     the equipment (a frozen list, resolved when it's proposed),
            the test (command_response or a library test) and the fleet
            limits (concurrency_per_system, max_concurrent, stagger,
            max_failures), and mode: "all" retests every member each time,
            "failures" only those that failed in the schedule's last run
  window    first_window_start + k * every_days, each window_minutes long
            (<= Cx 9's 8 h), and valid_until: the approval's end

Lifecycle:

  proposed  an admin proposes it; nothing runs. The answer carries the
            approval_hash (sha256 of the scope and window) and a preview
            fleet plan for the first window.
  approved  an admin - a signed-in person, not an API key - approves it by
            typing that hash. The approval snapshot (the envelope) is kept
            apart from the schedule; from then on PATCH may only narrow
            the schedule inside it: drop equipment, shorten the window,
            end the approval sooner, lower the limits, raise the stagger,
            or go from "all" to "failures". Anything else - more
            equipment, another test, a longer or moved window, a later
            valid_until - is refused: it needs a new schedule and a new
            approval.
  expired   valid_until has passed: the schedule stops scheduling.
  revoked   anyone with the operator role revoked it (the safe direction);
            a fleet it has running is stopped too.

What approval does NOT do: turn writes on. Each scheduled window runs only
if BALER_CX_ALLOW_WRITES=1 at that moment (and a point_write route is
configured); otherwise the window is skipped and the skip is audited. A
run is an ordinary fleet run (fleet.py, so active.py per member): planned
afresh from the current inventory as the approver may see it, checked
against the approval envelope once more, confirmed in the approver's name
and recorded with its schedule, approval hash and plan hash. Every
proposal, approval, edit, revocation, run, skip and expiry is a cx_events
row.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta

from baler.commissioning import active, db, fleet, procedures, runner, test_library

log = logging.getLogger(__name__)

TABLE = "cx_active_schedules"
MAX_VALIDITY = timedelta(days=400)
LIMIT_KEYS = ("concurrency_per_system", "max_concurrent", "max_failures")
MODES = ("all", "failures")


def _dt(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _num(body: dict, key: str, lo: float, hi: float, default=None, integer: bool = True):
    v = body.get(key, default)
    ok = isinstance(v, int) if integer else isinstance(v, (int, float))
    if isinstance(v, bool) or not ok or not lo <= v <= hi:
        raise ValueError(f"{key} must be {'an integer' if integer else 'a number'} in [{lo:g}, {hi:g}]")
    return v


def approval_hash(s: dict) -> str:
    body = {"id": s["id"], "project_id": s["project_id"], "scope": s["scope"], "timing": s["timing"]}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def parse(body: dict, equips: list[dict], now: datetime) -> tuple[dict, dict]:
    """(scope, window) from a proposal. Raises ValueError."""
    by_id = {e["id"]: e for e in equips}
    kind, test = body.get("kind", "command_response"), body.get("test")
    if kind not in ("command_response", "library"):
        raise ValueError("kind is command_response or library")
    if kind == "library" and test not in test_library.BY_ID:
        raise ValueError(f"test must be a library test (library: {', '.join(test_library.BY_ID)})")
    if body.get("equip_uris") is not None:
        uris = body["equip_uris"]
        if not isinstance(uris, list) or not all(isinstance(u, str) for u in uris):
            raise ValueError("equip_uris is a list of equipment ids")
        missing = [u for u in uris if u not in by_id]
        if missing:
            raise ValueError(f"not equipment in this project's inventory: {', '.join(missing[:5])}")
    elif body.get("equip_class"):
        uris = [e["id"] for e in equips if procedures.applies({"equip_class": str(body["equip_class"])}, e)]
    else:
        raise ValueError("name the scope: equip_uris or equip_class")
    uris = sorted(set(uris))
    if not uris or len(uris) > fleet.MAX_MEMBERS:
        raise ValueError(f"a scheduled retest covers 1-{fleet.MAX_MEMBERS} pieces of equipment")
    mode = body.get("mode", "all")
    if mode not in MODES:
        raise ValueError("mode is all (every member each time) or failures (only those that failed last time)")
    scope = {"equip_uris": uris, "kind": kind, "test": test if kind == "library" else None,
             "response_minutes": _num(body, "response_minutes", 0.01, active.MAX_HOLD_MINUTES, 2, integer=False),
             "concurrency_per_system": _num(body, "concurrency_per_system", 1, fleet.MAX_CONCURRENT, fleet.DEFAULTS["concurrency_per_system"]),
             "max_concurrent": _num(body, "max_concurrent", 1, fleet.MAX_CONCURRENT, fleet.DEFAULTS["max_concurrent"]),
             "stagger_seconds": _num(body, "stagger_seconds", 0, fleet.MAX_STAGGER_SECONDS, fleet.DEFAULTS["stagger_seconds"]),
             "max_failures": _num(body, "max_failures", 1, fleet.MAX_MEMBERS, fleet.DEFAULTS["max_failures"]), "mode": mode}
    try:
        first = _dt(body.get("first_window_start"))
        until = _dt(body.get("valid_until"))
    except (TypeError, ValueError) as exc:
        raise ValueError("first_window_start and valid_until are ISO date-times with a time zone") from exc
    if first.tzinfo is None or until.tzinfo is None:
        raise ValueError("first_window_start and valid_until need a time zone")
    window_minutes = _num(body, "window_minutes", 1, active.MAX_WINDOW.total_seconds() / 60)
    every_days = _num(body, "every_days", 1, 366)
    if until <= first + timedelta(minutes=window_minutes):
        raise ValueError("valid_until must be after the end of the first window")
    if until - now > MAX_VALIDITY:
        raise ValueError(f"an approval lasts at most {MAX_VALIDITY.days} days: valid_until is too far out")
    if until <= now:
        raise ValueError("valid_until has already passed")
    window = {"first_window_start": first.isoformat(), "window_minutes": window_minutes, "every_days": every_days,
              "valid_until": until.isoformat()}
    return scope, window


def new(project_id: str, name: str, scope: dict, window: dict, user_sub: str | None, now: datetime) -> dict:
    s = {"id": db.new_id(), "project_id": project_id, "name": name[:200] or "Scheduled retest", "status": "proposed",
         "scope": scope, "timing": window, "approval": None, "approval_hash": None, "approved_by": None, "approved_at": None,
         "approver_claims": None, "valid_until": _dt(window["valid_until"]), "created_by": user_sub, "created_at": now,
         "updated_at": now, "last_occurrence": None, "last_fleet_id": None, "revoked_by": None, "revoked_at": None,
         "history": []}
    s["approval_hash"] = approval_hash(s)
    return s


def narrows(new_scope: dict, new_window: dict, envelope: dict) -> list[str]:
    """Why (scope, window) is NOT inside the approved envelope; empty when
    it is. Inside means: no equipment added, the same test, limits no
    higher, a stagger no shorter, the same cadence, windows no longer,
    valid_until no later, and mode the same or all -> failures."""
    a_scope, a_window = envelope["scope"], envelope["timing"]
    why = []
    extra = sorted(set(new_scope["equip_uris"]) - set(a_scope["equip_uris"]))
    if extra:
        why.append(f"equipment outside the approved scope: {', '.join(extra[:5])}")
    for k in ("kind", "test", "response_minutes"):
        if new_scope.get(k) != a_scope.get(k):
            why.append(f"{k} differs from the approved {a_scope.get(k)!r}")
    for k in LIMIT_KEYS:
        if new_scope[k] > a_scope[k]:
            why.append(f"{k} {new_scope[k]} is above the approved {a_scope[k]}")
    if new_scope["stagger_seconds"] < a_scope["stagger_seconds"]:
        why.append(f"stagger_seconds {new_scope['stagger_seconds']} is below the approved {a_scope['stagger_seconds']}")
    if new_scope["mode"] != a_scope["mode"] and not (a_scope["mode"] == "all" and new_scope["mode"] == "failures"):
        why.append("mode can only narrow from all to failures")
    for k in ("first_window_start", "every_days"):
        if _norm(new_window[k]) != _norm(a_window[k]):
            why.append(f"{k} differs from the approved {a_window[k]} (the cadence can't move)")
    if new_window["window_minutes"] > a_window["window_minutes"]:
        why.append(f"window_minutes {new_window['window_minutes']} is longer than the approved {a_window['window_minutes']}")
    if _dt(new_window["valid_until"]) > _dt(a_window["valid_until"]):
        why.append(f"valid_until is later than the approved {a_window['valid_until']}")
    return why


def _norm(v):
    try:
        return _dt(v)
    except (TypeError, ValueError):
        return v


def patch(s: dict, body: dict, now: datetime) -> dict:
    """Changes for PATCH: only narrowing fields, and (approved) only inside
    the envelope. Raises ValueError (bad input) or PermissionError (would
    widen what was approved)."""
    allowed = {"name", "equip_uris", "window_minutes", "valid_until", "stagger_seconds", "mode", *LIMIT_KEYS}
    unknown = set(body) - allowed
    if unknown:
        raise PermissionError(f"{', '.join(sorted(unknown))} can't change on a schedule: propose a new one for approval")
    if s["status"] in ("revoked", "expired"):
        raise PermissionError(f"the schedule is {s['status']}")
    scope, window = dict(s["scope"]), dict(s["timing"])
    if "equip_uris" in body:
        uris = body["equip_uris"]
        if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
            raise ValueError("equip_uris is a non-empty list of equipment ids")
        scope["equip_uris"] = sorted(set(uris))
    for k in (*LIMIT_KEYS, "stagger_seconds"):
        if k in body:
            hi = fleet.MAX_STAGGER_SECONDS if k == "stagger_seconds" else (fleet.MAX_MEMBERS if k == "max_failures" else fleet.MAX_CONCURRENT)
            scope[k] = _num(body, k, 0 if k == "stagger_seconds" else 1, hi)
    if "mode" in body:
        if body["mode"] not in MODES:
            raise ValueError("mode is all or failures")
        scope["mode"] = body["mode"]
    if "window_minutes" in body:
        window["window_minutes"] = _num(body, "window_minutes", 1, active.MAX_WINDOW.total_seconds() / 60)
    if "valid_until" in body:
        try:
            window["valid_until"] = _dt(body["valid_until"]).isoformat()
        except (TypeError, ValueError) as exc:
            raise ValueError("valid_until is an ISO date-time with a time zone") from exc
    # whatever the status, a PATCH only ever narrows what was proposed
    envelope = s["approval"] or {"scope": s["scope"], "timing": s["timing"]}
    why = narrows(scope, window, envelope)
    if why:
        raise PermissionError("an approved schedule can't widen its scope or extend its window - propose a new one: "
                              + "; ".join(why))
    changes = {"scope": scope, "timing": window, "valid_until": _dt(window["valid_until"]), "updated_at": now,
               "name": str(body.get("name", s["name"]))[:200]}
    changes["history"] = [*s.get("history", []), {"at": now.isoformat(), "changed": sorted(body)}]
    if s["status"] == "proposed":
        changes["approval_hash"] = approval_hash({**s, **changes})
    return changes


def occurrence(window: dict, now: datetime) -> tuple[int, datetime, datetime] | None:
    """(k, start, end) of the scheduled window `now` is inside, or None.
    Windows never run past valid_until."""
    first = _dt(window["first_window_start"])
    if now < first:
        return None
    period = timedelta(days=window["every_days"])
    k = int((now - first) / period)
    start = first + k * period
    end = min(start + timedelta(minutes=window["window_minutes"]), _dt(window["valid_until"]))
    return (k, start, end) if start <= now < end else None


def next_window(window: dict, now: datetime) -> str | None:
    first = _dt(window["first_window_start"])
    period = timedelta(days=window["every_days"])
    k = 0 if now < first else int((now - first) / period) + 1
    start = first + k * period
    return start.isoformat() if start < _dt(window["valid_until"]) else None


def public(s: dict, now: datetime) -> dict:
    out = db.public_row(s)
    out["next_window"] = next_window(s["timing"], now) if s["status"] in ("proposed", "approved") else None
    return out


class Ticker:
    """The scheduler-tick hook: starts the fleet for each approved
    schedule's current window, once."""

    def __init__(self, repo, fleet_runner: fleet.FleetRunner | None):
        self.repo, self.fleet_runner = repo, fleet_runner

    def _event(self, s: dict, action: str, detail: dict, now: datetime) -> None:
        self.repo.add_event(s["project_id"], action, s.get("approved_by"), None,
                            {"schedule_id": s["id"], "approval_hash": (s.get("approval") or {}).get("hash"), **detail}, now)

    def __call__(self, now: datetime) -> list[str]:
        started = []
        for s in self.repo.find_rows(TABLE, status="approved"):
            try:
                got = self.one(s, now)
                if got:
                    started.append(got)
            except Exception:
                log.exception("cx scheduled retest %s", s["id"])
        return started

    def one(self, s: dict, now: datetime) -> str | None:
        envelope = s.get("approval")
        if not envelope:
            return None  # approved without an envelope can't happen through the API; never run one
        if now >= _dt(envelope["timing"]["valid_until"]) or now >= _dt(s["timing"]["valid_until"]):
            self.repo.update_row(TABLE, s["id"], {"status": "expired", "updated_at": now})
            self._event(s, "active_schedule.expire", {}, now)
            return None
        occ = occurrence(s["timing"], now)
        if occ is None:
            return None
        k, ws, we = occ
        if s.get("last_occurrence") is not None and k <= s["last_occurrence"]:
            return None

        def skip(reason: str) -> None:
            self.repo.update_row(TABLE, s["id"], {"last_occurrence": k, "updated_at": now})
            self._event(s, "active_schedule.skip", {"occurrence": k, "window_start": ws.isoformat(), "reason": reason}, now)

        why = narrows(s["scope"], s["timing"], envelope)
        if why:
            skip("outside its approval: " + "; ".join(why))
            return None
        if not active.writes_allowed():
            skip("active tests are switched off (BALER_CX_ALLOW_WRITES)")
            return None
        if self.fleet_runner is None:
            skip("no point_write route is configured")
            return None
        project = self.repo.get_project(s["project_id"])
        if project is None or project.get("archived_at"):
            skip("the project is archived")
            return None
        prev = self.repo.get_row(fleet.TABLE, s["last_fleet_id"]) if s.get("last_fleet_id") else None
        if prev is not None and prev["status"] not in fleet.TERMINAL:
            skip("the previous scheduled run is still running")
            return None
        scope = s["scope"]
        members = list(scope["equip_uris"])
        if scope["mode"] == "failures" and prev is not None:
            failed = set(fleet.failed_members(prev))
            members = [u for u in members if u in failed]
            if not members:
                skip("nothing failed last time: nothing to retest")
                return None
        claims = s.get("approver_claims") or {"org_id": project["org_id"], "site_ids": []}
        scopes, _ = runner.project_scopes(self.repo, project, claims)
        equips = fleet_inventory(self.repo, scopes)
        visible = {e["id"] for e in equips}
        members = [u for u in members if u in visible]
        if not members:
            skip("none of the approved equipment is in the approver's view of the inventory")
            return None
        body = {"equip_uris": members, "kind": scope["kind"], "test": scope["test"], "response_minutes": scope["response_minutes"],
                "window_start": now.isoformat(), "window_end": we.isoformat(),
                **{k: scope[k] for k in (*LIMIT_KEYS, "stagger_seconds")}}
        try:
            f = fleet.plan(self.repo, project, equips, body, f"schedule:{s['id']}", now)
        except ValueError as exc:
            skip(f"the plan was refused: {exc}")
            return None
        # once more, on what was actually planned: inside the approval
        if not {m["equip_uri"] for m in f["members"]} <= set(envelope["scope"]["equip_uris"]) \
                or _dt(f["window_end"]) > _dt(envelope["timing"]["valid_until"]) \
                or any(f["params"][k] > envelope["scope"][k] for k in LIMIT_KEYS) \
                or f["params"]["stagger_seconds"] < envelope["scope"]["stagger_seconds"]:
            skip("the planned run fell outside its approval")
            return None
        f.update(status="confirmed", confirmed_by=s["approved_by"], confirmed_at=now, heartbeat_at=now, schedule_id=s["id"])
        self.repo.put_rows(fleet.TABLE, [f])
        self.repo.update_row(TABLE, s["id"], {"last_occurrence": k, "last_fleet_id": f["id"], "updated_at": now})
        self._event(s, "active_schedule.run", {"occurrence": k, "window_start": ws.isoformat(), "window_end": we.isoformat(),
                                               "fleet_run_id": f["id"], "plan_hash": f["plan_hash"],
                                               "members": len(f["members"]), "writes": len(f["plan"])}, now)
        self.repo.add_event(s["project_id"], "fleet_run.confirm", s["approved_by"], None,
                            {"fleet_run_id": f["id"], "plan_hash": f["plan_hash"], "schedule_id": s["id"],
                             "members": len(f["members"]), "writes": len(f["plan"])}, now)
        self.fleet_runner.start(f["id"])
        return f["id"]


def fleet_inventory(repo, scopes) -> list[dict]:
    from baler.commissioning import inventory

    return inventory.build(repo.store, scopes)

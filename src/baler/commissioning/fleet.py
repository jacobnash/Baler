"""
Fleet active runs (Baler#275 slice d): one Cx 9 active test across many
equipment in one window - every terminal unit overnight - with
per-system concurrency, a stagger, an automatic stop at a failure
threshold, and retest-failures-only.

A fleet is a scheduler on top of active.py, not a second write path.
Each piece of equipment is still one ordinary `cx_active_runs` row
(`fleet_id` set) run by active.Executor, so every #45 safeguard applies
per write exactly as it does to a single run: the deployment switch
(BALER_CX_ALLOW_WRITES), admin only, the window, expiring writes,
priority 8 as `cx:<run id>`, relinquish in each step's `finally` and the
run's outer `finally`, the sweeper, the kill switch and the before-state
captured before each equipment's first write. Nothing in active.py
changes; this module only adds:

  - planning: every member's steps (command_response or a library test),
    its system (the equipment that feeds it, brick:feeds/isFedBy), and a
    slot - when it may start - simulated with the concurrency limits and
    stagger. The dry run is the flat list of every write across the
    fleet. A plan that doesn't fit the window is refused.
  - the plan hash: sha256 over the fleet id, test, window, limits and
    every member's steps and slot. The typed confirmation IS that hash;
    at confirmation the stored plan is re-hashed and every member is
    re-validated against today's inventory, so a plan that changed in any
    way (tampered, a point no longer writable, the equipment gone) can't
    be confirmed with the code the admin read.
  - the coordinator (FleetRunner): starts members in plan order, never
    before their slot, never past the concurrency limits or the stagger,
    re-checking the switch and the abort flag before every start. When
    failures reach the threshold, or on abort / kill, or if the switch is
    turned off, it stops starting members, flags the fleet so every child
    stops at its next check (ChildExecutor), relinquishes every level the
    fleet's children hold, waits for them, and relinquishes again.
  - persisted fleet state for crashes: a member's run id is written to the
    fleet row before its child row exists, the coordinator beats a
    heartbeat, and `sweep` (every scheduler tick) relinquishes every Cx
    level whose child belongs to a fleet that is stopped, aborted,
    finished or whose heartbeat went stale. `recover` (at startup) marks
    fleets the restart interrupted as failed and sweeps.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import traceback
from collections.abc import Callable
from datetime import datetime, timedelta

from baler.commissioning import active, db, procedures, test_library

log = logging.getLogger(__name__)

TABLE = "cx_fleet_runs"
MAX_MEMBERS = 500
MAX_CONCURRENT = 50
MAX_STAGGER_SECONDS = 3600
HEARTBEAT_STALE = timedelta(minutes=3)
TERMINAL = ("completed", "stopped", "aborted", "expired", "failed", "cancelled")
LIVE_CHILD = ("planned", "confirmed", "running")
NO_SYSTEM = "(no system)"
DEFAULTS = {"concurrency_per_system": 1, "max_concurrent": 10, "stagger_seconds": 30, "max_failures": 3}


def _dt(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))


def _int(body: dict, key: str, lo: int, hi: int) -> int:
    v = body.get(key, DEFAULTS[key])
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError(f"{key} must be an integer in [{lo}, {hi}]")
    return v


# ---------------------------------------------------------------- planning


def systems_of(store, equip_uris: list[str]) -> dict[str, str]:
    """The equipment that feeds each one (brick:feeds or brick:isFedBy):
    a VAV's AHU. Equipment nothing feeds shares one NO_SYSTEM bucket, so
    unknown topology is limited as one system, never as many."""
    from baler.store import BRICK

    out = {u: NO_SYSTEM for u in equip_uris}
    if not equip_uris:
        return out
    values = " ".join(f"<{u}>" for u in equip_uris if not any(c in u for c in "<> \"{}|\\^`"))
    rows = store.query(f"SELECT ?e ?up WHERE {{ VALUES ?e {{ {values} }} {{ ?up <{BRICK}feeds> ?e }}"
                       f" UNION {{ ?e <{BRICK}isFedBy> ?up }} }}")
    found: dict[str, list[str]] = {}
    for r in rows:
        found.setdefault(str(r[0]), []).append(str(r[1]))
    for e, ups in found.items():
        out[e] = min(ups)
    return out


def interleave(members: list[dict]) -> list[dict]:
    """Members ordered one system at a time, round robin: system A's first,
    B's first, ..., A's second - so systems run side by side."""
    by: dict[str, list[dict]] = {}
    for m in sorted(members, key=lambda m: (m["system"], m["equip_uri"])):
        by.setdefault(m["system"], []).append(m)
    out = []
    queues = [by[k] for k in sorted(by)]
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0))
    return out


def schedule(members: list[dict], start: datetime, end: datetime, per_system: int, max_concurrent: int,
             stagger: timedelta) -> None:
    """Give each member (in order) the earliest slot that keeps at most
    `per_system` of its system and `max_concurrent` in all running, and
    starts at least `stagger` after the previous member - assuming every
    member runs for its full hold. Sets slot_start / slot_end; raises
    ValueError if the last member wouldn't finish inside the window."""
    running: list[tuple[datetime, str]] = []
    last: datetime | None = None
    for m in members:
        t = start if last is None else max(start, last + stagger)
        while True:
            live = [(e, s) for e, s in running if e > t]
            same = [e for e, s in live if s == m["system"]]
            if len(live) < max_concurrent and len(same) < per_system:
                break
            blockers = [e for e, _ in live] if len(live) >= max_concurrent else same
            t = min(blockers)
        finish = t + timedelta(minutes=m["minutes"])
        if finish > end:
            raise ValueError(f"the fleet doesn't fit the window: {m['equip_uri']} would finish at {finish.isoformat()}, after "
                             f"{end.isoformat()} - raise the concurrency, shorten the stagger, or widen the window")
        running.append((finish, m["system"]))
        last = t
        m["slot_start"], m["slot_end"] = t.isoformat(), finish.isoformat()


def steps_for(kind: str, test: str | None, response_minutes: float, equip: dict, latest: dict) -> list[dict]:
    """The validated active-run steps for one member. Raises ValueError
    saying why this equipment can't take the test."""
    if kind == "command_response":
        steps = active.command_response_steps(equip, response_minutes)
        if not steps:
            raise ValueError("no command/status pair to test")
        return active.validate_steps(steps, equip)
    if not test_library.applies(str(test), equip):
        raise ValueError(f"{test} doesn't apply to this equipment's class")
    return active.validate_steps(test_library.compile_steps(str(test), equip, latest), equip)


def plan_hash(fleet: dict) -> str:
    """sha256 over everything the confirmation stands for."""
    body = {"id": fleet["id"], "project_id": fleet["project_id"], "kind": fleet["kind"],
            "window_start": _dt(fleet["window_start"]).isoformat(), "window_end": _dt(fleet["window_end"]).isoformat(),
            "params": fleet["params"],
            "members": [{k: m[k] for k in ("equip_uri", "system", "steps", "slot_start", "slot_end")} for m in fleet["members"]],
            "plan": fleet["plan"]}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def failed_members(fleet: dict) -> list[str]:
    return [m["equip_uri"] for m in fleet["members"] if m.get("outcome") == "fail"]


def plan(repo, project: dict, equips: list[dict], body: dict, user_sub: str | None, now: datetime,
         previous: dict | None = None) -> dict:
    """A planned fleet (not stored): the dry run. `equips` is the
    project's inventory as the planner may see it. `previous` is the fleet
    to retest the failures of. Raises ValueError."""
    try:
        start = _dt(str(body.get("window_start")).replace("Z", "+00:00"))
        end = _dt(str(body.get("window_end")).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("window_start and window_end are ISO date-times with a time zone") from exc
    active.check_window(start, end, now)
    by_id = {e["id"]: e for e in equips}
    if previous is not None:
        if previous["status"] not in TERMINAL:
            raise ValueError("retest_of must be a finished fleet run")
        kind, test = previous["params"]["kind"], previous["params"].get("test")
        rm = previous["params"].get("response_minutes", 2)
        wanted = failed_members(previous)
        if not wanted:
            raise ValueError("nothing failed in that fleet run: there is nothing to retest")
    else:
        kind, test = body.get("kind", "command_response"), body.get("test")
        rm = body.get("response_minutes", 2)
        if kind not in ("command_response", "library"):
            raise ValueError("kind is command_response or library (a fleet runs the same test on every member)")
        if kind == "library" and test not in test_library.BY_ID:
            raise ValueError(f"test must be a library test (library: {', '.join(test_library.BY_ID)})")
        if body.get("equip_uris") is not None:
            wanted = body["equip_uris"]
            if not isinstance(wanted, list) or not all(isinstance(u, str) for u in wanted):
                raise ValueError("equip_uris is a list of equipment ids")
            missing = [u for u in wanted if u not in by_id]
            if missing:
                raise ValueError(f"not equipment in this project's inventory: {', '.join(missing[:5])}")
        elif body.get("equip_class"):
            cls = str(body["equip_class"])
            wanted = [e["id"] for e in equips if procedures.applies({"equip_class": cls}, e)]
        else:
            raise ValueError("name the members: equip_uris, equip_class, or retest_of")
    if isinstance(rm, bool) or not isinstance(rm, (int, float)) or not 0 < rm <= active.MAX_HOLD_MINUTES:
        raise ValueError(f"response_minutes must be in (0, {active.MAX_HOLD_MINUTES}]")
    wanted = list(dict.fromkeys(wanted))
    if not wanted or len(wanted) > MAX_MEMBERS:
        raise ValueError(f"a fleet run has 1-{MAX_MEMBERS} members")
    params = {"kind": kind, "test": test if kind == "library" else None, "response_minutes": rm,
              "concurrency_per_system": _int(body, "concurrency_per_system", 1, MAX_CONCURRENT),
              "max_concurrent": _int(body, "max_concurrent", 1, MAX_CONCURRENT),
              "stagger_seconds": _int(body, "stagger_seconds", 0, MAX_STAGGER_SECONDS),
              "max_failures": _int(body, "max_failures", 1, MAX_MEMBERS)}
    latest: dict = {}
    if kind == "library":
        with repo.history() as h:
            latest = h.latest(sorted({p["id"] for u in wanted if u in by_id for p in by_id[u]["points"]}))
    systems = systems_of(repo.store, [u for u in wanted if u in by_id])
    members, skipped = [], []
    for u in wanted:
        equip = by_id.get(u)
        if equip is None:
            skipped.append({"equip_uri": u, "reason": "no longer in this project's inventory"})
            continue
        try:
            steps = steps_for(kind, test, float(rm), equip, latest)
        except (ValueError, TypeError) as exc:
            skipped.append({"equip_uri": u, "reason": str(exc)[:300]})
            continue
        members.append({"equip_uri": u, "dis": equip.get("dis"), "system": systems.get(u, NO_SYSTEM), "steps": steps,
                        "minutes": sum(st["hold_minutes"] for st in steps), "state": "pending", "run_id": None,
                        "outcome": None, "started_at": None, "note": None})
    if not members:
        raise ValueError("no member can take this test: " + "; ".join(f"{s['equip_uri']}: {s['reason']}" for s in skipped[:5]))
    members = interleave(members)
    schedule(members, start, end, params["concurrency_per_system"], params["max_concurrent"],
             timedelta(seconds=params["stagger_seconds"]))
    flat = []
    for m in members:
        for w in active.plan(m["steps"], _dt(m["slot_start"]), end):
            flat.append({"equip_uri": m["equip_uri"], **w, "not_before": m["slot_start"]})
    fleet = {"id": db.new_id(), "project_id": project["id"], "kind": kind if kind != "library" else f"library:{test}",
             "params": params, "members": members, "skipped": skipped, "plan": flat, "plan_hash": None, "status": "planned",
             "window_start": start, "window_end": end, "retest_of": previous["id"] if previous else None,
             "created_by": user_sub, "created_at": now, "confirmed_by": None, "confirmed_at": None, "started_at": None,
             "finished_at": None, "heartbeat_at": None, "failures": 0, "stop_reason": None, "error": None,
             "abort_requested": False}
    fleet["plan_hash"] = plan_hash(fleet)
    return fleet


def verify(fleet: dict, equips: list[dict]) -> list[str]:
    """Why the stored plan is no longer the plan that was hashed, or no
    longer valid against today's inventory - empty when it still is."""
    problems = []
    if plan_hash(fleet) != fleet.get("plan_hash"):
        problems.append("the stored plan no longer matches its hash")
    by_id = {e["id"]: e for e in equips}
    for m in fleet["members"]:
        equip = by_id.get(m["equip_uri"])
        if equip is None:
            problems.append(f"{m['equip_uri']} is no longer in your view of the project's inventory")
            continue
        try:
            if active.validate_steps(m["steps"], equip) != m["steps"]:
                problems.append(f"{m['equip_uri']}: its steps no longer validate unchanged")
            elif fleet["params"]["kind"] == "command_response" and active.validate_steps(
                    active.command_response_steps(equip, float(fleet["params"]["response_minutes"])), equip) != m["steps"]:
                problems.append(f"{m['equip_uri']}: its command/status pairs changed")
        except (ValueError, TypeError) as exc:
            problems.append(f"{m['equip_uri']}: {exc}")
    return problems


# --------------------------------------------------------------- executing


def before_state(repo, steps: list[dict], now: datetime) -> dict:
    """What the single-run confirmation captures (#45 safeguard 8): the
    latest value of every point written or watched, and Baler's priority
    array of every written point - before the first write."""
    points = sorted({w["point"] for st in steps for w in st["writes"]} | {e["point"] for st in steps for e in st["expect"]})
    with repo.history() as h:
        latest = h.latest(points)
    written = sorted({w["point"] for st in steps for w in st["writes"]})
    return {"latest": {u: {"ts": ts.isoformat(), "value": v if isinstance(v, (int, float, str, bool)) else str(v)}
                       for u, (ts, v) in latest.items()},
            "priority_arrays": {u: repo.point_levels(u) for u in written}, "captured_at": now.isoformat()}


def outcome(child: dict) -> str | None:
    """A finished member: pass | fail | inconclusive | aborted. None while
    it runs."""
    st = child["status"]
    if st in LIVE_CHILD:
        return None
    if st in ("aborted", "cancelled") or (st == "failed" and child.get("error") == db.INTERRUPTED):
        return "aborted"
    if st == "failed":
        return "fail"
    results = child.get("results") or []
    if any(r.get("status") == "fail" for r in results):
        return "fail"
    if st == "completed" and results and all(r.get("status") == "pass" for r in results):
        return "pass"
    return "inconclusive"


class ChildExecutor(active.Executor):
    """active.Executor, unchanged, except that a fleet member also stops
    when its fleet is stopped, aborted or finished - checked before every
    step and between polls, exactly where the run's own abort flag is.
    Only ever adds a reason to stop."""

    def _aborted(self, run_id: str) -> bool:
        if super()._aborted(run_id):
            return True
        r = self.repo.get_row("cx_active_runs", run_id)
        fid = r.get("fleet_id") if r else None
        if not fid:
            return False
        f = self.repo.get_row(TABLE, fid)
        return f is None or bool(f.get("abort_requested")) or f["status"] in TERMINAL


def child_ids(repo, fleet: dict) -> set[str]:
    ids = {m["run_id"] for m in fleet["members"] if m.get("run_id")}
    return ids | {r["id"] for r in repo.find_rows("cx_active_runs", fleet_id=fleet["id"])}


def relinquish_children(repo, writer: active.Writer, fleet: dict) -> list[str]:
    """Relinquish every Cx level any of the fleet's children holds, now."""
    ids = child_ids(repo, fleet)
    done = []
    for point, level, owner, _ in repo.cx_levels():
        rid = owner[len(active.WHO_PREFIX):] if owner.startswith(active.WHO_PREFIX) else None
        if rid in ids:
            try:
                writer.relinquish(point, rid, level)
                done.append(point)
            except Exception as exc:  # noqa: BLE001 - the sweeper retries
                log.warning("cx fleet %s: relinquish %s failed: %s", fleet["id"], point, exc)
    return done


def flag_stop(repo, writer: active.Writer | None, fleet_id: str, reason: str) -> list[str]:
    """Abort / kill / threshold: the fleet and every live child are
    flagged (children stop at their next check), and every level the
    fleet's children hold is relinquished now if a writer is configured."""
    fleet = repo.get_row(TABLE, fleet_id)
    if fleet is None:
        return []
    if not fleet.get("abort_requested"):
        repo.update_row(TABLE, fleet_id, {"abort_requested": True, "stop_reason": reason})
    for rid in child_ids(repo, fleet):
        c = repo.get_row("cx_active_runs", rid)
        if c is not None and c["status"] in LIVE_CHILD and not c.get("abort_requested"):
            repo.update_row("cx_active_runs", rid, {"abort_requested": True})
    return relinquish_children(repo, writer, fleet) if writer is not None else []


class FleetRunner:
    """Runs confirmed fleets, one coordinator thread each. Clock and sleep
    are injectable, as active.Executor's are."""

    def __init__(self, repo, executor: ChildExecutor, clock: Callable[[], datetime],
                 sleep: Callable[[float], None] = time.sleep, poll_seconds: float = active.POLL_SECONDS):
        self.repo, self.executor, self.clock, self.sleep, self.poll = repo, executor, clock, sleep, poll_seconds
        self.writer = executor.writer
        self.threads: dict[str, threading.Thread] = {}
        self._stopping: dict[str, str] = {}

    def start(self, fleet_id: str) -> None:
        t = threading.Thread(target=self.run, args=(fleet_id,), name=f"cx-fleet-{fleet_id[:8]}", daemon=True)
        self.threads[fleet_id] = t
        t.start()

    def _event(self, fleet: dict, action: str, detail: dict) -> None:
        self.repo.add_event(fleet["project_id"], action, fleet.get("confirmed_by"), None, {"fleet_run_id": fleet["id"], **detail},
                            self.clock())

    def run(self, fleet_id: str) -> None:
        fleet = self.repo.get_row(TABLE, fleet_id)
        if fleet is None:
            return
        status, error = "failed", None
        try:
            self.repo.update_row(TABLE, fleet_id, {"status": "running", "started_at": self.clock(), "heartbeat_at": self.clock()})
            while True:
                got = self.tick(fleet_id)
                if got is not None:
                    status = got
                    break
                self.sleep(self.poll)
        except Exception as exc:  # noqa: BLE001 - recorded; the finally relinquishes regardless
            log.error("cx fleet %s failed:\n%s", fleet_id, traceback.format_exc())
            error = f"{type(exc).__name__}: {exc}"[:2000]
            flag_stop(self.repo, None, fleet_id, f"the coordinator failed: {error}")
        finally:
            fleet = self.repo.get_row(TABLE, fleet_id) or fleet
            relinquish_children(self.repo, self.writer, fleet)
            self.repo.update_row(TABLE, fleet_id, {"status": status, "error": error, "finished_at": self.clock()})
            self._event(fleet, f"fleet_run.{status}", {"failures": fleet.get("failures", 0), "stop_reason": fleet.get("stop_reason"),
                                                       "error": error})

    def tick(self, fleet_id: str) -> str | None:
        """One pass: record finished members, stop if needed, start what
        may start. The fleet's final status once it's done, else None."""
        fleet = self.repo.get_row(TABLE, fleet_id)
        if fleet is None:
            return "failed"
        now = self.clock()
        p = fleet["params"]
        members = fleet["members"]
        end = _dt(fleet["window_end"])
        live: list[dict] = []
        for m in members:
            if m["state"] != "started":
                continue
            child = self.repo.get_row("cx_active_runs", m["run_id"])
            if child is None:
                m["state"], m["outcome"] = "done", "aborted"
                continue
            got = outcome(child)
            if got is None:
                live.append(m)
            else:
                m["state"], m["outcome"] = "done", got
        failures = sum(1 for m in members if m.get("outcome") == "fail")
        self.repo.update_row(TABLE, fleet_id, {"members": members, "failures": failures, "heartbeat_at": now})

        stop = None
        if fleet.get("abort_requested"):
            # stopped by this coordinator (threshold, switch) on an earlier
            # tick, or aborted from outside (abort, kill)
            stop = (self._stopping.get(fleet_id, "aborted"), fleet.get("stop_reason") or "aborted")
        elif failures >= p["max_failures"]:
            stop = ("stopped", f"failure threshold reached: {failures} failed (max_failures {p['max_failures']})")
        elif not active.writes_allowed():
            stop = ("stopped", "BALER_CX_ALLOW_WRITES was switched off")
        if stop is not None:
            if not fleet.get("abort_requested"):
                self._stopping[fleet_id] = stop[0]
                self._event(fleet, "fleet_run.stop", {"reason": stop[1], "failures": failures})
            flag_stop(self.repo, self.writer, fleet_id, stop[1])
            for m in members:
                if m["state"] == "pending":
                    m["state"], m["note"] = "not_run", stop[1]
            self.repo.update_row(TABLE, fleet_id, {"members": members})
            return None if live else stop[0]

        if now >= end:
            for m in members:
                if m["state"] == "pending":
                    m["state"], m["note"] = "skipped", "the window ended before its turn"
        stagger = timedelta(seconds=p["stagger_seconds"])
        starts = [_dt(m["started_at"]) for m in members if m.get("started_at")]
        last = max(starts) if starts else None
        for m in members:
            if m["state"] != "pending":
                continue
            if now < _dt(m["slot_start"]):
                break
            if now + timedelta(minutes=m["minutes"]) > end:
                m["state"], m["note"] = "skipped", "not enough of the window left for its hold"
                continue
            if len(live) >= p["max_concurrent"] or sum(1 for x in live if x["system"] == m["system"]) >= p["concurrency_per_system"]:
                break
            if last is not None and now - last < stagger:
                break
            if not self._start_member(fleet, m, now):
                break
            live.append(m)
            last = now
        self.repo.update_row(TABLE, fleet_id, {"members": members})
        if live or any(m["state"] == "pending" for m in members):
            return None
        return "expired" if any(m["state"] == "skipped" for m in members) else "completed"

    def _start_member(self, fleet: dict, m: dict, now: datetime) -> bool:
        """Create the member's child run (confirmed, before-state captured)
        and start it. Its id is persisted on the fleet first, so a crash
        after this point leaves state the sweeper can find."""
        cur = self.repo.get_row(TABLE, fleet["id"])
        if cur is None or cur.get("abort_requested") or not active.writes_allowed():
            return False
        child = active.new_run(fleet["project_id"], m["equip_uri"], fleet["kind"], m["steps"], now, _dt(fleet["window_end"]),
                               fleet.get("created_by"), now)
        m["run_id"], m["state"], m["started_at"] = child["id"], "started", now.isoformat()
        self.repo.update_row(TABLE, fleet["id"], {"members": fleet["members"]})
        child.update(status="confirmed", confirmed_by=fleet.get("confirmed_by"), confirmed_at=now, fleet_id=fleet["id"],
                     before=before_state(self.repo, m["steps"], now))
        self.repo.put_rows("cx_active_runs", [child])
        self.repo.add_event(fleet["project_id"], "active_run.confirm", fleet.get("confirmed_by"), None,
                            {"active_run_id": child["id"], "fleet_run_id": fleet["id"], "writes": len(child["plan"])}, now)
        self.executor.start(child["id"])
        return True


# ------------------------------------------------------- sweeping, crashes


def sweep(repo, writer: active.Writer, now: datetime) -> list[tuple[str, str]]:
    """Every scheduler tick, from persisted state only:
    - a running fleet whose coordinator stopped beating is failed (a
      crashed thread, or a process that died without `recover`);
    - every live child of a stopped / aborted / finished fleet is flagged;
    - every Cx level held by such a child is relinquished.
    active.sweep still runs too: it relinquishes any level whose run
    isn't running, whatever its fleet. Returns (point, who) relinquished."""
    for f in repo.find_rows(TABLE, status="running") + repo.find_rows(TABLE, status="confirmed"):
        beat = f.get("heartbeat_at") or f.get("confirmed_at")
        if beat is not None and now - _dt(beat) > HEARTBEAT_STALE:
            repo.update_row(TABLE, f["id"], {"status": "failed", "abort_requested": True, "finished_at": now,
                                             "error": "the fleet coordinator stopped responding",
                                             "stop_reason": f.get("stop_reason") or "coordinator lost"})
            repo.add_event(f["project_id"], "fleet_run.failed", None, None,
                           {"fleet_run_id": f["id"], "error": "the fleet coordinator stopped responding"}, now)
    done = []
    fleets: dict[str, dict | None] = {}
    for point, level, owner, _ in repo.cx_levels():
        rid = owner[len(active.WHO_PREFIX):] if owner.startswith(active.WHO_PREFIX) else None
        child = repo.get_row("cx_active_runs", rid) if rid else None
        fid = child.get("fleet_id") if child else None
        if not fid:
            continue
        if fid not in fleets:
            fleets[fid] = repo.get_row(TABLE, fid)
        f = fleets[fid]
        if f is None or f.get("abort_requested") or f["status"] in TERMINAL:
            if child is not None and child["status"] in LIVE_CHILD and not child.get("abort_requested"):
                repo.update_row("cx_active_runs", child["id"], {"abort_requested": True})
            try:
                writer.relinquish(point, rid, level)
                done.append((point, owner))
            except Exception as exc:  # noqa: BLE001 - next tick retries
                log.warning("cx fleet sweeper: relinquish %s (%s) failed: %s", point, owner, exc)
    return done


def recover(repo, writer: active.Writer, now: datetime) -> list[dict]:
    """At startup, before anything runs: every fleet a restart interrupted
    (confirmed or running) is failed and flagged, its live children are
    failed, and the sweeper relinquishes what they held. Returns the
    fleets it failed."""
    hit = repo.find_rows(TABLE, status="running") + repo.find_rows(TABLE, status="confirmed")
    for f in hit:
        repo.update_row(TABLE, f["id"], {"status": "failed", "abort_requested": True, "error": db.INTERRUPTED, "finished_at": now,
                                         "stop_reason": f.get("stop_reason") or "interrupted by a restart"})
        for rid in child_ids(repo, f):
            c = repo.get_row("cx_active_runs", rid)
            if c is not None and c["status"] in LIVE_CHILD:
                repo.update_row("cx_active_runs", rid, {"status": "failed", "error": db.INTERRUPTED, "finished_at": now,
                                                        "abort_requested": True})
        repo.add_event(f["project_id"], "fleet_run.failed", None, None, {"fleet_run_id": f["id"], "error": db.INTERRUPTED}, now)
    sweep(repo, writer, now)
    return hit


def public(fleet: dict) -> dict:
    """The API shape: the row, plus a tally of the members' states."""
    out = db.public_row(fleet)
    tally: dict[str, int] = {}
    for m in fleet["members"]:
        key = m.get("outcome") or m["state"]
        tally[key] = tally.get(key, 0) + 1
    out["tally"] = tally
    return out


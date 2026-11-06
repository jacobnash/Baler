"""
Active functional tests through point_write, inside a safety envelope
(Cx 9, Baler#45; plan §6.4). This is the only Cx code that commands
equipment.

Every safeguard is required, and each is enforced here or at the API:

  1. Deployment switch: nothing is written unless BALER_CX_ALLOW_WRITES=1
     in commissioning_api's environment (`writes_allowed()`).
  2. Admin only: the gateway's policy rows, and the API checks the role
     in the verified JWT as well.
  3. A scheduled window: a run is planned for [window_start, window_end]
     and can only start inside it.
  4. Every write's expires_at is no later than the window end (and no
     later than its step's hold time).
  5. A dry run: planning a run lists every write (point, value, level,
     hold, expires_at) and the before-state; nothing happens until an
     admin confirms that exact run by typing its id.
  6. Guaranteed relinquish: each step relinquishes what it wrote in a
     `finally`, the run relinquishes everything it ever wrote in an outer
     `finally`, and a sweeper (the scheduler tick) relinquishes any Cx
     level whose run isn't running or whose expiry has passed.
  7. Kill switch: POST /active-runs/{id}/abort stops a run between polls
     (and relinquishes); POST /active/kill aborts every running run and
     relinquishes every Cx level at once.
  8. Before-state: the latest value of every point the run writes or
     watches, and Baler's priority array for each written point, are
     captured before the first write.

Every write and relinquish is logged on the run (and in cx_events), and
the run report shows exactly what was commanded.

Writes go at Haystack/BACnet priority CX_LEVEL (8, manual operator) with
`who` = "cx:<run id>", which is how the sweeper tells Cx's levels from a
person's.

Rung 4 of the ladder (command response) is the `command_response` test:
for a command/status pair (ladder.PAIRS), command on, expect the status
to follow within the response time, command off, expect it to follow
again. Without the switch it stays passive - the rung-3 cmd_status check
over history.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import traceback
from collections.abc import Callable
from datetime import datetime, timedelta

from baler.commissioning import db, ladder

log = logging.getLogger(__name__)

CX_LEVEL = 8
WHO_PREFIX = "cx:"
MAX_WINDOW = timedelta(hours=8)
MAX_HOLD_MINUTES = 60
MAX_STEPS = 20
POLL_SECONDS = 10.0
TERMINAL = ("completed", "failed", "aborted", "expired")


def writes_allowed() -> bool:
    return os.environ.get("BALER_CX_ALLOW_WRITES", "").strip() == "1"


def who(run_id: str) -> str:
    return WHO_PREFIX + run_id


# ---------------------------------------------------------------- planning


def _binary(v) -> bool:
    return isinstance(v, bool) or v in (0, 1)


def command_response_steps(equip: dict, response_minutes: float = 2.0) -> list[dict]:
    """Rung 4 for every command/status pair on the equipment."""
    steps: list[dict] = []
    for pair in ladder.PAIRS:
        if pair.kind != "cmd_status":
            continue
        cmd, _ = ladder._pick(equip["points"], pair.a, True)
        sts, _ = ladder._pick(equip["points"], pair.b, True)
        if cmd is None or sts is None:
            continue
        for value, word in ((1, "on"), (0, "off")):
            steps.append({"id": f"{pair.key}-{word}", "title": f"{pair.label.split(' follows')[0]}: command {word}",
                          "writes": [{"point": cmd["id"], "value": value}], "hold_minutes": response_minutes,
                          "expect": [{"point": sts["id"], "equals": value}], "within_minutes": response_minutes})
    return steps


def validate_steps(steps, equip: dict) -> list[dict]:
    """Every write is to a writable point of this equipment; every
    expectation is about one of its points. Raises ValueError/TypeError."""
    if not isinstance(steps, list) or not steps or len(steps) > MAX_STEPS:
        raise ValueError(f"steps must be a list of 1-{MAX_STEPS}")
    points = {p["id"]: p for p in equip["points"]}
    out = []
    for i, st in enumerate(steps):
        if not isinstance(st, dict):
            raise TypeError("a step is an object")
        sid = str(st.get("id") or f"step-{i + 1}")[:60]
        writes = st.get("writes")
        if not isinstance(writes, list) or not writes:
            raise ValueError(f"step {sid}: writes is a non-empty list of {{point, value}}")
        for w in writes:
            if not isinstance(w, dict) or w.get("point") not in points:
                raise ValueError(f"step {sid}: every write is to a point of {equip['id']}")
            if "writable" not in (points[w["point"]].get("tags") or ()):
                raise ValueError(f"step {sid}: {w['point']} isn't marked writable")
            if isinstance(w.get("value"), bool) or not isinstance(w.get("value"), (int, float)):
                raise TypeError(f"step {sid}: a write's value is a number (1/0 for on/off)")
        nums = {}
        for name, v in (("hold_minutes", st.get("hold_minutes", 2)), ("within_minutes", st.get("within_minutes", st.get("hold_minutes", 2)))):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= MAX_HOLD_MINUTES:
                raise ValueError(f"step {sid}: {name} must be in (0, {MAX_HOLD_MINUTES}]")
            nums[name] = float(v)
        hold, within = nums["hold_minutes"], nums["within_minutes"]
        if within > hold:
            raise ValueError(f"step {sid}: within_minutes can't exceed hold_minutes (the write is relinquished after the hold)")
        expect = st.get("expect") or []
        if not isinstance(expect, list):
            raise TypeError(f"step {sid}: expect is a list")
        for e in expect:
            if not isinstance(e, dict) or e.get("point") not in points or not ({"equals", "min", "max"} & set(e)):
                raise ValueError(f"step {sid}: each expectation is {{point (of this equipment), equals | min | max}}")
        out.append({"id": sid, "title": str(st.get("title") or sid)[:200], "writes": [{"point": w["point"], "value": w["value"]} for w in writes],
                    "hold_minutes": hold, "within_minutes": within, "expect": expect})
    return out


def plan(steps: list[dict], window_start: datetime, window_end: datetime) -> list[dict]:
    """The dry run: every write, when it would end at the latest, and its
    expires_at (never after the window end)."""
    t = window_start
    out = []
    for st in steps:
        hold = timedelta(minutes=st["hold_minutes"])
        for w in st["writes"]:
            out.append({"step": st["id"], "point": w["point"], "value": w["value"], "level": CX_LEVEL,
                        "hold_minutes": st["hold_minutes"], "expires_at": min(t + hold, window_end).isoformat()})
        t += hold
    if t > window_end:
        raise ValueError(f"the steps take {(t - window_start).total_seconds() / 60:g} min, longer than the window")
    return out


def check_window(start: datetime, end: datetime, now: datetime) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("window times need a time zone")
    if end <= start:
        raise ValueError("window_end must be after window_start")
    if end - start > MAX_WINDOW:
        raise ValueError(f"a test window is at most {MAX_WINDOW.total_seconds() / 3600:g} h")
    if end <= now:
        raise ValueError("the window has already ended")


# --------------------------------------------------------------- executing


class Writer:
    """point_write through the Windrow instance that owns the point, and
    Baler's record of the level (point_write.record_level)."""

    def __init__(self, repo, commander=None):
        self.repo = repo
        self.commander = commander

    def write(self, point_uri: str, value, run_id: str, user_sub: str | None, expires_at: datetime | None) -> None:
        self.repo.command_point(point_uri, value, CX_LEVEL, who(run_id), user_sub, expires_at, self.commander)

    def relinquish(self, point_uri: str, run_id: str | None, level: int = CX_LEVEL) -> None:
        self.repo.command_point(point_uri, None, level, who(run_id) if run_id else "cx", None, None, self.commander)


def _expectation(e: dict, samples: list) -> tuple[bool | None, object]:
    """Judged on the point's state now: its last sample, which may be
    from before the write (a status that never changed is evidence too).
    No sample at all is inconclusive."""
    vals = ladder.numeric(samples)
    if not vals:
        return None, None
    last = vals[-1][1]
    if "equals" in e:
        return abs(last - float(e["equals"])) < 1e-6, last
    ok = (e.get("min") is None or last >= e["min"]) and (e.get("max") is None or last <= e["max"])
    return ok, last


class Executor:
    """Runs confirmed active runs, one thread each. `sleep` and `clock`
    are injectable so tests don't wait minutes."""

    def __init__(self, repo, writer: Writer, clock: Callable[[], datetime], sleep: Callable[[float], None] = time.sleep,
                 poll_seconds: float = POLL_SECONDS):
        self.repo, self.writer, self.clock, self.sleep, self.poll = repo, writer, clock, sleep, poll_seconds
        self.threads: dict[str, threading.Thread] = {}

    def start(self, run_id: str) -> None:
        t = threading.Thread(target=self.execute, args=(run_id,), name=f"cx-active-{run_id[:8]}", daemon=True)
        self.threads[run_id] = t
        t.start()

    def _aborted(self, run_id: str) -> bool:
        r = self.repo.get_row("cx_active_runs", run_id)
        return bool(r and r.get("abort_requested"))

    def _log(self, run: dict, entry: dict) -> None:
        run["writes"].append({**entry, "at": self.clock().isoformat()})
        self.repo.update_row("cx_active_runs", run["id"], {"writes": run["writes"]})

    def _wait(self, run_id: str, until: datetime) -> bool:
        """Sleep until `until`, polling the abort flag. False if aborted."""
        while self.clock() < until:
            if self._aborted(run_id):
                return False
            self.sleep(min(self.poll, max((until - self.clock()).total_seconds(), 0.0)) or 0.01)
        return not self._aborted(run_id)

    def execute(self, run_id: str) -> None:
        run = self.repo.get_row("cx_active_runs", run_id)
        if run is None:
            return
        run["writes"] = list(run.get("writes") or [])
        written: set[str] = set()
        results = []
        status, error = "completed", None
        try:
            self.repo.update_row("cx_active_runs", run_id, {"status": "running", "started_at": self.clock()})
            window_end = run["window_end"] if isinstance(run["window_end"], datetime) else datetime.fromisoformat(run["window_end"])
            for st in run["steps"]:
                if self._aborted(run_id):
                    status = "aborted"
                    break
                if self.clock() >= window_end:
                    status, error = "expired", "the window ended before every step ran"
                    break
                started = self.clock()
                hold_end = min(started + timedelta(minutes=st["hold_minutes"]), window_end)
                step_written: list[str] = []
                outcome = {"id": st["id"], "title": st["title"], "started_at": started.isoformat(), "expect": []}
                try:
                    for w in st["writes"]:
                        self.writer.write(w["point"], w["value"], run_id, run.get("confirmed_by"), hold_end)
                        written.add(w["point"])
                        step_written.append(w["point"])
                        self._log(run, {"action": "write", "step": st["id"], "point": w["point"], "value": w["value"],
                                        "level": CX_LEVEL, "expires_at": hold_end.isoformat()})
                    deadline = min(started + timedelta(minutes=st["within_minutes"]), hold_end)
                    met = False
                    checks: list = []
                    if not st["expect"] and not self._wait(run_id, hold_end):  # a plain hold
                        status = "aborted"
                    while status != "aborted":
                        with self.repo.history() as h:
                            series = h.window([e["point"] for e in st["expect"]], started - timedelta(days=1),
                                              self.clock() + timedelta(seconds=1))
                        checks = [(e, *_expectation(e, [x for x in series.get(e["point"], []) if x[0] <= self.clock()]))
                                  for e in st["expect"]]
                        met = all(ok for _, ok, _ in checks)
                        if met or self.clock() >= deadline:
                            break
                        if not self._wait(run_id, min(self.clock() + timedelta(seconds=self.poll), deadline)):
                            status = "aborted"
                            break
                    outcome["expect"] = [{"point": e["point"], **{k: e[k] for k in ("equals", "min", "max") if k in e},
                                          "observed": last, "ok": ok} for e, ok, last in checks]
                    outcome["status"] = "pass" if met else ("inconclusive" if any(ok is None for _, ok, _ in checks) else "fail")
                    if status == "aborted":
                        outcome["status"] = "inconclusive"
                finally:
                    for p in step_written:
                        self._relinquish(run, p, st["id"])
                outcome["finished_at"] = self.clock().isoformat()
                results.append(outcome)
                self.repo.update_row("cx_active_runs", run_id, {"results": results})
                if status == "aborted":
                    break
        except Exception as exc:  # noqa: BLE001 - recorded on the run; the finally relinquishes regardless
            log.error("cx active run %s failed:\n%s", run_id, traceback.format_exc())
            status, error = "failed", f"{type(exc).__name__}: {exc}"[:2000]
        finally:
            for p in sorted(written):
                self._relinquish(run, p, None)
            self.repo.update_row("cx_active_runs", run_id, {"status": status, "error": error, "results": results,
                                                            "finished_at": self.clock()})
            self.repo.add_event(run["project_id"], f"active_run.{status}", run.get("confirmed_by"), None,
                                {"active_run_id": run_id, "writes": sum(1 for w in run["writes"] if w["action"] == "write"),
                                 "error": error}, self.clock())

    def _relinquish(self, run: dict, point: str, step: str | None) -> None:
        try:
            self.writer.relinquish(point, run["id"])
            self._log(run, {"action": "relinquish", "step": step, "point": point, "level": CX_LEVEL})
        except Exception as exc:  # noqa: BLE001 - the sweeper retries; never mask the run's own outcome
            log.warning("cx active run %s: relinquish %s failed: %s", run["id"], point, exc)
            self._log(run, {"action": "relinquish_failed", "step": step, "point": point, "error": str(exc)[:500]})


def sweep(repo, writer: Writer, now: datetime) -> list[tuple[str, str]]:
    """Relinquish every Cx-owned level whose run isn't running, or whose
    expiry has passed. Returns (point, who) relinquished."""
    done = []
    for point, level, owner, expires_at in repo.cx_levels():
        run_id = owner[len(WHO_PREFIX):] if owner.startswith(WHO_PREFIX) else None
        run = repo.get_row("cx_active_runs", run_id) if run_id else None
        past = expires_at is not None and expires_at <= now
        if run is None or run["status"] != "running" or past:
            try:
                writer.relinquish(point, run_id, level)
                done.append((point, owner))
            except Exception as exc:  # noqa: BLE001 - next tick retries
                log.warning("cx sweeper: relinquish %s (%s) failed: %s", point, owner, exc)
    return done


def new_run(project_id: str, equip_uri: str, kind: str, steps: list[dict], start: datetime, end: datetime,
            user_sub: str | None, now: datetime) -> dict:
    return {"id": db.new_id(), "project_id": project_id, "equip_uri": equip_uri, "kind": kind, "steps": steps,
            "plan": plan(steps, start, end), "status": "planned", "window_start": start, "window_end": end,
            "created_by": user_sub, "created_at": now, "confirmed_by": None, "confirmed_at": None, "started_at": None,
            "finished_at": None, "before": None, "writes": [], "results": [], "error": None, "abort_requested": False}

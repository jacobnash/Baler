"""
Passive override tests (Baler#212): every human override is a free
functional test, judged against the schedule in force.

When someone overrides a setpoint or a command, the controlled variable
should get there. Baler doesn't command anything here; it watches what
people already did and checks the equipment answered:

  override:<pair>   one result per overridden point, per run, for each of
                    ladder.PAIRS: a setpoint whose sensor should track it
                    (zone/supply air temperature, duct static, airflow),
                    or a command whose status should follow it (fan, pump,
                    run). subject = the overridden point.

Finding the override (`detect`), from evidence Baler already has:

  - a step in the setpoint or command's own history that then holds (at
    least `min_hold_minutes`, so a reset sequence that moves every few
    minutes isn't an "override") by at least `min_step`;
  - minus Baler's own writes (Cx 9 active runs, Haystack pointWrite,
    timed overrides: point_write_levels and the active-run write log);
  - minus its release (the value going back to what it was before);
  - minus the schedule's own writes: a change within
    `schedule_slack_minutes` of a transition of a BACnet Schedule that
    controls the point (schedules.py, read from Windrow);
  - and, where Windrow reports the controller's priority array (Windrow#25,
    stored by `refresh`), annotated with the level a writer other than
    Baler held. `basis: "priority_array"` counts only those.

Judging it (`judge`): the controlled variable must come within the pair's
tolerance of the new value (a status must equal the command) within
`within_minutes`. Schedule-aware: if the equipment was scheduled
unoccupied while the override stood - by a Schedule controlling one of
its points, or its own occupancy point's history - a setpoint override
is not_applicable ("the schedule had it unoccupied"), never a fail. A
commanded fan/pump/run isn't exempt: the operator's level beats the
schedule's, so the status should follow regardless.

  pass            reached within the time (evidence says how long it took)
  fail            the window passed and it never got there
  inconclusive    no samples of the controlled variable, or the override
                  was released before the window ended without reaching it
  not_applicable  scheduled unoccupied (setpoint overrides only)

An override whose window hasn't finished and that hasn't reached yet is
pending: not judged in this run (it is evidence of nothing yet). A point
overridden several times in the window gets the status of its latest
judged override; the evidence lists them all (newest 20). A point nobody
overrode gets no result at all - there was no test.

Opt-in per project: settings.overrides = {"enabled": true, ...}. The
results are ordinary Cx results, so issues, gates, fault mirroring,
procedures (`{"check": "override:"}`) and reports pick them up unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from baler import brick_vocab
from baler.commissioning import schedules as sched
from baler.commissioning.ladder import (
    PAIRS,
    STATUS_CANDIDATES,
    TRACK_CANDIDATES,
    Pair,
    _pick,
    _to_f,
    numeric,
)

log = logging.getLogger(__name__)

CHECK_PREFIX = "override:"
MAX_EVIDENCE = 20

# How close the controlled variable must get to count as "reached", and
# how big a step counts as an override, per pair (track_rel ones are a
# fraction of the setpoint). Projects override either per pair.
REACH = {"zone_air_temperature": 1.0, "supply_air_temperature": 1.5, "supply_air_static_pressure": 0.1, "air_flow": 0.1}
STEP = {"zone_air_temperature": 1.0, "supply_air_temperature": 1.0, "supply_air_static_pressure": 0.1, "air_flow": 0.1}

DEFAULTS: dict = {
    "enabled": False,
    "within_minutes": 30.0,
    "min_hold_minutes": 10.0,
    "schedule_slack_minutes": 5.0,
    "baler_slack_minutes": 2.0,
    "basis": "any",
    "tz": None,
    "refresh_minutes": 15.0,
}
_NUMBERS = {"within_minutes": (1, 24 * 60), "min_hold_minutes": (0, 24 * 60), "schedule_slack_minutes": (0, 120),
            "baler_slack_minutes": (0, 120), "refresh_minutes": (0, 24 * 60)}
BASES = ("any", "priority_array")
# Level 16 is where a controller's own program and its schedules usually
# write; an override is someone holding a level above it (8 = manual
# operator, the level Haystack pointWrite and Baler's Cx tests use).
PROGRAM_LEVEL = 16
OCCUPANCY_CLASSES = ("Occupancy_Command", "Occupancy_Status", "Occupied_Mode_Status")

OVERRIDE_CANDIDATES = [
    {"cause": "a higher-priority writer holds the point (another operator, a life-safety or a program level above 8)",
     "distinguish": "read the controller's priority array: is a level above the override's set?"},
    {"cause": "the loop is disabled or in manual in the controller (so the setpoint has no effect)",
     "distinguish": "loop enable / auto-manual flag in the vendor tool while the override stands"},
]


def validate_settings(cfg) -> dict:
    """settings.overrides: raises ValueError/TypeError on anything unknown
    or out of range."""
    if not isinstance(cfg, dict):
        raise TypeError("overrides must be an object")
    pair_keys = {p.key for p in PAIRS}
    for k, v in cfg.items():
        if k == "enabled":
            if not isinstance(v, bool):
                raise TypeError("overrides.enabled is true or false")
        elif k in _NUMBERS:
            lo, hi = _NUMBERS[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise ValueError(f"overrides.{k} must be a number in [{lo:g}, {hi:g}]")
        elif k == "basis":
            if v not in BASES:
                raise ValueError(f"overrides.basis is one of {', '.join(BASES)}")
        elif k == "tz":
            if v is not None:
                try:
                    ZoneInfo(str(v))
                except (ZoneInfoNotFoundError, ValueError) as exc:
                    raise ValueError(f"overrides.tz {v!r} isn't a time zone") from exc
        elif k in ("tolerances", "min_step"):
            if not isinstance(v, dict):
                raise TypeError(f"overrides.{k} is an object of {{pair: number}}")
            for pk, n in v.items():
                if pk not in pair_keys:
                    raise ValueError(f"overrides.{k}: unknown pair {pk!r} (known: {', '.join(sorted(pair_keys))})")
                if isinstance(n, bool) or not isinstance(n, (int, float)) or n < 0:
                    raise ValueError(f"overrides.{k}.{pk} must be a number >= 0")
        elif k == "unoccupied_values":
            if not isinstance(v, list) or not all(isinstance(x, (int, float, str, bool)) for x in v):
                raise TypeError("overrides.unoccupied_values is a list of schedule values")
        else:
            raise ValueError(f"unknown overrides setting {k!r}")
    return cfg


def effective(cfg: dict | None) -> dict:
    out = {**DEFAULTS, **(cfg or {})}
    out["tolerances"] = {**REACH, **((cfg or {}).get("tolerances") or {})}
    out["min_step"] = {**STEP, **((cfg or {}).get("min_step") or {})}
    out["unoccupied_values"] = tuple((cfg or {}).get("unoccupied_values") or sched.UNOCCUPIED)
    return out


def enabled(settings: dict | None) -> bool:
    return bool(((settings or {}).get("overrides") or {}).get("enabled"))


# ------------------------------------------------------------- the inputs


@dataclass
class Context:
    """What the detector needs beyond history, for the run's points.

    schedules: [{id, name, body, calendars: {object: calendar}, points}]
    baler_writes: point -> times Baler itself commanded it
    priority: point -> [{observed_at, levels: {"8": {"value", "baler"}}}]
    """

    schedules: list[dict] = field(default_factory=list)
    baler_writes: dict[str, list[datetime]] = field(default_factory=dict)
    priority: dict[str, list[dict]] = field(default_factory=dict)

    def schedules_for(self, point: str) -> list[dict]:
        return [s for s in self.schedules if point in (s.get("points") or [])]


def context_from(source, point_uris: list[str], start: datetime, end: datetime) -> Context:
    """A Context from a repo (MemoryRepo / PgRepo). A source that can't
    answer one part still gives the rest: missing evidence makes fewer
    exclusions and annotations, never a crash."""
    ctx = Context()
    if source is None or not point_uris:
        return ctx
    for name, fn in (("schedules", lambda: source.schedules_for_points(point_uris)),
                     ("baler_writes", lambda: source.baler_writes(point_uris, start, end)),
                     ("priority", lambda: source.priority_observations(point_uris, start, end))):
        try:
            setattr(ctx, name, fn())
        except Exception:
            log.warning("override context: %s unavailable", name, exc_info=True)
    return ctx


# --------------------------------------------------------------- detecting


def _roles(pair: Pair) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(overridden point classes, controlled variable classes)."""
    return (pair.a, pair.b) if pair.kind == "cmd_status" else (pair.b, pair.a)


def _changed(pair: Pair, prev: float, cur: float, step: float) -> bool:
    if pair.kind == "cmd_status":
        return (prev > 0.5) != (cur > 0.5)
    if pair.kind == "track_rel":
        base = max(abs(prev), 1e-9)
        return abs(cur - prev) / base >= step
    return abs(cur - prev) >= step


def _near(times: list[datetime], t: datetime, slack: timedelta) -> datetime | None:
    return next((x for x in times if abs(x - t) <= slack), None)


def _schedule_changes(ctx: Context, point: str, start: datetime, end: datetime, tz: ZoneInfo) -> list[tuple[datetime, str]]:
    out = []
    for s in ctx.schedules_for(point):
        for when, _v in sched.transitions(s["body"], start, end, tz, s.get("calendars")):
            out.append((when, s.get("name") or s.get("id") or "schedule"))
    return out


def _held_level(ctx: Context, point: str, start: datetime, end: datetime) -> dict | None:
    """The highest-priority level held by someone other than Baler seen in
    a priority-array observation during [start, end] (or the latest one
    before start, which was still in force)."""
    obs = sorted(ctx.priority.get(point) or [], key=lambda o: o["observed_at"])
    before = [o for o in obs if o["observed_at"] <= start][-1:]
    for o in before + [o for o in obs if start < o["observed_at"] <= end]:
        levels = sorted(((int(k), v) for k, v in (o.get("levels") or {}).items()
                         if v is not None and not (v.get("baler") if isinstance(v, dict) else False)), key=lambda x: x[0])
        levels = [(lvl, v) for lvl, v in levels if lvl < PROGRAM_LEVEL]
        if levels:
            lvl, v = levels[0]
            return {"level": lvl, "value": v.get("value") if isinstance(v, dict) else v,
                    "observed_at": o["observed_at"].isoformat()}
    return None


def detect(pair: Pair, point: dict, samples: list, now: datetime, cfg: dict, ctx: Context,
           tz: ZoneInfo) -> tuple[list[dict], dict]:
    """(overrides, excluded counts) on one overridden point. Each override
    is {at, value, previous, held_until, level?}; held_until None means it
    was still standing at the end of the history read."""
    s = numeric(samples)
    if pair.temperature:
        s = [(t, _to_f(v, point.get("unit"))) for t, v in s]
    step = float(cfg["min_step"].get(pair.key, 0.0)) if pair.kind != "cmd_status" else 0.5
    hold = timedelta(minutes=cfg["min_hold_minutes"])
    baler = sorted(ctx.baler_writes.get(point["id"]) or [])
    excluded = {"baler": 0, "schedule": 0, "no_priority_array": 0, "not_held": 0, "release": 0}
    standing: dict | None = None  # the last step seen, so its release can be told apart
    out: list[dict] = []
    if len(s) < 2:
        return out, excluded
    sched_changes = _schedule_changes(ctx, point["id"], s[0][0] - timedelta(hours=1), now, tz)
    i = 1
    while i < len(s):
        t, v = s[i]
        prev = s[i - 1][1]
        if not _changed(pair, prev, v, step):
            i += 1
            continue
        # how long does the new value hold?
        j = i + 1
        while j < len(s) and not _changed(pair, v, s[j][1], step):
            j += 1
        held_until = s[j][0] if j < len(s) else None
        if standing is not None and standing["held_until"] == t and not _changed(pair, standing["previous"], v, step):
            # back to what it was before the last change: its release, not a new override
            excluded["release"] += 1
            standing = None
            i = j
            continue
        standing = {"previous": prev, "held_until": held_until}
        stood = (held_until or now) - t
        if stood < hold:
            if held_until is not None:
                excluded["not_held"] += 1
            i = j
            continue
        if _near(baler, t, timedelta(minutes=cfg["baler_slack_minutes"])):
            excluded["baler"] += 1
            i = j
            continue
        slack = timedelta(minutes=cfg["schedule_slack_minutes"])
        by_schedule = next((name for when, name in sched_changes if abs(when - t) <= slack), None)
        if by_schedule:
            excluded["schedule"] += 1
            i = j
            continue
        level = _held_level(ctx, point["id"], t, held_until or now)
        if cfg["basis"] == "priority_array" and level is None:
            excluded["no_priority_array"] += 1
            i = j
            continue
        out.append({"at": t, "value": v, "previous": prev, "held_until": held_until, "level": level,
                    "basis": "priority_array" if level else "history_step"})
        i = j
    return out, excluded


# ---------------------------------------------------------------- judging


def _occupancy_windows(equip: dict, override_point: str, series: dict, ctx: Context, start: datetime,
                       end: datetime, tz: ZoneInfo, unocc) -> str | None:
    """Why the equipment counts as scheduled unoccupied at some moment in
    [start, end], or None. Schedules controlling one of its points (other
    than the overridden one) first, then its own occupancy points."""
    for p in equip["points"]:
        if p["id"] == override_point:
            continue
        for s in ctx.schedules_for(p["id"]):
            body, cals = s["body"], s.get("calendars")
            moments = [start, *(w for w, _ in sched.transitions(body, start, end, tz, cals))]
            for m in moments:
                got = sched.value_at(body, m, tz, cals)
                if sched.is_unoccupied(got["value"], unocc):
                    return (f"schedule {s.get('name') or s.get('id')} had it at {got['value']!r} "
                            f"({got['source']}) at {m.astimezone(tz).strftime('%Y-%m-%d %H:%M %Z')}")
    fam: set[str] = set()
    vocab = brick_vocab.load()
    for c in OCCUPANCY_CLASSES:
        fam |= vocab.descendants(c) or {c}
    for p in equip["points"]:
        if p.get("brickClass") not in fam:
            continue
        rows = [(t, v) for t, v in series.get(p["id"], []) if t <= end]
        before = [r for r in rows if r[0] <= start][-1:]
        for t, v in before + [r for r in rows if start < r[0] <= end]:
            if sched.is_unoccupied(v, unocc):
                return f"{p.get('dis') or p['id']} read {v!r} at {t.astimezone(tz).strftime('%Y-%m-%d %H:%M %Z')}"
    return None


def _reached(pair: Pair, target: float, x: float, tol: float) -> bool:
    if pair.kind == "cmd_status":
        return (x > 0.5) == (target > 0.5)
    if pair.kind == "track_rel":
        return target > 0 and abs(x - target) / target <= tol
    return abs(x - target) <= tol


def judge(pair: Pair, ov: dict, equip: dict, cv: dict, op: dict, series: dict, now: datetime, cfg: dict,
          ctx: Context, tz: ZoneInfo) -> dict | None:
    """One override's verdict, or None while it's pending."""
    within = timedelta(minutes=cfg["within_minutes"])
    t, target = ov["at"], ov["value"]
    deadline = t + within
    end = min(deadline, ov["held_until"] or deadline, now)
    tol = float(cfg["tolerances"].get(pair.key, pair.tolerance)) if pair.kind != "cmd_status" else 0.0
    rows = [(ts, v) for ts, v in numeric(series.get(cv["id"], [])) if t <= ts <= end]
    if pair.temperature:
        rows = [(ts, _to_f(v, cv.get("unit"))) for ts, v in rows]
    base = {"at": t.isoformat(), "value": target, "previous": ov["previous"], "basis": ov["basis"],
            "held_until": ov["held_until"].isoformat() if ov["held_until"] else None, "level": ov["level"],
            "within_minutes": cfg["within_minutes"], "tolerance": tol, "samples": len(rows)}
    hit = next(((ts, v) for ts, v in rows if _reached(pair, target, v, tol)), None)
    if hit:
        return {**base, "status": "pass", "reached_at": hit[0].isoformat(),
                "reached_after_minutes": round((hit[0] - t).total_seconds() / 60, 1), "observed": hit[1]}
    window_done = deadline <= now or (ov["held_until"] is not None and ov["held_until"] <= now)
    if not window_done:
        return None  # pending: neither reached nor out of time yet
    if pair.kind != "cmd_status":
        why = _occupancy_windows(equip, op["id"], series, ctx, t, end, tz, cfg["unoccupied_values"])
        if why:
            return {**base, "status": "not_applicable", "reason": f"scheduled unoccupied: {why}"}
    if not rows:
        return {**base, "status": "inconclusive",
                "reason": f"no samples of {cv.get('dis') or cv['id']} while the override stood"}
    if ov["held_until"] is not None and ov["held_until"] < deadline:
        return {**base, "status": "inconclusive",
                "reason": f"released after {(ov['held_until'] - t).total_seconds() / 60:.0f} min, before the "
                          f"{cfg['within_minutes']:g} min response window ended"}
    closest = min(rows, key=lambda r: abs(r[1] - target))
    return {**base, "status": "fail", "closest": closest[1], "closest_at": closest[0].isoformat()}


def _symptom(pair: Pair, verdict: dict, op: dict, cv: dict, tz: ZoneInfo) -> str:
    at = datetime.fromisoformat(verdict["at"]).astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")
    who = f" (level {verdict['level']['level']})" if verdict.get("level") else ""
    if pair.kind == "cmd_status":
        return (f"at {at} {op.get('dis') or op['id']} was overridden to {verdict['value']:g}{who}; "
                f"{cv.get('dis') or cv['id']} didn't follow within {verdict['within_minutes']:g} min")
    return (f"at {at} {op.get('dis') or op['id']} was overridden from {verdict['previous']:g} to {verdict['value']:g}{who}; "
            f"{cv.get('dis') or cv['id']} got no closer than {verdict['closest']:g} within "
            f"{verdict['within_minutes']:g} min (tolerance {verdict['tolerance']:g})")


def run(equips: list[dict], series: dict, now: datetime, settings: dict | None, ctx: Context) -> list[dict]:
    """override:<pair> results for every overridden point on `equips`."""
    cfg = effective(settings)
    tz = ZoneInfo(cfg["tz"]) if cfg.get("tz") else ZoneInfo("UTC")
    out = []
    for e in equips:
        for pair in PAIRS:
            op_classes, cv_classes = _roles(pair)
            op, _ = _pick(e["points"], op_classes, True)
            cv, _ = _pick(e["points"], cv_classes, True)
            if op is None or cv is None:
                continue
            found, excluded = detect(pair, op, series.get(op["id"], []), now, cfg, ctx, tz)
            verdicts = [v for v in (judge(pair, ov, e, cv, op, series, now, cfg, ctx, tz) for ov in found) if v]
            if not verdicts:
                continue
            latest = verdicts[-1]
            ev = {"pair": pair.label, "controlled": cv["id"], "overrides": verdicts[-MAX_EVIDENCE:],
                  "judged": len(verdicts), "pending": len(found) - len(verdicts), "excluded": excluded,
                  "schedules": sorted({s.get("name") or s["id"] for p in e["points"] for s in ctx.schedules_for(p["id"])}),
                  "settings": {k: cfg[k] for k in ("within_minutes", "min_hold_minutes", "basis", "tz")}}
            symptom, cands = None, []
            if latest["status"] == "fail":
                symptom = _symptom(pair, latest, op, cv, tz)
                cands = (STATUS_CANDIDATES if pair.kind == "cmd_status" else TRACK_CANDIDATES) + OVERRIDE_CANDIDATES
            elif latest["status"] in ("inconclusive", "not_applicable"):
                symptom = latest.get("reason")
            out.append({"equip_uri": e["id"], "check_id": CHECK_PREFIX + pair.key, "subject": op["id"],
                        "status": latest["status"], "severity": None, "symptom": symptom, "candidates": cands,
                        "evidence": ev})
    return out


# ---------------------------------------------------------- refresh (I/O)

# Windrow command-channel actions this uses. `connection_schedules` exists
# (Baler#165). `read_priority_arrays` is the Windrow#25 contract: for a
# connection id, the priority array of each commandable point it polls,
#   {"points": {label: {"levels": {"8": 68.0, "16": 72.0}}}}
# levels as strings 1-16, only the non-null ones. A Windrow without it
# answers "unknown action", which refresh records and carries on.
SCHEDULES_ACTION = "connection_schedules"
PRIORITY_ACTION = "read_priority_arrays"
COMMAND_TIMEOUT_S = 30.0


def _allowed(scopes, uri: str) -> bool:
    return all(s.allows(uri) for s in scopes)


def refresh(repo, commander, scopes, now: datetime) -> dict:
    """Read every in-scope connection's schedules, calendars and priority
    arrays from the Windrow instances the commander knows, and store them
    (repo.put_schedules / repo.record_priority_arrays). Reads only: it
    never writes to a controller. Returns what it did, per instance."""
    summary: dict = {"instances": [], "schedules": 0, "calendars": 0, "priority_points": 0, "errors": []}
    for (site, instance), status in sorted(commander.statuses().items()):
        if isinstance(status, dict) and status.get("state") == "offline":
            continue
        inst = {"site": site, "instance": instance, "connections": 0, "priority_arrays": "ok"}
        try:
            conns = commander.command(site, instance, "list_connections", {}, timeout=COMMAND_TIMEOUT_S) or []
        except Exception as exc:  # noqa: BLE001 - one instance down never stops the rest
            summary["errors"].append(f"{site}/{instance}: list_connections: {exc}")
            continue
        for c in conns if isinstance(conns, list) else []:
            prefix = c.get("topic_prefix")
            if not prefix or not _allowed(scopes, f"urn:point:{prefix}/x"):
                continue
            inst["connections"] += 1
            try:
                got = commander.command(site, instance, SCHEDULES_ACTION, {"id": c["id"]}, timeout=COMMAND_TIMEOUT_S) or {}
                rows = []
                for kind, key in (("schedule", "schedules"), ("calendar", "calendars")):
                    for body in got.get(key) or []:
                        rows.append({"id": f"{site}/{instance}/{c['id']}/{body.get('object')}", "site": site,
                                     "instance": instance, "connection_id": c["id"], "topic_prefix": prefix, "kind": kind,
                                     "object": body.get("object"), "name": body.get("name"), "body": body,
                                     "points": sched.controlled_points(body, prefix) if kind == "schedule" else [],
                                     "read_at": now})
                        summary[key] += 1
                repo.put_schedules(rows)
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(f"{site}/{instance}/{c['id']}: schedules: {exc}")
            if inst["priority_arrays"] != "ok":
                continue
            try:
                arrays = commander.command(site, instance, PRIORITY_ACTION, {"id": c["id"]}, timeout=COMMAND_TIMEOUT_S) or {}
            except Exception as exc:  # noqa: BLE001
                inst["priority_arrays"] = "unsupported" if "unknown action" in str(exc).lower() else f"error: {exc}"
                continue
            obs = {}
            for label, entry in ((arrays or {}).get("points") or {}).items():
                uri = f"urn:point:{prefix}/{label}"
                mine = repo.point_levels(uri)
                levels = {}
                for lvl, value in ((entry or {}).get("levels") or {}).items():
                    held = mine.get(str(lvl))
                    levels[str(lvl)] = {"value": value, "baler": bool(held) and held.get("val") == value}
                obs[uri] = levels
            summary["priority_points"] += repo.record_priority_arrays(obs, now)
        summary["instances"].append(inst)
    return summary


class Refresher:
    """The scheduler-tick hook: refresh each project with overrides
    enabled every refresh_minutes (0 = only on request)."""

    def __init__(self, repo, commander, scopes_for):
        self.repo, self.commander, self.scopes_for = repo, commander, scopes_for
        self.last: dict[str, datetime] = {}

    def tick(self, now: datetime) -> list[str]:
        done = []
        for p in self.repo.list_projects_with_setting("overrides"):
            cfg = effective((p.get("settings") or {}).get("overrides"))
            every = float(cfg.get("refresh_minutes") or 0)
            if not cfg.get("enabled") or every <= 0 or p.get("archived_at"):
                continue
            last = self.last.get(p["id"])
            if last is not None and now - last < timedelta(minutes=every):
                continue
            self.last[p["id"]] = now
            try:
                got = refresh(self.repo, self.commander, self.scopes_for(p), now)
                done.append(p["id"])
                if got["errors"]:
                    log.warning("override refresh %s: %s", p["id"], "; ".join(got["errors"][:5]))
            except Exception:
                log.exception("override refresh %s failed", p["id"])
        return done

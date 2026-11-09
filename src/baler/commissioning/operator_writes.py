"""
Audited operator writes from the UI, and schedule/calendar writes
(Baler#213).

A person commands a point - or changes a BACnet Schedule or Calendar -
from the Commissioning page, and Baler records who, why, at what level
and until when, then undoes it on time by itself. Every write is a row in
cx_operator_writes (with a log of every command it sent) and a cx_events
row on the project.

Point writes (operator; gateway/njs/policy.js, and checked here):

  - go through the same path as Haystack pointWrite and Cx 9:
    repo.command_point -> point_write.Commander -> Windrow `point_write`
    -> BACnet WriteProperty at the priority, refusing past expires_at;
  - only at levels 8-16 (8 = manual operator). 1-7 are life safety,
    critical equipment and minimum on/off: never from a browser;
  - always expire: `hold_minutes` (or `expires_at`) is required, at most
    MAX_HOLD; the sweeper (the scheduler tick) relinquishes the level when
    it ends, if this write still holds it. A crash loses nothing: the
    row is in Postgres and the next tick relinquishes;
  - always say why: `reason` is required;
  - are made by a person: a session token, never an API key, and the row
    records the caller's own sub - there is no "on behalf of";
  - never take a level from a Cx active test (who "cx:..."): 409. A level
    another person holds is only replaced with `replace: true`, and the
    before-state (Baler's priority array and the point's latest value) is
    kept on the row either way.

The level's `who` is "op:<write id>", so Baler's record says whose it
is, and neither Cx 9's sweeper ("cx:" only) nor this one ever touches
another owner's level.

Schedule and calendar writes (admin):

  - go over the command channel to the Windrow instance that owns the
    connection: actions `write_schedule` / `write_calendar` (the Windrow#26
    contract, below). The connection's topic prefix must be in the
    project's scope;
  - the object is read first (`connection_schedules`) and kept as the
    before-state;
  - need a reason, and either `revert_at` (at most MAX_REVERT ahead: the
    sweeper writes the before-state back then, unless someone has changed
    the object since, which is recorded as a conflict and left alone) or
    an explicit `permanent: true`.

Windrow#26 contract (Baler's side; Windrow implements it):

  write_schedule {id: connection id, object: "schedule,N",
                  weekly?: {monday: [{time, value}], ... all seven},
                  exceptions?: [{period, priority, times: [{time, value}]}],
                  default?: value, effective_period?: [start, end]}
  write_calendar {id, object: "calendar,N", dates: [period, ...]}

  Keys left out are not written. Shapes are windrow/bacnet_schedules.py's
  read format (null value = relinquish). The result is the object read
  back after the write, in that same format. Like point_write, it carries
  expires_at and is refused after it.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta

from baler.commissioning import db

LEVELS = range(8, 17)
DEFAULT_LEVEL = 8
WHO_PREFIX = "op:"
MAX_HOLD = timedelta(hours=24)
MAX_REVERT = timedelta(days=31)
MAX_REASON = 500
TABLE = "cx_operator_writes"
ACTIVE = "active"
COMMAND_TIMEOUT_S = 30.0
WRITE_SCHEDULE, WRITE_CALENDAR, READ_SCHEDULES = "write_schedule", "write_calendar", "connection_schedules"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$")
_DATE_FIELD = {"year": re.compile(r"^(\*|\d{4})$"), "month": re.compile(r"^(\*|0[1-9]|1[0-2]|odd|even)$"),
               "day": re.compile(r"^(\*|0[1-9]|[12]\d|3[01]|last|odd|even)$")}
MAX_TIMES = 24
MAX_EXCEPTIONS = 64
MAX_DATES = 128


class Conflict(Exception):
    """The level or object is held by someone the caller may not replace."""


def who(write_id: str) -> str:
    return WHO_PREFIX + write_id


# ------------------------------------------------------------- validation


def reason(body: dict) -> str:
    r = body.get("reason")
    if not isinstance(r, str) or len(r.strip()) < 3:
        raise ValueError("reason is required: say why (at least 3 characters)")
    return r.strip()[:MAX_REASON]


def _when(v, what: str) -> datetime:
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{what} is an ISO date-time with a time zone") from exc
    if t.tzinfo is None:
        raise ValueError(f"{what} needs a time zone")
    return t


def expiry(body: dict, now: datetime) -> datetime:
    """hold_minutes or expires_at, required, in (now, now + MAX_HOLD]."""
    if body.get("hold_minutes") is not None:
        m = body["hold_minutes"]
        if isinstance(m, bool) or not isinstance(m, (int, float)) or not 0 < m <= MAX_HOLD.total_seconds() / 60:
            raise ValueError(f"hold_minutes must be in (0, {MAX_HOLD.total_seconds() / 60:g}]")
        return now + timedelta(minutes=float(m))
    if body.get("expires_at") is None:
        raise ValueError("every operator write expires: give hold_minutes (or expires_at)")
    t = _when(body["expires_at"], "expires_at")
    if not now < t <= now + MAX_HOLD:
        raise ValueError(f"expires_at must be after now and at most {MAX_HOLD.total_seconds() / 3600:g} h ahead")
    return t


def point_write(body: dict, equips: list[dict], now: datetime) -> dict:
    """A point write request -> {point, equip_uri, value, level, reason,
    expires_at, replace}. Raises ValueError/TypeError."""
    points = {p["id"]: (e, p) for e in equips for p in e["points"]}
    hit = points.get(body.get("point"))
    if hit is None:
        raise ValueError("point must be a point of this project's equipment")
    equip, p = hit
    if "writable" not in (p.get("tags") or ()):
        raise ValueError(f"{p['id']} isn't marked writable")
    v = body.get("value")
    if isinstance(v, bool):
        v = 1 if v else 0
    if not isinstance(v, (int, float)):
        raise TypeError("value is a number (1/0 for on/off); relinquish with POST /writes/{id}/relinquish")
    level = body.get("level", DEFAULT_LEVEL)
    if isinstance(level, bool) or not isinstance(level, int) or level not in LEVELS:
        raise ValueError("level must be 8-16 (8 = manual operator); 1-7 are reserved for life safety and the controller")
    if "replace" in body and not isinstance(body["replace"], bool):
        raise TypeError("replace is true or false")
    return {"point": p["id"], "equip_uri": equip["id"], "value": v, "level": level, "reason": reason(body),
            "expires_at": expiry(body, now), "replace": bool(body.get("replace"))}


def _times(tvs, where: str) -> list[dict]:
    if not isinstance(tvs, list) or len(tvs) > MAX_TIMES:
        raise ValueError(f"{where} is a list of up to {MAX_TIMES} {{time, value}}")
    out = []
    for tv in tvs:
        if not isinstance(tv, dict) or set(tv) - {"time", "value"} or not _TIME.match(str(tv.get("time", ""))):
            raise ValueError(f"{where}: each entry is {{time: \"HH:MM[:SS]\", value}}")
        val = tv.get("value")
        if val is not None and not isinstance(val, (int, float, str, bool)):
            raise TypeError(f"{where}: a value is a number, boolean, state name or null (relinquish)")
        t = tv["time"] if tv["time"].count(":") == 2 else tv["time"] + ":00"
        out.append({"time": t, "value": val})
    return sorted(out, key=lambda x: x["time"])


def _date(s, where: str) -> str:
    parts = str(s).split("-")
    if len(parts) != 3 or not all(_DATE_FIELD[k].match(v) for k, v in zip(("year", "month", "day"), parts)):
        raise ValueError(f"{where}: a date is YYYY-MM-DD, * for any field (*-12-25), odd/even months, last/odd/even days")
    return str(s)


def period(p, where: str, calendar_ok: bool = True) -> dict:
    if not isinstance(p, dict) or len(p) != 1:
        raise ValueError(f"{where}: a period is one of {{date}}, {{date_range}}, {{week_n_day}}"
                         + (", {calendar}" if calendar_ok else ""))
    if "date" in p:
        return {"date": _date(p["date"], where)}
    if "date_range" in p:
        r = p["date_range"]
        if not isinstance(r, list) or len(r) != 2:
            raise ValueError(f"{where}: date_range is [start, end]")
        return {"date_range": [_date(r[0], where), _date(r[1], where)]}
    if "week_n_day" in p:
        w = p["week_n_day"]
        if not isinstance(w, dict) or set(w) - {"month", "week", "weekday"}:
            raise ValueError(f"{where}: week_n_day is {{month, week, weekday}}")
        if w.get("month") is not None and w["month"] not in range(1, 15):
            raise ValueError(f"{where}: week_n_day.month is 1-12 (13 odd, 14 even) or null")
        if w.get("week") is not None and w["week"] not in range(1, 7):
            raise ValueError(f"{where}: week_n_day.week is 1-6 (6 = last 7 days) or null")
        if w.get("weekday") is not None and w["weekday"] not in WEEKDAYS:
            raise ValueError(f"{where}: week_n_day.weekday is a weekday name or null")
        return {"week_n_day": {k: w.get(k) for k in ("month", "week", "weekday")}}
    if "calendar" in p and calendar_ok:
        if not re.fullmatch(r"calendar,\d+", str(p["calendar"])):
            raise ValueError(f"{where}: calendar is a calendar object id like \"calendar,1\"")
        return {"calendar": str(p["calendar"])}
    raise ValueError(f"{where}: unknown period {sorted(p)}")


def schedule_change(body: dict) -> dict:
    """The fields of a write_schedule / write_calendar, normalized."""
    kind = body.get("kind", "schedule")
    obj = str(body.get("object") or "")
    out: dict = {}
    if kind == "schedule":
        if not re.fullmatch(r"schedule,\d+", obj):
            raise ValueError("object is a schedule object id like \"schedule,1\"")
        if "weekly" in body:
            w = body["weekly"]
            if not isinstance(w, dict) or set(w) != set(WEEKDAYS):
                raise ValueError("weekly has all seven days, monday..sunday, each a list of {time, value}")
            out["weekly"] = {d: _times(w[d], f"weekly.{d}") for d in WEEKDAYS}
        if "exceptions" in body:
            ex = body["exceptions"]
            if not isinstance(ex, list) or len(ex) > MAX_EXCEPTIONS:
                raise ValueError(f"exceptions is a list of up to {MAX_EXCEPTIONS}")
            out["exceptions"] = []
            for i, e in enumerate(ex):
                where = f"exceptions[{i}]"
                if not isinstance(e, dict) or set(e) - {"period", "priority", "times"}:
                    raise ValueError(f"{where} is {{period, priority, times}}")
                pr = e.get("priority", 16)
                if isinstance(pr, bool) or not isinstance(pr, int) or not 1 <= pr <= 16:
                    raise ValueError(f"{where}.priority is 1-16")
                out["exceptions"].append({"period": period(e.get("period"), where), "priority": pr,
                                          "times": _times(e.get("times") or [], f"{where}.times")})
        if "default" in body:
            if body["default"] is not None and not isinstance(body["default"], (int, float, str, bool)):
                raise TypeError("default is a number, boolean, state name or null")
            out["default"] = body["default"]
        if "effective_period" in body:
            ep = body["effective_period"]
            if not isinstance(ep, list) or len(ep) != 2:
                raise ValueError("effective_period is [start, end]")
            out["effective_period"] = [_date(ep[0], "effective_period"), _date(ep[1], "effective_period")]
    elif kind == "calendar":
        if not re.fullmatch(r"calendar,\d+", obj):
            raise ValueError("object is a calendar object id like \"calendar,1\"")
        dates = body.get("dates")
        if not isinstance(dates, list) or len(dates) > MAX_DATES:
            raise ValueError(f"dates is a list of up to {MAX_DATES} periods")
        out["dates"] = [period(d, f"dates[{i}]", calendar_ok=False) for i, d in enumerate(dates)]
    else:
        raise ValueError("kind is schedule or calendar")
    if not out:
        raise ValueError("nothing to write: give weekly, exceptions, default or effective_period (dates for a calendar)")
    return {"kind": kind, "object": obj, "fields": out}


def schedule_write(body: dict, now: datetime) -> dict:
    target = {k: body.get(k) for k in ("site", "instance", "connection_id")}
    if not all(isinstance(v, str) and v for v in target.values()):
        raise ValueError("site, instance and connection_id name the Windrow connection that owns the object")
    change = schedule_change(body)
    permanent = body.get("permanent", False)
    if not isinstance(permanent, bool):
        raise TypeError("permanent is true or false")
    revert_at = None
    if body.get("revert_at") is not None:
        if permanent:
            raise ValueError("give revert_at or permanent, not both")
        revert_at = _when(body["revert_at"], "revert_at")
        if not now < revert_at <= now + MAX_REVERT:
            raise ValueError(f"revert_at must be after now and at most {MAX_REVERT.days} days ahead")
    elif not permanent:
        raise ValueError("say when it ends: revert_at (the old schedule is written back then), or permanent: true")
    return {**target, **change, "reason": reason(body), "revert_at": revert_at, "permanent": permanent}


# ------------------------------------------------------------------ rows


def new_row(project_id: str, kind: str, target: str, claims: dict, email: str | None, reason_text: str, now: datetime,
            **extra) -> dict:
    return {"id": db.new_id(), "project_id": project_id, "kind": kind, "target": target, "equip_uri": None,
            "site": None, "instance": None, "connection_id": None, "level": None, "value": None, "before": None,
            "reason": reason_text, "who": email or claims.get("sub"), "user_sub": claims.get("sub"),
            "role": claims.get("role"), "status": "pending", "expires_at": None, "created_at": now, "ended_at": None,
            "ended_by": None, "end_reason": None, "error": None, "log": [], **extra}


def _log(repo, row: dict, entry: dict, now: datetime) -> None:
    row["log"] = [*(row.get("log") or []), {**entry, "at": now.isoformat()}]
    repo.update_row(TABLE, row["id"], {"log": row["log"]})


# ------------------------------------------------------------ point writes


def holder(levels: dict, level: int) -> str | None:
    got = levels.get(str(level))
    return got.get("who") if got else None


def write_point(repo, commander, row: dict, replace: bool, now: datetime, latest=None) -> dict:
    """Send it. Raises Conflict before anything is sent if the level
    isn't the caller's to take; a refused command marks the row failed and
    re-raises."""
    point, level = row["target"], row["level"]
    levels = repo.point_levels(point)
    owner = holder(levels, level)
    superseded = None
    if owner and owner.startswith("cx:"):
        raise Conflict(f"a Cx active test ({owner}) holds level {level} on this point; wait for it to finish or abort it")
    if owner and owner.startswith(WHO_PREFIX):
        superseded = owner[len(WHO_PREFIX):]
    elif owner and not replace:
        raise Conflict(f"level {level} is held by {owner}; send replace: true to take it (their value is kept on the record)")
    row["before"] = {"priority_array": levels, "latest": latest, "captured_at": now.isoformat()}
    repo.put_rows(TABLE, [row])
    try:
        repo.command_point(point, row["value"], level, who(row["id"]), row["user_sub"], row["expires_at"], commander)
    except Exception as exc:
        repo.update_row(TABLE, row["id"], {"status": "failed", "error": str(exc)[:1000], "ended_at": now})
        _log(repo, row, {"action": "write", "ok": False, "error": str(exc)[:500]}, now)
        raise
    repo.update_row(TABLE, row["id"], {"status": ACTIVE})
    _log(repo, row, {"action": "write", "ok": True, "value": row["value"], "level": level}, now)
    if superseded:
        old = repo.get_row(TABLE, superseded)
        if old and old["status"] == ACTIVE:
            repo.update_row(TABLE, superseded, {"status": "superseded", "ended_at": now, "ended_by": row["user_sub"],
                                                "end_reason": f"replaced by write {row['id']}"})
    return repo.get_row(TABLE, row["id"]) or row


def relinquish(repo, commander, row: dict, now: datetime, by: str | None, why: str, status: str = "relinquished") -> dict:
    """Release the level if this write still holds it. A failed relinquish
    leaves the row active (and logged) so the sweeper retries."""
    if row["status"] != ACTIVE:
        return row
    levels = repo.point_levels(row["target"])
    if holder(levels, row["level"]) == who(row["id"]):
        try:
            repo.command_point(row["target"], None, row["level"], who(row["id"]), by, None, commander)
        except Exception as exc:
            _log(repo, row, {"action": "relinquish", "ok": False, "error": str(exc)[:500]}, now)
            repo.update_row(TABLE, row["id"], {"error": str(exc)[:1000]})
            raise
        _log(repo, row, {"action": "relinquish", "ok": True}, now)
    else:
        why = f"{why}; the level was already released or replaced"
        _log(repo, row, {"action": "relinquish", "ok": True, "note": "level no longer held by this write"}, now)
    repo.update_row(TABLE, row["id"], {"status": status, "ended_at": now, "ended_by": by, "end_reason": why[:MAX_REASON]})
    return repo.get_row(TABLE, row["id"]) or row


# ----------------------------------------------------- schedule writes


def find_object(snapshot: dict, kind: str, obj: str) -> dict | None:
    key = "schedules" if kind == "schedule" else "calendars"
    return next((o for o in (snapshot or {}).get(key) or [] if o.get("object") == obj), None)


def connection(commander, site: str, instance: str, connection_id: str) -> dict | None:
    conns = commander.command(site, instance, "list_connections", {}, timeout=COMMAND_TIMEOUT_S) or []
    return next((c for c in conns if isinstance(c, dict) and c.get("id") == connection_id), None)


def _send(commander, row: dict, fields: dict) -> dict:
    action = WRITE_SCHEDULE if row["value"]["kind"] == "schedule" else WRITE_CALENDAR
    body = {"id": row["connection_id"], "object": row["value"]["object"], **fields}
    got = commander.command(row["site"], row["instance"], action, body, timeout=COMMAND_TIMEOUT_S,
                            suffix=action.replace("_", "-"))
    return got if isinstance(got, dict) else {}


def write_schedule(repo, commander, row: dict, now: datetime) -> dict:
    """Read the before-state, send the change, keep what came back."""
    snap = commander.command(row["site"], row["instance"], READ_SCHEDULES, {"id": row["connection_id"]},
                             timeout=COMMAND_TIMEOUT_S) or {}
    before = find_object(snap, row["value"]["kind"], row["value"]["object"])
    if before is None:
        raise ValueError(f"no {row['value']['object']} on connection {row['connection_id']}")
    row["before"] = before
    repo.put_rows(TABLE, [row])
    try:
        after = _send(commander, row, row["value"]["fields"])
    except Exception as exc:
        repo.update_row(TABLE, row["id"], {"status": "failed", "error": str(exc)[:1000], "ended_at": now})
        _log(repo, row, {"action": WRITE_SCHEDULE, "ok": False, "error": str(exc)[:500]}, now)
        raise
    status = ACTIVE if row.get("expires_at") else "permanent"
    repo.update_row(TABLE, row["id"], {"status": status})
    _log(repo, row, {"action": "write", "ok": True, "after": after}, now)
    return repo.get_row(TABLE, row["id"]) or row


def _unchanged_since(current: dict | None, written: dict) -> bool:
    return current is not None and all(current.get(k) == v for k, v in written.items())


def revert_schedule(repo, commander, row: dict, now: datetime, by: str | None, why: str) -> dict:
    """Write the before-state's changed fields back, unless the object
    has been changed by someone else since (then: conflict, untouched)."""
    if row["status"] not in (ACTIVE, "permanent"):
        return row
    fields = row["value"]["fields"]
    snap = commander.command(row["site"], row["instance"], READ_SCHEDULES, {"id": row["connection_id"]},
                             timeout=COMMAND_TIMEOUT_S) or {}
    current = find_object(snap, row["value"]["kind"], row["value"]["object"])
    if not _unchanged_since(current, fields):
        _log(repo, row, {"action": "revert", "ok": False, "note": "changed by someone else since; left as it is"}, now)
        repo.update_row(TABLE, row["id"], {"status": "conflict", "ended_at": now, "ended_by": by,
                                           "end_reason": f"{why}; not reverted: the object was changed since this write"[:MAX_REASON]})
        return repo.get_row(TABLE, row["id"]) or row
    back = {k: copy.deepcopy((row["before"] or {}).get(k)) for k in fields}
    try:
        after = _send(commander, row, back)
    except Exception as exc:
        _log(repo, row, {"action": "revert", "ok": False, "error": str(exc)[:500]}, now)
        repo.update_row(TABLE, row["id"], {"error": str(exc)[:1000]})
        raise
    _log(repo, row, {"action": "revert", "ok": True, "after": after}, now)
    repo.update_row(TABLE, row["id"], {"status": "reverted", "ended_at": now, "ended_by": by, "end_reason": why[:MAX_REASON]})
    return repo.get_row(TABLE, row["id"]) or row


# ------------------------------------------------------------------ sweep


def sweep(repo, commander, now: datetime) -> list[tuple[str, str]]:
    """The scheduler tick: relinquish every expired point write and revert
    every schedule write whose revert_at has come. Returns (id, outcome).
    One failure never stops the rest; the next tick retries."""
    done = []
    for row in repo.find_rows(TABLE, status=ACTIVE):
        exp = row.get("expires_at")
        if isinstance(exp, str):
            exp = datetime.fromisoformat(exp)
        if exp is None or exp > now:
            continue
        try:
            if row["kind"] == "point":
                got = relinquish(repo, commander, row, now, None, "expired: auto-relinquished", status="expired")
            else:
                got = revert_schedule(repo, commander, row, now, None, "revert_at reached: reverted automatically")
            done.append((row["id"], got["status"]))
            repo.add_event(row["project_id"], f"operator_write.{got['status']}", None, None,
                           {"write_id": row["id"], "kind": row["kind"], "target": row["target"]}, now)
        except Exception as exc:  # noqa: BLE001 - logged on the row; the next tick retries
            done.append((row["id"], f"retry: {exc}"))
    return done

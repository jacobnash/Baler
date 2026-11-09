"""
BACnet Schedule and Calendar objects, as Windrow reads them, evaluated in
time (Baler#212): what a schedule commanded at a given moment, and when
it changed.

The input is exactly what Windrow's `connection_schedules` command
answers (windrow/bacnet_schedules.py, Baler#165):

  schedule: {object, name, present_value, default, priority_for_writing,
             effective_period: [start, end] | null,
             weekly: {monday: [{time, value}], ...} | null,
             exceptions: [{period, priority, times: [{time, value}]}],
             controls: [{object, property, label?, device?}]}
  calendar: {object, name, present_value, dates: [period, ...]}

  period:   {date: "YYYY-MM-DD"} | {date_range: [a, b]} |
            {week_n_day: {month, week, weekday}} | {calendar: "calendar,N"}

Dates use `*` for a wildcard field (`*-12-25` is every Christmas), months
may be `odd`/`even` and days `last`/`odd`/`even`. Times are `HH:MM:SS`.

Evaluation follows ANSI/ASHRAE 135 clause 12.24.4 in the simple form a
controller implements: each day starts at the schedule default; within a
day the latest time-value at or before the moment applies, and a null
value relinquishes (back to the default for the weekly part, down to the
next exception for an exception). Exceptions that apply on the date are
taken highest priority (lowest number) first, and only once their first
time-value has passed. Outside the effective period nothing is scheduled
(the default stands).

Times are the controller's local wall clock, so every function takes the
site's time zone. Nothing here does I/O.
"""

from __future__ import annotations

import calendar as _calendar
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# What a schedule value means for occupancy when a project doesn't say.
# A schedule driving a binary object writes active/inactive; one driving
# a multi-state occupancy object usually writes 1 = occupied; 0/false/off
# and the words below mean the equipment is scheduled off.
UNOCCUPIED = (0, False, "inactive", "off", "unoccupied", "unocc", "standby", "false")


# ------------------------------------------------------------------ dates


def _field_matches(spec: str, value: int, kind: str, d: date) -> bool:
    if spec == "*":
        return True
    if kind == "month" and spec in ("odd", "even"):
        return (value % 2 == 1) == (spec == "odd")
    if kind == "day":
        if spec == "last":
            return value == _calendar.monthrange(d.year, d.month)[1]
        if spec in ("odd", "even"):
            return (value % 2 == 1) == (spec == "odd")
    try:
        return int(spec) == value
    except ValueError:
        return False


def date_matches(spec: str, d: date) -> bool:
    """`YYYY-MM-DD` with wildcards against a date."""
    parts = str(spec).split("-")
    if len(parts) != 3:
        return False
    y, m, dd = parts
    return (_field_matches(y, d.year, "year", d) and _field_matches(m, d.month, "month", d)
            and _field_matches(dd, d.day, "day", d))


def _bound(spec: str, d: date, upper: bool) -> date | None:
    """A date-range end as a concrete date (a wildcard is open-ended)."""
    parts = str(spec).split("-")
    if len(parts) != 3 or "*" in parts[0]:
        return None
    try:
        y = int(parts[0])
        m = 12 if parts[1] in ("*", "odd", "even") and upper else (1 if parts[1] in ("*", "odd", "even") else int(parts[1]))
        if parts[2] in ("*", "last", "odd", "even"):
            day = _calendar.monthrange(y, m)[1] if upper else 1
        else:
            day = int(parts[2])
        return date(y, m, day)
    except ValueError:
        return None


def week_n_day_matches(w: dict, d: date) -> bool:
    month, week, weekday = w.get("month"), w.get("week"), w.get("weekday")
    if month is not None:
        if month == 13 and d.month % 2 == 0 or month == 14 and d.month % 2 == 1:
            return False
        if month not in (13, 14) and month != d.month:
            return False
    if weekday is not None and WEEKDAYS.index(weekday) != d.weekday():
        return False
    if week is not None:
        if week == 6:
            last = _calendar.monthrange(d.year, d.month)[1]
            if d.day <= last - 7:
                return False
        elif not (week - 1) * 7 < d.day <= week * 7:
            return False
    return True


def period_matches(period: dict, d: date, calendars: dict[str, dict]) -> bool:
    """One calendar entry / special-event period against a date.
    `calendars` maps calendar object ids ("calendar,1") to the calendar."""
    if not isinstance(period, dict):
        return False
    if "date" in period:
        return date_matches(period["date"], d)
    if "date_range" in period:
        a, b = (period["date_range"] + [None, None])[:2]
        lo, hi = _bound(a, d, False), _bound(b, d, True)
        return (lo is None or lo <= d) and (hi is None or d <= hi)
    if "week_n_day" in period:
        return week_n_day_matches(period["week_n_day"] or {}, d)
    if "calendar" in period:
        cal = calendars.get(str(period["calendar"])) or {}
        return any(period_matches(e, d, calendars) for e in cal.get("dates") or [])
    return False


def in_effect(schedule: dict, d: date) -> bool:
    eff = schedule.get("effective_period")
    if not eff:
        return True
    lo, hi = _bound(eff[0], d, False), _bound(eff[1], d, True)
    return (lo is None or lo <= d) and (hi is None or d <= hi)


# ------------------------------------------------------------------ times


def parse_time(text: str) -> time:
    """`HH:MM:SS[.hh]`; a wildcard field counts as zero."""
    head = str(text).split(".", 1)[0]
    parts = [0 if p in ("*", "") else int(p) for p in head.split(":")] + [0, 0, 0]
    h, m, s = parts[:3]
    return time(min(h, 23), min(m, 59), min(s, 59))


def _last_before(times: list[dict], t: time) -> tuple[bool, object]:
    """(found, value) of the latest time-value at or before t."""
    found, value = False, None
    for tv in sorted(times or [], key=lambda x: parse_time(x.get("time", "00:00:00"))):
        if parse_time(tv.get("time", "00:00:00")) <= t:
            found, value = True, tv.get("value")
    return found, value


# ------------------------------------------------------------- evaluation


def value_at(schedule: dict, at: datetime, tz: ZoneInfo, calendars: dict[str, dict] | None = None) -> dict:
    """What the schedule commanded at `at` (aware): {value, source, priority?}.
    source is "exception", "weekly", "default" or "not_in_effect"."""
    local = at.astimezone(tz)
    d, t = local.date(), local.time()
    cals = calendars or {}
    default = schedule.get("default")
    if not in_effect(schedule, d):
        return {"value": default, "source": "not_in_effect"}
    events = sorted((e for e in schedule.get("exceptions") or [] if period_matches(e.get("period") or {}, d, cals)),
                    key=lambda e: int(e.get("priority") or 16))
    for e in events:
        found, value = _last_before(e.get("times") or [], t)
        if found and value is not None:
            return {"value": value, "source": "exception", "priority": int(e.get("priority") or 16),
                    "period": e.get("period")}
    weekly = schedule.get("weekly") or {}
    found, value = _last_before(weekly.get(WEEKDAYS[d.weekday()]) or [], t)
    if found and value is not None:
        return {"value": value, "source": "weekly"}
    return {"value": default, "source": "default"}


def change_times(schedule: dict, start: datetime, end: datetime, tz: ZoneInfo,
                 calendars: dict[str, dict] | None = None) -> list[datetime]:
    """Every moment in [start, end] the schedule could change value: each
    local midnight and every time-value of the weekly part and of every
    exception, on each day in range."""
    out: set[datetime] = set()
    day = start.astimezone(tz).date()
    last = end.astimezone(tz).date()
    stamps = {t.get("time", "00:00:00") for tvs in (schedule.get("weekly") or {}).values() for t in tvs or []}
    stamps |= {t.get("time", "00:00:00") for e in schedule.get("exceptions") or [] for t in e.get("times") or []}
    while day <= last:
        for s in {"00:00:00", *stamps}:
            moment = datetime.combine(day, parse_time(s), tz)
            if start <= moment <= end:
                out.add(moment)
        day += timedelta(days=1)
    return sorted(out)


def transitions(schedule: dict, start: datetime, end: datetime, tz: ZoneInfo,
                calendars: dict[str, dict] | None = None) -> list[tuple[datetime, object]]:
    """(when, new value) for every change of the scheduled value inside
    (start, end]. The value in force at `start` is value_at(start)."""
    out = []
    prev = value_at(schedule, start, tz, calendars)["value"]
    for moment in change_times(schedule, start, end, tz, calendars):
        if moment <= start:
            continue
        v = value_at(schedule, moment, tz, calendars)["value"]
        if v != prev:
            out.append((moment, v))
            prev = v
    return out


def is_unoccupied(value, unoccupied=UNOCCUPIED) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {str(u).lower() for u in unoccupied}
    if isinstance(value, bool):
        return value in unoccupied
    if value is None:
        return False  # nothing scheduled: no evidence it was off
    return any(not isinstance(u, (str, bool)) and float(value) == float(u) for u in unoccupied)


def controlled_points(schedule: dict, topic_prefix: str | None) -> list[str]:
    """urn:point: URIs of the points a schedule writes, where Windrow named
    them by label (a point of the same connection: topic `<prefix>/<label>`)."""
    if not topic_prefix:
        return []
    return [f"urn:point:{topic_prefix}/{c['label']}" for c in schedule.get("controls") or []
            if isinstance(c, dict) and c.get("label") and not c.get("device")]

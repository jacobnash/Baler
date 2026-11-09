"""PM schedules (#199), with no services: calendar, run hours from raw
status history, condition on a point, completions, exports."""

import csv
import io
from datetime import datetime, timedelta, timezone

import pytest

from baler.commissioning.history import InMemoryHistory
from baler.lifecycle import pm, routes, rows

T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
POS = "urn:equip:fbf/hosp/ahu-1/sf"
STATUS = "urn:point:fbf/hosp/ahu-1/sf/sf-status"
DP = "urn:point:fbf/hosp/ahu-1/sf/filter-dp"


class Client:
    def __init__(self, now):
        self.rows, self.now, self.history = rows.MemoryRows(), now, InMemoryHistory()

    def __call__(self, method, path, body=None, **query):
        req = routes.Request(method=method, path=path, rows=self.rows, org_id="org1", body=body, now=self.now,
                             query={k: str(v) for k, v in query.items()}, who={"sub": "u1"},
                             services={"history": self.history})
        return routes.dispatch(req)

    def ok(self, *a, **kw):
        st, out = self(*a, **kw)
        assert st in (200, 201), out
        return out


def _fan(c, at=T0):
    a = c.ok("POST", "/assets", {"asset_type": "fan", "tag": "SF-1"})
    c.ok("POST", f"/assets/{a['id']}/install", {"position_uri": POS, "at": at.isoformat()})
    return a


def test_run_seconds_caps_gaps_and_holds_the_prior_value():
    s = [(T0, True), (T0 + timedelta(minutes=10), False), (T0 + timedelta(minutes=20), True)]
    end = T0 + timedelta(hours=2)
    # on 0-10 min, then on from 20 min but the next sample never comes: capped at 15 min
    assert pm.run_seconds(s, T0, end) == 10 * 60 + 15 * 60
    # the value before the window holds from its start (capped) until the first sample
    assert pm.run_seconds([(T0 + timedelta(minutes=5), False)], T0, end, before=1.0) == 5 * 60
    assert pm.is_on("Running") and not pm.is_on(0) and pm.is_on(1.0) and pm.is_on(None) is None


def test_run_hour_task_counts_every_raw_sample():
    c = Client(T0 + timedelta(days=10))
    a = _fan(c)
    # 1-minute samples: on for 12 h a day for 10 days = 120 run hours
    for day in range(10):
        for minute in range(24 * 60):
            c.history.add(STATUS, T0 + timedelta(days=day, minutes=minute), minute < 12 * 60)
    c.ok("POST", "/pm/tasks", {"title": "Change filters", "asset_type": "fan", "trigger": "run_hours",
                              "interval_hours": 125, "run_point": "sf-status"})
    item = c.ok("GET", "/pm/due")[0]
    assert item["point"] == STATUS and item["run_hours"] == pytest.approx(120.0, abs=0.02)
    assert item["status"] == "due_soon" and item["remaining_hours"] == pytest.approx(5.0, abs=0.02)
    # done on day 6: the count restarts there (4 days x 12 h)
    done = c.ok("POST", f"/pm/tasks/{item['task_id']}/complete", {"asset_id": a["id"], "done_at": (T0 + timedelta(days=6)).isoformat()})
    assert done["run_hours"] == pytest.approx(72.0, abs=0.02)
    item = c.ok("GET", "/pm/due")[0]
    assert item["run_hours"] == pytest.approx(48.0, abs=0.02) and item["status"] == "ok"
    assert item["since_what"] == "last completion"


def test_calendar_and_condition_tasks():
    c = Client(T0 + timedelta(days=85))
    a = _fan(c)
    c.ok("POST", "/pm/tasks", {"title": "Belt inspection", "asset_id": a["id"], "trigger": "calendar", "interval_days": 90})
    c.ok("POST", "/pm/tasks", {"title": "Filter by dP", "asset_type": "fan", "trigger": "condition",
                              "condition_point": "filter-dp", "condition_op": ">=", "condition_value": 1.0,
                              "condition_unit": "inH2O"})
    c.history.add(DP, c.now - timedelta(minutes=5), 1.2)
    items = {i["title"]: i for i in c.ok("GET", "/pm/due")}
    assert items["Belt inspection"]["status"] == "due_soon" and items["Belt inspection"]["due_at"].startswith("2026-05-30")
    assert items["Filter by dP"]["status"] == "overdue" and items["Filter by dP"]["value"] == 1.2
    c.now = T0 + timedelta(days=95)  # the dP reading is now stale
    items = {i["title"]: i for i in c.ok("GET", "/pm/due")}
    assert items["Belt inspection"]["status"] == "overdue" and items["Filter by dP"]["status"] == "unknown"
    assert [i["title"] for i in c.ok("GET", "/pm/due", status="overdue")] == ["Belt inspection"]


def test_validation():
    c = Client(T0)
    for bad in ({"title": "x", "trigger": "calendar", "asset_type": "fan"},
                {"title": "x", "trigger": "run_hours", "asset_type": "fan", "interval_hours": 10},
                {"title": "x", "trigger": "condition", "asset_type": "fan", "condition_point": "p", "condition_op": "=="},
                {"title": "x", "trigger": "calendar", "interval_days": 30},
                {"title": "x", "trigger": "calendar", "interval_days": 30, "asset_type": "fan", "asset_id": "a"}):
        assert c("POST", "/pm/tasks", bad)[0] == 400, bad


def test_a_replaced_asset_starts_its_own_count_and_paused_tasks_drop_out():
    c = Client(T0 + timedelta(days=100))
    _fan(c)
    t = c.ok("POST", "/pm/tasks", {"title": "Grease", "asset_type": "fan", "trigger": "calendar", "interval_days": 90})
    c.ok("POST", "/positions/replace", {"position_uri": POS, "at": (T0 + timedelta(days=60)).isoformat(),
                                        "remove_reason": "failed", "asset": {"asset_type": "fan", "tag": "SF-1b"}})
    item = c.ok("GET", "/pm/due")[0]
    assert item["asset"]["tag"] == "SF-1b" and item["status"] == "ok" and item["since_what"] == "install"
    c.ok("PATCH", f"/pm/tasks/{t['id']}", {"status": "paused"})
    assert c.ok("GET", "/pm/due") == []


def test_exports():
    c = Client(T0 + timedelta(days=100))
    _fan(c)
    c.ok("POST", "/pm/tasks", {"title": "Grease", "asset_type": "fan", "trigger": "calendar", "interval_days": 90,
                              "duration_hours": 0.5})
    st, raw = c("GET", "/pm/due/export")
    got = list(csv.DictReader(io.StringIO(raw.body.decode())))
    assert st == 200 and got[0]["status"] == "overdue" and got[0]["asset_tag"] == "SF-1"
    st, raw = c("GET", "/pm/due/export", format="cobie-job")
    job = next(iter(csv.DictReader(io.StringIO(raw.body.decode()))))
    assert list(job) == list(pm.COBIE_JOB_COLUMNS)
    assert (job["Frequency"], job["FrequencyUnit"], job["TypeName"], job["Category"]) == ("90", "day", "fan", "PM")
    assert c("GET", "/pm/due/export", format="xml")[0] == 400

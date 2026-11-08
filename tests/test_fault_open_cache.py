"""
fault_detector.OpenFaults (Baler#190): a reading that changes nothing
writes nothing. close_fault on every in-range reading was an UPDATE round
trip per reading per rule - the bulk of Postgres's statements under load -
while faults that are open still open, stay open and close exactly as
before. No services: open_fault/close_fault are stubbed.
"""

import pytest

from baler import fault_detector
from baler.fault_detector import OpenFaults, handle_cur_reading

RULE = {"id": "r1", "mode": "cur", "type": "range", "applies_to": {"topic_glob": "fbf/s/*"}, "min": 0.0, "max": 100.0}
URI = "urn:point:fbf/s/temp"


class Rules:
    def get(self):
        return [RULE]


class Conn:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        assert "ended_at IS NULL" in sql
        return iter(self.rows)


@pytest.fixture
def calls(monkeypatch):
    log = []
    monkeypatch.setattr(fault_detector, "open_fault", lambda conn, rid, uri, now, detail=None, severity="warning":
                        log.append(("open", rid, uri)) or 1)
    monkeypatch.setattr(fault_detector, "close_fault", lambda conn, rid, uri, now: log.append(("close", rid, uri)) or 1)
    monkeypatch.setattr(fault_detector, "_fire_webhooks", lambda *a: log.append(("webhook", a[1])))
    return log


def test_in_range_readings_with_nothing_open_write_nothing(calls):
    open_faults = OpenFaults()
    open_faults.refresh(Conn([]))
    for v in (50.0, 51.0, 52.0):
        handle_cur_reading(None, Rules(), "w.json", "fbf/s/temp", v, open_faults)
    assert calls == []


def test_a_fault_opens_once_and_closes_once(calls):
    open_faults = OpenFaults()
    open_faults.refresh(Conn([]))
    for v in (150.0, 160.0, 50.0, 51.0):
        handle_cur_reading(None, Rules(), "w.json", "fbf/s/temp", v, open_faults)
    assert calls == [("open", "r1", URI), ("webhook", "fault.opened"), ("close", "r1", URI), ("webhook", "fault.resolved")]


def test_a_fault_open_in_postgres_at_startup_still_closes(calls):
    open_faults = OpenFaults()
    open_faults.refresh(Conn([("r1", URI)]))
    handle_cur_reading(None, Rules(), "w.json", "fbf/s/temp", 50.0, open_faults)
    assert calls[0] == ("close", "r1", URI)


def test_without_a_cache_behaviour_is_unchanged(calls):
    handle_cur_reading(None, Rules(), "w.json", "fbf/s/temp", 50.0)
    assert calls[0] == ("close", "r1", URI)  # every reading tries, as before


def test_an_open_during_a_refresh_survives_it():
    open_faults = OpenFaults()

    class RacingConn(Conn):
        def execute(self, sql, params=None):
            open_faults.opened("r1", URI)  # committed after the SELECT's snapshot
            return iter([])

    open_faults.refresh(RacingConn([]))
    assert open_faults.is_open("r1", URI)

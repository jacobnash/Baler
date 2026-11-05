"""Requires docker-compose's postgres service (localhost:5433)."""

from datetime import datetime, timezone

import pytest

from baler.timeseries import (
    connect,
    delete_point_value,
    read_latest,
    read_range,
    write_point_value,
)


@pytest.fixture
def conn():
    c = connect()
    c.execute("DELETE FROM point_history WHERE point_uri LIKE %s", ("urn:test-ts:%",))
    return c


@pytest.mark.integration
def test_typed_roundtrip(conn):
    now = datetime.now(timezone.utc)
    write_point_value(conn, "urn:test-ts:temp", 71.5, now, unit="degF")
    write_point_value(conn, "urn:test-ts:fan", True, now)
    write_point_value(conn, "urn:test-ts:mode", "active", now)
    write_point_value(conn, "urn:test-ts:missing", None, now)

    assert read_latest(conn, "urn:test-ts:temp") == 71.5
    assert isinstance(read_latest(conn, "urn:test-ts:temp"), float)
    assert read_latest(conn, "urn:test-ts:fan") is True
    assert read_latest(conn, "urn:test-ts:mode") == "active"
    assert read_latest(conn, "urn:test-ts:missing") is None


@pytest.mark.integration
def test_bool_is_not_confused_with_int(conn):
    """bool is a subclass of int in Python - write_point_value must check
    it first or True/False would land in the numeric column as 1.0/0.0."""
    now = datetime.now(timezone.utc)
    write_point_value(conn, "urn:test-ts:flag", False, now)
    assert read_latest(conn, "urn:test-ts:flag") is False


@pytest.mark.integration
def test_conflict_on_same_point_and_ts_overwrites(conn):
    """Matches Haxall's IHisExt.write contract: an existing timestamp plus
    a new value overwrites the current value - last write wins, not
    first, so this store can act as a real history-provider backend."""
    now = datetime.now(timezone.utc)
    write_point_value(conn, "urn:test-ts:temp", 71.5, now)
    write_point_value(conn, "urn:test-ts:temp", 999.0, now)

    rows = read_range(conn, "urn:test-ts:temp", now, now)
    assert rows == [(now, 999.0)]


@pytest.mark.integration
def test_delete_point_value_removes_exactly_that_row(conn):
    """The Baler side of Haxall's IHisExt val==None.val delete
    convention - a specific timestamp is removed outright."""
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    write_point_value(conn, "urn:test-ts:temp", 1.0, t1)
    write_point_value(conn, "urn:test-ts:temp", 2.0, t2)

    assert delete_point_value(conn, "urn:test-ts:temp", t1) is True
    assert delete_point_value(conn, "urn:test-ts:temp", t1) is False  # already gone

    rows = read_range(conn, "urn:test-ts:temp", t1, t2)
    assert rows == [(t2, 2.0)]


@pytest.mark.integration
def test_read_range_orders_by_time(conn):
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    write_point_value(conn, "urn:test-ts:temp", 1.0, t2)
    write_point_value(conn, "urn:test-ts:temp", 0.0, t1)

    rows = read_range(conn, "urn:test-ts:temp", t1, t2)
    assert rows == [(t1, 0.0), (t2, 1.0)]

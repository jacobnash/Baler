import pytest

from baler.haystack_pull_state import (
    ensure_schema,
    get_checkpoint,
    set_checkpoint,
)
from baler.timeseries import connect

POINT_URI = "urn:point:test-haystack-pull-state:p1"


def _conn():
    conn = connect()
    ensure_schema(conn)
    conn.execute("DELETE FROM haystack_pull_checkpoint WHERE point_uri = %s", (POINT_URI,))
    return conn


@pytest.mark.integration
def test_get_checkpoint_with_no_prior_pull_is_none():
    conn = _conn()
    assert get_checkpoint(conn, POINT_URI) is None


@pytest.mark.integration
def test_set_then_get_checkpoint_round_trips():
    conn = _conn()
    set_checkpoint(conn, POINT_URI, 12345.5)
    assert get_checkpoint(conn, POINT_URI) == 12345.5


@pytest.mark.integration
def test_set_checkpoint_twice_overwrites_not_duplicates():
    conn = _conn()
    set_checkpoint(conn, POINT_URI, 100.0)
    set_checkpoint(conn, POINT_URI, 200.0)
    assert get_checkpoint(conn, POINT_URI) == 200.0

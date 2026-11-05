"""
Pull-cycle logic tested against a real Postgres table standing in for an
"external" SQL source (Baler's own test DB, a throwaway table - the
puller only needs psycopg wire compatibility, not a genuinely separate
server) plus an in-memory Store() (same idiom as test_haystack_puller.py).
Needs live Postgres, hence integration.
"""

import time

import pytest

from baler.sql_pull_state import ensure_schema, get_checkpoint
from baler.sql_puller import pull_once
from baler.store import Store
from baler.timeseries import DEFAULT_DSN, connect, read_latest

SOURCE_ID = "test-sql-source"
QUERY = "SELECT point, value, ts FROM sql_puller_test_source WHERE ts >= %(since)s AND ts < %(until)s"


@pytest.fixture
def ts_conn():
    conn = connect()
    ensure_schema(conn)
    conn.execute("DELETE FROM point_history WHERE point_uri LIKE %s", (f"urn:point:sql/{SOURCE_ID}/%",))
    conn.execute("DELETE FROM sql_pull_checkpoint WHERE source_id = %s", (SOURCE_ID,))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sql_puller_test_source (
            point TEXT NOT NULL,
            value DOUBLE PRECISION NOT NULL,
            ts DOUBLE PRECISION NOT NULL
        )
        """
    )
    conn.execute("DELETE FROM sql_puller_test_source")
    return conn


@pytest.mark.integration
def test_first_pull_backfills_rows_already_in_the_lookback_window(ts_conn):
    store = Store()
    now = time.time()
    ts_conn.execute(
        "INSERT INTO sql_puller_test_source (point, value, ts) VALUES (%s, %s, %s)",
        ("p1", 42.0, now - 3600),
    )

    count = pull_once(DEFAULT_DSN, store, ts_conn, SOURCE_ID, QUERY, backfill_days=1.0)

    assert count == 1
    assert read_latest(ts_conn, f"urn:point:sql/{SOURCE_ID}/p1") == 42.0
    assert get_checkpoint(ts_conn, SOURCE_ID) is not None


@pytest.mark.integration
def test_second_pull_only_ingests_rows_after_the_checkpoint(ts_conn):
    store = Store()
    now = time.time()
    ts_conn.execute(
        "INSERT INTO sql_puller_test_source (point, value, ts) VALUES (%s, %s, %s)",
        ("p1", 1.0, now - 10),
    )
    first_count = pull_once(DEFAULT_DSN, store, ts_conn, SOURCE_ID, QUERY, backfill_days=1.0)
    assert first_count == 1

    time.sleep(0.05)
    ts_conn.execute(
        "INSERT INTO sql_puller_test_source (point, value, ts) VALUES (%s, %s, %s)",
        ("p1", 2.0, time.time() - 0.01),
    )
    second_count = pull_once(DEFAULT_DSN, store, ts_conn, SOURCE_ID, QUERY, backfill_days=1.0)

    assert second_count == 1
    assert read_latest(ts_conn, f"urn:point:sql/{SOURCE_ID}/p1") == 2.0

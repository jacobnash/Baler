"""Pre-folded rollups (#51/#54): read_rollup from the TimescaleDB continuous
aggregates must give exactly what folding raw samples gives - every fold,
interval and alignment, with part of the span materialized and part not.
Needs TimescaleDB (skips on plain Postgres)."""

import math
from datetime import datetime, timedelta, timezone

import pytest

from baler import timeseries

PREFIX = "urn:point:test-rollv/"


@pytest.fixture
def conn():
    c = timeseries.connect()
    has = c.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')").fetchone()[0]
    if not has:
        pytest.skip("needs TimescaleDB")
    c.execute("DELETE FROM point_history WHERE point_uri LIKE %s", (PREFIX + "%",))
    timeseries._views_present = None
    yield c
    c.execute("DELETE FROM point_history WHERE point_uri LIKE %s", (PREFIX + "%",))
    c.close()


def _seed(conn, start, hours):
    rows = []
    for p in range(3):
        uri = f"{PREFIX}p{p}"
        t = start
        i = 0
        while t < start + timedelta(hours=hours):
            if p == 2:  # a run status: bools fold as 0/1
                rows.append((uri, t, None, None, i % 3 == 0))
            elif i % 17 == 0:  # text rows are skipped by every fold
                rows.append((uri, t, None, "fault", None))
            else:
                rows.append((uri, t, 20.0 + p + math.sin(i / 7.0) * 3, None, None))
            t += timedelta(seconds=37)
            i += 1
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO point_history (point_uri, ts, value, value_text, value_bool) VALUES (%s, %s, %s, %s, %s)", rows)


def _raw(conn, monkeypatch, *args):
    monkeypatch.setattr(timeseries, "_rollup_view_for", lambda *a: None)
    try:
        return timeseries.read_rollup(conn, *args)
    finally:
        monkeypatch.undo()


@pytest.mark.integration
@pytest.mark.parametrize("fold", ["avg", "min", "max", "sum", "count"])
@pytest.mark.parametrize("interval", [300, 900, 3600, 7200])
def test_views_match_raw_folds(conn, monkeypatch, fold, interval):
    hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = hour - timedelta(hours=6)
    _seed(conn, start, 7)
    # materialize only the first part: the rest comes from real-time aggregation
    conn.execute("CALL refresh_continuous_aggregate('point_history_5m', %s, %s)", (start, start + timedelta(hours=3)))
    conn.execute("CALL refresh_continuous_aggregate('point_history_1h', %s, %s)", (start, start + timedelta(hours=2)))
    end = datetime.now(timezone.utc)  # not on a bucket boundary: the tail comes from raw rows
    uris = [f"{PREFIX}p{p}" for p in range(3)]
    args = (uris, start, end, fold, interval, None, "UTC")

    assert timeseries._rollup_view_for(conn, start, end, interval) is not None
    fast = timeseries.read_rollup(conn, *args)
    slow = _raw(conn, monkeypatch, *args)
    assert set(fast) == set(slow)
    for uri in uris:
        assert [b for b, _ in fast[uri]] == [b for b, _ in slow[uri]], uri
        for (_, a), (_, b) in zip(fast[uri], slow[uri], strict=True):
            assert a == pytest.approx(b, rel=1e-9, abs=1e-9), (uri, fold, interval)


@pytest.mark.integration
def test_unaligned_or_old_spans_use_raw_rows(conn):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    assert timeseries._rollup_view_for(conn, now + timedelta(seconds=7), now + timedelta(hours=1), 300) is None
    assert timeseries._rollup_view_for(conn, now, now + timedelta(hours=1), 450) is None  # 5 min doesn't divide it
    assert timeseries._rollup_view_for(conn, now - timedelta(days=60), now, 3600) is None  # not materialized there
    assert timeseries._rollup_view_for(conn, now, now + timedelta(hours=1), 3600) == ("point_history_1h", 3600)
    assert timeseries._rollup_view_for(conn, now + timedelta(minutes=5), now + timedelta(hours=1), 3600) == ("point_history_5m", 300)

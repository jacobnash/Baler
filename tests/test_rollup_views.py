"""Pre-folded rollups (#51/#54): read_rollup from the TimescaleDB continuous
aggregates must give exactly what folding raw samples gives - every fold,
interval and alignment, with part of the span materialized and part not,
and with backfill the last refresh hasn't seen. The integration tests need
TimescaleDB (skip on plain Postgres); the plan arithmetic is tested without
any database."""

import math
from datetime import datetime, timedelta, timezone
from itertools import pairwise

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
    monkeypatch.setattr(timeseries, "_rollup_levels", lambda *a: [])
    try:
        return timeseries.read_rollup(conn, *args)
    finally:
        monkeypatch.undo()


def _assert_same(fast, slow, uris, *what):
    assert set(fast) == set(slow)
    for uri in uris:
        assert [b for b, _ in fast[uri]] == [b for b, _ in slow[uri]], (uri, *what)
        for (_, a), (_, b) in zip(fast[uri], slow[uri], strict=True):
            # sums/averages add the same numbers in another order: equal to
            # the last couple of bits (the raw fold's own order isn't fixed
            # either - it depends on the plan and parallel workers)
            assert a == pytest.approx(b, rel=1e-9, abs=1e-9), (uri, *what)


def _plan(conn, start, end, interval):
    with conn.transaction():
        return timeseries._rollup_plan(conn, timeseries._rollup_levels(start, interval), start, end)


@pytest.mark.integration
@pytest.mark.parametrize("fold", ["avg", "min", "max", "sum", "count"])
@pytest.mark.parametrize("interval", [300, 900, 3600, 7200])
def test_views_match_raw_folds(conn, monkeypatch, fold, interval):
    hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = hour - timedelta(hours=6)
    _seed(conn, start, 7)
    # materialize only the first part: the rest comes from raw rows
    conn.execute("CALL refresh_continuous_aggregate('point_history_5m', %s, %s)", (start, start + timedelta(hours=3)))
    conn.execute("CALL refresh_continuous_aggregate('point_history_1h', %s, %s)", (start, start + timedelta(hours=2)))
    end = datetime.now(timezone.utc)  # not on a bucket boundary: the tail comes from raw rows
    uris = [f"{PREFIX}p{p}" for p in range(3)]
    args = (uris, start, end, fold, interval, None, "UTC")

    plan = _plan(conn, start, end, interval)
    assert plan is not None and any(p[0] == "view" for p in plan)
    _assert_same(timeseries.read_rollup(conn, *args), _raw(conn, monkeypatch, *args), uris, fold, interval)


@pytest.mark.integration
@pytest.mark.parametrize("fold", ["avg", "count", "max"])
def test_backfill_since_the_last_refresh_is_not_stale(conn, monkeypatch, fold):
    """Rows written into already-materialized buckets (a source backfilling
    history, a hisWrite, late data) are below the watermark, where the view
    itself serves its stale materialized rows until the next refresh."""
    hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = hour - timedelta(hours=6)
    _seed(conn, start, 5)
    conn.execute("CALL refresh_continuous_aggregate('point_history_5m', %s, %s)", (start, hour))
    conn.execute("CALL refresh_continuous_aggregate('point_history_1h', %s, %s)", (start, hour))
    uris = [f"{PREFIX}p{p}" for p in range(3)]
    # backfill: new samples between existing ones, and an overwrite
    t0 = start + timedelta(hours=2, seconds=11)
    for i in range(40):
        timeseries.write_point_value(conn, uris[0], 900.0 + i, t0 + timedelta(seconds=37 * i))
    timeseries.write_point_value(conn, uris[1], -5.0, start + timedelta(hours=1))
    view_count = conn.execute(
        "SELECT sum(vcount) FROM point_history_5m WHERE point_uri = %s AND bucket >= %s AND bucket < %s",
        (uris[0], start, hour)).fetchone()[0]
    raw_count = conn.execute(
        "SELECT count(value) FROM point_history WHERE point_uri = %s AND ts >= %s AND ts < %s",
        (uris[0], start, hour)).fetchone()[0]
    assert view_count < raw_count  # the view alone is stale here: what this test guards against

    end = hour
    for interval in (300, 3600):
        args = (uris, start, end, fold, interval, None, "UTC")
        _assert_same(timeseries.read_rollup(conn, *args), _raw(conn, monkeypatch, *args), uris, fold, interval)
    # 5m refreshed, 1h not: the 1h view is stale through its source's changes
    conn.execute("CALL refresh_continuous_aggregate('point_history_5m', %s, %s)", (start, hour))
    args = (uris, start, end, fold, 3600, None, "UTC")
    _assert_same(timeseries.read_rollup(conn, *args), _raw(conn, monkeypatch, *args), uris, fold, "5m only")
    plan = _plan(conn, start, end, 3600)
    assert plan is not None and any(p[0] == "view" and p[2].endswith(str(_mat_id(conn, "point_history_5m"))) for p in plan)


def _mat_id(conn, view):
    return conn.execute("SELECT mat_hypertable_id FROM _timescaledb_catalog.continuous_agg WHERE user_view_name = %s",
                        (view,)).fetchone()[0]


@pytest.mark.integration
def test_catalog_unreadable_falls_back_to_raw(conn, monkeypatch):
    hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = hour - timedelta(hours=3)
    _seed(conn, start, 3)
    uris = [f"{PREFIX}p{p}" for p in range(3)]
    args = (uris, start, hour, "avg", 300, None, "UTC")
    slow = _raw(conn, monkeypatch, *args)
    monkeypatch.setattr(timeseries, "_DIRTY_SQL", "SELECT * FROM _timescaledb_catalog.no_such_table")
    assert _plan(conn, start, hour, 300) is None
    _assert_same(timeseries.read_rollup(conn, *args), slow, uris, "fallback")


@pytest.mark.integration
def test_old_spans_use_raw_rows(conn):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    plan = _plan(conn, now - timedelta(days=400), now - timedelta(days=399), 3600)  # never materialized there
    assert plan is None


def test_levels_need_a_dividing_bucket_and_an_aligned_start():
    hour = datetime(2026, 11, 7, 13, tzinfo=timezone.utc)
    assert timeseries._rollup_levels(hour + timedelta(seconds=7), 300) == []
    assert timeseries._rollup_levels(hour, 450) == []  # 5 min doesn't divide it
    assert timeseries._rollup_levels(hour, 3600) == [("point_history_1h", 3600), ("point_history_5m", 300)]
    assert timeseries._rollup_levels(hour + timedelta(minutes=5), 3600) == [("point_history_5m", 300)]
    assert timeseries._rollup_levels(hour.replace(tzinfo=None), 3600) == []
    assert timeseries._rollup_levels(hour, None) == []


US = 1_000_000
H = 3600 * US
INF = 2**63


@pytest.mark.parametrize("dirty_5m,dirty_1h", [
    ([], []),
    ([(13 * H, None)], [(12 * H, None)]),  # unrefreshed tails
    ([(2 * H + 300 * US, 2 * H + 900 * US), (13 * H, None)], [(2 * H, 3 * H), (12 * H, None)]),  # plus a backfill
    ([(None, None)], [(None, None)]),  # nothing materialized
])
def test_plan_covers_the_span_exactly_once(monkeypatch, dirty_5m, dirty_1h):
    """Every instant of the span is in exactly one piece; view pieces are
    whole buckets of that view and avoid its dirty ranges. Offsets are from
    midnight, None is -inf/+inf."""
    day = int(datetime(2026, 11, 7, tzinfo=timezone.utc).timestamp()) * US

    def absolute(ranges):
        return [(-INF if a is None else day + a, INF if b is None else day + b) for a, b in ranges]

    dirty = {"point_history_5m": absolute(dirty_5m), "point_history_1h": absolute(dirty_1h)}
    tables = {"point_history_5m": ("s", "m5"), "point_history_1h": ("s", "h1")}
    monkeypatch.setattr(timeseries, "_dirty_by_view", lambda conn, views: (dirty, tables))
    start = datetime(2026, 11, 7, tzinfo=timezone.utc)
    end = start + timedelta(hours=13, minutes=47, seconds=3)
    plan = timeseries._rollup_plan(None, timeseries._rollup_levels(start, 3600), start, end)
    if dirty_5m == [(None, None)]:
        assert plan is None  # nothing usable: plain raw read
        return
    assert plan is not None
    pieces = sorted((p[-2], p[-1], p) for p in plan)
    assert pieces[0][0] == day and pieces[-1][1] == int(end.timestamp() * US)
    for (_, b, _), (a, _, _) in pairwise(pieces):
        assert a == b  # contiguous, no overlap
    for a, b, p in pieces:
        if p[0] == "view":
            size = p[3] * US
            assert a % size == 0 and b % size == 0
            view = {"m5": "point_history_5m", "h1": "point_history_1h"}[p[2]]
            assert all(b <= da or a >= db for da, db in dirty[view])
    if not dirty_5m:
        assert [p[2] for p in plan if p[0] == "view"] == ["h1", "m5"]  # 1h up to 13:00, 5m to 13:45

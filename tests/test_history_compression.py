"""point_history's native compression (#192).

The setup must be idempotent and in place (the live install upgrades its
existing database), keep its policy in step with BALER_HISTORY_COMPRESS_AFTER,
and never fail a connection. Once a chunk is compressed, late writes,
retained-message replays, overwrites and deletes into it must still work,
and every history read must return exactly what it returned before.

The unit tests drive _ensure_compression with a fake connection; the
integration tests need docker-compose's TimescaleDB."""

import logging
import math
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from baler import ddl, timeseries
from baler.commissioning.history import _AVAILABILITY_SQL

PREFIX = "urn:point:test-compress/"


# --- unit: which DDL runs, from the catalog state ---------------------------


class _FakeConn:
    def __init__(self, state, error=None):
        self.state, self.error = state, error

    def execute(self, query, params=None):
        if self.error:
            raise self.error
        state = self.state

        class _Cur:
            def fetchone(self):
                return state

        return _Cur()


def _ran(monkeypatch, state, fail=None):
    ran: list = []

    def fake_run(conn, *stmts):
        if fail:
            raise fail
        ran.extend(str(s) for s in stmts)

    monkeypatch.setattr(ddl, "run", fake_run)
    timeseries._ensure_compression(_FakeConn(state))
    return " ".join(ran)


def test_nothing_runs_once_set_up(monkeypatch):
    assert _ran(monkeypatch, (True, True, True)) == ""


def test_no_hypertable_yet_does_nothing(monkeypatch):
    assert _ran(monkeypatch, None) == ""


def test_fresh_database_enables_compression_and_adds_the_policy(monkeypatch):
    ran = _ran(monkeypatch, (False, False, False))
    assert "lock_timeout" in ran
    assert "compress_segmentby = 'point_uri, unit, label'" in ran and "compress_orderby = 'ts DESC'" in ran
    assert "add_compression_policy" in ran and "if_not_exists => true" in ran
    assert "remove_compression_policy" not in ran


def test_changed_compress_after_replaces_the_policy(monkeypatch):
    monkeypatch.setattr(timeseries, "COMPRESS_AFTER", "30 days")
    ran = _ran(monkeypatch, (True, True, False))
    assert "ALTER TABLE" not in ran
    assert ran.index("remove_compression_policy") < ran.index("add_compression_policy")
    assert "'30 days'" in ran


def test_ddl_failure_never_fails_the_connection(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="baler.timeseries"):
        _ran(monkeypatch, (False, False, False), fail=psycopg.errors.LockNotAvailable("busy"))
    assert "retried on the next new connection" in caplog.text


def test_bad_interval_is_logged_not_raised(monkeypatch, caplog):
    monkeypatch.setattr(ddl, "run", lambda *a: pytest.fail("no DDL with a bad interval"))
    with caplog.at_level(logging.ERROR, logger="baler.timeseries"):
        timeseries._ensure_compression(_FakeConn(None, error=psycopg.errors.InvalidDatetimeFormat("bad")))
    assert "BALER_HISTORY_COMPRESS_AFTER" in caplog.text


# --- integration ------------------------------------------------------------


@pytest.fixture
def conn():
    c = timeseries.connect()
    if not c.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')").fetchone()[0]:
        pytest.skip("needs TimescaleDB")
    c.execute("DELETE FROM point_history WHERE point_uri LIKE %s", (PREFIX + "%",))
    yield c
    c.execute("DELETE FROM point_history WHERE point_uri LIKE %s", (PREFIX + "%",))
    c.close()


def _policy(conn):
    return conn.execute(
        "SELECT h.compression_enabled, (j.config->>'compress_after')::interval FROM timescaledb_information.hypertables h "
        "LEFT JOIN timescaledb_information.jobs j ON j.proc_name = 'policy_compression' AND j.hypertable_name = h.hypertable_name "
        "WHERE h.hypertable_name = 'point_history'").fetchone()


@pytest.mark.integration
def test_connect_sets_up_compression(conn):
    enabled, after = _policy(conn)
    assert enabled
    assert after == conn.execute("SELECT %s::interval", (timeseries.COMPRESS_AFTER,)).fetchone()[0]
    settings = dict(conn.execute(
        "SELECT attname, segmentby_column_index IS NOT NULL FROM timescaledb_information.compression_settings "
        "WHERE hypertable_name = 'point_history'").fetchall())
    # segment by point (and its constant unit/label), order by ts
    assert settings == {"point_uri": True, "unit": True, "label": True, "ts": False}


@pytest.mark.integration
def test_setup_is_a_no_op_when_present(conn, monkeypatch):
    monkeypatch.setattr(ddl, "run", lambda *a: pytest.fail("DDL ran although compression is set up"))
    timeseries._setup(conn)
    timeseries._ensure_compression(conn)


@pytest.mark.integration
def test_compress_after_follows_the_setting(conn, monkeypatch):
    original = timeseries.COMPRESS_AFTER
    try:
        monkeypatch.setattr(timeseries, "COMPRESS_AFTER", "9 days")
        timeseries._ensure_compression(conn)
        assert _policy(conn)[1] == timedelta(days=9)
    finally:
        monkeypatch.setattr(timeseries, "COMPRESS_AFTER", original)
        timeseries._ensure_compression(conn)
    assert _policy(conn)[1] == conn.execute("SELECT %s::interval", (original,)).fetchone()[0]


def _seed(conn, start, hours, step=30):
    rows = []
    for p in range(3):
        uri = f"{PREFIX}p{p}"
        n = int(hours * 3600 / step)
        for i in range(n):
            t = start + timedelta(seconds=step * i)
            if p == 2:
                rows.append((uri, t, None, None, i % 3 == 0, None, None))
            elif i % 17 == 0:
                rows.append((uri, t, None, "fault", None, None, None))
            else:
                rows.append((uri, t, 20.0 + p + math.sin(i / 7.0) * 3, None, None,
                             "degF" if p == 1 and i % 100 == 0 else None, None))
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO point_history (point_uri, ts, value, value_text, value_bool, unit, label) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)", rows)


def _compress(conn, start, end):
    """Compress every chunk overlapping [start, end); returns how many are compressed."""
    conn.execute(
        "SELECT compress_chunk(c, if_not_compressed => true) FROM show_chunks('point_history', newer_than => %s, older_than => %s) c",
        (start - timedelta(days=8), end + timedelta(days=8)))
    return conn.execute(
        "SELECT count(*) FILTER (WHERE is_compressed), count(*) FROM timescaledb_information.chunks "
        "WHERE hypertable_name = 'point_history' AND range_start < %s AND range_end > %s", (end, start)).fetchone()


def _reads(conn, uris, start, end):
    raw_levels = timeseries._rollup_levels
    out = {
        "range": timeseries.read_range(conn, uris[0], start, end),
        "range_many": timeseries.read_range_many(conn, uris, start, end, 100_000),
        "range_many_limited": timeseries.read_range_many(conn, uris, start, end, 50),
        "latest": timeseries.read_latest_many(conn, uris),
        "latest_asof": timeseries.read_latest_many(conn, uris, before=start + timedelta(hours=2), with_value=True,
                                                   since=start + timedelta(hours=1)),
        "labels": timeseries.read_point_labels(conn, uris),
        "rollup_planned": timeseries.read_rollup(conn, uris, start, end, "avg", 3600, None, "UTC"),
        "rollup_month": timeseries.read_rollup(conn, uris, start, end, "max", None, 1, "UTC"),
        "availability": sorted(conn.execute(_AVAILABILITY_SQL, {"uris": uris, "start": start, "end": end, "gap": 60.0}).fetchall()),
    }
    timeseries._rollup_levels = lambda *a: []  # type: ignore[assignment]
    try:
        out["rollup_raw"] = timeseries.read_rollup(conn, uris, start, end, "sum", 900, None, "UTC")
    finally:
        timeseries._rollup_levels = raw_levels  # type: ignore[assignment]
    return out


def _old_span():
    # Well past any compress_after, so the policy would compress it too.
    start = (datetime.now(timezone.utc) - timedelta(days=60)).replace(minute=0, second=0, microsecond=0)
    return start, start + timedelta(hours=6)


@pytest.mark.integration
def test_reads_are_unchanged_by_compression(conn):
    start, end = _old_span()
    _seed(conn, start, 6)
    uris = [f"{PREFIX}p{p}" for p in range(3)]
    for view in ("point_history_5m", "point_history_1h"):
        conn.execute("CALL refresh_continuous_aggregate(%s, %s, %s)", (view, start, end))
    before = _reads(conn, uris, start, end)
    compressed, total = _compress(conn, start, end)
    assert compressed == total > 0
    after = _reads(conn, uris, start, end)
    for k in before:
        if k.startswith("rollup"):
            for uri in uris:
                assert [b for b, _ in before[k][uri]] == [b for b, _ in after[k][uri]], k
                for (_, a), (_, b) in zip(before[k][uri], after[k][uri], strict=True):
                    assert a == pytest.approx(b, rel=1e-9, abs=1e-9), k
        else:
            assert before[k] == after[k], k


@pytest.mark.integration
def test_late_writes_into_compressed_chunks(conn, monkeypatch):
    start, end = _old_span()
    _seed(conn, start, 6)
    uris = [f"{PREFIX}p{p}" for p in range(3)]
    assert _compress(conn, start, end)[0] > 0
    n0 = conn.execute("SELECT count(*) FROM point_history WHERE point_uri LIKE %s", (PREFIX + "%",)).fetchone()[0]

    # retained-message replay: the same (point, ts) again, idempotent
    t = start + timedelta(hours=1)
    timeseries.write_point_value(conn, uris[0], 999.0, t)
    assert timeseries.read_range(conn, uris[0], t, t) == [(t, 999.0)]  # last write wins, as before
    # trend backfill: new samples between existing ones, hours late
    late = [start + timedelta(hours=2, seconds=30 * i + 7) for i in range(200)]
    for i, ts in enumerate(late):
        timeseries.write_point_value(conn, uris[1], 500.0 + i, ts)
    # a bulk replay in one statement (ON CONFLICT DO NOTHING over existing rows)
    conn.execute(
        "INSERT INTO point_history (point_uri, ts, value) SELECT %s, ts, -1 "
        "FROM generate_series(%s::timestamptz, %s::timestamptz, interval '30 seconds') ts ON CONFLICT DO NOTHING",
        (uris[0], start, end - timedelta(seconds=30)))
    # a deleted timestamp (hisWrite with val == None.val)
    gone = start + timedelta(hours=3)
    assert timeseries.delete_point_value(conn, uris[0], gone)
    assert timeseries.read_range(conn, uris[0], gone, gone) == []

    n1 = conn.execute("SELECT count(*) FROM point_history WHERE point_uri LIKE %s", (PREFIX + "%",)).fetchone()[0]
    assert n1 == n0 + len(late) - 1
    got = dict(timeseries.read_range(conn, uris[1], late[0], late[-1]))
    assert all(got[ts] == 500.0 + i for i, ts in enumerate(late))
    assert all(v != -1 for _, v in timeseries.read_range(conn, uris[0], start, end))  # DO NOTHING kept the originals

    # the policy's job recompresses the chunk; reads agree before and after
    before = _reads(conn, uris, start, end)
    conn.execute("SELECT compress_chunk(c) FROM show_chunks('point_history', newer_than => %s, older_than => %s) c",
                 (start - timedelta(days=8), end + timedelta(days=8)))
    assert _reads(conn, uris, start, end)["range_many"] == before["range_many"]

    # continuous aggregates refresh over compressed chunks; the views then agree with the raw fold
    for view in ("point_history_5m", "point_history_1h"):
        conn.execute("CALL refresh_continuous_aggregate(%s, %s, %s)", (view, start, end))
    with conn.transaction():
        plan = timeseries._rollup_plan(conn, timeseries._rollup_levels(start, 3600), start, end)
    assert plan is not None and all(p[0] == "view" for p in plan)
    planned = timeseries.read_rollup(conn, uris, start, end, "avg", 3600, None, "UTC")
    monkeypatch.setattr(timeseries, "_rollup_levels", lambda *a: [])
    raw = timeseries.read_rollup(conn, uris, start, end, "avg", 3600, None, "UTC")
    for uri in uris:
        assert [b for b, _ in planned[uri]] == [b for b, _ in raw[uri]]
        for (_, a), (_, b) in zip(planned[uri], raw[uri], strict=True):
            assert a == pytest.approx(b, rel=1e-9, abs=1e-9)

"""point_history's chunk interval and compression schedule (#245).

Settings, not schema: blank leaves the database as it is (a small site's
defaults and behaviour unchanged), set they apply to new chunks only, and
late data still lands in compressed chunks at a large site's batch sizes.
Unit tests use a fake connection; the integration tests need docker-compose's
TimescaleDB."""

import logging
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from baler import ddl, history_chunks, timeseries

PREFIX = "urn:point:test-chunks/"
DAY, HOUR = 86400, 3600


# --- unit: sizing -------------------------------------------------------------


def test_small_site_keeps_the_shipped_defaults():
    rec = history_chunks.recommend(50 / 5, 8 * 10**9)  # 50 points every 5 s
    assert rec.is_default and rec.chunk_s == 7 * DAY and rec.compress_after_s == 7 * DAY
    assert set(rec.env().values()) == {""}


def test_100k_points_gets_hour_chunks():
    rec = history_chunks.recommend(100_000 / 5, 32 * 10**9)
    assert (rec.chunk_s, rec.compress_after_s, rec.schedule_s) == (HOUR, 3 * HOUR, HOUR // 2)
    assert rec.env() == {"BALER_HISTORY_CHUNK_INTERVAL": "1 hour", "BALER_HISTORY_COMPRESS_AFTER": "3 hours",
                         "BALER_HISTORY_COMPRESS_SCHEDULE": ""}
    assert any("over 25%" in n for n in rec.notes)  # 18 GB a chunk: the rule says so honestly


def test_middle_rates_pick_the_longest_chunk_that_fits():
    rec = history_chunks.recommend(2_000, 64 * 10**9)  # 1.8 GB/h raw, 16 GB budget
    assert rec.chunk_s == 6 * HOUR and rec.compress_after_s == 18 * HOUR and rec.schedule_s == 3 * HOUR
    rec = history_chunks.recommend(100, 16 * 10**9)  # 2.16 GB/day raw, 4 GB budget
    assert rec.chunk_s == DAY and rec.compress_after_s == 3 * DAY and rec.schedule_s == 12 * HOUR


def test_recommend_cli(capsys):
    assert history_chunks.main(["recommend", "--points", "100000", "--pg-memory", "32g"]) == 0
    out = capsys.readouterr().out
    assert "BALER_HISTORY_CHUNK_INTERVAL=1 hour" in out and "BALER_HISTORY_COMPRESS_AFTER=3 hours" in out


# --- unit: what ensure() runs ---------------------------------------------------


class _FakeConn:
    def __init__(self, row, error=None):
        self.row, self.error, self.params = row, error, None

    def execute(self, query, params=None):
        if self.error:
            raise self.error
        self.params = params
        row = self.row

        class _Cur:
            def fetchone(self):
                return row

        return _Cur()


def _ensure(monkeypatch, row, chunk, schedule="", error=None):
    ran: list = []
    monkeypatch.setattr(ddl, "run", lambda conn, *stmts: ran.extend(str(s) for s in stmts))
    conn = _FakeConn(row, error)
    changed = history_chunks.ensure(conn, chunk=chunk, schedule=schedule)
    return " ".join(ran), changed, conn


def test_blank_settings_read_nothing_and_change_nothing(monkeypatch):
    ran, changed, conn = _ensure(monkeypatch, None, "")
    assert ran == "" and changed == [] and conn.params is None  # not even the catalog read


def test_matching_settings_are_a_no_op(monkeypatch):
    h = timedelta(hours=1)
    ran, changed, _ = _ensure(monkeypatch, (h, h, 1000, h / 2, h / 2), "1 hour")
    assert ran == "" and changed == []


def test_new_interval_and_schedule(monkeypatch):
    week, h = timedelta(days=7), timedelta(hours=1)
    ran, changed, conn = _ensure(monkeypatch, (week, h, 1000, timedelta(hours=12), h / 2), "1 hour")
    assert conn.params == {"chunk": "1 hour", "schedule": None}
    assert "lock_timeout" in ran and "set_chunk_time_interval" in ran and "alter_job(" in ran and "Literal(1000)" in ran
    assert len(changed) == 2 and "new chunks only" in changed[0]


def test_no_policy_job_changes_only_the_interval(monkeypatch):
    week, h = timedelta(days=7), timedelta(hours=1)
    ran, _, _ = _ensure(monkeypatch, (week, h, None, None, h / 2), "1 hour")
    assert "set_chunk_time_interval" in ran and "alter_job" not in ran


def test_bad_interval_is_logged_not_raised(monkeypatch, caplog):
    with caplog.at_level(logging.ERROR, logger="baler.history_chunks"):
        ran, changed, _ = _ensure(monkeypatch, None, "soon", error=psycopg.errors.InvalidDatetimeFormat("bad"))
    assert ran == "" and changed == [] and "BALER_HISTORY_CHUNK_INTERVAL" in caplog.text


def test_lock_failure_never_fails_the_connection(monkeypatch, caplog):
    def busy(*a):
        raise psycopg.errors.LockNotAvailable("busy")

    monkeypatch.setattr(ddl, "run", busy)
    with caplog.at_level(logging.WARNING, logger="baler.history_chunks"):
        assert history_chunks.ensure(_FakeConn((timedelta(days=7), timedelta(hours=1), None, None, None)),
                                     chunk="1 hour") == []
    assert "next process start" in caplog.text


# --- integration ----------------------------------------------------------------


@pytest.fixture
def conn():
    c = timeseries.connect()
    if not c.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')").fetchone()[0]:
        pytest.skip("needs TimescaleDB")
    c.execute("DELETE FROM point_history WHERE point_uri LIKE %s", (PREFIX + "%",))
    yield c
    c.execute("DELETE FROM point_history WHERE point_uri LIKE %s", (PREFIX + "%",))
    c.close()


def _state(conn):
    return conn.execute(
        "SELECT d.time_interval, j.schedule_interval FROM timescaledb_information.dimensions d "
        "LEFT JOIN timescaledb_information.jobs j ON j.proc_name = 'policy_compression' "
        "AND j.hypertable_name = 'point_history' WHERE d.hypertable_name = 'point_history'").fetchone()


@pytest.mark.integration
def test_interval_and_schedule_follow_the_settings_and_blank_leaves_them(conn, monkeypatch):
    before = _state(conn)
    try:
        assert history_chunks.ensure(conn, chunk="", schedule="") == []  # blank: untouched
        assert _state(conn) == before
        changed = history_chunks.ensure(conn, chunk="2 hours", schedule="")
        assert _state(conn) == (timedelta(hours=2), timedelta(hours=1))  # derived: half a chunk
        assert changed
        assert history_chunks.ensure(conn, chunk="2 hours", schedule="") == []  # idempotent
        history_chunks.ensure(conn, chunk="2 hours", schedule="20 minutes")
        assert _state(conn)[1] == timedelta(minutes=20)
        history_chunks.ensure(conn, chunk="2 days", schedule="")
        assert _state(conn) == (timedelta(days=2), timedelta(hours=12))
    finally:
        conn.execute("SELECT set_chunk_time_interval('point_history', %s::interval)", (before[0],))
        if before[1] is not None:
            conn.execute("SELECT alter_job(job_id, schedule_interval => %s) FROM timescaledb_information.jobs "
                         "WHERE proc_name = 'policy_compression' AND hypertable_name = 'point_history'", (before[1],))
    assert _state(conn) == before


@pytest.mark.integration
def test_new_interval_applies_to_new_chunks_only(conn):
    before = _state(conn)[0]
    far = datetime(2031, 1, 1, tzinfo=timezone.utc)  # clear of every other test's chunks
    old = [(f"{PREFIX}a", far + timedelta(hours=i), float(i)) for i in range(3)]

    def chunks():
        return conn.execute(
            "SELECT range_start, range_end FROM timescaledb_information.chunks WHERE hypertable_name = 'point_history' "
            "AND range_end > %s AND range_start < %s ORDER BY range_start",
            (far - timedelta(days=30), far + timedelta(days=60))).fetchall()

    try:
        timeseries.write_point_values(conn, [(u, v, t) for u, t, v in old])
        existing = chunks()
        assert len(existing) == 1
        width = existing[0][1] - existing[0][0]
        history_chunks.ensure(conn, chunk="1 hour", schedule="")
        # inside the existing chunk's range: still lands there, nothing rewritten
        timeseries.write_point_value(conn, f"{PREFIX}a", 9.0, far + timedelta(hours=5))
        assert chunks() == existing
        # past its end: a new 1-hour chunk
        after_end = existing[0][1] + timedelta(minutes=30)
        timeseries.write_point_value(conn, f"{PREFIX}a", 10.0, after_end)
        new = [c for c in chunks() if c not in existing]
        assert len(new) == 1 and new[0][1] - new[0][0] <= timedelta(hours=1) and width > timedelta(hours=1)
        assert conn.execute("SELECT count(*) FROM point_history WHERE point_uri = %s", (f"{PREFIX}a",)).fetchone()[0] == 5
    finally:
        conn.execute("SELECT set_chunk_time_interval('point_history', %s::interval)", (before,))
        conn.execute("DELETE FROM point_history WHERE point_uri LIKE %s", (PREFIX + "%",))
        conn.execute("SELECT drop_chunks('point_history', newer_than => %s::timestamptz)", (far - timedelta(days=30),))


@pytest.mark.integration
def test_large_late_batch_into_compressed_chunks(conn):
    """A retained replay / spool replay at a large site's batch size: one
    write_point_values batch that overwrites existing rows of 2,000 points in
    compressed chunks (one compressed batch each, 2,000 x 360 rows) and adds
    new rows between them. Over TimescaleDB's default 100,000-row
    decompression cap, which made the listener's batch fail and the listener
    restart-loop on redelivery (#245). No errors, no duplicates, last write
    wins, and the chunk recompresses with every row."""
    start = (datetime.now(timezone.utc) - timedelta(days=90)).replace(minute=0, second=0, microsecond=0)
    n_points, n_steps = 2000, 360
    conn.execute(
        "INSERT INTO point_history (point_uri, ts, value) "
        "SELECT %s || 'p' || p, %s::timestamptz + make_interval(secs => 60 * i), i "
        "FROM generate_series(1, %s) p, generate_series(0, %s) i",
        (PREFIX, start, n_points, n_steps - 1))
    conn.execute("SELECT compress_chunk(c, if_not_compressed => true) FROM show_chunks('point_history', "
                 "newer_than => %s::timestamptz - interval '8 days', older_than => %s::timestamptz + interval '8 days') c",
                 (start, start))
    assert conn.execute(
        "SELECT bool_and(is_compressed) FROM timescaledb_information.chunks WHERE hypertable_name = 'point_history' "
        "AND range_start <= %s AND range_end > %s", (start, start)).fetchone()[0]
    replay_ts = start + timedelta(minutes=30)  # existing (point, ts): overwritten
    backfill_ts = start + timedelta(minutes=30, seconds=20)  # new, between existing samples
    rows = [(f"{PREFIX}p{p}", -1.0, replay_ts) for p in range(1, n_points + 1)]
    rows += [(f"{PREFIX}p{p}", -2.0, backfill_ts) for p in range(1, n_points + 1)]
    assert timeseries.write_point_values(conn, rows) == 2 * n_points
    # nothing else in this session was lifted: the cap is per transaction
    assert conn.execute("SHOW timescaledb.max_tuples_decompressed_per_dml_transaction").fetchone()[0] == "100000"
    total, dupes = conn.execute(
        "SELECT count(*), count(*) - count(DISTINCT (point_uri, ts)) FROM point_history WHERE point_uri LIKE %s",
        (PREFIX + "%",)).fetchone()
    assert (total, dupes) == (n_points * (n_steps + 1), 0)
    vals = dict(conn.execute("SELECT ts, array_agg(DISTINCT value) FROM point_history WHERE point_uri LIKE %s "
                             "AND ts IN (%s, %s) GROUP BY ts", (PREFIX + "%", replay_ts, backfill_ts)).fetchall())
    assert vals == {replay_ts: [-1.0], backfill_ts: [-2.0]}
    conn.execute("SELECT compress_chunk(c) FROM show_chunks('point_history', newer_than => %s::timestamptz - interval "
                 "'8 days', older_than => %s::timestamptz + interval '8 days') c", (start, start))
    assert conn.execute("SELECT count(*) FROM point_history WHERE point_uri LIKE %s",
                        (PREFIX + "%",)).fetchone()[0] == total

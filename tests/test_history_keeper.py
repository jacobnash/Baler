"""baler.history_keeper (#337): point_history compression upkeep.

nash-srv's 6 h soak at 100k points: a compression policy run failed with
only "Failed to convert '1' chunks to columnstore" to show for it, late
writes left compressed chunks at 12 GB on disk against 565 MB in the chunk
stats, and nothing reported either. The unit tests drive the keeper's
decisions and the Ops card; the integration tests need TimescaleDB: a
forced failure is recorded with its real cause and a later retry
compresses the chunk; late rows are recompressed once they settle and the
disk comes back to near the stats figure."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import psycopg.types.json
import pytest

from baler import history_keeper as hk
from baler import ops_status, timeseries

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


def _chunk(name="c1", end_ago=timedelta(hours=10), status=0, disk=100, stats=None, writes=0):
    return hk.Chunk(name, NOW - end_ago, status, disk, disk, stats, writes)


def _policy(**kw):
    return hk.Policy(**{"job_id": 1000, "compress_after": 3 * HOUR, "schedule": HOUR / 2, **kw})


def _plan(cs, pol=None, failing=None, settle=None, now=NOW, **kw):
    settle = settle or hk.Settle()
    settle.observe(cs, now)
    return [(c.name, a) for c, a, _ in hk.plan(cs, pol or _policy(), failing or {}, settle, now, **kw)]


# --- unit: what a pass decides ------------------------------------------------


def test_an_uncompressed_chunk_is_compressed_once_the_policy_had_its_chance():
    due = _chunk("due", end_ago=3 * HOUR + HOUR / 2 + timedelta(minutes=1))
    early = _chunk("early", end_ago=3 * HOUR + timedelta(minutes=10))  # compress_after passed, the schedule not yet
    assert _plan([due, early]) == [("due", "compress")]


def test_nothing_heavy_while_the_policy_runs_or_is_paused():
    cs = [_chunk("due"), _chunk("late", status=hk.COMPRESSED | hk.PARTIAL)]
    settle = hk.Settle()
    settle.observe(cs, NOW - timedelta(hours=2))  # quiet for 2 h
    assert _plan(cs, _policy(running=True), settle=settle) == []
    assert _plan(cs, _policy(paused=True), settle=settle) == []
    assert _plan(cs, settle=settle) == [("due", "compress"), ("late", "recompress")]


def test_late_rows_are_recompressed_once_they_settle_or_waited_too_long():
    c = _chunk("late", status=hk.COMPRESSED | hk.PARTIAL, writes=10)
    settle = hk.Settle()
    assert _plan([c], settle=settle, settle_s=600, max_defer_s=3600) == []  # just seen
    later = NOW + timedelta(seconds=601)
    assert _plan([c], settle=settle, now=later, settle_s=600, max_defer_s=3600) == [("late", "recompress")]
    # still being written: deferred, until it has been partial for max_defer_s
    settle = hk.Settle()
    for i, t in enumerate(range(0, 3600, 300)):
        busy = _chunk("late", status=hk.COMPRESSED | hk.PARTIAL, writes=10 + i)
        assert _plan([busy], settle=settle, now=NOW + timedelta(seconds=t), settle_s=600, max_defer_s=3600) == []
    busy = _chunk("late", status=hk.COMPRESSED | hk.PARTIAL, writes=99)
    assert _plan([busy], settle=settle, now=NOW + timedelta(seconds=3600), settle_s=600,
                 max_defer_s=3600) == [("late", "recompress")]


def test_a_failed_chunk_is_retried_with_backoff():
    c = _chunk("due")
    f = hk.Failing("due", "compress", NOW - timedelta(seconds=10), "55P03", "lock timeout", None, attempts=1)
    assert _plan([c], failing={"due": f}) == []
    assert _plan([c], failing={"due": f}, now=f.next_try()) == [("due", "compress")]
    three = hk.Failing("due", "compress", NOW, "XX000", "boom", None, attempts=3)
    assert three.next_try() - NOW == timedelta(seconds=4 * hk.RETRY_BASE_S)
    assert hk.Failing("due", "compress", NOW, None, None, None, attempts=30).next_try() - NOW == timedelta(
        seconds=hk.RETRY_MAX_S)


def test_a_bloated_compressed_chunk_is_reclaimed_and_heavy_work_is_capped():
    mb = 1024 * 1024
    fat = _chunk("fat", status=hk.COMPRESSED, disk=200 * mb, stats=12 * mb)
    lean = _chunk("lean", status=hk.COMPRESSED, disk=13 * mb, stats=12 * mb)
    assert fat.bloated() and not lean.bloated()
    assert _plan([fat, lean]) == [("fat", "reclaim")]
    due = [_chunk(f"d{i}") for i in range(6)]
    assert _plan(due, max_chunks=2) == [("d0", "compress"), ("d1", "compress")]
    assert _plan([*due, fat], max_chunks=2)[-1] == ("fat", "reclaim")  # reclaim isn't capped


def test_settle_tracks_writes_to_compressed_chunks():
    s = hk.Settle()
    s.observe([_chunk("late", status=hk.COMPRESSED | hk.PARTIAL, writes=5)], NOW)
    s.observe([_chunk("late", status=hk.COMPRESSED, writes=5)], NOW + HOUR)
    assert s.partial_since == {}  # recompressed: no longer partial
    assert s.settled("late", NOW + HOUR, settle_s=600)  # no writes for an hour
    s.observe([_chunk("late", status=hk.COMPRESSED, writes=6)], NOW + 2 * HOUR)
    assert not s.settled("late", NOW + 2 * HOUR, settle_s=600)  # a late write just now
    s.observe([], NOW + 3 * HOUR)  # dropped
    assert s.writes == {}


class _ReindexConn:
    """Records statements; REINDEX ... CONCURRENTLY fails when told to,
    leaving an invalid _ccnew copy behind as Postgres does."""

    autocommit = True

    def __init__(self, concurrently_fails=False):
        self.fails, self.ran = concurrently_fails, []

    def execute(self, query, params=None):
        text = query.as_string(None) if hasattr(query, "as_string") else query
        self.ran.append(text)
        if "CONCURRENTLY" in text and text.startswith("REINDEX") and self.fails:
            raise psycopg.errors.FeatureNotSupported("cannot reindex this relation concurrently")

        class _Cur:
            def fetchall(self_inner):
                return [("_timescaledb_internal.\"c1_ts_idx_ccnew\"",)] if "pg_index" in text else []

        return _Cur()


def test_reindex_is_concurrent_so_late_writes_are_not_blocked():
    conn = _ReindexConn()
    assert hk._reindex(conn, _chunk("_timescaledb_internal._hyper_1_1_chunk"), settled=False) == "concurrently"
    assert conn.ran == ["REINDEX TABLE CONCURRENTLY _timescaledb_internal._hyper_1_1_chunk"]


def test_a_failed_concurrent_reindex_falls_back_only_on_a_settled_chunk():
    c = _chunk("_timescaledb_internal._hyper_1_1_chunk")
    busy = _ReindexConn(concurrently_fails=True)
    form = hk._reindex(busy, c, settled=False)
    assert form.startswith("skipped") and "cannot reindex this relation concurrently" in form
    assert not any(q.startswith("REINDEX TABLE _timescaledb") for q in busy.ran)  # never the blocking form
    assert any(q.startswith("DROP INDEX CONCURRENTLY") and "_ccnew" in q for q in busy.ran)  # leftover copy dropped
    quiet = _ReindexConn(concurrently_fails=True)
    assert hk._reindex(quiet, c, settled=True).startswith("plain (concurrently failed")
    assert quiet.ran[-1] == "REINDEX TABLE _timescaledb_internal._hyper_1_1_chunk"


def test_attempt_refuses_a_connection_in_a_transaction():
    class InTx:
        autocommit = False

    with pytest.raises(ValueError, match="autocommit"):
        hk.attempt(InTx(), _chunk(), "reclaim")


# --- unit: the Ops card -----------------------------------------------------------


def _building(comp, keeper=True, beat_age=10):
    beat = (datetime.now(timezone.utc) - timedelta(seconds=beat_age)).isoformat()
    return {"compression": comp, "history_keeper": comp if keeper else None,
            "heartbeats": {hk.SERVICE: beat} if keeper else {}}


def _comp(**kw):
    return {"chunks": {"total": 7, "compressed": 2, "partial": 0, "hot": 5}, "compressed_on_disk_bytes": 600_000_000,
            "compressed_stats_bytes": 565_000_000, "on_disk_vs_stats": 1.06, "failing": [], "policy": {}, **kw}


def test_the_card_shows_a_failing_chunk_with_its_real_error():
    f = {"chunk": "_timescaledb_internal._hyper_1_9_chunk", "action": "compress", "attempts": 2,
         "at": "2026-11-09T12:56:00+00:00", "next_try_at": "2026-11-09T13:06:00+00:00", "sqlstate": "55P03",
         "message": "canceling statement due to lock timeout", "detail": None}
    state, detail = ops_status.compression_state(_building(_comp(failing=[f])))
    assert state == "degraded"
    assert "_hyper_1_9_chunk" in detail and "55P03" in detail and "lock timeout" in detail and "next try" in detail


def test_the_card_compares_true_disk_with_the_chunk_stats():
    state, detail = ops_status.compression_state(_building(_comp()))
    assert state == "ok" and "0.60 GB on disk" in detail and "0.56 GB" in detail
    bloated = _comp(compressed_on_disk_bytes=12_000_000_000, on_disk_vs_stats=21.2,
                    chunks={"total": 7, "compressed": 2, "partial": 1, "hot": 5})
    state, detail = ops_status.compression_state(_building(bloated))
    assert state == "degraded" and "12.00 GB on disk" in detail and "21.2x" in detail


def test_the_card_without_a_keeper():
    assert ops_status.compression_state({"compression": {}})[0] == "off"
    assert ops_status.compression_state(_building(_comp(), keeper=False))[0] == "degraded"
    assert ops_status.compression_state(_building(_comp(), beat_age=3600))[0] == "down"


# --- integration -----------------------------------------------------------------

PREFIX = "urn:point:test-keeper/"
POINTS, STEP = 200, 10


def _span():
    # 413 days back: no other test writes there, so its chunk is this test's
    start = (datetime.now(timezone.utc) - timedelta(days=413)).replace(minute=0, second=0, microsecond=0)
    return start, start + HOUR


@pytest.fixture
def conn():
    from test_history_compression import (
        _hold_compression_policy,
        _release_compression_policy,
    )

    c = timeseries.connect()
    if not c.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')").fetchone()[0]:
        pytest.skip("needs TimescaleDB")
    _hold_compression_policy(c)
    start, end = _span()
    try:
        yield c
    finally:
        try:
            c.execute("DELETE FROM point_history WHERE point_uri LIKE %s", (PREFIX + "%",))
            c.execute("SELECT drop_chunks('point_history', older_than => %s, newer_than => %s)",
                      (end + timedelta(days=8), start - timedelta(days=8)))
            c.execute("DELETE FROM history_compression_events WHERE chunk = ANY(%s)", (_SEEDED,))
            _SEEDED.clear()
        finally:
            _release_compression_policy(c)
            c.close()


_SEEDED: list[str] = []  # chunks this test's events are about


def _seed(conn):
    start, end = _span()
    rows = [(f"{PREFIX}p{p}", 20.0 + p + (i % 7) * 0.1, start + timedelta(seconds=STEP * i))
            for p in range(POINTS) for i in range(int(3600 / STEP))]
    timeseries.write_point_values(conn, rows)
    (name,) = conn.execute(
        "SELECT format('%%I.%%I', chunk_schema, chunk_name) FROM timescaledb_information.chunks "
        "WHERE hypertable_name = 'point_history' AND range_start <= %s AND range_end > %s", (start, start)).fetchone()
    conn.execute("DELETE FROM history_compression_events WHERE chunk = %s", (name,))  # a run that died before cleanup
    _SEEDED.append(name)
    return name, start, end


def _keeper_conn(lock_timeout="1s"):
    c = timeseries.connect()
    c.execute(f"SET lock_timeout = '{lock_timeout}'")
    return c


def _state(conn, name):
    return next(c for c in hk.chunks(conn) if c.name == name)


@pytest.mark.integration
def test_a_forced_failure_records_its_real_cause_and_a_retry_compresses_the_chunk(conn):
    name, start, end = _seed(conn)
    kc = _keeper_conn()
    keeper = hk.Keeper(kc, settle_s=0, max_defer_s=0)
    holder = psycopg.connect(timeseries.DEFAULT_DSN)
    try:
        # What the policy says about the same failure: only its count. Its
        # cause is a WARNING (a notice to this session; the server log for
        # the background job), which is why the keeper records it itself.
        holder.execute(f"LOCK TABLE {name} IN ACCESS EXCLUSIVE MODE")
        out = keeper.run_pass(only={name}, respect_pause=False)
        [done] = out["done"]
        assert (done["action"], done["ok"], done["sqlstate"]) == ("compress", False, "55P03")
        assert "lock timeout" in done["message"]
        row = conn.execute("SELECT action, ok, sqlstate, message FROM history_compression_events "
                           "WHERE chunk = %s ORDER BY at DESC LIMIT 1", (name,)).fetchone()
        assert row[:3] == ("compress", False, "55P03") and "lock timeout" in row[3]
        [failing] = [f for f in hk.compression_status(conn)["failing"] if f["chunk"] == name]
        assert failing["attempts"] == 1 and "lock timeout" in failing["message"]
        state, detail = ops_status.compression_state(
            {"compression": hk.compression_status(conn), "history_keeper": {},
             "heartbeats": {hk.SERVICE: datetime.now(timezone.utc).isoformat()}})
        assert state == "degraded" and name in detail and "lock timeout" in detail

        # backing off: an immediate pass leaves it alone
        assert keeper.run_pass(only={name}, respect_pause=False)["done"] == []
    finally:
        holder.rollback()
        holder.close()
    later = datetime.now(timezone.utc) + timedelta(seconds=hk.RETRY_BASE_S + 1)
    out = keeper.run_pass(now=later, only={name}, respect_pause=False)
    [done] = out["done"]
    assert (done["action"], done["ok"]) == ("compress", True)
    assert _state(conn, name).compressed
    assert name not in {f["chunk"] for f in hk.compression_status(conn)["failing"]}
    n = conn.execute("SELECT count(*) FROM point_history WHERE point_uri LIKE %s AND ts >= %s AND ts < %s",
                     (PREFIX + "%", start, end)).fetchone()[0]
    assert n == POINTS * 3600 / STEP
    kc.close()


@pytest.mark.integration
def test_the_policy_hides_the_cause_that_the_keeper_records(conn):
    """The nash-srv message, reproduced: compress_chunk's own error reaches
    only a WARNING, the run fails with "Failed to convert '1' chunks"."""
    name, _, _ = _seed(conn)
    job = conn.execute("SELECT job_id FROM timescaledb_information.jobs WHERE proc_name = 'policy_compression' "
                       "AND hypertable_name = 'point_history'").fetchone()[0]
    holder = psycopg.connect(timeseries.DEFAULT_DSN)
    notices: list = []
    runner = psycopg.connect(timeseries.DEFAULT_DSN, autocommit=True)
    runner.add_notice_handler(lambda d: notices.append((d.severity, d.message_primary, d.message_detail)))
    try:
        holder.execute(f"LOCK TABLE {name} IN ACCESS EXCLUSIVE MODE")
        runner.execute("SET lock_timeout = '1s'")
        # the policy compresses every chunk past compress_after: only ours is 400 days old
        config = runner.execute("SELECT config FROM timescaledb_information.jobs WHERE job_id = %s", (job,)).fetchone()[0]
        runner.execute("SELECT alter_job(%s, config => jsonb_set(%s, '{compress_after}', '\"400 days\"'))",
                       (job, psycopg.types.json.Jsonb(config)))
        with pytest.raises(psycopg.Error) as exc:
            runner.execute("CALL run_job(%s)", (job,))
    finally:
        runner.execute("SELECT alter_job(%s, config => %s)", (job, psycopg.types.json.Jsonb(config)))
        holder.rollback()
        holder.close()
        runner.close()
    assert exc.value.diag.message_primary == "columnstore policy failure"
    assert "Failed to convert '1' chunks" in exc.value.diag.message_detail
    [warning] = [n for n in notices if n[0] == "WARNING" and name in n[1]]
    assert "lock timeout" in warning[2]  # the real cause, only here


@pytest.mark.integration
def test_failed_policy_runs_are_copied_from_job_errors(conn):
    kc = _keeper_conn()
    keeper = hk.Keeper(kc)
    pol = hk.policy(kc)
    marker = f"columnstore policy failure test-keeper {uuid.uuid4().hex[:6]}"
    at = datetime.now(timezone.utc) + timedelta(days=1)  # newer than any copied so far
    conn.execute(
        "INSERT INTO _timescaledb_internal.bgw_job_stat_history (job_id, pid, execution_start, execution_finish, "
        "succeeded, data) VALUES (%s, 1, %s, %s, false, %s)",
        (pol.job_id, at, at, psycopg.types.json.Jsonb(
            {"error_data": {"message": marker, "detail": "Failed to convert '1' chunks to columnstore.",
                            "sqlerrcode": "22000"}})))
    try:
        rows = keeper.policy_errors(pol)
        assert [(r[1], marker in r[2]) for r in rows] == [("22000", True)]
        assert keeper.policy_errors(pol) == []  # copied once
    finally:
        conn.execute("DELETE FROM _timescaledb_internal.bgw_job_stat_history WHERE data->'error_data'->>'message' = %s",
                     (marker,))
        conn.execute("DELETE FROM history_compression_events WHERE message = %s", (marker,))
        kc.close()


@pytest.mark.integration
def test_late_rows_are_recompressed_once_settled_and_the_disk_comes_back(conn):
    name, start, end = _seed(conn)
    conn.execute("SELECT compress_chunk(%s::regclass)", (name,))
    compressed = _state(conn, name)
    assert compressed.compressed and not compressed.partial and compressed.stats_bytes

    # A late round, as nash-srv's soak sent it: each point's reading again
    # (a spool replay) and one new reading between two (trend backfill).
    t = start + timedelta(minutes=30)
    late = [(f"{PREFIX}p{p}", 999.25, t) for p in range(POINTS)]
    late += [(f"{PREFIX}p{p}", 998.75, t + timedelta(seconds=1.234567)) for p in range(POINTS)]
    timeseries.write_point_values(conn, late)
    inflated = _state(conn, name)
    assert inflated.partial
    assert inflated.disk_bytes > 2 * compressed.stats_bytes  # each late row decompressed its point's batch

    # the chunk's indexes before the keeper touches it: however many the
    # schema gives point_history, a reindex must leave exactly these, all valid
    # (a failed REINDEX CONCURRENTLY would leave an extra invalid *_ccnew one)
    indexes_before = conn.execute("SELECT count(*) FROM pg_index WHERE indrelid = %s::regclass", (name,)).fetchone()[0]
    assert indexes_before >= 1

    kc = _keeper_conn("5s")
    keeper = hk.Keeper(kc, settle_s=600, max_defer_s=3600)
    now = datetime.now(timezone.utc)
    assert keeper.run_pass(now=now, only={name}, respect_pause=False)["done"] == []  # settling
    out = keeper.run_pass(now=now + timedelta(seconds=601), only={name}, respect_pause=False)
    [done] = out["done"]
    assert (done["action"], done["ok"]) == ("recompress", True)
    after = _state(conn, name)
    assert after.compressed and not after.partial
    assert after.disk_bytes <= 2 * after.stats_bytes, (after.disk_bytes, after.stats_bytes)
    assert done["disk_before"] == inflated.disk_bytes and done["disk_after"] == after.disk_bytes
    assert done["reindex"] == "concurrently"  # never blocks late writes into the chunk
    assert conn.execute("SELECT message FROM history_compression_events WHERE chunk = %s AND action = 'recompress' "
                        "ORDER BY at DESC LIMIT 1", (name,)).fetchone()[0] == "reindex: concurrently"
    assert conn.execute("SELECT bool_and(x.indisvalid), count(*) FROM pg_index x WHERE x.indrelid = %s::regclass",
                        (name,)).fetchone() == (True, indexes_before)
    # nothing lost or doubled: the late values won, the backfill is in
    rows = conn.execute("SELECT count(*), count(*) FILTER (WHERE value = 999.25), count(*) FILTER (WHERE value = 998.75) "
                        "FROM point_history WHERE point_uri LIKE %s AND ts >= %s AND ts < %s",
                        (PREFIX + "%", start, end)).fetchone()
    assert rows == (POINTS * 3600 / STEP + POINTS, POINTS, POINTS)
    status = hk.compression_status(conn)
    assert status["compressed_on_disk_bytes"] >= after.disk_bytes and status["compressed_stats_bytes"]
    kc.close()


def test_a_chunk_is_not_reclaimed_again_until_it_grows():
    mb = 1024 * 1024
    fat = _chunk("fat", status=hk.COMPRESSED, disk=200 * mb, stats=12 * mb)
    assert _plan([fat], reclaimed={"fat": 190 * mb}) == []  # what the last reclaim couldn't free is real
    assert _plan([fat], reclaimed={"fat": 100 * mb}) == [("fat", "reclaim")]

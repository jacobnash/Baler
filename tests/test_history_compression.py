"""point_history's native compression (#192).

The setup must be idempotent and in place (the live install upgrades its
existing database), keep its policy in step with BALER_HISTORY_COMPRESS_AFTER,
and never fail a connection. Once a chunk is compressed, late writes,
retained-message replays, overwrites and deletes into it must still work,
and every history read must return exactly what it returned before.

Migration 0001 enables it (#194); timeseries keeps the policy in step with
the setting. The unit tests drive _ensure_compression with a fake
connection; the integration tests need docker-compose's TimescaleDB."""

import logging
import math
import time
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from baler import ddl, schema, timeseries
from baler.commissioning.history import _VALUED_SQL
from baler.migrations import v0001_baseline as baseline

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


def test_disabled_compression_is_left_alone(monkeypatch, caplog):
    """Enabling it is migration 0001's job (#194); the settings sync only
    keeps the policy in step and never turns compression back on."""
    with caplog.at_level(logging.WARNING, logger="baler.timeseries"):
        assert _ran(monkeypatch, (False, False, False)) == ""
    assert "disabled" in caplog.text


def test_missing_policy_is_added(monkeypatch):
    ran = _ran(monkeypatch, (True, False, False))
    assert "lock_timeout" in ran and "ALTER TABLE" not in ran
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
        _ran(monkeypatch, (True, True, False), fail=psycopg.errors.LockNotAvailable("busy"))
    assert "retried at the next process start" in caplog.text


def test_bad_interval_is_logged_not_raised(monkeypatch, caplog):
    monkeypatch.setattr(ddl, "run", lambda *a: pytest.fail("no DDL with a bad interval"))
    with caplog.at_level(logging.ERROR, logger="baler.timeseries"):
        timeseries._ensure_compression(_FakeConn(None, error=psycopg.errors.InvalidDatetimeFormat("bad")))
    assert "BALER_HISTORY_COMPRESS_AFTER" in caplog.text


# --- integration ------------------------------------------------------------


_POLICIES = ("SELECT job_id FROM timescaledb_information.jobs "
             "WHERE proc_name = 'policy_compression' AND hypertable_name = 'point_history'")


def _hold_compression_policy(c, timeout: float = 60.0) -> None:
    """Pause point_history's compression policy and wait out any run of it
    in progress, so no background job compresses or recompresses the chunks
    a test is reading and writing.

    test_compress_after_follows_the_setting replaces the policy, and
    TimescaleDB runs a new policy straight away, over the 60-day-old chunks
    these tests use, so a test's chunks could change state (uncompressed,
    partial, compressed) under its reads: in CI (PR #281)
    test_probes_give_the_same_rows_across_the_compressed_boundary's plain
    reference query, `ORDER BY ts ASC LIMIT 1`, missed every old row of a
    point while the probe found them. Locally, back-to-back runs of this
    file failed every time once the policy had compressed the chunks
    ("retrieved too many tuples in a bounded sort", now turned off for
    every Baler connection: timeseries._PLANNER_WORKAROUNDS). Each test
    compresses what it needs itself (_compress)."""
    c.execute(f"SELECT alter_job(job_id, scheduled => false) FROM ({_POLICIES}) j")
    deadline = time.monotonic() + timeout
    while True:
        running = c.execute(
            f"SELECT (SELECT count(*) FROM timescaledb_information.job_stats "
            f"        WHERE job_id IN ({_POLICIES}) AND job_status = 'Running') "
            "     + (SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE 'Compression Policy%%')"
        ).fetchone()[0]
        if not running:
            return
        if time.monotonic() > deadline:
            pytest.fail(f"point_history's compression policy still running after {timeout:.0f} s")
        time.sleep(0.2)


def _release_compression_policy(c) -> None:
    c.execute(f"SELECT alter_job(job_id, scheduled => true) FROM ({_POLICIES}) j")


@pytest.fixture
def conn():
    c = timeseries.connect()
    if not c.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')").fetchone()[0]:
        pytest.skip("needs TimescaleDB")
    _hold_compression_policy(c)
    c.execute("DELETE FROM point_history WHERE point_uri LIKE %s", (PREFIX + "%",))
    try:
        yield c
    finally:
        try:
            c.execute("DELETE FROM point_history WHERE point_uri LIKE %s", (PREFIX + "%",))
        finally:
            _release_compression_policy(c)
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
    monkeypatch.setattr(schema, "upgrade", lambda *a, **k: pytest.fail("migrated although current"))
    assert schema.plan(conn) == []
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
    """Compress every chunk overlapping [start, end); returns how many are compressed.

    The stack's own compression policy can be compressing the same chunk at
    the same moment: test_compress_after_follows_the_setting replaces the
    policy, and TimescaleDB runs a new policy straight away, over these
    60-day-old chunks. compress_chunk checks `if_not_compressed` before it
    takes the chunk's lock, so losing that race raises "already compressed"
    (CI, PR #272) though the chunk ends up exactly as wanted. Then compress
    again: every chunk the job finished is skipped, and the caller still
    asserts all of them are compressed."""
    sql = ("SELECT compress_chunk(c, if_not_compressed => true) "
           "FROM show_chunks('point_history', newer_than => %s, older_than => %s) c")
    span = (start - timedelta(days=8), end + timedelta(days=8))
    for attempt in range(5):
        try:
            conn.execute(sql, span)
            break
        except psycopg.errors.DuplicateObject:
            if attempt == 4:
                raise
            time.sleep(0.5)
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
        "availability": sorted(conn.execute(_VALUED_SQL, {"uris": uris, "start": start, "end": end, "gap": 60.0}).fetchall()),
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


@pytest.mark.integration
def test_batched_writes_into_compressed_chunks(conn):
    """mqtt_listener's and hisWrite's batched path (write_point_values: COPY
    into the staging table, one upsert) lands in compressed chunks like the
    single-row path does: an overwrite wins, new rows go in, quality (#183)
    rides along, and the connection still commits durably (#240)."""
    start, end = _old_span()
    _seed(conn, start, 6)
    uris = [f"{PREFIX}p{p}" for p in range(3)]
    assert _compress(conn, start, end)[0] > 0
    assert conn.execute("SHOW synchronous_commit").fetchone()[0] == "on"
    n0 = conn.execute("SELECT count(*) FROM point_history WHERE point_uri LIKE %s", (PREFIX + "%",)).fetchone()[0]

    t = start + timedelta(hours=1)  # an existing (point, ts): replay overwrites
    late = [start + timedelta(hours=2, seconds=30 * i + 7) for i in range(200)]  # new, between existing
    rows = [(uris[0], 999.0, t, None, None, "stale")]
    rows += [(uris[1], 500.0 + i, ts, None, None, "ok" if i % 2 else "bad") for i, ts in enumerate(late)]
    assert timeseries.write_point_values(conn, rows) == 1 + len(late)

    n1 = conn.execute("SELECT count(*) FROM point_history WHERE point_uri LIKE %s", (PREFIX + "%",)).fetchone()[0]
    assert n1 == n0 + len(late)
    assert timeseries.read_range(conn, uris[0], t, t) == [(t, 999.0)]
    assert conn.execute("SELECT quality FROM point_history WHERE point_uri = %s AND ts = %s",
                        (uris[0], t)).fetchone()[0] == "stale"
    got = dict(timeseries.read_range(conn, uris[1], late[0], late[-1]))
    assert all(got[ts] == 500.0 + i for i, ts in enumerate(late))
    bad = conn.execute("SELECT count(*) FROM point_history WHERE point_uri = %s AND ts = ANY(%s) AND quality = 'bad'",
                       (uris[1], late)).fetchone()[0]
    assert bad == len(late) // 2


# --- #237's per-point probes and empty-rows index, on compressed chunks ------


def _reference(conn, uris, newest, extra="", params=()):
    """The probe's answer the slow, obvious way."""
    order = "DESC" if newest else "ASC"
    out = {}
    for u in uris:
        row = conn.execute(f"SELECT ts, value, value_text, value_bool FROM point_history WHERE point_uri = %s {extra} "
                           f"ORDER BY ts {order} LIMIT 1", (u, *params)).fetchone()
        if row:
            out[u] = row
    return out


@pytest.mark.integration
def test_probes_give_the_same_rows_across_the_compressed_boundary(conn):
    start, end = _old_span()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old_only, both, recent_only, ghost = (f"{PREFIX}{k}" for k in ("old-only", "both", "recent-only", "ghost"))
    rows = [(old_only, start + timedelta(minutes=i), float(i)) for i in range(30)]
    rows += [(both, start + timedelta(minutes=i), float(i)) for i in range(30)]
    rows += [(both, now - timedelta(minutes=i), 100.0 + i) for i in range(5)]
    rows += [(recent_only, now - timedelta(minutes=i), 200.0 + i) for i in range(5)]
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO point_history (point_uri, ts, value) VALUES (%s, %s, %s)", rows)
    conn.execute("INSERT INTO point_history (point_uri, ts) VALUES (%s, %s)", (both, now + timedelta(seconds=1)))  # empty row
    uris = [old_only, both, recent_only, ghost]

    def check():
        latest = timeseries.read_latest_many(conn, uris)
        assert latest == {u: (r[0], timeseries._row_to_value(r[1:])) for u, r in _reference(conn, uris, True).items()}
        valued = timeseries.read_latest_many(conn, uris, with_value=True)
        assert valued == {u: (r[0], timeseries._row_to_value(r[1:])) for u, r in _reference(
            conn, uris, True, "AND (value IS NOT NULL OR value_text IS NOT NULL OR value_bool IS NOT NULL)").items()}
        asof = timeseries.read_latest_many(conn, uris, before=end, since=start)
        assert asof == {u: (r[0], timeseries._row_to_value(r[1:])) for u, r in _reference(
            conn, uris, True, "AND ts <= %s AND ts >= %s", (end, start)).items()}
        first = timeseries.probe_points(conn, uris, "ts", [], [], newest=False)
        assert {u: r[0] for u, r in first.items()} == {u: r[0] for u, r in _reference(conn, uris, False).items()}
        assert ghost not in latest and ghost not in first

    check()  # whatever the shared database's chunks are now
    _compress(conn, start, end)
    assert timeseries._compressed_below(conn) is not None
    check()


@pytest.mark.integration
def test_connections_turn_off_metadata_only_columnar_scans(conn):
    """timeseries._PLANNER_WORKAROUNDS: read_point_labels' top-N read over a
    metadata-only columnar scan fails with "retrieved too many tuples in a
    bounded sort" on compressed chunks (TimescaleDB 2.31)."""
    for name in timeseries._PLANNER_WORKAROUNDS:
        row = conn.execute("SELECT setting FROM pg_settings WHERE name = %s", (name,)).fetchone()
        if row is not None:  # a TimescaleDB without the feature has nothing to turn off
            assert row[0] == "off", name


def test_compressed_below_is_none_without_timescale_catalog():
    class Broken:
        def transaction(self):
            import contextlib
            return contextlib.nullcontext()

        def execute(self, *a):
            raise psycopg.errors.UndefinedTable("no view")

    assert timeseries._compressed_below(Broken()) is None


@pytest.mark.integration
def test_empty_rows_index_and_availability_on_compressed_chunks(conn):
    from baler.commissioning import history

    start, end = _old_span()
    uris = [f"{PREFIX}e{p}" for p in range(3)]
    rows = []
    for p, uri in enumerate(uris):
        for i in range(360):
            empty = p == 0 and i % 11 == 0  # one point with empty rows (Baler#85)
            rows.append((uri, start + timedelta(minutes=i), None if empty else float(i)))
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO point_history (point_uri, ts, value) VALUES (%s, %s, %s)", rows)
    _compress(conn, start, end)
    # the background build works with compressed chunks present, and the
    # report's index-only path gives what the heap path gives
    assert history.ensure_empty_rows_index(conn) in ("built", "present", "another process is building it")
    params = {"uris": uris, "start": start, "end": end, "gap": 90.0}
    got = history.PostgresHistory(conn).availability(uris, start, end, timedelta(seconds=90))
    heap = {u: (n, history.coverage_pct(n, float(cov or 0), f, start, end, timedelta(seconds=90)))
            for u, n, cov, f in conn.execute(history._VALUED_SQL, params).fetchall()}
    assert got == heap
    assert got[uris[0]][0] < got[uris[1]][0] == 360


# --- a column added beside compression (#183's quality) ----------------------


@pytest.mark.integration
@pytest.mark.parametrize("column_first", [True, False])
def test_add_nullable_column_beside_compression(conn, column_first):
    """#183 adds `quality TEXT` with ALTER TABLE ... ADD COLUMN IF NOT EXISTS.
    Either order of upgrade works, also once chunks are compressed, and the
    new column stays a compressed column, not a segment column."""
    t = "ph_compress_addcol"
    conn.execute(f"DROP TABLE IF EXISTS {t}")
    legacy = baseline.POINT_HISTORY.replace("point_history", t).replace("    quality    TEXT,\n", "")
    assert "quality" not in legacy  # a table from before #183
    conn.execute(legacy)
    conn.execute(f"SELECT create_hypertable('{t}', 'ts')")
    add = baseline.QUALITY_COLUMN.replace("point_history", t)
    enable = baseline.ENABLE_COMPRESSION.replace("point_history", t)
    try:
        start, _ = _old_span()
        with conn.cursor() as cur:
            cur.executemany(f"INSERT INTO {t} (point_uri, ts, value) VALUES (%s, %s, %s)",
                            [("p", start + timedelta(minutes=i), float(i)) for i in range(100)])
        if column_first:
            conn.execute(add)
            conn.execute(enable)
            conn.execute(f"SELECT compress_chunk(c) FROM show_chunks('{t}') c")
        else:
            conn.execute(enable)
            conn.execute(f"SELECT compress_chunk(c) FROM show_chunks('{t}') c")
            conn.execute(add)  # on already-compressed chunks
        conn.execute(add)  # idempotent
        # write and overwrite a flag in the compressed range
        conn.execute(f"INSERT INTO {t} (point_uri, ts, value, quality) VALUES ('p', %s, 1, 'fault') "
                     "ON CONFLICT (point_uri, ts) DO UPDATE SET value = EXCLUDED.value, quality = EXCLUDED.quality",
                     (start + timedelta(minutes=5),))
        conn.execute(f"INSERT INTO {t} (point_uri, ts, value, quality) VALUES ('p', %s, 2, 'spike')",
                     (start + timedelta(minutes=5, seconds=30),))
        conn.execute(f"SELECT compress_chunk(c) FROM show_chunks('{t}') c")
        flagged = conn.execute(f"SELECT ts, value, quality FROM {t} WHERE quality IS NOT NULL ORDER BY ts").fetchall()
        assert flagged == [(start + timedelta(minutes=5), 1.0, "fault"), (start + timedelta(minutes=5, seconds=30), 2.0, "spike")]
        assert conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 101
        seg = {a: s for a, s in conn.execute(
            "SELECT attname, segmentby_column_index FROM timescaledb_information.compression_settings "
            "WHERE hypertable_name = %s", (t,)).fetchall()}
        assert seg.get("quality") is None and seg["point_uri"] == 1
    finally:
        conn.execute(f"DROP TABLE IF EXISTS {t}")

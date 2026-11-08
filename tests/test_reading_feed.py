"""
The reading feed and the set-based His sweep against real Postgres
(Baler#243): batches arrive whole, in commit order and typed as stored;
a consumer resumes from its offset; a batch pruned before a consumer read
it is counted against that consumer, never silently lost; point_registry
follows every writer; and stale/stuck verdicts are the per-point ones.

Shares the stack with the running daemons (the live listener appends its
own batches meanwhile), so assertions look only at this test's points, and
the prune test runs in a transaction it rolls back - the stack's real
consumers must not be charged with misses.
"""

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from baler import reading_feed, timeseries
from baler.fault_detector import evaluate_his_rules
from baler.faults import ensure_schema, list_faults

pytestmark = pytest.mark.integration


@pytest.fixture
def conn():
    c = timeseries.connect()
    ensure_schema(c)
    return c


def _tag():
    return f"test-feed-{uuid.uuid4().hex[:8]}"


def _write_batch(conn, rows, alarms=None):
    timeseries.write_point_values(conn, rows, then=lambda c: reading_feed.append_from_stage(c, alarms))


def _drain(consumer, prefix):
    """Poll until the feed is empty past the offset; this test's readings."""
    mine, alarms = [], []
    for _ in range(50):
        batches = consumer.poll()
        if not batches:
            break
        for b in batches:
            mine += [r for r in b.readings if r[0].startswith(prefix)]
            alarms += [a for a in b.alarms if a[0].startswith(prefix[len("urn:point:"):])]
        consumer.commit(batches)
    return mine, alarms


def test_a_batch_reaches_a_consumer_whole_typed_and_in_ts_order(conn):
    tag = _tag()
    consumer = reading_feed.FeedConsumer(timeseries.connect(), f"test:{tag}")
    consumer.start()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    p = f"urn:point:fbf/{tag}"
    _write_batch(conn, [
        (f"{p}/b", 2.5, now + timedelta(seconds=2)),
        (f"{p}/a", True, now + timedelta(seconds=1), None, None, "fault"),
        (f"{p}/a", "active", now),
        (f"{p}/a", 9.0, now),  # same (point, ts): the last row wins, as in point_history
    ], alarms=[[f"fbf/{tag}/a/alarm", {"object": "ai,1", "to_state": "high-limit"}, 1.0]])
    readings, alarms = _drain(consumer, p)
    assert readings == [(f"{p}/a", now, 9.0, None), (f"{p}/a", now + timedelta(seconds=1), True, "fault"),
                        (f"{p}/b", now + timedelta(seconds=2), 2.5, None)]
    assert alarms == [[f"fbf/{tag}/a/alarm", {"object": "ai,1", "to_state": "high-limit"}, 1.0]]

    points = reading_feed.FeedConsumer(timeseries.connect(), f"test:{tag}:points", points_only=True)
    points.seq = consumer.seq - 1  # read the batch above again, points only
    batch = next(b for b in points.poll() if any(r[0].startswith(p) for r in b.readings))
    assert [r for r in batch.readings if r[0].startswith(p)] == [(f"{p}/a", None, None, None), (f"{p}/a", None, None, None),
                                                                 (f"{p}/b", None, None, None)]

    again = reading_feed.FeedConsumer(timeseries.connect(), f"test:{tag}")
    again.start()
    assert again.seq == consumer.seq  # resumes, nothing replayed
    assert reading_feed.consumer_status(conn)[f"test:{tag}"]["seq"] == consumer.seq


def test_a_rolled_back_batch_never_reaches_the_feed(conn):
    tag = _tag()
    consumer = reading_feed.FeedConsumer(timeseries.connect(), f"test:{tag}")
    consumer.start()

    def append_then_fail(c):
        reading_feed.append_from_stage(c)
        raise RuntimeError("commit never happens")

    with pytest.raises(RuntimeError):
        timeseries.write_point_values(conn, [(f"urn:point:fbf/{tag}/x", 1.0, datetime.now(timezone.utc))],
                                      then=append_then_fail)
    assert _drain(consumer, f"urn:point:fbf/{tag}") == ([], [])


def test_a_batch_pruned_unread_is_counted_against_that_consumer(conn):
    tag = _tag()
    lagging = reading_feed.FeedConsumer(timeseries.connect(), f"test:{tag}", log=lambda m: None)
    lagging.start()
    _write_batch(conn, [(f"urn:point:fbf/{tag}/{i}", float(i), datetime.now(timezone.utc)) for i in range(7)])
    with psycopg.connect(timeseries.DEFAULT_DSN) as tx:  # rolled back on exit (not autocommit, no commit)
        deleted, missed = reading_feed.prune(tx, retention_s=0)
        mine = {c: (r, b) for c, r, b in missed}
        assert deleted >= 1 and mine[f"test:{tag}"][0] >= 7
        row = tx.execute("SELECT missed_readings, seq > %s FROM reading_feed_consumers WHERE consumer = %s",
                         (lagging.seq, f"test:{tag}")).fetchone()
        assert row[0] >= 7 and row[1]  # charged, and moved past what's gone
        tx.rollback()


def test_prune_keeps_what_a_consumer_has_not_read(conn):
    tag = _tag()
    lagging = reading_feed.FeedConsumer(timeseries.connect(), f"test:{tag}")
    lagging.start()
    _write_batch(conn, [(f"urn:point:fbf/{tag}/x", 1.0, datetime.now(timezone.utc))])
    with psycopg.connect(timeseries.DEFAULT_DSN) as tx:
        reading_feed.prune(tx, retention_s=3600)
        assert tx.execute("SELECT count(*) FROM reading_feed WHERE seq > %s", (lagging.seq,)).fetchone()[0] >= 1
        tx.rollback()
    assert _drain(lagging, f"urn:point:fbf/{tag}")[0] != []


def test_every_writer_keeps_point_registry_current_and_coarse(conn):
    tag = _tag()
    a, b = f"urn:point:fbf/{tag}/a", f"urn:point:fbf/{tag}/b"
    t = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=10)
    timeseries.write_point_value(conn, a, 1.0, t)
    timeseries.write_point_values(conn, [(b, 1.0, t), (b, 2.0, t + timedelta(seconds=5))])

    def last(uri):
        return conn.execute("SELECT last_ts FROM point_registry WHERE point_uri = %s", (uri,)).fetchone()[0]

    assert last(a) == t and last(b) == t + timedelta(seconds=5)
    timeseries.write_point_values(conn, [(b, 3.0, t + timedelta(seconds=20))])
    assert last(b) == t + timedelta(seconds=5)  # within the granularity: no write
    timeseries.write_point_values(conn, [(b, 4.0, t + timedelta(seconds=60))])
    assert last(b) == t + timedelta(seconds=60)
    timeseries.write_point_value(conn, b, 5.0, t)  # older: never moves back
    assert last(b) == t + timedelta(seconds=60)


def _his(conn, rules, now):
    return evaluate_his_rules(conn, conn, rules, "/nonexistent-webhooks.json", now=now)


def _open(conn, rule_id):
    return {f["point_uri"] for f in list_faults(conn, rule_id=rule_id, status="open")}


def test_stale_is_exact_through_the_coarse_registry(conn):
    tag = _tag()
    now = datetime.now(timezone.utc)
    p = f"urn:point:fbf/{tag}/p"
    # registry says now-100 (the later sample is within its granularity)
    timeseries.write_point_values(conn, [(p, 1.0, now - timedelta(seconds=100))])
    timeseries.write_point_values(conn, [(p, 1.0, now - timedelta(seconds=80))])
    rules = [{"id": f"test-feed:{tag}:s90", "mode": "his", "type": "stale", "applies_to": {"topic_glob": f"fbf/{tag}/*"},
              "max_age_seconds": 90},
             {"id": f"test-feed:{tag}:s70", "mode": "his", "type": "stale", "applies_to": {"topic_glob": f"fbf/{tag}/*"},
              "max_age_seconds": 70}]
    _his(conn, rules, now)
    assert _open(conn, rules[0]["id"]) == set()  # newest is 80 s old, not 100
    assert _open(conn, rules[1]["id"]) == {p}
    timeseries.write_point_value(conn, p, 2.0, now)
    _his(conn, rules, now + timedelta(seconds=1))
    assert _open(conn, rules[1]["id"]) == set()  # reported again: resolved


@pytest.mark.parametrize("values, stuck", [
    ([("B", -200), ("A", -60), ("A", -30)], True),     # only A inside the window
    ([("A", -60), ("B", -30)], False),
    ([("A", -60), (None, -30)], True),                 # a null sample counts, but isn't a value
    ([(None, -60), (None, -30)], False),
    ([("A", -60), ("B!", -30)], True),                 # B! is flagged bad: ignored
    ([("A", -200)], False),                            # nothing in the window: silence, not stuck
])
def test_stuck_matches_the_per_point_verdict(conn, values, stuck):
    tag = _tag()
    now = datetime.now(timezone.utc)
    p = f"urn:point:fbf/{tag}/p"
    rows = [(p, (v.rstrip("!") if v else v), now + timedelta(seconds=s), None, None, "fault" if v and v.endswith("!") else None)
            for v, s in values]
    timeseries.write_point_values(conn, rows)
    rule = {"id": f"test-feed:{tag}:stuck", "mode": "his", "type": "stuck", "applies_to": {"topic_glob": f"fbf/{tag}/*"},
            "stuck_seconds": 120}
    _his(conn, [rule], now)
    assert _open(conn, rule["id"]) == ({p} if stuck else set())

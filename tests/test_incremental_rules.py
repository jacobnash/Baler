"""
The rule daemons' incremental path (Baler#243), without services:

- fault_detector reads committed batches from the reading feed: a batch
  gives the same transitions and webhooks, in the same order, as handling
  each reading's MQTT message did; Cur-mode rules are matched through a
  per-point index that follows rules.json and the Brick rosters;
- the listener puts each batch (readings and controller alarms) into the
  feed inside the batch's own transaction, acknowledging after;
- derivation_engine's Cur mode runs each triggered row once per batch from
  an index of the select's rows by input point, not a SPARQL query and a run
  per reading;
- falling behind or missing readings shows in /ops/status.
"""

import json
import os
import random
import weakref
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from baler import (
    derivation_engine,
    fault_detector,
    mqtt_listener,
    ops_status,
    reading_feed,
)
from baler.fault_detector import (
    OpenFaults,
    RuleCache,
    handle_cur_reading,
    handle_feed_batch,
)

T0 = datetime(2026, 11, 8, tzinfo=timezone.utc)


@pytest.fixture
def calls(monkeypatch):
    log = []
    monkeypatch.setattr(fault_detector, "open_fault", lambda conn, rid, uri, now, detail=None, severity="warning":
                        log.append(("open", rid, uri, detail["value"])) or 1)
    monkeypatch.setattr(fault_detector, "close_fault", lambda conn, rid, uri, now: log.append(("close", rid, uri)) or 1)
    monkeypatch.setattr(fault_detector, "_fire_webhooks", lambda path, event, rid, uri, status, detail:
                        log.append(("webhook", event, rid, uri)))
    return log


def _rules_file(tmp_path, rules):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(rules))
    return path


RULES = [
    {"id": "hot", "mode": "cur", "type": "range", "applies_to": {"topic_glob": "fbf/s*/temp"}, "min": 0.0, "max": 100.0},
    {"id": "any", "mode": "cur", "type": "range", "applies_to": {"topic_glob": "fbf/*"}, "min": -50.0, "max": 150.0},
    {"id": "his", "mode": "his", "type": "stale", "applies_to": {"topic_glob": "fbf/*"}, "max_age_seconds": 60},
]


def _batch(readings, alarms=(), seq=1):
    return reading_feed.Batch(seq, T0, [(f"urn:point:{t}", T0 + timedelta(seconds=i), v, q)
                                        for i, (t, v, q) in enumerate(readings)], list(alarms))


def test_a_feed_batch_gives_the_same_transitions_as_reading_by_reading(tmp_path, calls):
    """Random readings across points and rules: the batch path and the
    per-message path open, close and fire exactly the same, in order."""
    rng = random.Random(243)
    topics = ["fbf/s1/temp", "fbf/s2/temp", "fbf/x/flow", "other/s1/temp"]
    readings = [(rng.choice(topics), rng.choice([50.0, 120.0, 200.0, -80.0, "active", True]), None) for _ in range(400)]
    path = _rules_file(tmp_path, RULES)

    open_a = OpenFaults()
    for topic, value, _ in readings:
        handle_cur_reading(None, RuleCache(str(path)), "w.json", topic, value, open_a)
    by_message = list(calls)
    calls.clear()

    open_b = OpenFaults()
    handle_feed_batch(None, RuleCache(str(path)), "w.json", _batch(readings), open_b)
    assert calls == by_message
    assert any(c[0] == "open" for c in calls) and any(c[0] == "close" for c in calls)


def test_flagged_readings_neither_open_nor_resolve(tmp_path, calls):
    path = _rules_file(tmp_path, RULES[:1])
    open_faults = OpenFaults()
    stats = handle_feed_batch(None, RuleCache(str(path)), "w.json",
                              _batch([("fbf/s1/temp", 500.0, "fault"), ("fbf/s1/temp", 500.0, None),
                                      ("fbf/s1/temp", 50.0, "overridden")]), open_faults)
    assert calls == [("open", "hot", "urn:point:fbf/s1/temp", 500.0), ("webhook", "fault.opened", "hot", "urn:point:fbf/s1/temp")]
    assert open_faults.is_open("hot", "urn:point:fbf/s1/temp")
    assert stats == {"readings": 3, "evaluations": 1, "alarms": 0}


def test_feed_alarms_go_to_the_alarm_handler(tmp_path, calls, monkeypatch):
    handled = []
    monkeypatch.setattr(fault_detector.bacnet_alarms, "handle_alarm",
                        lambda conn, topic, event, ts, fire: handled.append((topic, event["to_state"], ts)))
    path = _rules_file(tmp_path, RULES)
    handle_feed_batch(None, RuleCache(str(path)), "w.json",
                      _batch([], alarms=[["fbf/s1/temp/alarm", {"object": "ai,1", "to_state": "high-limit"}, 5.0]]))
    assert handled == [("fbf/s1/temp/alarm", "high-limit", 5.0)] and calls == []


def test_the_point_index_follows_rules_json(tmp_path):
    path = _rules_file(tmp_path, RULES[:1])
    cache = RuleCache(str(path))
    cache.get()
    assert [r["id"] for r in cache.cur_rules_for("urn:point:fbf/s1/temp")] == ["hot"]
    assert cache.cur_rules_for("urn:point:fbf/x/flow") == ()
    path.write_text(json.dumps(RULES))
    os.utime(path, (1, 1))  # a different mtime, however fast the test runs
    cache.get()
    assert [r["id"] for r in cache.cur_rules_for("urn:point:fbf/x/flow")] == ["any"]
    assert [r["id"] for r in cache.cur_rules_for("urn:point:fbf/s1/temp")] == ["hot", "any"]


class RosterStore:
    def __init__(self):
        self.members = {"urn:point:a"}
        self.queries = 0

    def query(self, sparql):
        self.queries += 1
        return [SimpleNamespace(point=m) for m in self.members]


def test_brick_rosters_refresh_on_their_ttl(tmp_path, monkeypatch):
    """A point classified after the rules loaded is picked up within the
    roster TTL, not only when rules.json changes."""
    path = _rules_file(tmp_path, [{"id": "zt", "mode": "cur", "type": "range",
                                    "applies_to": {"brick_class": "Zone_Air_Temperature_Sensor"}, "min": 0, "max": 1}])
    store = RosterStore()
    clock = [1000.0]
    monkeypatch.setattr(fault_detector.time, "monotonic", lambda: clock[0])
    cache = RuleCache(str(path), store=store, roster_ttl=300)
    cache.get()
    assert cache.cur_rules_for("urn:point:b") == ()
    store.members.add("urn:point:b")
    clock[0] += 100
    cache.get()
    assert cache.cur_rules_for("urn:point:b") == () and store.queries == 1
    clock[0] += 250
    cache.get()
    assert [r["id"] for r in cache.cur_rules_for("urn:point:b")] == ["zt"] and store.queries == 2


def test_glob_prefixes_narrow_his_queries():
    assert fault_detector._glob_prefix("fbf/scale/s*/x") == "fbf/scale/s"
    assert fault_detector._glob_prefix("fbf/[ab]/x") == "fbf/"
    assert fault_detector._glob_prefix("*") == ""
    assert fault_detector._prefix_filter(["urn:point:a", "urn:point:b", "urn:point:a"]) == (
        " AND (starts_with(point_uri, %s) OR starts_with(point_uri, %s))", ["urn:point:a", "urn:point:b"])
    assert fault_detector._prefix_filter(["urn:point:a", ""]) == ("", [])


# --- the listener writes the feed --------------------------------------------


class AckClient:
    def __init__(self):
        self.acks = []

    def ack(self, mid, qos):
        self.acks.append((mid, qos))


class Msg:
    def __init__(self, topic, value, mid):
        self.topic, self.mid, self.qos = topic, mid, 1
        self.payload = json.dumps({"value": value, "ts": 10.0}).encode()


def test_the_listener_feeds_readings_and_alarms_in_the_batch_transaction(monkeypatch):
    log = []

    def ingest(store, conn, rows, then=None):
        log.append(("write", [r[0] for r in rows]))
        then("conn")  # inside write_point_values' transaction

    monkeypatch.setattr(mqtt_listener, "ingest_readings", ingest)
    monkeypatch.setattr(mqtt_listener.reading_feed, "append_from_stage",
                        lambda conn, alarms, tags=None: log.append(("feed", alarms)))
    client = AckClient()
    batcher = mqtt_listener.ReadingBatcher(client, store=None, ts_conn=None, feed=True)
    on_message = mqtt_listener.make_on_message(None, None, batcher)
    alarm = {"object": "analog-input,1", "to_state": "high-limit"}
    on_message(client, None, Msg("fbf/s/ahu/zt", 71.0, 1))
    on_message(client, None, Msg("fbf/s/ahu/zt/alarm", alarm, 2))
    assert client.acks == []  # the alarm waits for the batch too
    batcher.flush([batcher.queue.get_nowait() for _ in range(2)])
    assert log == [("write", ["fbf/s/ahu/zt"]), ("feed", [["fbf/s/ahu/zt/alarm", alarm, 10.0]])]
    assert client.acks == [(1, 1), (2, 1)]


def test_without_the_feed_alarms_are_acked_inline_and_not_queued(monkeypatch):
    client = AckClient()
    batcher = mqtt_listener.ReadingBatcher(client, store=None, ts_conn=None, feed=False)
    on_message = mqtt_listener.make_on_message(None, None, batcher)
    on_message(client, None, Msg("fbf/s/ahu/zt/alarm", {"object": "ai,1", "to_state": "normal"}, 3))
    assert batcher.queue.empty() and client.acks == [(3, 1)]


def test_a_prune_failure_is_logged_not_fatal(monkeypatch, capsys):
    def boom(conn, retention):
        raise RuntimeError("feed table locked")

    monkeypatch.setattr(mqtt_listener.reading_feed, "prune", boom)
    mqtt_listener.ReadingBatcher(AckClient(), store=None, ts_conn=None, feed=True).prune()
    assert "prune failed" in capsys.readouterr().out


def test_a_prune_miss_is_logged_with_counts(monkeypatch, capsys):
    monkeypatch.setattr(mqtt_listener.reading_feed, "prune", lambda conn, retention: (3, [["fault_detector", 15000, 3]]))
    mqtt_listener.ReadingBatcher(AckClient(), store=None, ts_conn=None, feed=True, feed_retention_s=600).prune()
    assert "fault_detector MISSED 15000 reading(s) in 3 batch(es)" in capsys.readouterr().out


# --- derivation_engine's Cur mode --------------------------------------------


class SelectStore:
    def __init__(self, rows):
        self.rows, self.queries = rows, 0

    def query(self, sparql):
        self.queries += 1
        return self.rows


def _row(target, a, b):
    return SimpleNamespace(target=target, a=a, b=b)


DERIVATION = {"id": "d1", "kind": "formula", "select": "SELECT ...", "target_var": "target", "input_vars": ["a", "b"],
              "applies_to_topic_glob": "fbf/*", "fn_source": "def run(inputs, row):\n    return 1", "window_seconds": 60}


def test_cur_mode_runs_each_triggered_row_once_per_batch(monkeypatch):
    store = SelectStore([_row("urn:t1", "urn:point:fbf/a1", "urn:point:fbf/b1"),
                         _row("urn:t2", "urn:point:fbf/a2", "urn:point:fbf/b2"),
                         _row("urn:t3", "urn:point:fbf/a1", "urn:point:fbf/b3")])
    ran = []
    monkeypatch.setattr(derivation_engine, "_evaluate_formula",
                        lambda store, ts, fc, hc, d, now, dry_run, rows=None: ran.append([r.target for r in rows]))
    monkeypatch.setattr(derivation_engine, "_Snapshot", lambda fc, hc: None)
    targets = derivation_engine.CurTargets(ttl=60)
    # both inputs of t1 and one of t3 reported in this batch; t2's didn't
    n = derivation_engine.evaluate_cur_points(store, None, None, None, [DERIVATION],
                                             {"urn:point:fbf/a1", "urn:point:fbf/b1", "urn:point:other/x"}, targets)
    assert ran == [["urn:t1", "urn:t3"]] and n == 2
    derivation_engine.evaluate_cur_points(store, None, None, None, [DERIVATION], {"urn:point:fbf/b2"}, targets)
    assert ran[-1] == ["urn:t2"]
    assert store.queries == 1  # the select ran once, not per reading or per batch


def test_cur_targets_rerun_the_select_when_it_changes_or_expires(monkeypatch):
    store = SelectStore([_row("urn:t1", "urn:point:fbf/a1", "urn:point:fbf/b1")])
    clock = [0.0]
    monkeypatch.setattr(derivation_engine.time, "monotonic", lambda: clock[0])
    targets = derivation_engine.CurTargets(ttl=60)
    targets.rows_for(store, DERIVATION, {"urn:point:fbf/a1"})
    targets.rows_for(store, {**DERIVATION, "select": "SELECT other"}, {"urn:point:fbf/a1"})
    assert store.queries == 2
    clock[0] = 61
    targets.rows_for(store, {**DERIVATION, "select": "SELECT other"}, {"urn:point:fbf/a1"})
    assert store.queries == 3


def test_rollups_and_globless_derivations_have_no_cur_mode():
    assert derivation_engine.cur_derivations([DERIVATION, {**DERIVATION, "id": "r", "kind": "rollup"},
                                               {**DERIVATION, "id": "n", "applies_to_topic_glob": None}]) == [DERIVATION]


class CountingStore:
    def __init__(self):
        self.writes = 0

    def add_entity(self, *a):
        self.writes += 1

    def add_derived_from(self, *a):
        self.writes += 1


def test_output_rows_are_written_in_one_transaction_per_derivation(monkeypatch):
    """#273: _batched_writes collects a derivation's rows for one
    write_point_values call, instead of a commit per row."""
    monkeypatch.setattr(derivation_engine, "link_point_to_equip", lambda store, p, e: None)
    single, batches = [], []
    monkeypatch.setattr(derivation_engine.timeseries, "write_point_value", lambda *a, **k: single.append(a))
    monkeypatch.setattr(derivation_engine.timeseries, "write_point_values", lambda conn, rows: batches.append(list(rows)))
    store = CountingStore()
    with derivation_engine._batched_writes("conn"):
        with derivation_engine._batched_writes("conn"):  # nested: the outer batch
            derivation_engine._write_and_attach(store, None, "urn:point:computed/d/t1", "urn:t1", 1.0, T0,
                                                {"unit": "°F"}, ["urn:point:a"])
        derivation_engine._write_and_attach(store, None, "urn:point:computed/d/t2", "urn:t2", 2.0, T0, {}, ["urn:point:a"])
        assert batches == []  # nothing written until the derivation is done
    assert not single
    assert batches == [[("urn:point:computed/d/t1", 1.0, T0, "°F", None), ("urn:point:computed/d/t2", 2.0, T0, None, None)]]


class HealthConn:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append(sql.split()[0])
        return SimpleNamespace(fetchone=lambda: (1,))


def test_a_success_is_recorded_once_until_the_target_fails():
    conn = HealthConn()
    for _ in range(3):
        derivation_engine._record_success_once(conn, "d", "urn:t")
    assert len(conn.statements) == 1  # the row exists (list_targets), then nothing to change
    derivation_engine._record_target_failure(conn, "d", "urn:t", False, ValueError("x"), SimpleNamespace(
        set_attribute=lambda *a: None))
    derivation_engine._record_success_once(conn, "d", "urn:t")
    assert len(conn.statements) == 3  # the failure, then the success that resets it


def test_cur_mode_derivations_are_swept_on_the_backstop_interval_not_every_tick():
    sweep = derivation_engine.sweep_interval
    assert sweep(DERIVATION, 5.0, 300.0) == 300.0
    assert sweep({**DERIVATION, "applies_to_topic_glob": None}, 5.0, 300.0) == 5.0
    assert sweep({**DERIVATION, "interval_seconds": 60}, 5.0, 300.0) == 60.0


def test_tick_stats_running_totals():
    t = derivation_engine.TickStats()
    t.record(1.0, 10, 60.0, 0.0)
    out = t.record(2.0, 20, 60.0, 1.5)
    assert out == {"last_s": 2.0, "last_rows": 20, "interval_s": 60.0, "late_s": 1.5, "passes": 2, "rows": 30,
                   "busy_s": 3.0}


def test_lineage_is_written_once_per_process(monkeypatch):
    monkeypatch.setattr(derivation_engine, "link_point_to_equip", lambda store, p, e: None)
    monkeypatch.setattr(derivation_engine.timeseries, "write_point_value", lambda *a, **k: None)
    store = CountingStore()
    for _ in range(3):
        derivation_engine._write_and_attach(store, None, "urn:point:computed/d/t", "urn:t", 1.0, T0, {}, ["urn:point:a"])
    assert store.writes == 2  # one entity, one derivedFrom: not 6
    derivation_engine._write_and_attach(store, None, "urn:point:computed/d/t", "urn:t", 1.0, T0, {}, ["urn:point:b"])
    assert store.writes == 4  # new lineage is written


# --- visible in /ops/status --------------------------------------------------


def _iso(seconds_ago=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _building(**feed):
    return {"heartbeats": {"fault_detector": _iso(), "derivation_engine": _iso()}, "open_faults": 0,
            "reading_feed": {"fault_detector": {"lag_s": 0.4, "lag_readings": 10, "missed_readings": 0,
                                                "last_missed_at": None, **feed}}}


def test_ops_says_ok_while_a_rule_daemon_keeps_up():
    assert ops_status._infer_faults(_building(), platform_ok=True) == ("ok", "running; 0 open fault(s)")


def test_ops_degrades_a_rule_daemon_that_falls_behind():
    state, detail = ops_status._infer_faults(_building(lag_s=95.0, lag_readings=190000), platform_ok=True)
    assert state == "degraded" and "95 s (190000 readings)" in detail


def test_ops_degrades_a_rule_daemon_that_missed_readings_lately():
    state, detail = ops_status._infer_faults(_building(missed_readings=4200, last_missed_at=_iso(120)), platform_ok=True)
    assert state == "degraded" and "missed 4200 reading(s)" in detail
    old = _building(missed_readings=4200, last_missed_at=_iso(7200))
    assert ops_status._infer_faults(old, platform_ok=True)[0] == "ok"
    assert ops_status._infer_derivations(_building(), platform_ok=True)[0] == "ok"  # no feed row of its own yet


def test_feed_run_together_hands_a_whole_poll_at_once():
    """#273: derivation_engine's Cur mode handles a poll's batches together."""
    import threading

    from baler import reading_feed

    stop = threading.Event()

    class Fake(reading_feed.FeedConsumer):
        def __init__(self):
            super().__init__(None, "fake", max_batches=3)
            self.polls = [[reading_feed.Batch(i, T0, [(f"urn:point:p{i}", None, None, None)]) for i in (1, 2, 3)], []]
            self.committed = []

        def poll(self):
            if not self.polls:
                stop.set()
                return []
            return self.polls.pop(0)

        def commit(self, batches):
            self.committed.extend(b.seq for b in batches)

    seen = []
    c = Fake()
    c.run(lambda batches: seen.append([b.seq for b in batches]), stop=stop, idle_s=0, together=True)
    assert seen == [[1, 2, 3]] and c.committed == [1, 2, 3]  # one call per poll, nothing for an empty poll


# --- bounded memory (#336) ---------------------------------------------------


class _Series(list):
    """A list that can be weakly referenced: alive while the engine holds it."""


def _bulk_reader(monkeypatch, per_point):
    """read_range_bulk stand-in: `per_point` readings for every point, and
    a log of how many points each call read and how many readings from
    earlier calls the engine still held when it ran."""
    refs, calls = [], []

    def read(conn, uris, start, end):
        calls.append({"points": len(uris), "still_held": sum(len(s) for s in (r() for r in refs) if s is not None)})
        out = {u: _Series((start + timedelta(seconds=i), float(i)) for i in range(per_point)) for u in uris}
        refs.extend(weakref.ref(s) for s in out.values())
        return out

    monkeypatch.setattr(derivation_engine.timeseries, "read_range_bulk", read)
    return calls


class LineageStore(SelectStore):
    def add_entity(self, *a):
        pass

    def add_derived_from(self, *a):
        pass


def test_a_pass_larger_than_the_read_budget_runs_in_bounded_chunks(monkeypatch):
    """#336: a pass whose rows' input windows hold far more readings than
    BALER_DERIVATION_READ_BUDGET (here 300 rows x 2 inputs x 50 readings =
    30,000 against a budget of 1,000) reads them a chunk at a time, each
    chunk about the budget, the previous chunk freed before the next is
    read, and still computes every row from its own inputs."""
    monkeypatch.setattr(derivation_engine, "READ_BUDGET", 1_000)
    monkeypatch.setattr(derivation_engine, "_Snapshot", lambda fc, hc: None)
    monkeypatch.setattr(derivation_engine, "link_point_to_equip", lambda store, p, e: None)
    calls = _bulk_reader(monkeypatch, per_point=50)
    written = []
    monkeypatch.setattr(derivation_engine.timeseries, "write_point_values", lambda conn, rows: written.extend(rows))
    rows = [_row(f"urn:t{i}", f"urn:point:fbf/a{i}", f"urn:point:fbf/b{i}") for i in range(300)]
    fn = "def run(inputs, row):\n    return len(inputs['a']) + len(inputs['b'])"
    trace = derivation_engine.evaluate_derivations(LineageStore(rows), "ts", None, None,
                                                   [{**DERIVATION, "applies_to_topic_glob": None, "fn_source": fn}])
    assert len(trace) == 300 and all(t["computed_value"] == 100 for t in trace)
    assert len(written) == 300
    assert len(calls) > 1
    # each read: at most the budget (2 inputs x 50 readings a row, 10 rows),
    # never all 300 rows' 30,000 readings at once
    assert max(c["points"] for c in calls) * 50 <= 1_000
    assert sum(c["points"] for c in calls) == 600
    # what earlier chunks read is gone when the next chunk reads
    assert all(c["still_held"] == 0 for c in calls)


def test_cur_mode_triggered_rows_are_read_in_bounded_chunks_too(monkeypatch):
    """The same bound for a Cur-mode poll that triggers every row."""
    monkeypatch.setattr(derivation_engine, "READ_BUDGET", 500)
    monkeypatch.setattr(derivation_engine, "_Snapshot", lambda fc, hc: None)
    monkeypatch.setattr(derivation_engine, "link_point_to_equip", lambda store, p, e: None)
    calls = _bulk_reader(monkeypatch, per_point=25)
    monkeypatch.setattr(derivation_engine.timeseries, "write_point_values", lambda conn, rows: None)
    rows = [_row(f"urn:t{i}", f"urn:point:fbf/a{i}", f"urn:point:fbf/b{i}") for i in range(200)]
    points = {p for r in rows for p in (str(r.a), str(r.b))}
    n = derivation_engine.evaluate_cur_points(LineageStore(rows), "ts", None, None, [DERIVATION], points,
                                             derivation_engine.CurTargets())
    assert n == 200
    assert len(calls) > 1 and max(c["points"] for c in calls) * 25 <= 500


def test_output_writes_are_flushed_every_write_batch_rows(monkeypatch):
    """#336: _batched_writes holds at most BALER_DERIVATION_WRITE_BATCH
    output rows, however many rows the derivation has."""
    monkeypatch.setattr(derivation_engine, "WRITE_BATCH", 3)
    monkeypatch.setattr(derivation_engine, "link_point_to_equip", lambda store, p, e: None)
    batches = []
    monkeypatch.setattr(derivation_engine.timeseries, "write_point_values", lambda conn, rows: batches.append(len(rows)))
    with derivation_engine._batched_writes("conn"):
        for i in range(8):
            derivation_engine._write_and_attach(CountingStore(), None, f"urn:point:computed/d/t{i}", f"urn:t{i}", 1.0,
                                                T0, {}, ["urn:point:a"])
    assert batches == [3, 3, 2]


def test_budget_env_vars_fall_back_on_junk(monkeypatch, capsys):
    monkeypatch.setenv("BALER_DERIVATION_READ_BUDGET", "lots")
    assert derivation_engine._env_int("BALER_DERIVATION_READ_BUDGET", 7) == 7
    assert "not a whole number" in capsys.readouterr().out
    monkeypatch.setenv("BALER_DERIVATION_READ_BUDGET", "")
    assert derivation_engine._env_int("BALER_DERIVATION_READ_BUDGET", 7) == 7
    monkeypatch.setenv("BALER_DERIVATION_READ_BUDGET", "50000")
    assert derivation_engine._env_int("BALER_DERIVATION_READ_BUDGET", 7) == 50000


def test_ops_degrades_a_derivation_engine_in_a_restart_loop(monkeypatch):
    """#336: OOM-killed and restarted every couple of minutes, the engine
    beats between kills, so only its start count shows the loop. Same
    threshold as the alerter's restarts alert (#277)."""
    monkeypatch.setenv("BALER_ALERT_RESTART_COUNT", "3")
    monkeypatch.setenv("BALER_ALERT_RESTART_WINDOW_SECONDS", "900")
    b = {**_building(), "recent_starts": {"baler-derivation-engine": 2}}
    assert ops_status._infer_derivations(b, platform_ok=True)[0] == "ok"
    b["recent_starts"]["baler-derivation-engine"] = 7
    state, detail = ops_status._infer_derivations(b, platform_ok=True)
    assert state == "degraded" and "restart loop: started 7 times in the last 15 min" in detail
    monkeypatch.setenv("BALER_ALERT_RESTART_COUNT", "0")  # the alerter's "off"
    assert ops_status._infer_derivations(b, platform_ok=True)[0] == "ok"


def test_recent_start_counts_only_count_the_window(monkeypatch):
    monkeypatch.setenv("BALER_ALERT_RESTART_WINDOW_SECONDS", "900")
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(ops_status.errors, "recent_starts", lambda conn: {
        "baler-derivation-engine": [now - timedelta(seconds=s) for s in (10, 200, 400, 2000)]})
    assert ops_status._recent_start_counts(None) == {"baler-derivation-engine": 3}

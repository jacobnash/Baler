"""The Haxall baseline target (Baler#236): the pure parts of
scripts/scale/haxall.py and haxall_bridge.py. The runs themselves need a
Haxall image and run on nash-srv (docs-site scale-test.mdx)."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "scale"))

import common
import haxall
import haxall_bridge as hb


def test_topic_ref_is_the_provisioned_id():
    assert hb.topic_ref("fbf/scale/s00000/e00001/zone-temp") == "scale.s00000.e00001.zone-temp"
    assert hb.topic_ref("fbf/site/analogValue,1") == "site.analogValue_1"  # not a ref character
    assert hb.topic_ref(common.CANARY_TOPIC) == "scale.canary.lag"


def test_values_and_timestamps_encode_as_haystack_json():
    assert hb.encode_value(71.5) == "n:71.5" and hb.encode_value(3) == "n:3.0"
    assert hb.encode_value(True) is True
    assert hb.encode_value("active") is None and hb.encode_value(None) is None
    assert hb.encode_ts(0.5) == "t:1970-01-01T00:00:00.500000Z UTC"


def test_batch_grid_has_a_column_per_point_and_a_row_per_reading():
    g = hb.batch_grid([("a", 1.0, "n:1.0"), ("b", 1.5, "n:2.0"), ("a", 6.0, "n:3.0")])
    assert g["cols"] == [{"name": "ts"}, {"name": "v0", "id": "r:a"}, {"name": "v1", "id": "r:b"}]
    assert [sorted(r) for r in g["rows"]] == [["ts", "v0"], ["ts", "v1"], ["ts", "v0"]]
    per = hb.point_grids([("a", 6.0, "n:3.0"), ("b", 1.5, "n:2.0"), ("a", 1.0, "n:1.0")])
    assert per["a"]["meta"]["id"] == "r:a" and [r["val"] for r in per["a"]["rows"]] == ["n:1.0", "n:3.0"]


class FakeHaxall:
    def __init__(self, batch_ok=True, bad=()):
        self.batch_ok, self.bad, self.posts = batch_ok, set(bad), []

    def _post(self, op, grid):
        self.posts.append(grid)
        ids = [c["id"][2:] for c in grid["cols"] if "id" in c] or [grid["meta"]["id"][2:]]
        multi = any("id" in c for c in grid["cols"])
        if (multi and not self.batch_ok) or self.bad & set(ids):
            return {"meta": {"err": "m:", "dis": "s:nope"}}
        return {"meta": {}}


def test_auto_mode_falls_back_to_one_hiswrite_per_point_when_batches_are_refused():
    hx = FakeHaxall(batch_ok=False)
    w = hb.Writer(hx, "auto")
    assert w.write([("a", 1.0, "n:1.0"), ("b", 1.0, "n:2.0")]) == 0
    assert w.mode == "per-point" and w.batch_refused
    n = len(hx.posts)
    w.write([("a", 2.0, "n:1.0"), ("b", 2.0, "n:2.0")])
    assert len(hx.posts) - n == 2  # straight to per point now


def test_a_bad_id_is_isolated_and_counted_not_fatal():
    hx = FakeHaxall(batch_ok=True, bad={"b"})
    w = hb.Writer(hx, "auto")
    assert w.write([("a", 1.0, "n:1.0"), ("b", 1.0, "n:2.0"), ("b", 2.0, "n:2.0")]) == 2
    assert w.mode == "auto"  # batches do work; one id was bad


class AckClient:
    def __init__(self):
        self.acks = []

    def ack(self, mid, qos):
        self.acks.append(mid)


def _msg(topic, payload, mid):
    return SimpleNamespace(topic=topic, payload=json.dumps(payload).encode() if not isinstance(payload, bytes) else payload,
                           mid=mid, qos=1)


def test_the_bridge_acks_only_after_the_hiswrite_returns():
    hx, client = FakeHaxall(), AckClient()
    bridge = hb.Bridge(client, hb.Writer(hx, "batch"))
    bridge.on_message(client, None, _msg("fbf/scale/s00000/e00000/sat", {"point": "sat", "value": 70.5, "ts": 10.0}, 1))
    bridge.on_message(client, None, _msg("fbf/scale/s00000/e00000/sat/tags", {"value": "{}"}, 2))  # not history
    bridge.on_message(client, None, _msg("fbf/scale/s00000/e00000/mode", {"value": "auto", "ts": 1.0}, 3))  # a string
    bridge.on_message(client, None, _msg("fbf/scale/x", b"not json", 4))
    assert client.acks == [2, 3, 4] and hx.posts == []
    bridge.flush([bridge.queue.get_nowait()])
    assert client.acks == [2, 3, 4, 1]
    assert hx.posts[0]["cols"][1]["id"] == "r:scale.s00000.e00000.sat"
    st = bridge.stats()
    assert st["written"] == 1 and st["skipped"] == 2 and st["rejected"] == 0  # tags aren't history, not counted


def test_an_unreachable_haxall_acks_nothing():
    class Down:
        def _post(self, op, grid):
            raise ConnectionError("refused")

    client = AckClient()
    bridge = hb.Bridge(client, hb.Writer(Down(), "batch"))
    bridge.on_message(client, None, _msg("fbf/scale/a/b", {"value": 1, "ts": 1.0}, 9))
    try:
        bridge.flush([bridge.queue.get_nowait()])
    except ConnectionError:
        pass
    assert client.acks == []


def test_records_are_added_once_across_steps():
    first = haxall.records(common.Shape(1000), 0)
    second = haxall.records(common.Shape(2500), 1000)
    ids = [r["id"] for r in first + second]
    assert len(ids) == len(set(ids))
    points = [r for r in first + second if r.get("point")]
    assert len(points) == 2500
    assert sum(1 for r in first + second if r.get("equip")) == 250
    assert sum(1 for r in first + second if r.get("site")) == 5
    p0 = next(r for r in first if r.get("point"))
    assert p0["id"] == "@scale.s00000.e00000.sat" and p0["his"] and p0["equipRef"] == "@scale.s00000.e00000"
    assert p0["scaleHis1"] and p0["kind"] == "Number" and p0["tz"] == "UTC"
    # a step that starts mid-equip adds the rest of its points, not the equip again
    mid = haxall.records(common.Shape(15), 5)
    assert [r["id"] for r in mid if r.get("equip")] == ["@scale.s00000.e00001"]
    assert sum(1 for r in mid if r.get("point")) == 10


def test_axon_literals_and_commits():
    assert haxall.axon_dict({"id": "@a.b", "dis": 'x "y" $z', "point": True, "n": 2, "no": False}) == \
        '{id:@a.b, dis:"x \\"y\\" \\$z", point, n:2}'
    exprs = haxall.commit_exprs([{"id": f"@p{i}", "point": True} for i in range(5)], chunk=2)
    assert len(exprs) == 3 and exprs[0] == "[diff(null, {id:@p0, point}, {add}), diff(null, {id:@p1, point}, {add})].commit"


def test_haxall_gets_balers_storage_and_read_path_caps():
    caps = haxall.haxall_caps({}, nproc=64)
    # postgres 2 + oxigraph 1 + haystack_api 1 + ingest_api 1; 6g + 4g + 2g + 2g
    assert caps["SCALE_CPUS_HAXALL"] == "5" and caps["SCALE_MEM_HAXALL"] == "14g"
    assert caps["SCALE_CPUS_HAXALL_BRIDGE"] == "1" and caps["SCALE_MEM_HAXALL_BRIDGE"] == "1g"
    assert haxall.haxall_caps({"SCALE_CPUS_POSTGRES": "4", "SCALE_MEM_POSTGRES": "512m"}, 64)["SCALE_MEM_HAXALL"] == "8.5g"
    assert haxall.haxall_caps({"SCALE_CPUS_HAXALL": "3"}, 64)["SCALE_CPUS_HAXALL"] == "3"
    assert haxall.haxall_caps({}, nproc=2)["SCALE_CPUS_HAXALL"] == "2"  # clamped to the host


def test_bridge_stats_from_its_log():
    log = ("noise\n"
           'haxall_bridge-1  | BRIDGE_STATS {"written_per_s": 0, "cpu_pct": 1, "queue": 0, "rejected": 0}\n'
           'haxall_bridge-1  | BRIDGE_STATS {"written_per_s": 2000, "cpu_pct": 40, "queue": 900, "last_batch": 500, '
           '"last_write_s": 0.2, "write_busy_pct": 70, "rejected": 0, "skipped": 0, "mode": "batch"}\n'
           "BRIDGE_STATS {broken\n")
    stats = haxall.parse_bridge_stats(log)
    assert len(stats) == 2
    s = haxall.summarize_bridge(stats)
    assert s["written_per_s_mean"] == 2000 and s["queue_max"] == 900 and s["mode"] == "batch"
    assert haxall.summarize_bridge([]) == {}


def test_stored_count_expression_covers_the_window_only():
    e = haxall.stored_expr(100.0, 200.5)
    assert "fromJavaMillis(100000, \"UTC\")..fromJavaMillis(200500, \"UTC\")" in e
    assert "r->ts < fromJavaMillis(200500" in e and e.endswith(".fold(sum)")


def test_side_by_side_rows_pair_steps_both_runs_have():
    def probe(name, s, status=200):
        return {"name": name, "median_s": s, "status": status}

    baler = {"steps": [{"points": 10000, "ingest": {"stored_rps": 2000.0}, "lag": {"p95_s": 1.2},
                        "probes": [probe("read?filter=point", 0.4), probe("hisRead 1000 points (today)", 1.1)],
                        "resources": {"postgres": {"mem_last": "900MiB"}, "oxigraph": {"mem_last": "300MiB"}},
                        "db": {"db_bytes": 2_000_000_000}},
                       {"points": 25000}]}
    hx = {"steps": [{"points": 10000, "ingest": {"stored_rps": 1999.0}, "lag": {"p95_s": 3.0},
                     "probes": [probe("read?filter=point", 0.2), probe("hisRead 1000 points (today)", 9.0, "boom")],
                     "resources": {"haxall": {"mem_last": "3GiB"}}, "disk_bytes": 1_500_000_000}]}
    rows = haxall.side_by_side(baler, hx)
    assert rows == [("| 10,000 | 2000.0 | 1999.0 | 1.2 s | 3.0 s | 0.4 s | 0.2 s | 1.1 s | error | "
                     "900MiB+300MiB / 2.0 GB | 3GiB / 1.5 GB |")]


def test_the_ladder_takes_a_target(monkeypatch):
    import ladder

    seen = {}
    monkeypatch.setattr(ladder, "cmd_run", lambda args: seen.update(vars(args)))
    ladder.main(["run", "--target", "haxall", "--steps", "1k", "--no-up"])
    assert seen["target"] == "haxall"
    monkeypatch.setattr(ladder, "cmd_crash", lambda args: seen.update(kill=args.kill))
    ladder.main(["crash", "--target", "haxall"])
    assert seen["kill"] == ladder.CRASH_KILL_DEFAULT  # cmd_crash swaps in haxall,haxall_bridge for this target

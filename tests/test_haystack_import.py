"""haystack_import: Haystack export files -> Baler, via the puller's path."""

import pytest

oh = pytest.importorskip("openhay")

from rdflib import RDF

from baler import haystack_import as hi
from baler.ingest import haystack_ref_to_equip_uri, topic_to_point_uri
from baler.store import BRICK, HAYSTACK, TD, Store

RECORDS = '''ver:"3.0"
id,dis,site,equip,ahu,point,sensor,discharge,air,temp,sp,writable,his,kind,unit,tz,siteRef,equipRef,curVal
@s1 "HQ","HQ",M,,,,,,,,,,,,,"New_York",,,
@e1 "AHU-1","AHU-1",,M,M,,,,,,,,,,,,@s1,,
@p1 "AHU-1 DAT","AHU-1 DAT",,,,M,M,M,M,M,,,M,"Number","°F","New_York",@s1,@e1,55.2°F
@p2 "AHU-1 DAT SP","AHU-1 DAT SP",,,,M,,M,M,M,M,M,,"Number","°F","New_York",@s1,@e1,
,"orphan row without an id",,,,M,,,,,,,,,,,,,
'''

HIS = '''ver:"3.0" id:@p1 hisStart:2024-01-02T00:00:00-05:00 New_York hisEnd:2024-01-03T00:00:00-05:00 New_York
ts,val
2024-01-02T10:00:00-05:00 New_York,54.5°F
2024-01-02T11:00:00-05:00 New_York,56°F
'''


class FakeConn:
    def __init__(self):
        self.rows = []

    def execute(self, sql, params=None):
        if "INSERT INTO point_history" in sql:
            self.rows.append(params)
        return self


@pytest.fixture
def store():
    return Store()


def test_records_land_through_the_puller_path(store, tmp_path):
    f = tmp_path / "records.zinc"
    f.write_text(RECORDS)
    out = hi.import_file(store, None, str(f), "skyspark-hq")
    assert "2 equip/site, 2 points" in out and "1 rows without an id skipped" in out

    g = store.graph
    equip = haystack_ref_to_equip_uri("skyspark-hq:e1")
    dat = topic_to_point_uri("haystack/skyspark-hq/p1")
    sp = topic_to_point_uri("haystack/skyspark-hq/p2")
    assert (equip, HAYSTACK.hasTag, None) in g
    assert (dat, BRICK.isPointOf, equip) in g and (sp, BRICK.isPointOf, equip) in g
    tags = {str(o) for o in g.objects(dat, HAYSTACK.hasTag)}
    assert {"point", "sensor", "temp", "his"} <= tags
    assert str(g.value(dat, HAYSTACK.unit)) == "°F"
    # classified past a bare raw point
    assert any(t != TD.RawPoint for t in g.objects(dat, RDF.type))


def test_history_grid_writes_samples_with_original_timestamps(store, tmp_path):
    (tmp_path / "records.zinc").write_text(RECORDS)
    (tmp_path / "his-p1.zinc").write_text(HIS)
    conn = FakeConn()
    hi.import_file(store, conn, str(tmp_path / "records.zinc"), "skyspark-hq")
    out = hi.import_file(store, conn, str(tmp_path / "his-p1.zinc"), "skyspark-hq")
    assert out.endswith("2 samples")
    uris = {r[0] for r in conn.rows}
    assert uris == {str(topic_to_point_uri("haystack/skyspark-hq/p1"))}
    assert sorted(r[2] for r in conn.rows) == [54.5, 56.0]
    assert sorted(r[1].hour for r in conn.rows) == [15, 16]  # 10:00/11:00 New York = 15:00/16:00 UTC


def test_reimport_is_idempotent(store, tmp_path):
    f = tmp_path / "records.zinc"
    f.write_text(RECORDS)
    hi.import_file(store, None, str(f), "src")
    before = len(store.graph)
    hi.import_file(store, None, str(f), "src")
    assert len(store.graph) == before


def test_json3_and_trio_exports_too(store, tmp_path):
    grid = oh.decode_grid(RECORDS)
    (tmp_path / "r.json").write_text(oh.encode_grid(grid, "json3"))
    assert "2 points" in hi.import_file(store, None, str(tmp_path / "r.json"), "j3")
    (tmp_path / "r.trio").write_text(oh.encode_grid(grid, "trio"))
    assert "2 points" in hi.import_file(store, None, str(tmp_path / "r.trio"), "tr")


def test_history_without_db_is_a_clear_error(store, tmp_path):
    (tmp_path / "h.zinc").write_text(HIS)
    with pytest.raises(ValueError, match="history grid"):
        hi.import_file(store, None, str(tmp_path / "h.zinc"), "x")


def test_cli_imports_records_before_history(monkeypatch, tmp_path):
    (tmp_path / "a-his.zinc").write_text(HIS)
    (tmp_path / "b-records.zinc").write_text(RECORDS)
    seen = []
    monkeypatch.setattr(hi, "import_file", lambda s, c, path, src, fmt=None: seen.append(path) or path)
    import baler.remote_store
    import baler.timeseries

    monkeypatch.setattr(baler.remote_store, "RemoteStore", lambda: object())
    monkeypatch.setattr(baler.timeseries, "connect", lambda: object())
    assert hi.main([str(tmp_path / "a-his.zinc"), str(tmp_path / "b-records.zinc"), "--source-id", "x"]) == 0
    assert [p.split("/")[-1] for p in seen] == ["b-records.zinc", "a-his.zinc"]

"""Building export (#193), services-free: the Parquet writer's point-month
split and bounded row groups, Postgres CSV parsing (NULL vs '', NaN,
newlines, booleans), path escaping, and verify() catching damage. The
round trip through a real Postgres and Oxigraph is in
test_export_integration.py."""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from baler import export


def _pg_csv(rows) -> bytes:
    """What COPY (SELECT ts AT TIME ZONE 'UTC', ...) TO STDOUT CSV writes."""
    out = []
    for ts, v, vt, vb, unit, label, q in rows:
        def f(x):
            if x is None:
                return ""
            if isinstance(x, bool):
                return "t" if x else "f"
            if isinstance(x, str):
                return '"' + x.replace('"', '""') + '"'
            if isinstance(x, float) and math.isnan(x):
                return "NaN"
            return str(x)
        out.append(",".join([ts.strftime("%Y-%m-%d %H:%M:%S.%f"), f(v), f(vt), f(vb), f(unit), f(label), f(q)]))
    return ("\n".join(out) + "\n").encode()


def _chunks(data: bytes, size: int = 7):
    for i in range(0, len(data), size):
        yield data[i:i + size]


def test_csv_batches_keep_null_empty_nan_newlines_and_booleans():
    rows = [
        (datetime(2026, 1, 31, 23, 59, 59, 999999, tzinfo=UTC), 1.5, None, None, None, None, None),
        (datetime(2026, 2, 1, tzinfo=UTC), float("nan"), None, None, None, None, "spike"),
        (datetime(2026, 2, 2, tzinfo=UTC), None, 'a,"b"\nc', None, None, None, None),
        (datetime(2026, 2, 3, tzinfo=UTC), None, "", None, None, None, None),
        (datetime(2026, 2, 4, tzinfo=UTC), None, "NaN", True, "degF", "Supply", "ok"),
        (datetime(2026, 2, 5, tzinfo=UTC), None, None, False, None, None, None),
    ]
    got = pa.Table.from_batches(list(export.csv_batches(_chunks(_pg_csv(rows))))).to_pylist()
    assert got[0]["value"] == 1.5 and got[0]["ts"] == rows[0][0].replace(tzinfo=None)
    assert math.isnan(got[1]["value"]) and got[1]["quality"] == "spike"
    assert got[2]["value_text"] == 'a,"b"\nc'
    assert got[3]["value_text"] == "" and got[0]["value_text"] is None
    assert got[4]["value_text"] == "NaN" and got[4]["value_bool"] is True and got[4]["unit"] == "degF"
    assert got[5]["value_bool"] is False


def _batch(start_us: int, n: int, step_us: int):
    ts = pa.array([start_us + i * step_us for i in range(n)], pa.int64()).cast(pa.timestamp("us"))
    return pa.RecordBatch.from_arrays(
        [ts, pa.array([float(i) for i in range(n)]), pa.nulls(n, pa.string()), pa.nulls(n, pa.bool_()),
         pa.nulls(n, pa.string()), pa.nulls(n, pa.string()), pa.nulls(n, pa.string())],
        names=[f.name for f in export.arrow_schema()])


def test_write_point_splits_by_utc_month_with_bounded_row_groups(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "ROW_GROUP", 1000)
    start = int(datetime(2026, 1, 31, 23, 0, tzinfo=UTC).timestamp() * 1e6)
    minute = 60_000_000
    # 2 batches straddling Jan -> Feb -> (much later) Apr, with an empty one
    batches = [_batch(start, 3000, minute), _batch(start + 3000 * minute, 0, minute),
               _batch(int(datetime(2026, 4, 1, tzinfo=UTC).timestamp() * 1e6), 10, minute)]
    p = export.write_point(str(tmp_path), "urn:point:fbf/a/ahu-1/sat", batches)
    assert [f["month"] for f in p.files] == ["2026-01", "2026-02", "2026-04"]
    assert [f["rows"] for f in p.files] == [60, 2940, 10]
    assert p.rows == 3010
    assert p.files[0]["last"] == "2026-01-31T23:59:00Z" and p.files[1]["first"] == "2026-02-01T00:00:00Z"
    feb = pq.ParquetFile(tmp_path / "history/fbf/a/ahu-1/sat/2026-02.parquet")
    assert feb.metadata.num_rows == 2940
    assert max(feb.metadata.row_group(i).num_rows for i in range(feb.metadata.num_row_groups)) <= 1000
    assert feb.schema_arrow.equals(export.arrow_schema())
    assert feb.metadata.metadata[b"point_uri"] == b"urn:point:fbf/a/ahu-1/sat"
    t = feb.read()
    assert t.column("value").to_pylist() == [float(i) for i in range(60, 3000)]


@pytest.mark.parametrize("uri, expected", [
    ("urn:point:fbf/a/ahu-1/sat", "fbf/a/ahu-1/sat"),
    ("urn:point:fbf/a/../b", "fbf/a/%2E%2E/b"),
    ("urn:point:fbf//x", "fbf/%00/x"),
    ("urn:point:fbf/a b?#", "fbf/a b%3F%23"),
    ("urn:baler:computed:1", "_other/urn%3Abaler%3Acomputed%3A1"),
])
def test_point_dir_escapes(uri, expected):
    assert export.point_dir(uri) == expected


def _small_export(root: str) -> None:
    start = int(datetime(2026, 1, 31, 23, 0, tzinfo=UTC).timestamp() * 1e6)
    p = export.write_point(root, "urn:point:fbf/a/x", [_batch(start, 120, 60_000_000)])
    export._write_history_index(root, [p])
    os.makedirs(os.path.join(root, "records"))
    export.export_documents(root)
    manifest = {"format": export.FORMAT, "format_version": 1, "created_at": "2026-02-02T00:00:00Z",
                "scope": {"site": None}, "records": {}, "not_included": export.NOT_YET_IN_BALER,
                "history": {"points": 1, "rows": p.rows, "files": len(p.files), "first": "a", "last": "b",
                            "until": "2026-02-02T00:00:00+00:00", "since": None}}
    with open(os.path.join(root, "manifest.json"), "w") as f:
        json.dump(manifest, f)
    with open(os.path.join(root, "README.md"), "w") as f:
        f.write(export.readme(manifest))
    export.write_sha256sums(root)


def test_verify_passes_a_good_export_and_catches_damage(tmp_path):
    root = str(tmp_path / "e")
    _small_export(root)
    res = export.verify(root)
    assert res.ok, res.errors
    assert res.rows == 120
    sums = Path(root, "SHA256SUMS").read_text()
    assert "history/fbf/a/x/2026-01.parquet" in sums and "manifest.json" in sums and "README.md" in sums

    # a stray file, and a corrupted Parquet file
    Path(root, "extra.txt").write_text("x")
    with open(os.path.join(root, "history/fbf/a/x/2026-02.parquet"), "r+b") as f:
        f.seek(10)
        f.write(b"\0\0\0\0")
    errors = export.verify(root).errors
    assert "not in SHA256SUMS: extra.txt" in errors
    assert "sha256 mismatch: history/fbf/a/x/2026-02.parquet" in errors


def test_readme_explains_itself_without_baler(tmp_path):
    root = str(tmp_path / "e")
    _small_export(root)
    text = Path(root, "README.md").read_text()
    for needle in ("sha256sum -c SHA256SUMS", "read_parquet", "rdflib", "Not included", "Baler#197", "Baler#205"):
        assert needle in text


def test_import_refuses_a_newer_format(tmp_path):
    root = tmp_path / "e"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps({"format": export.FORMAT, "format_version": 99}))
    with pytest.raises(export.ExportError, match="newer"):
        export._read_manifest(str(root))


def test_export_refuses_an_existing_directory(tmp_path):
    with pytest.raises(export.ExportError, match="already exists"):
        export.export_building("postgresql://unused", "http://unused", str(tmp_path))


def test_csv_batches_of_no_rows_is_empty():
    assert list(export.csv_batches(iter([b"", b""]))) == []
    assert list(export.csv_batches(iter([]))) == []

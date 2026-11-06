"""
One-time import of a Haystack export (SkySpark, Haxall, nhaystack, or
anything else that writes standard Haystack files) into Baler. The "move
off SkySpark" path when you have files rather than a live server to pull
from.

    python -m baler.haystack_import --source-id skyspark-hq records.zinc his-*.zinc

Each file is Zinc, Haystack 3 JSON, Hayson or Trio (sniffed, or set with
--format) and is one of:

  - **records**: rows with ids, e.g. `readAll(site or equip or point)` saved
    with ioWriteZinc. Equipment and sites first, then points, through the
    exact path haystack_puller uses (same identities, same Brick
    classification), so an import and a later live pull of the same server
    land on the same points.
  - **history**: a `hisRead` grid (meta `id`, columns `ts`/`val`), written to
    that point's history. Import the records first so the point exists with
    its tags.

Idempotent: re-importing the same files rewrites the same tags and the
same (point, ts) samples.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path

import openhay as oh

from baler import mapping, tracing
from baler.ingest import (
    ingest_haystack_equip_tags,
    ingest_reading,
    ingest_tags,
    link_equip_ref,
)

tracer = tracing.get_tracer(__name__)

EQUIP_RULES_PATH = Path(__file__).resolve().parents[2] / "rules" / "haystack_equip_to_brick.yaml"


def plain(v):
    """openhay value -> the plain shape haystack_puller rows use: markers
    True, refs their id, numbers their value, times ISO strings."""
    if v is oh.MARKER:
        return True
    if v is oh.NA or v is oh.REMOVE or v is None:
        return None
    if isinstance(v, oh.Ref):
        return v.id
    if isinstance(v, oh.Number):
        return v.val
    if isinstance(v, (oh.Uri, oh.Symbol)):
        return v.val
    if isinstance(v, oh.XStr):
        return {"type": v.type, "val": v.val}
    if isinstance(v, oh.Coord):
        return {"lat": v.lat, "lng": v.lng}
    if isinstance(v, (datetime, date, dtime)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [plain(x) for x in v]
    return v


def _typed(v):
    """What ingest stores for a value: Numbers (with unit), Refs and Ref
    lists, Uris, dates and times stay typed (see ingest._tag_triples); the rest
    takes the plain shape the puller uses."""
    if isinstance(v, (oh.Number, oh.Ref, oh.Uri, date, dtime)):  # datetime is a date
        return v
    if isinstance(v, list) and v and all(isinstance(x, oh.Ref) for x in v):
        return v
    return plain(v)


def _namespaced(row: dict, source_id: str) -> dict:
    """Every Ref (and Ref list) points at another record from the same
    source, so it gets the same source prefix the ids got - otherwise
    siteRef/spaceRef/custom refs would point at nothing."""
    def ns(r):
        return oh.Ref(f"{source_id}:{r.id}")
    out = {}
    for k, v in row.items():
        if isinstance(v, oh.Ref):
            v = ns(v)
        elif isinstance(v, list) and v and all(isinstance(x, oh.Ref) for x in v):
            v = [ns(x) for x in v]
        out[k] = v
    return out


def _row(rec: dict) -> dict:
    return {k: p for k, v in rec.items() if (p := _typed(v)) is not None}


def is_history(grid: oh.Grid) -> bool:
    return "id" in grid.meta and "ts" in grid.col_names


def import_records(store, rows: list[dict], source_id: str) -> dict[str, int]:
    """Equips/sites, then points - points' equipRef must resolve to an
    equip URI that already exists."""
    counts = {"equip": 0, "point": 0, "skipped": 0}
    equip_rules = mapping.load_rules(str(EQUIP_RULES_PATH))
    with tracer.start_as_current_span("haystack_import.records") as span:
        for rec in rows:
            if not isinstance(rec.get("id"), oh.Ref) or not ("equip" in rec or "site" in rec):
                continue
            uri = ingest_haystack_equip_tags(store, f"{source_id}:{rec['id'].id}", _namespaced(_row(rec), source_id))
            mapping.classify_point(store, uri, rules=equip_rules)
            counts["equip"] += 1
        for rec in rows:
            if not isinstance(rec.get("id"), oh.Ref):
                counts["skipped"] += 1
                continue
            if "point" not in rec:
                continue
            row = _namespaced(_row(rec), source_id)
            uri = ingest_tags(store, f"haystack/{source_id}/{rec['id'].id}", row)
            link_equip_ref(store, uri)
            mapping.classify_point_with_fallback(store, uri)
            counts["point"] += 1
        span.set_attribute("equip", counts["equip"])
        span.set_attribute("point", counts["point"])
    return counts


def import_history(store, ts_conn, grid: oh.Grid, source_id: str) -> int:
    ref = grid.meta["id"]
    ref_id = ref.id if isinstance(ref, oh.Ref) else str(ref).lstrip("@")
    topic = f"haystack/{source_id}/{ref_id}"
    n = 0
    with tracer.start_as_current_span("haystack_import.history") as span:
        for r in grid.rows:
            ts, val = r.get("ts"), plain(r.get("val"))
            if isinstance(ts, datetime) and val is not None:
                ingest_reading(store, ts_conn, topic, val, ts=ts.timestamp())
                n += 1
        span.set_attribute("rows", n)
    return n


def import_file(store, ts_conn, path: str, source_id: str, fmt: str | None = None) -> str:
    grid = oh.decode_grid(Path(path).read_text(encoding="utf-8"), fmt)
    if is_history(grid):
        if ts_conn is None:
            raise ValueError(f"{path} is a history grid; needs a database connection")
        return f"{path}: {import_history(store, ts_conn, grid, source_id)} samples"
    c = import_records(store, grid.rows, source_id)
    return f"{path}: {c['equip']} equip/site, {c['point']} points" + (f", {c['skipped']} rows without an id skipped" if c["skipped"] else "")


def main(argv=None) -> int:
    from baler.remote_store import RemoteStore
    from baler.timeseries import connect

    p = argparse.ArgumentParser(description="Import Haystack export files (records and hisRead grids) into Baler")
    p.add_argument("files", nargs="+")
    p.add_argument("--source-id", required=True, help="names the source; points become haystack/<source-id>/<id>")
    p.add_argument("--format", choices=["zinc", "json3", "hayson", "trio"], default=None, help="default: sniff")
    args = p.parse_args(argv)
    tracing.init_tracing("baler-haystack-import")
    store, conn = RemoteStore(), connect()
    # Records before history, whatever order the files were given in.
    order = sorted(args.files, key=lambda f: is_history(oh.decode_grid(Path(f).read_text(encoding="utf-8"), args.format)))
    for f in order:
        print(import_file(store, conn, f, args.source_id, args.format))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Building export (Baler#193): a building, or one site of a multi-site install,
as open files any tool can read without Baler. The migration exit, the
archive tier, and the backbone of the sale package
(todo/building-lifecycle-ubis.md §10.2).

    python -m baler.export export OUT_DIR [--site SITE_ID] [--since ...] [--until ...]
    python -m baler.export verify OUT_DIR [--against-source]
    python -m baler.export import OUT_DIR [--org-id ORG] [--site-id SITE]

What is written (README.md in the export says the same, for someone who
has never heard of Baler):

    README.md                   what this is and how to read it
    manifest.json               scope, counts, what isn't included and why
    SHA256SUMS                  every file's sha256 (`sha256sum -c SHA256SUMS`)
    history/schema.json         the Parquet columns and what they mean
    history/points.csv          one row per point: its directory, rows, span
    history/files.csv           one row per Parquet file: point, month, rows, sha256
    history/<point>/<YYYY-MM>.parquet   raw samples, one file per point-month (UTC)
    graph/building.ttl          the entity graph (Brick/Haystack/REC) as Turtle
    records/<table>.jsonl       Cx records and faults, one JSON object per row
    records/cx_issues.csv       the issues log, as a spreadsheet
    documents/manifest.csv      documents by sha256

Not a backup: scripts/backup.py dumps the whole database for disaster
recovery into the same software. This is the owner-readable form, per
building, in formats that will outlive it.

Memory stays bounded whatever the history's size: each point's samples
arrive by COPY in ~1 MB CSV blocks and leave in Parquet row groups of at
most ROW_GROUP rows, so only one point-month's open row group is held at a
time. Each point is read in its own statement, not in one long snapshot:
a multi-hour transaction would hold locks that make the compression policy
wait, with ingest queued behind it. History is cut at --until (default: the
moment the export started), so a later `verify --against-source` compares
like with like.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import logging
import os
import shutil
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from importlib import metadata
from urllib.parse import quote

import psycopg
import requests
from psycopg import sql

from baler import quality, scope
from baler.scope import Scope

log = logging.getLogger(__name__)

FORMAT = "baler-building-export"
FORMAT_VERSION = 1
# Rows per Parquet row group, and so the most rows held in memory at once.
ROW_GROUP = 128 * 1024
# CSV bytes per block read from COPY.
CSV_BLOCK = 1 << 20
# Standard Parquet encodings every current reader decodes (pyarrow, DuckDB,
# Spark, pandas): timestamps delta-encoded, float bytes split into streams
# before zstd, dictionaries only for the repetitive text columns. 2.7x
# smaller than zstd over plain/dictionary pages on a 1 s sine series.
PARQUET_OPTIONS: dict = {
    "compression": "zstd",
    "use_dictionary": ["value_text", "unit", "label", "quality"],
    "column_encoding": {"ts": "DELTA_BINARY_PACKED", "value": "BYTE_STREAM_SPLIT"},
}

HISTORY_COLUMNS: list[tuple[str, str, str]] = [
    ("ts", "timestamp[us, tz=UTC]", "When the sample was taken. Never null. Unique per point."),
    ("value", "double", "The value, when numeric (int or float sources)."),
    ("value_text", "string", "The value, when text (e.g. a BACnet state name like \"active\")."),
    ("value_bool", "bool", "The value, when boolean."),
    ("unit", "string", "The point's unit, when the source gave one (usually only computed points)."),
    ("label", "string", "A display label, when the source gave one (usually only computed points)."),
    ("quality", "string", "Null means ok. Otherwise one of: " + ", ".join(quality.FLAGS[1:]) + "."),
]
_PG_COLUMNS = "ts AT TIME ZONE 'UTC', value, value_text, value_bool, unit, label, quality"

# Cx records, in an order that satisfies their foreign keys on import. How
# each is narrowed to the exported projects: "project" (project_id), "run"
# (run_id of an exported cx_runs row), "issue" (issue_id of an exported
# cx_issues row). Tables that don't exist in this install are skipped, so
# tables added later (assets #197, packs #205) only need a line here.
CX_TABLES: list[tuple[str, str]] = [
    ("cx_projects", "self"),
    ("cx_phases", "project"),
    ("cx_events", "project"),
    ("cx_runs", "project"),
    ("cx_results", "run"),
    ("cx_issues", "project"),
    ("cx_issue_events", "issue"),
    ("cx_risks", "issue"),
    ("cx_test_procedures", "project"),
    ("cx_test_runs", "project"),
    ("cx_spec_documents", "project"),
    ("cx_spec_items", "project"),
    ("cx_mappings", "project"),
    ("cx_active_runs", "project"),
    ("cx_agent_sessions", "project"),
    ("cx_agent_proposals", "project"),
]
# Point-keyed records, narrowed by the site's prefixes (Scope.allows).
POINT_TABLES = ["faults"]
SERIAL_TABLES = ["cx_events", "cx_issue_events", "faults"]
ISSUE_CSV_COLUMNS = ["id", "project_id", "status", "severity", "title", "equip_uri", "check_id", "subject",
                     "assignee", "source", "source_id", "created_at", "updated_at", "resolved_at", "closed_at"]
DOCUMENT_COLUMNS = ["sha256", "filename", "media_type", "size", "document_id", "project_id", "created_at",
                    "content_included"]

# Lifecycle record parts the design names (§10.2) that this install has no
# tables for yet. Listed in the manifest so a reader knows they weren't
# dropped.
NOT_YET_IN_BALER = {
    "assets": "position/asset history is Baler#197, not built in this version",
    "packs": "requirement packs are Baler#205, not built in this version",
    "document_content": ("Baler keeps no document files yet (Cx 7 reads a spec and keeps only its sha256; "
                         "the document store is Baler#201): documents/manifest.csv lists each by sha256 so "
                         "your own copies can be matched and checked"),
}


class ExportError(SystemExit):
    """Exits non-zero with the message."""


def _pa():
    try:
        import pyarrow as pa
        import pyarrow.csv as pacsv
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - the extra is in every install that runs this
        raise ExportError("building export needs pyarrow: pip install -e \".[export]\"") from exc
    return pa, pacsv, pq


def arrow_schema():
    pa, _, _ = _pa()
    return pa.schema([
        ("ts", pa.timestamp("us", tz="UTC"), False),
        ("value", pa.float64()),
        ("value_text", pa.string()),
        ("value_bool", pa.bool_()),
        ("unit", pa.string()),
        ("label", pa.string()),
        ("quality", pa.string()),
    ])


def point_dir(point_uri: str) -> str:
    """history/ subdirectory for a point. `urn:point:fbf/a/ahu-1/sat` ->
    `fbf/a/ahu-1/sat` (each segment percent-encoded, so `..`, empty
    segments or odd characters can't escape or collide); anything else
    under `_other/`. points.csv maps each back to its exact URI."""
    key = scope.entity_key(point_uri) if point_uri.startswith("urn:point:") else None
    if key is None:
        return "_other/" + _segment(point_uri)
    return "/".join(_segment(s) for s in key.split("/"))


def _segment(s: str) -> str:
    q = quote(s, safe="-_.~ ")
    return {"": "%00", ".": "%2E", "..": "%2E%2E"}.get(q, q)


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _month_key(us: int) -> str:
    d = _EPOCH + timedelta(microseconds=us)
    return f"{d.year:04d}-{d.month:02d}"


def _month_start_us(year: int, month: int) -> int:
    return int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp() * 1_000_000)


def _next_month_start_us(key: str) -> int:
    y, m = int(key[:4]), int(key[5:])
    return _month_start_us(y + (m == 12), m % 12 + 1)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _iso_us(us: int) -> str:
    return (_EPOCH + timedelta(microseconds=us)).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- history

@dataclass
class PointFiles:
    point_uri: str
    directory: str
    rows: int = 0
    first_us: int | None = None
    last_us: int | None = None
    files: list[dict] = field(default_factory=list)


class _ReadableChunks:
    """A file-like .read() over an iterator of byte chunks, for pyarrow's
    streaming CSV reader."""

    def __init__(self, chunks: Iterator[bytes]):
        self._chunks = chunks
        self._buf = bytearray()
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        while n < 0 or len(self._buf) < n:
            try:
                self._buf += next(self._chunks)
            except StopIteration:
                break
        if n < 0:
            n = len(self._buf)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


def csv_batches(chunks: Iterator[bytes]):
    """Arrow record batches (the history schema, ts as naive UTC) from
    Postgres CSV COPY output of _PG_COLUMNS: NULL is an unquoted empty
    field, '' a quoted one; booleans are t/f; NaN/Infinity stay values."""
    pa, pacsv, _ = _pa()
    first = next((c for c in chunks if c), None)
    if first is None:  # no rows (pyarrow refuses an empty CSV)
        return
    chunks = itertools.chain([first], chunks)
    schema = arrow_schema()
    types = {f.name: f.type for f in schema}
    types["ts"] = pa.timestamp("us")
    reader = pacsv.open_csv(
        _ReadableChunks(chunks),
        read_options=pacsv.ReadOptions(column_names=[f.name for f in schema], block_size=CSV_BLOCK, use_threads=False),
        parse_options=pacsv.ParseOptions(newlines_in_values=True),
        convert_options=pacsv.ConvertOptions(column_types=types, null_values=[""], strings_can_be_null=True,
                                             quoted_strings_can_be_null=False, true_values=["t"],
                                             false_values=["f"]),
    )
    yield from reader


class _MonthWriter:
    """Writes one point's batches (ts ascending) into a Parquet file per
    UTC month, buffering at most ROW_GROUP rows."""

    def __init__(self, root: str, point: PointFiles):
        self.root = root
        self.point = point
        self.month: str | None = None
        self.writer = None
        self.pending: list = []
        self.pending_rows = 0
        self.file_rows = 0
        self.file_first: int | None = None
        self.file_last: int | None = None

    def add(self, batch) -> None:
        pa, _, _ = _pa()
        ts = batch.column(0).cast(pa.int64())
        n = len(batch)
        i = 0
        while i < n:
            first = ts[i].as_py()
            key = _month_key(first)
            if key != self.month:
                self._close_file()
                self._open_file(key)
            end_us = _next_month_start_us(key)
            # rows are ts-ascending: this month's run ends at the first ts >= end_us
            j = i + _count_below(ts.slice(i), end_us)
            part = batch.slice(i, j - i)
            self._buffer(part, first, ts[j - 1].as_py())
            i = j

    def _buffer(self, part, first: int, last: int) -> None:
        self.pending.append(part)
        self.pending_rows += len(part)
        self.file_rows += len(part)
        if self.file_first is None:
            self.file_first = first
        self.file_last = last
        if self.pending_rows >= ROW_GROUP:
            self._flush()

    def _flush(self) -> None:
        if not self.pending:
            return
        pa, _, _ = _pa()
        table = pa.Table.from_batches(self.pending).cast(arrow_schema())
        assert self.writer is not None
        self.writer.write_table(table, row_group_size=ROW_GROUP)
        self.pending = []
        self.pending_rows = 0

    def _open_file(self, key: str) -> None:
        _, _, pq = _pa()
        self.month = key
        rel = f"history/{self.point.directory}/{key}.parquet"
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.rel = rel
        writer = pq.ParquetWriter(path, arrow_schema(), **PARQUET_OPTIONS)
        writer.add_key_value_metadata({"point_uri": self.point.point_uri, "month": key,
                                       "format": FORMAT, "format_version": str(FORMAT_VERSION)})
        self.writer = writer
        self.file_rows = 0
        self.file_first = self.file_last = None

    def _close_file(self) -> None:
        if self.writer is None:
            return
        self._flush()
        self.writer.close()
        self.writer = None
        path = os.path.join(self.root, self.rel)
        p = self.point
        assert self.file_first is not None and self.file_last is not None
        p.files.append({"path": self.rel, "month": self.month, "rows": self.file_rows,
                        "first": _iso_us(self.file_first), "last": _iso_us(self.file_last),
                        "bytes": os.path.getsize(path), "sha256": sha256_file(path)})
        p.rows += self.file_rows
        p.first_us = self.file_first if p.first_us is None else p.first_us
        p.last_us = self.file_last

    def close(self) -> None:
        self._close_file()


def _count_below(ts, bound: int) -> int:
    """How many of the ascending int64 array `ts` are < bound."""
    import pyarrow.compute as pc

    n = len(ts)
    if n == 0 or ts[n - 1].as_py() < bound:
        return n
    return int(pc.sum(pc.less(ts, bound)).as_py() or 0)


def write_point(root: str, point_uri: str, batches: Iterable) -> PointFiles:
    """One point's samples (record batches in ts order) as point-month
    Parquet files under root/history/."""
    point = PointFiles(point_uri, point_dir(point_uri))
    w = _MonthWriter(root, point)
    try:
        for batch in batches:
            if len(batch):
                w.add(batch)
    finally:
        w.close()
    return point


def _copy_chunks(conn: psycopg.Connection, point_uri: str, since: datetime | None, until: datetime) -> Iterator[bytes]:
    conds = ["point_uri = {}", "ts < {}"]
    params: list = [point_uri, until]
    if since is not None:
        conds.append("ts >= {}")
        params.append(since)
    query = sql.SQL(
        f"COPY (SELECT {_PG_COLUMNS} FROM point_history WHERE {' AND '.join(conds)} ORDER BY ts) "
        "TO STDOUT WITH (FORMAT csv)"
    ).format(*[sql.Literal(p) for p in params])
    with conn.cursor() as cur, cur.copy(query) as copy:
        for chunk in copy:
            yield bytes(chunk)


def list_points(conn: psycopg.Connection, sc: Scope) -> list[str]:
    """Every point with history in scope. Postgres' index skip-scan on
    point_uri: one probe per point, not a scan of every sample."""
    rows = conn.execute("""
        WITH RECURSIVE p AS (
            (SELECT point_uri FROM point_history ORDER BY point_uri LIMIT 1)
            UNION ALL
            SELECT (SELECT h.point_uri FROM point_history h WHERE h.point_uri > p.point_uri
                    ORDER BY h.point_uri LIMIT 1)
            FROM p WHERE p.point_uri IS NOT NULL
        )
        SELECT point_uri FROM p WHERE point_uri IS NOT NULL
    """).fetchall()
    return [u for (u,) in rows if sc.allows(u)]


def export_history(conn: psycopg.Connection, root: str, sc: Scope, since: datetime | None, until: datetime,
                   progress: Callable[[str], None] = lambda _m: None) -> list[PointFiles]:
    conn.execute("SET TimeZone = 'UTC'")
    points = list_points(conn, sc)
    out = []
    for n, uri in enumerate(points, 1):
        p = write_point(root, uri, csv_batches(_copy_chunks(conn, uri, since, until)))
        if p.rows:
            out.append(p)
        if n % 100 == 0 or n == len(points):
            progress(f"history: {n}/{len(points)} points")
    _write_history_index(root, out)
    return out


def _write_history_index(root: str, points: list[PointFiles]) -> None:
    os.makedirs(os.path.join(root, "history"), exist_ok=True)
    with open(os.path.join(root, "history", "schema.json"), "w") as f:
        json.dump({
            "format": "Apache Parquet, zstd-compressed, one file per point per UTC calendar month",
            "path": "history/<point directory>/<YYYY-MM>.parquet (points.csv maps each directory to its point URI)",
            "file_metadata": "each file's key-value metadata repeats point_uri and month",
            "sort": "rows ascending by ts; ts is unique within a point",
            "columns": [{"name": n, "type": t, "description": d} for n, t, d in HISTORY_COLUMNS],
            "value_rule": "at most one of value, value_text, value_bool is set on a row",
            "quality_flags": list(quality.FLAGS),
        }, f, indent=2)
        f.write("\n")
    with open(os.path.join(root, "history", "points.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["point_uri", "directory", "rows", "files", "first", "last"])
        for p in points:
            w.writerow([p.point_uri, p.directory, p.rows, len(p.files),
                        _iso_us(p.first_us) if p.first_us is not None else "",
                        _iso_us(p.last_us) if p.last_us is not None else ""])
    with open(os.path.join(root, "history", "files.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["point_uri", "path", "month", "rows", "first", "last", "bytes", "sha256"])
        for p in points:
            for fi in p.files:
                w.writerow([p.point_uri, fi["path"], fi["month"], fi["rows"], fi["first"], fi["last"],
                            fi["bytes"], fi["sha256"]])


# ---------------------------------------------------------------- graph

def _graph_query(sc: Scope) -> str:
    cond = sc.sparql("s")
    return f"CONSTRUCT {{ ?s ?p ?o }} WHERE {{ ?s ?p ?o . FILTER({cond}) }}"


def export_graph(oxigraph_url: str, root: str, sc: Scope) -> int:
    """The entity graph as Turtle, streamed to disk. Everything: the
    default graph (entity data, as backup.py takes it; the ontology named
    graph is a copy of a published file). A site: the records its scope
    can see, by the same SPARQL filter the APIs apply."""
    path = os.path.join(root, "graph", "building.ttl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    base = oxigraph_url.rstrip("/")
    if sc.unrestricted:
        resp = requests.get(f"{base}/store", params={"default": ""}, headers={"Accept": "text/turtle"},
                            stream=True, timeout=(10, 600))
    else:
        resp = requests.post(f"{base}/query", data=_graph_query(sc).encode(),
                             headers={"Content-Type": "application/sparql-query", "Accept": "text/turtle"},
                             stream=True, timeout=(10, 600))
    with resp:
        resp.raise_for_status()
        with open(path, "wb") as f:
            f.writelines(resp.iter_content(1 << 20))
    return os.path.getsize(path)


# ---------------------------------------------------------------- records

def _table_exists(conn: psycopg.Connection, table: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone()
    return bool(row and row[0])


def _cx_where(kind: str, site_id: str | None) -> tuple[str, list]:
    projects = "SELECT id FROM cx_projects WHERE site_id = %s" if site_id else "SELECT id FROM cx_projects"
    p = [site_id] if site_id else []
    if kind == "self":
        return ("WHERE site_id = %s", p) if site_id else ("", [])
    if kind == "project":
        return f"WHERE project_id IN ({projects})", p
    if kind == "run":
        return f"WHERE run_id IN (SELECT id FROM cx_runs WHERE project_id IN ({projects}))", p
    if kind == "issue":
        return f"WHERE issue_id IN (SELECT id FROM cx_issues WHERE project_id IN ({projects}))", p
    raise ValueError(kind)


def _stream_json_rows(conn: psycopg.Connection, query: str, params: list) -> Iterator[str]:
    with conn.transaction(), conn.cursor(name="export_rows") as cur:
        cur.itersize = 2000
        cur.execute(query, params)
        for (row,) in cur:
            yield row


def export_records(conn: psycopg.Connection, root: str, sc: Scope, site_id: str | None) -> dict[str, int]:
    """Cx records of the exported projects (a site's own projects, or
    every project), and point-keyed records in scope, as JSON Lines: one
    row_to_json object per row, so every column and type survives and
    jsonb_populate_record reads it straight back."""
    counts: dict[str, int] = {}
    os.makedirs(os.path.join(root, "records"), exist_ok=True)
    for table, kind in CX_TABLES:
        if not _table_exists(conn, table):
            continue
        where, params = _cx_where(kind, site_id)
        counts[table] = _write_jsonl(root, table, _stream_json_rows(
            conn, f"SELECT row_to_json(t)::text FROM {table} t {where} ORDER BY 1", params))
    for table in POINT_TABLES:
        if not _table_exists(conn, table):
            continue
        rows = (r for r in _stream_json_rows(conn, f"SELECT row_to_json(t)::text FROM {table} t ORDER BY id", [])
                if sc.unrestricted or sc.allows(json.loads(r).get("point_uri") or ""))
        counts[table] = _write_jsonl(root, table, rows)
    _write_issues_csv(root)
    return counts


def _write_jsonl(root: str, table: str, rows: Iterable[str]) -> int:
    n = 0
    with open(os.path.join(root, "records", f"{table}.jsonl"), "w") as f:
        for row in rows:
            f.write(row)
            f.write("\n")
            n += 1
    return n


def _iter_jsonl(path: str) -> Iterator[dict]:
    with open(path) as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def _write_issues_csv(root: str) -> None:
    src = os.path.join(root, "records", "cx_issues.jsonl")
    if not os.path.exists(src):
        return
    with open(os.path.join(root, "records", "cx_issues.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(ISSUE_CSV_COLUMNS)
        for row in _iter_jsonl(src):
            w.writerow(["" if row.get(c) is None else row[c] for c in ISSUE_CSV_COLUMNS])


def export_documents(root: str) -> int:
    """documents/manifest.csv from the exported spec documents: Baler
    stores only each file's sha256, never the file (NOT_YET_IN_BALER)."""
    os.makedirs(os.path.join(root, "documents"), exist_ok=True)
    src = os.path.join(root, "records", "cx_spec_documents.jsonl")
    n = 0
    with open(os.path.join(root, "documents", "manifest.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(DOCUMENT_COLUMNS)
        if os.path.exists(src):
            for row in _iter_jsonl(src):
                w.writerow([row.get("sha256"), row.get("filename"), row.get("media_type"), row.get("size"),
                            row.get("id"), row.get("project_id"), row.get("created_at"), "false"])
                n += 1
    return n


# ---------------------------------------------------------------- export

def _site(conn: psycopg.Connection, site_id: str) -> dict:
    scope.ensure_schema(conn)
    row = conn.execute("SELECT id, org_id, name, topic_prefixes FROM sites WHERE id = %s", (site_id,)).fetchone()
    if row is None:
        raise ExportError(f"no site {site_id!r}")
    prefixes = sorted(scope.normalize_prefix(p) for p in (row[3] or []))
    if not prefixes:
        raise ExportError(f"site {site_id!r} owns no topic prefixes, so no data is its own; "
                          "give it prefixes (PUT /auth/sites/{id}/topic-prefixes) or export everything")
    return {"id": row[0], "org_id": row[1], "name": row[2], "topic_prefixes": prefixes}


def _baler_version() -> str:
    try:
        return metadata.version("baler")
    except metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


def export_building(dsn: str, oxigraph_url: str, out_dir: str, site_id: str | None = None,
                    since: datetime | None = None, until: datetime | None = None,
                    progress: Callable[[str], None] = lambda _m: None) -> dict:
    """Write the export to out_dir (which must not exist yet). Built in
    out_dir + '.partial' and renamed when complete, so a failed export
    never looks like a finished one."""
    if os.path.exists(out_dir):
        raise ExportError(f"{out_dir} already exists: export into a new directory")
    started = datetime.now(timezone.utc)
    until = until or started
    tmp = out_dir.rstrip("/") + ".partial"
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            site = _site(conn, site_id) if site_id else None
            sc = Scope(tuple(site["topic_prefixes"])) if site else scope.EVERYTHING
            points = export_history(conn, tmp, sc, since, until, progress)
            progress("graph")
            graph_bytes = export_graph(oxigraph_url, tmp, sc)
            progress("records")
            records = export_records(conn, tmp, sc, site_id)
            documents = export_documents(tmp)
        manifest = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "created_at": started.isoformat().replace("+00:00", "Z"),
            "finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "baler_version": _baler_version(),
            "scope": {"site": site} if site else {"site": None, "note": "everything in this install"},
            "history": {
                "since": since.isoformat() if since else None,
                "until": until.isoformat(),
                "points": len(points),
                "files": sum(len(p.files) for p in points),
                "rows": sum(p.rows for p in points),
                "first": min((_iso_us(p.first_us) for p in points if p.first_us is not None), default=None),
                "last": max((_iso_us(p.last_us) for p in points if p.last_us is not None), default=None),
            },
            "graph": {"path": "graph/building.ttl", "bytes": graph_bytes},
            "records": records,
            "documents": documents,
            "not_included": NOT_YET_IN_BALER,
        }
        with open(os.path.join(tmp, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=2)
            f.write("\n")
        with open(os.path.join(tmp, "README.md"), "w") as f:
            f.write(readme(manifest))
        write_sha256sums(tmp)
        os.replace(tmp, out_dir)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return manifest


def _all_files(root: str) -> Iterator[str]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            if rel != "SHA256SUMS":
                yield rel.replace(os.sep, "/")


def write_sha256sums(root: str) -> None:
    """`sha256sum -c SHA256SUMS` checks the export with nothing but
    coreutils. Parquet files' sums are already in files.csv; reused."""
    known = {}
    files_csv = os.path.join(root, "history", "files.csv")
    if os.path.exists(files_csv):
        with open(files_csv, newline="") as f:
            known = {r["path"]: r["sha256"] for r in csv.DictReader(f)}
    with open(os.path.join(root, "SHA256SUMS"), "w") as out:
        out.writelines(f"{known.get(rel) or sha256_file(os.path.join(root, rel))}  {rel}\n" for rel in _all_files(root))


def readme(m: dict) -> str:
    h = m["history"]
    site = m["scope"]["site"]
    what = (f"site **{site['name']}** (`{site['id']}`), which owns the topic prefixes "
            + ", ".join(f"`{p}`" for p in site["topic_prefixes"])) if site else "every site in the install"
    records = "\n".join(f"| `records/{t}.jsonl` | {n} |" for t, n in m["records"].items()) or "| (none) | |"
    missing = "\n".join(f"- **{k}**: {v}." for k, v in m["not_included"].items())
    return f"""# Building export

This directory is a complete, self-describing export of one building's
record from Baler, taken {m['created_at']}: {what}. It is meant to be read
without Baler, with ordinary open tools, for as long as the files last.

## Contents

| Path | What |
|---|---|
| `history/` | Every raw sample of every point, as Apache Parquet: one file per point per UTC month |
| `history/schema.json` | The Parquet columns and what they mean |
| `history/points.csv` | One row per point: its exact URI, directory, row count and time span |
| `history/files.csv` | One row per Parquet file: point, month, rows, sha256 |
| `graph/building.ttl` | The building model (sites, equipment, points, their relationships) as RDF Turtle, using the Brick, Project Haystack and RealEstateCore vocabularies |
| `records/*.jsonl` | Commissioning records and faults: one JSON object per line, one file per table |
| `records/cx_issues.csv` | The issues log as a spreadsheet |
| `documents/manifest.csv` | Documents known to the record, by sha256 |
| `manifest.json` | Scope, counts, Baler version, and what isn't included |
| `SHA256SUMS` | The sha256 of every file here |

History: {h['points']} points, {h['rows']} samples in {h['files']} files, from
{h['first']} to {h['last']} (cut at {h['until']}). Samples are raw, exactly
as recorded: never averaged or downsampled.

| Records | Rows |
|---|---|
{records}

## Checking it

```sh
sha256sum -c SHA256SUMS
```

## Reading the history

Each Parquet file holds the columns in `history/schema.json`: `ts` (UTC),
the value in whichever of `value`, `value_text`, `value_bool` fits it,
`unit`, `label`, and `quality` (empty means ok). Any Parquet reader works:

```python
import pyarrow.dataset as ds            # pip install pyarrow
t = ds.dataset("history", format="parquet").to_table()   # or one file: pyarrow.parquet.read_table(path)
```

```sql
-- DuckDB
SELECT * FROM read_parquet('history/**/*.parquet', filename = true);
```

The point a file belongs to is in `history/points.csv` (by directory), in
`history/files.csv` (by path), and in each file's own key-value metadata
(`point_uri`).

## Reading the graph

`graph/building.ttl` is standard RDF Turtle. Points are `urn:point:...`,
equipment `urn:equip:...`; a point's URI is the same string used in
`history/points.csv`. Load it into any RDF store or library, e.g.
`rdflib.Graph().parse("graph/building.ttl")` in Python. The Brick schema is
at https://brickschema.org, Project Haystack at https://project-haystack.org.

## Records

Each `records/<table>.jsonl` line is one row with every column, named as
in Baler's database: `cx_projects` (commissioning projects), `cx_phases`,
`cx_runs` and `cx_results` (data-driven checks and their evidence),
`cx_issues`, `cx_issue_events` and `cx_risks` (the issues log and accepted
risks), `cx_test_procedures` and `cx_test_runs` (start-up and functional
tests), `cx_spec_*` and `cx_mappings` (specification items and how they
matched equipment), `cx_active_runs` (active tests), `cx_agent_*` (the
assistant's sessions and proposals), `cx_events` (the audit trail), and
`faults` (detected faults on this building's points).

## Not included

{missing}

## Loading it into another Baler

```sh
python -m baler.export import <this directory> --org-id <org> [--site-id <site>]
```
"""


# ---------------------------------------------------------------- verify

@dataclass
class VerifyResult:
    errors: list[str] = field(default_factory=list)
    files_checked: int = 0
    rows: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


def verify(root: str, dsn: str | None = None) -> VerifyResult:
    """Every file against SHA256SUMS; every Parquet file's schema and row
    count against files.csv; points.csv totals against files.csv. With a
    dsn, also each point-month's row count against the source database,
    over the export's own time cut."""
    _, _, pq = _pa()
    res = VerifyResult()
    sums_path = os.path.join(root, "SHA256SUMS")
    if not os.path.exists(sums_path):
        res.errors.append("no SHA256SUMS")
        return res
    listed = set()
    with open(sums_path) as f:
        for line in f:
            digest, _, rel = line.rstrip("\n").partition("  ")
            listed.add(rel)
            path = os.path.join(root, rel)
            if not os.path.exists(path):
                res.errors.append(f"missing: {rel}")
            elif sha256_file(path) != digest:
                res.errors.append(f"sha256 mismatch: {rel}")
            res.files_checked += 1
    for rel in _all_files(root):
        if rel not in listed:
            res.errors.append(f"not in SHA256SUMS: {rel}")
    expected = arrow_schema()
    per_point: dict[str, list[dict]] = {}
    with open(os.path.join(root, "history", "files.csv"), newline="") as f:
        for r in csv.DictReader(f):
            per_point.setdefault(r["point_uri"], []).append(r)
            path = os.path.join(root, r["path"])
            if not os.path.exists(path):
                continue
            pf = pq.ParquetFile(path)
            if not pf.schema_arrow.equals(expected):
                res.errors.append(f"schema differs: {r['path']}")
            if pf.metadata.num_rows != int(r["rows"]):
                res.errors.append(f"{r['path']}: {pf.metadata.num_rows} rows, files.csv says {r['rows']}")
            meta = pf.metadata.metadata or {}
            if meta.get(b"point_uri", b"").decode() != r["point_uri"]:
                res.errors.append(f"{r['path']}: point_uri metadata differs from files.csv")
            res.rows += pf.metadata.num_rows
    with open(os.path.join(root, "history", "points.csv"), newline="") as f:
        for r in csv.DictReader(f):
            files = per_point.get(r["point_uri"], [])
            if sum(int(x["rows"]) for x in files) != int(r["rows"]) or len(files) != int(r["files"]):
                res.errors.append(f"points.csv totals differ from files.csv for {r['point_uri']}")
    if dsn:
        _verify_against_source(root, dsn, per_point, res)
    return res


def _verify_against_source(root: str, dsn: str, per_point: dict[str, list[dict]], res: VerifyResult) -> None:
    with open(os.path.join(root, "manifest.json")) as f:
        m = json.load(f)
    since, until = m["history"]["since"], m["history"]["until"]
    site = m["scope"]["site"]
    sc = Scope(tuple(site["topic_prefixes"])) if site else scope.EVERYTHING
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET TimeZone = 'UTC'")
        source_points = set(list_points(conn, sc))
        for uri in sorted(source_points | set(per_point)):
            rows = conn.execute(
                "SELECT to_char(date_trunc('month', ts), 'YYYY-MM'), count(*) FROM point_history "
                "WHERE point_uri = %s AND ts < %s AND (%s::timestamptz IS NULL OR ts >= %s::timestamptz) GROUP BY 1",
                (uri, until, since, since)).fetchall()
            source = {k: n for k, n in rows if n}
            exported = {r["month"]: int(r["rows"]) for r in per_point.get(uri, [])}
            if source != exported:
                res.errors.append(f"{uri}: source has {sum(source.values())} rows over {len(source)} months, "
                                  f"export {sum(exported.values())} over {len(exported)}")


# ---------------------------------------------------------------- import

def _read_manifest(root: str) -> dict:
    with open(os.path.join(root, "manifest.json")) as f:
        m = json.load(f)
    if m.get("format") != FORMAT:
        raise ExportError(f"{root} is not a {FORMAT}")
    if int(m.get("format_version", 0)) > FORMAT_VERSION:
        raise ExportError(f"export format {m['format_version']} is newer than this Baler reads ({FORMAT_VERSION})")
    return m


def _compression_jobs(conn: psycopg.Connection) -> list[int]:
    return [j for (j,) in conn.execute(
        "SELECT job_id FROM timescaledb_information.jobs WHERE proc_name = 'policy_compression' "
        "AND hypertable_name = 'point_history' AND scheduled").fetchall()]


def import_history(conn: psycopg.Connection, root: str, progress: Callable[[str], None] = lambda _m: None) -> int:
    """Each Parquet file into point_history through a staging table, in
    ROW_GROUP-row batches, month by month so each chunk is filled while
    it's hot. ON CONFLICT DO NOTHING: re-running an import adds only
    what's missing.

    The compression policy is paused meanwhile (and resumed however the
    import ends): imported history is mostly older than COMPRESS_AFTER,
    and a chunk the policy compresses mid-import makes every later insert
    into it take the slow decompress-and-recompress path."""
    _, pacsv, pq = _pa()
    from baler import timeseries

    timeseries._setup(conn)
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS export_stage "
                 "(ts timestamptz, value float8, value_text text, value_bool boolean, unit text, label text, "
                 "quality text)")
    with open(os.path.join(root, "history", "files.csv"), newline="") as f:
        files = sorted((r["month"], r["point_uri"], r["path"], r["first"], r["last"]) for r in csv.DictReader(f))
    paused = _compression_jobs(conn)
    for job in paused:
        conn.execute("SELECT alter_job(%s, scheduled => false)", (job,))
    total = 0
    try:
        for n, (_month, point_uri, path, first, last) in enumerate(files, 1):
            pf = pq.ParquetFile(os.path.join(root, path))
            with conn.transaction():
                conn.execute("TRUNCATE export_stage")
                with conn.cursor() as cur, cur.copy(
                        "COPY export_stage (ts, value, value_text, value_bool, unit, label, quality) "
                        "FROM STDIN WITH (FORMAT csv)") as copy:
                    for batch in pf.iter_batches(batch_size=ROW_GROUP):
                        buf = _CsvSink()
                        pacsv.write_csv(batch, buf, write_options=pacsv.WriteOptions(include_header=False))
                        copy.write(buf.getvalue())
                # Nothing of this point in the file's span yet (a fresh
                # install): a plain INSERT, ~40% faster than the upsert.
                row = conn.execute("SELECT EXISTS (SELECT 1 FROM point_history WHERE point_uri = %s "
                                   "AND ts >= %s AND ts <= %s)", (point_uri, first, last)).fetchone()
                upsert = " ON CONFLICT (point_uri, ts) DO NOTHING" if row and row[0] else ""
                cur = conn.execute(
                    "INSERT INTO point_history (point_uri, ts, value, value_text, value_bool, unit, label, quality) "
                    "SELECT %s, ts, value, value_text, value_bool, unit, label, quality FROM export_stage" + upsert,
                    (point_uri,))
                total += cur.rowcount
            if n % 500 == 0 or n == len(files):
                progress(f"history: {n}/{len(files)} files")
    finally:
        for job in paused:
            conn.execute("SELECT alter_job(%s, scheduled => true)", (job,))
    return total


class _CsvSink:
    def __init__(self):
        self._parts: list[bytes] = []

    def write(self, b) -> int:
        self._parts.append(bytes(b))
        return len(b)

    def getvalue(self) -> bytes:
        return b"".join(self._parts)

    def flush(self) -> None:
        pass

    closed = False


def import_graph(oxigraph_url: str, root: str) -> None:
    """graph/building.ttl into the default graph (Oxigraph's bulk load
    adds to what's there)."""
    with open(os.path.join(root, "graph", "building.ttl"), "rb") as f:
        resp = requests.post(f"{oxigraph_url.rstrip('/')}/store", params={"default": ""},
                             headers={"Content-Type": "text/turtle"}, data=f, timeout=(10, 600))
    resp.raise_for_status()


def import_records(conn: psycopg.Connection, root: str, org_id: str | None, site_id: str | None) -> dict[str, int]:
    """records/*.jsonl back into their tables, rows that already exist (by
    primary key) left alone. Cx projects move into org_id (and site_id, or
    org-wide when none is given): the source's org and site don't exist in
    a fresh install."""
    from baler import auth, faults
    from baler.commissioning import db as cx_db

    auth.ensure_schema(conn)
    scope.ensure_schema(conn)
    cx_db.ensure_schema(conn)
    faults.ensure_schema(conn)
    counts: dict[str, int] = {}
    has_cx = any(os.path.exists(os.path.join(root, "records", f"{t}.jsonl")) and
                 os.path.getsize(os.path.join(root, "records", f"{t}.jsonl")) for t, _ in CX_TABLES)
    if has_cx and not org_id:
        raise ExportError("this export has commissioning records: pass --org-id (the org to own them)")
    if org_id and not conn.execute("SELECT 1 FROM orgs WHERE id = %s", (org_id,)).fetchone():
        raise ExportError(f"no org {org_id!r} in the target")
    for table in [t for t, _ in CX_TABLES] + POINT_TABLES:
        path = os.path.join(root, "records", f"{table}.jsonl")
        if not os.path.exists(path):
            continue

        def rows(path=path, table=table):
            for row in _iter_jsonl(path):
                if table == "cx_projects":
                    row["org_id"] = org_id
                    row["site_id"] = site_id
                yield row

        counts[table] = _insert_json_rows(conn, table, rows())
    for table in SERIAL_TABLES:
        if table in counts:
            conn.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                         f"GREATEST((SELECT max(id) FROM {table}), 1))")
    return counts


def _insert_json_rows(conn: psycopg.Connection, table: str, rows: Iterable[dict], batch: int = 1000) -> int:
    total = 0
    buf: list[dict] = []

    def flush() -> int:
        cur = conn.execute(
            f"INSERT INTO {table} SELECT * FROM jsonb_populate_recordset(NULL::{table}, %s::jsonb) "
            "ON CONFLICT DO NOTHING", (json.dumps(buf),))
        buf.clear()
        return cur.rowcount

    for row in rows:
        buf.append(row)
        if len(buf) >= batch:
            total += flush()
    if buf:
        total += flush()
    return total


def import_building(root: str, dsn: str, oxigraph_url: str, org_id: str | None = None, site_id: str | None = None,
                    graph: bool = True, progress: Callable[[str], None] = lambda _m: None) -> dict:
    """Into a fresh install: checks the export first, refuses a damaged one."""
    _read_manifest(root)
    res = verify(root)
    if not res.ok:
        raise ExportError("the export doesn't check out, nothing imported:\n  " + "\n  ".join(res.errors[:20]))
    out: dict = {}
    with psycopg.connect(dsn, autocommit=True) as conn:
        out["records"] = import_records(conn, root, org_id, site_id)
        out["history_rows"] = import_history(conn, root, progress)
    if graph:
        import_graph(oxigraph_url, root)
        out["graph"] = True
    return out


# ---------------------------------------------------------------- CLI

def _ts(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    from baler import timeseries

    ap = argparse.ArgumentParser(prog="python -m baler.export", description=__doc__.split("\n\n")[0])
    ap.add_argument("--dsn", default=timeseries.DEFAULT_DSN, help="Postgres (default: $BALER_PG_DSN)")
    ap.add_argument("--oxigraph-url", default=os.environ.get("OXIGRAPH_URL", "http://localhost:7878"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="write a building export to a new directory")
    e.add_argument("out_dir")
    e.add_argument("--site", help="one site's id (its topic prefixes and its own Cx projects); default everything")
    e.add_argument("--since", type=_ts, help="history from this time (ISO 8601; default: all of it)")
    e.add_argument("--until", type=_ts, help="history before this time (default: when the export starts)")
    v = sub.add_parser("verify", help="check an export's files, and optionally its row counts against the source")
    v.add_argument("out_dir")
    v.add_argument("--against-source", action="store_true", help="compare row counts per point-month with --dsn")
    i = sub.add_parser("import", help="load an export into a (fresh) install")
    i.add_argument("out_dir")
    i.add_argument("--org-id", help="org to own the imported Cx projects (required if there are any)")
    i.add_argument("--site-id", help="site for the imported Cx projects (default: org-wide)")
    i.add_argument("--no-graph", action="store_true", help="skip loading graph/building.ttl")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    def say(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    if args.cmd == "export":
        m = export_building(args.dsn, args.oxigraph_url, args.out_dir, args.site, args.since, args.until, say)
        h = m["history"]
        print(f"{args.out_dir}: {h['points']} points, {h['rows']} samples in {h['files']} files, "
              f"graph {m['graph']['bytes']} bytes, records {sum(m['records'].values())} rows")
        return 0
    if args.cmd == "verify":
        res = verify(args.out_dir, args.dsn if args.against_source else None)
        for err in res.errors:
            print(err)
        print(f"{'ok' if res.ok else 'FAILED'}: {res.files_checked} files, {res.rows} history rows")
        return 0 if res.ok else 1
    out = import_building(args.out_dir, args.dsn, args.oxigraph_url, args.org_id, args.site_id,
                          graph=not args.no_graph, progress=say)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

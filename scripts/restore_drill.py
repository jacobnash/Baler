"""
Restore drill (#196): prove a backup restores. Restores a scripts/backup.py
backup (the newest in backups/ unless you name one) with scripts/restore.py
into a scratch stack - its own compose project and volumes, beside the
live stack, never touching it - then checks it, prints a report and tears
the scratch stack down. Exits non-zero if any check fails.

Checks:
  * every table in the dump has exactly the rows the dump has, compressed
    chunk tables and TimescaleDB's own catalog included;
  * point_history holds as many samples as the dump (compressed and
    uncompressed chunks), and how fresh the newest one is;
  * TimescaleDB is out of restore mode, on the dump's version (or updated
    from it), with its compression and refresh policies scheduled;
  * the continuous aggregates, refreshed over a recent window and over the
    newest compressed chunk, equal a fold of the raw rows;
  * a write into a compressed chunk works (an idempotent re-insert);
  * the graph has as many triples as the backup file;
  * the latest commissioning run's report renders.

Needs docker, and baler importable (run it from a checkout after
`pip install -e .`). See docs-site/pages/backups.mdx.
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg
import requests
from psycopg import sql

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DRILL_COMPOSE = os.path.join(HERE, "restore-drill.compose.yml")
DRILL_PASSWORD = "restore-drill"  # set by restore-drill.compose.yml
DEFAULT_PROJECT = "baler-restore-drill"
_COPY = re.compile(r"^COPY (\S+) \((.*)\) FROM stdin;$")
_OWNER = re.compile(r"^-- Name: [^;]+; Type: TABLE; Schema: public; Owner: (\S+)$")
_STAMP = re.compile(r"(\d{8}T\d{6}Z)")
_V = "COALESCE(value, value_bool::int::float8)"  # timeseries.ROLLUP_DDL's value expression


# --- the dump: what the restore has to reproduce ----------------------------

class DumpFacts:
    """One streaming pass over a plain pg_dump: rows per COPY block, the
    owner role, and what's needed to count point_history's samples
    (TimescaleDB's hypertable catalog and the compressed chunks' row counts)."""

    def __init__(self, path: str):
        self.rows: dict[str, int] = {}
        self.owner: str | None = None
        self.hypertables: list[dict] = []
        self.compression_settings: list[dict] = []
        self.compressed_samples: dict[str, int] = {}  # compressed chunk table -> samples packed into it
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            table, cols, n, meta_idx = None, [], 0, None
            for line in f:
                if table is None:
                    if self.owner is None and (m := _OWNER.match(line.rstrip("\n"))):
                        self.owner = m.group(1)
                    if m := _COPY.match(line.rstrip("\n")):
                        table, cols, n = m.group(1), [c.strip().strip('"') for c in m.group(2).split(",")], 0
                        meta_idx = cols.index("_ts_meta_count") if "_ts_meta_count" in cols else None
                        if meta_idx is not None:
                            self.compressed_samples[table] = 0
                    continue
                if line.startswith("\\."):
                    self.rows[table] = n
                    table = None
                    continue
                n += 1
                if table in ("_timescaledb_catalog.hypertable", "_timescaledb_catalog.compression_settings"):
                    row = dict(zip(cols, line.rstrip("\n").split("\t")))
                    (self.hypertables if table.endswith("hypertable") else self.compression_settings).append(row)
                elif meta_idx is not None:
                    self.compressed_samples[table] += int(line.split("\t")[meta_idx])

    def point_history_samples(self) -> int | None:
        """Rows in point_history's chunks plus the samples packed into its
        compressed chunks; None if the dump has no point_history hypertable."""
        ht = next((h for h in self.hypertables if h.get("table_name") == "point_history"), None)
        if ht is None:
            return None
        raw = re.compile(rf"^_timescaledb_internal\._hyper_{ht['id']}_\d+_chunk$")
        # Where each chunk's compressed rows live: the pinned TimescaleDB
        # (2.31) records a table per chunk (_hyper_1_1_chunk_compressed) in
        # compression_settings; earlier 2.x kept them in a compressed
        # hypertable's chunks (compress_hyper_<id>_*). Both are counted.
        compressed = {c["compress_relid"] for c in self.compression_settings if raw.match(c.get("relid", ""))}
        comp_id = ht.get("compressed_hypertable_id", "\\N")
        old_layout = re.compile(rf"^_timescaledb_internal\.compress_hyper_{comp_id}_\d+_chunk$") if comp_id.isdigit() else None
        return (sum(n for t, n in self.rows.items() if raw.match(t))
                + sum(n for t, n in self.compressed_samples.items()
                      if t in compressed or (old_layout is not None and old_layout.match(t))))


def turtle_triples(path: str) -> int:
    from rdflib import Graph

    g = Graph()
    with gzip.open(path, "rb") as f:
        g.parse(data=f.read(), format="turtle")
    return len(g)


def latest_backup(out_dir: str, kind: str) -> str | None:
    pattern = {"postgres": "postgres-*.sql.gz", "oxigraph": "oxigraph-*.ttl.gz"}[kind]
    files = sorted(glob.glob(os.path.join(out_dir, pattern)))
    return files[-1] if files else None


# --- the scratch stack -------------------------------------------------------

def live_project_name() -> str:
    name = os.environ.get("COMPOSE_PROJECT_NAME") or os.path.basename(os.path.abspath(REPO))
    return re.sub(r"[^a-z0-9_-]", "", name.lower())


class Scratch:
    """postgres + oxigraph from docker-compose.yml under their own project
    name and volume prefix. Only ever removes volumes that Docker labels
    as belonging to this project."""

    def __init__(self, project: str, pg_user: str | None = None):
        if project == live_project_name():
            raise SystemExit(f"refusing to use the live compose project {project!r} as a scratch stack")
        self.project = project
        self.env = {**os.environ, "COMPOSE_PROJECT_NAME": project, "BALER_VOLUME_PREFIX": project,
                    "COMPOSE_FILE": os.pathsep.join([os.path.join(REPO, "docker-compose.yml"), DRILL_COMPOSE])}
        # docker-compose.yml requires these (`:?`) for services the drill never
        # starts; compose still interpolates the whole file, and a host with
        # no .env in the checkout (nash-srv keeps its env file elsewhere)
        # would refuse `up`. Placeholders only: nothing here uses them.
        for var in ("MQTT_PASSWORD", "BALER_JWT_SECRET", "BALER_GATEWAY_SECRET"):
            self.env.setdefault(var, "unused-by-restore-drill")
        if pg_user:  # the dump's owner role has to exist in the target
            self.env["BALER_PG_USER"] = pg_user

    def compose(self, *args: str, capture: bool = False) -> str:
        out = subprocess.run(["docker", "compose", *args], cwd=REPO, env=self.env, check=True,
                             capture_output=capture, text=True)
        return out.stdout.strip() if capture else ""

    def up(self) -> None:
        self.down()  # a scratch stack left by an interrupted drill
        self.compose("up", "-d", "--wait", "postgres", "oxigraph")
        self.attach()

    def attach(self) -> None:
        """Find the running stack's ports and wait until both answer."""
        self.pg_port = int(self.compose("port", "postgres", "5432", capture=True).rsplit(":", 1)[1])
        self.ox_port = int(self.compose("port", "oxigraph", "7878", capture=True).rsplit(":", 1)[1])
        user, db = self.compose("exec", "-T", "postgres", "sh", "-c", 'echo "$POSTGRES_USER $POSTGRES_DB"',
                                capture=True).split()
        self.dsn = f"postgresql://{user}:{DRILL_PASSWORD}@127.0.0.1:{self.pg_port}/{db}"
        self.oxigraph_url = f"http://127.0.0.1:{self.ox_port}"
        # The healthcheck (pg_isready on the socket) passes during the image's
        # first-boot init, which then restarts the server; that init server
        # doesn't listen on TCP, so a TCP connection means the real one is up.
        deadline = time.monotonic() + 120
        while True:
            try:
                psycopg.connect(self.dsn, connect_timeout=3).close()
                requests.get(f"{self.oxigraph_url}/query", params={"query": "ASK {}"}, timeout=3).raise_for_status()
                break
            except (psycopg.OperationalError, requests.RequestException):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)

    def down(self) -> None:
        self.compose("down", "--remove-orphans")
        vols = subprocess.run(["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={self.project}"],
                              check=True, capture_output=True, text=True).stdout.split()
        if vols:
            subprocess.run(["docker", "volume", "rm", *vols], check=True, capture_output=True)


# --- checks ------------------------------------------------------------------

class Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []
        self.facts: dict = {}

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""), flush=True)
        return ok

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c["ok"] for c in self.checks)


def _one(conn: psycopg.Connection, q, params=None):
    row = conn.execute(q, params).fetchone()
    return None if row is None else row[0]


def check_tables(conn: psycopg.Connection, facts: DumpFacts, rep: Report) -> None:
    # An extension's configuration tables are dumped only as far as the
    # extension says (pg_extension_config_dump's filter: TimescaleDB's
    # bgw_job keeps its own jobs, id < 1000, out of the dump), so count
    # them through the same filter.
    filters = dict(conn.execute(
        "SELECT c.oid::regclass::text, x.cond FROM pg_extension e, unnest(e.extconfig, e.extcondition) AS x(oid, cond) "
        "JOIN pg_class c ON c.oid = x.oid").fetchall())
    wrong = []
    for table, n in sorted(facts.rows.items()):
        # the dump's own (quoted where needed) name
        got = _one(conn, f"SELECT count(*) FROM ONLY {table} {filters.get(table) or ''}")
        if got != n:
            wrong.append(f"{table}: dump {n}, restored {got}")
    rep.facts["tables"] = len(facts.rows)
    rep.facts["rows"] = sum(facts.rows.values())
    rep.check("every table has the dump's rows", not wrong,
              "; ".join(wrong[:10]) if wrong else f"{len(facts.rows)} tables, {sum(facts.rows.values())} rows")


def check_timescaledb(conn: psycopg.Connection, dump_version: str | None, rep: Report) -> bool:
    version = _one(conn, "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
    rep.facts["timescaledb"] = {"dump": dump_version, "restored": version}
    if dump_version is None:
        rep.check("no TimescaleDB in the dump", True, "skipping the hypertable checks")
        return False
    rep.check("out of TimescaleDB restore mode", _one(conn, "SHOW timescaledb.restoring") == "off")
    rep.check("TimescaleDB version", version is not None, f"dump {dump_version}, restored {version}")
    jobs = conn.execute(
        "SELECT proc_name, hypertable_name, scheduled FROM timescaledb_information.jobs "
        "WHERE proc_name IN ('policy_compression', 'policy_refresh_continuous_aggregate') ORDER BY 1, 2").fetchall()
    rep.facts["policies"] = [f"{p} {h}" for p, h, _ in jobs]
    rep.check("compression and refresh policies scheduled", bool(jobs) and all(s for _, _, s in jobs),
              ", ".join(f"{p.removeprefix('policy_')} on {h}" + ("" if s else " (NOT scheduled)") for p, h, s in jobs))
    return True


def check_history(conn: psycopg.Connection, facts: DumpFacts, backup_at: datetime | None, rep: Report) -> dict | None:
    expected = facts.point_history_samples()
    if expected is None:
        rep.check("point_history", False, "no point_history hypertable in the dump")
        return None
    got, points, newest = conn.execute("SELECT count(*), count(DISTINCT point_uri), max(ts) FROM point_history").fetchone()
    chunks = conn.execute(
        "SELECT count(*) FILTER (WHERE is_compressed), count(*) FROM timescaledb_information.chunks "
        "WHERE hypertable_schema = 'public' AND hypertable_name = 'point_history'").fetchone()
    rep.facts["point_history"] = {"samples": got, "points": points, "newest": newest.isoformat() if newest else None,
                                  "compressed_chunks": chunks[0], "chunks": chunks[1]}
    rep.check("point_history samples", got == expected,
              f"dump {expected}, restored {got}; {points} points; {chunks[0]} of {chunks[1]} chunks compressed")
    if newest and backup_at:
        rep.check("newest sample", True, f"{newest.isoformat()}, {_age(backup_at - newest)} before the backup")
    return {"newest": newest, "compressed": chunks[0]}


def _age(d: timedelta) -> str:
    s = max(int(d.total_seconds()), 0)
    return f"{s // 86400} d {s % 86400 // 3600} h" if s >= 86400 else f"{s // 3600} h {s % 3600 // 60} min"


def _windows(conn: psycopg.Connection, newest: datetime, hours: int) -> list[tuple[str, datetime, datetime]]:
    """Whole hours: the last `hours` before the newest sample's hour, and
    the last `hours` of data in the newest compressed chunk (a chunk's range
    can start well before its first sample)."""
    def ending_at(ts: datetime) -> tuple[datetime, datetime]:
        end = ts.replace(minute=0, second=0, microsecond=0)
        return end - timedelta(hours=hours), end

    out = [("recent", *ending_at(newest))]
    chunk = conn.execute("SELECT range_start, range_end FROM timescaledb_information.chunks WHERE hypertable_schema = "
                         "'public' AND hypertable_name = 'point_history' AND is_compressed "
                         "ORDER BY range_start DESC LIMIT 1").fetchone()
    if chunk is not None:
        last = _one(conn, "SELECT max(ts) FROM point_history WHERE ts >= %s AND ts < %s", chunk)
        if last is not None:
            out.append(("compressed", *ending_at(last)))
    return out


def check_rollups(conn: psycopg.Connection, newest: datetime, hours: int, rep: Report) -> None:
    views = [v for v, width in (("point_history_5m", "5 minutes"), ("point_history_1h", "1 hour"))
             if _one(conn, "SELECT to_regclass(%s) IS NOT NULL", (v,))]
    if not views:
        rep.check("continuous aggregates", False, "point_history_5m/point_history_1h missing")
        return
    for label, a, b in _windows(conn, newest, hours):
        for view, width in (("point_history_5m", "5 minutes"), ("point_history_1h", "1 hour")):
            if view not in views:
                continue
            # 5m first: 1h is built on it.
            conn.execute(sql.SQL("CALL refresh_continuous_aggregate({}, {}, {})").format(
                sql.Literal(view), sql.Literal(a), sql.Literal(b)))
            bad, total = conn.execute(sql.SQL(f"""
                WITH v AS (SELECT point_uri, bucket, vsum, vcount, vmin, vmax FROM {{}} WHERE bucket >= %(a)s AND bucket < %(b)s),
                     r AS (SELECT point_uri, time_bucket(INTERVAL '{width}', ts) AS bucket, sum({_V}) AS vsum,
                                  count({_V}) AS vcount, min({_V}) AS vmin, max({_V}) AS vmax
                           FROM point_history WHERE ts >= %(a)s AND ts < %(b)s GROUP BY 1, 2)
                SELECT count(*) FILTER (WHERE v.point_uri IS NULL OR r.point_uri IS NULL OR v.vcount <> r.vcount
                                         OR v.vmin IS DISTINCT FROM r.vmin OR v.vmax IS DISTINCT FROM r.vmax
                                         OR (v.vsum IS NULL) <> (r.vsum IS NULL)
                                         OR abs(v.vsum - r.vsum) > 1e-9 * greatest(1, abs(r.vsum))),
                       count(*)
                FROM v FULL JOIN r USING (point_uri, bucket)""").format(sql.Identifier(view)), {"a": a, "b": b}).fetchone()
            rep.check(f"{view} = raw fold, {label} window", bad == 0 and total > 0,
                      f"{a:%Y-%m-%d %H:%M}..{b:%H:%M} UTC, {total} buckets" + (f", {bad} differ" if bad else ""))


def check_compressed_write(conn: psycopg.Connection, rep: Report) -> None:
    """Re-insert an existing sample into the newest compressed chunk through
    the ingest path's upsert (retained-message replays do exactly this)."""
    from baler.timeseries import write_point_value

    chunk = _one(conn, "SELECT format('%I.%I', chunk_schema, chunk_name) FROM timescaledb_information.chunks "
                       "WHERE hypertable_schema = 'public' AND hypertable_name = 'point_history' AND is_compressed "
                       "ORDER BY range_start DESC LIMIT 1")
    if chunk is None:
        rep.check("write into a compressed chunk", True, "skipped: no compressed chunk")
        return
    row = conn.execute(f"SELECT point_uri, ts, value, value_text, value_bool, unit, label, quality FROM {chunk} "
                       "ORDER BY ts DESC LIMIT 1").fetchone()
    if row is None:
        rep.check("write into a compressed chunk", True, "skipped: newest compressed chunk is empty")
        return
    uri, ts, value, text, flag, unit, label, quality = row
    before = _one(conn, "SELECT count(*) FROM point_history WHERE point_uri = %s", (uri,))
    write_point_value(conn, uri, flag if flag is not None else value if value is not None else text, ts, unit, label, quality)
    after = conn.execute("SELECT count(*), max(value) FILTER (WHERE ts = %s) FROM point_history WHERE point_uri = %s",
                         (ts, uri)).fetchone()
    rep.check("write into a compressed chunk", after[0] == before and after[1] == value, f"{uri} @ {ts.isoformat()}")


def check_graph(oxigraph_url: str, path: str | None, rep: Report) -> None:
    if path is None:
        rep.check("graph", True, "skipped: no oxigraph backup")
        return
    want = turtle_triples(path)
    resp = requests.get(f"{oxigraph_url}/query", params={"query": "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }"},
                        headers={"Accept": "application/sparql-results+json"}, timeout=120)
    resp.raise_for_status()
    got = int(resp.json()["results"]["bindings"][0]["n"]["value"])
    rep.facts["triples"] = got
    rep.check("graph triples", got == want, f"backup {want}, restored {got}")


def render_cx_report(dsn: str, oxigraph_url: str) -> tuple[str, str, int] | None:
    """(run id, the run report's HTML, results) for the newest finished
    commissioning run, the way GET /cx/runs/{id}/report?format=html builds
    it (minus scope filtering and the issues log); None if there's no run."""
    from psycopg_pool import ConnectionPool

    from baler.commissioning import db, report
    from baler.remote_store import RemoteStore

    with psycopg.connect(dsn, autocommit=True) as conn:
        if not _one(conn, "SELECT to_regclass('cx_runs') IS NOT NULL"):
            return None
        rid = _one(conn, "SELECT id FROM cx_runs WHERE finished_at IS NOT NULL ORDER BY started_at DESC, id LIMIT 1")
    if rid is None:
        return None
    with ConnectionPool(dsn, min_size=1, max_size=2, kwargs={"autocommit": True}) as pool:
        repo = db.PgRepo(pool, RemoteStore(oxigraph_url))
        run = repo.get_run(rid)
        assert run is not None
        project = repo.get_project(run["project_id"])
        assert project is not None
        results = repo.get_results(rid)
        return rid, report.html(report.build(db.public_project(project), run, results)), len(results)


def check_cx_report(dsn: str, oxigraph_url: str, rep: Report) -> None:
    try:
        out = render_cx_report(dsn, oxigraph_url)
    except Exception as exc:  # noqa: BLE001 - any failure to render is the finding
        rep.check("Cx report renders", False, repr(exc))
        return
    if out is None:
        rep.check("Cx report renders", True, "skipped: no finished commissioning run")
        return
    rid, page, n = out
    rep.check("Cx report renders", page.startswith("<!doctype html>") or "<html" in page[:200],
              f"run {rid}: {n} results, {len(page)} bytes of HTML")


def run_checks(scratch: Scratch, pg_file: str | None, ox_file: str | None, window_hours: int, rep: Report) -> None:
    if pg_file:
        facts = DumpFacts(pg_file)
        sys.path.insert(0, HERE)
        from restore import dump_timescaledb_version

        dump_version = dump_timescaledb_version(pg_file)
        m = _STAMP.search(os.path.basename(pg_file))
        backup_at = datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc) if m else None
        with psycopg.connect(scratch.dsn, autocommit=True) as conn:
            check_tables(conn, facts, rep)  # first, before anything below writes
            if check_timescaledb(conn, dump_version, rep):
                hist = check_history(conn, facts, backup_at, rep)
                if hist and hist["newest"]:
                    check_rollups(conn, hist["newest"], window_hours, rep)
                    check_compressed_write(conn, rep)
        check_cx_report(scratch.dsn, scratch.oxigraph_url, rep)
    check_graph(scratch.oxigraph_url, ox_file, rep)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backup-dir", default=os.path.join(REPO, "backups"),
                        help="Where to find the newest backup when no file is named (default: backups/).")
    parser.add_argument("--postgres-file", help="Postgres backup to restore (default: the newest in --backup-dir).")
    parser.add_argument("--oxigraph-file", help="Oxigraph backup to restore (default: the newest in --backup-dir).")
    parser.add_argument("--project", default=DEFAULT_PROJECT,
                        help=f"Compose project and volume prefix of the scratch stack (default: {DEFAULT_PROJECT}).")
    parser.add_argument("--window-hours", type=int, default=6,
                        help="Hours of each window the continuous aggregates are refreshed and checked over (default: 6).")
    parser.add_argument("--report", help="Also write the report as JSON to this file.")
    parser.add_argument("--keep", action="store_true", help="Leave the scratch stack running afterwards (remove it with --down).")
    parser.add_argument("--down", action="store_true", help="Only remove a scratch stack left by --keep, then exit.")
    args = parser.parse_args()

    if args.down:
        Scratch(args.project).down()
        return
    pg_file = args.postgres_file or latest_backup(args.backup_dir, "postgres")
    ox_file = args.oxigraph_file or latest_backup(args.backup_dir, "oxigraph")
    if not pg_file and not ox_file:
        parser.error(f"no backups in {args.backup_dir}; pass --postgres-file/--oxigraph-file")

    started = datetime.now(timezone.utc)
    rep = Report()
    rep.facts.update({"postgres_file": pg_file, "oxigraph_file": ox_file, "project": args.project,
                      "started": started.isoformat()})
    print(f"restore drill: {pg_file or '-'} + {ox_file or '-'} -> scratch project {args.project}", flush=True)
    scratch = Scratch(args.project, DumpFacts(pg_file).owner if pg_file else None)
    try:
        scratch.up()
        restore = [sys.executable, os.path.join(HERE, "restore.py"), "--compose-project-dir", REPO,
                   "--oxigraph-url", scratch.oxigraph_url]
        restore += ["--postgres-file", pg_file] if pg_file else []
        restore += ["--oxigraph-file", ox_file] if ox_file else []
        t0 = time.monotonic()
        restored = subprocess.run(restore, env=scratch.env, check=False).returncode == 0
        rep.facts["restore_seconds"] = round(time.monotonic() - t0, 1)
        if rep.check("restore.py", restored, f"{rep.facts['restore_seconds']} s"):
            run_checks(scratch, pg_file, ox_file, args.window_hours, rep)
    finally:
        if args.keep:
            print(f"scratch stack kept: {getattr(scratch, 'dsn', '')} {getattr(scratch, 'oxigraph_url', '')} "
                  f"(remove: {sys.argv[0]} --down --project {args.project})")
        else:
            scratch.down()
    rep.facts["seconds"] = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
    print(f"restore drill {'PASSED' if rep.ok else 'FAILED'} in {rep.facts['seconds']} s", flush=True)
    if args.report:
        with open(args.report, "w") as f:
            json.dump({"ok": rep.ok, "checks": rep.checks, "facts": rep.facts}, f, indent=2, default=str)
    sys.exit(0 if rep.ok else 1)


if __name__ == "__main__":
    main()

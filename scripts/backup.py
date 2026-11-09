"""
One-shot backup of the two volumes that hold real state: postgres-data
(point_history/faults/checkpoints, via pg_dump through the running
postgres container) and oxigraph-data (the entity/ontology graph, via
the same bulk graph-store GET shacl_validate.graph_from_remote_store
already uses). grafana-data is regenerable dashboard config, deliberately
out of scope - see scripts/restore.py for the inverse.

Requires the postgres/oxigraph containers to be up (docker compose up -d
postgres oxigraph is enough, the full stack isn't required) - this
shells out to `docker compose exec` for pg_dump rather than requiring
pg_dump installed on the host.

Optional off-site copy (#276): with BALER_BACKUP_S3_BUCKET set, each
successful backup is then encrypted with age and uploaded to that
S3-compatible bucket, and old copies there are pruned (baler.backup_offsite;
needs `pip install -e '.[offsite]'`). A failed upload keeps the local files,
is reported, and exits 2. Every run reports to /ops/status (the live
database's service_heartbeats, row "backup") and backup-status.json in
--out-dir. See docs-site/pages/backups.mdx.
"""

import argparse
import datetime
import gzip
import os
import shutil
import socket
import subprocess
import sys

import requests

DEFAULT_OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "backups")


def _write_atomically(path: str, write) -> None:
    """Writes to <path>.partial and renames only once complete, so a failed
    or interrupted backup never leaves a truncated file that looks like the
    latest good one (the restore drill restores the newest file it finds)."""
    tmp = path + ".partial"
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def backup_postgres(out_dir: str, timestamp: str, compose_project_dir: str) -> str:
    """Plain-format pg_dump, streamed through gzip (never held in memory).
    The dump records the TimescaleDB version it came from, which
    scripts/restore.py reads back to restore into the same version (#241)."""
    path = os.path.join(out_dir, f"postgres-{timestamp}.sql.gz")

    def write(tmp: str) -> None:
        with gzip.open(tmp, "wb") as f:
            proc = subprocess.Popen(
                ["docker", "compose", "exec", "-T", "postgres", "sh", "-c", 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"'],
                cwd=compose_project_dir,
                stdout=subprocess.PIPE,
            )
            assert proc.stdout is not None
            shutil.copyfileobj(proc.stdout, f, 1 << 20)
            if proc.wait() != 0:
                raise subprocess.CalledProcessError(proc.returncode, proc.args)

    _write_atomically(path, write)
    return path


def backup_oxigraph(out_dir: str, timestamp: str, oxigraph_url: str) -> str:
    """The default graph only (entity data). The ontology named graph that
    RemoteStore.load_ontology writes is not in it: Turtle can't carry named
    graphs, and that graph is a copy of a published ontology file: load it
    again with load_ontology after a restore if you had loaded one."""
    path = os.path.join(out_dir, f"oxigraph-{timestamp}.ttl.gz")

    def write(tmp: str) -> None:
        with requests.get(f"{oxigraph_url}/store", params={"default": ""}, headers={"Accept": "text/turtle"}, stream=True) as resp:
            resp.raise_for_status()
            with gzip.open(tmp, "wb") as f:
                for chunk in resp.iter_content(1 << 20):
                    f.write(chunk)

    _write_atomically(path, write)
    return path


class _NoStatus:
    """Stand-in for baler.backup_status when baler isn't importable (a
    copy of this file run on its own, as nash-srv's pinned one is): the
    backup itself works exactly as before, just without reporting."""

    @staticmethod
    def now_iso() -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def previous(service, compose_project_dir):
        return None

    @staticmethod
    def record(service, detail, compose_project_dir) -> bool:
        return False

    @staticmethod
    def write_file(path, detail) -> None:
        return None

    @staticmethod
    def alert(event, detail) -> None:
        return None


def _baler():
    """(baler.backup_offsite, baler.backup_status), from an install or,
    failing that, from this checkout's src/; (None, _NoStatus) when neither
    is there. The off-site step also needs `pip install -e '.[offsite]'`."""
    try:
        from baler import backup_offsite, backup_status
    except ImportError:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
        try:
            from baler import backup_offsite, backup_status
        except ImportError:
            print("note: baler isn't importable from here: backing up without reporting to /ops/status "
                  "or an off-site copy", file=sys.stderr)
            return None, _NoStatus
    return backup_offsite, backup_status


def offsite_copy(offsite, out_dir: str, stamp: str, files: dict[str, str], env=os.environ) -> dict:
    """Upload this run's files and apply retention there. Never touches the
    local files. Returns the report's "offsite" section; raises nothing:
    a failure is in the section (ok False, error), message redacted."""
    report: dict = {"enabled": True, "ok": False}
    target = None
    try:
        target = offsite.target_from_env(env)
        report["target"] = target.describe()
        recipients = offsite.recipients_from_env(env)
        daily, weekly = offsite.retention_from_env(env)
        s3 = offsite.client(target)
        with offsite.WorkDir(out_dir) as work:
            manifest = offsite.upload_set(target, recipients, stamp, files, work, s3)
        report.update(ok=True, stamp=stamp, uploaded_at=manifest["uploaded_at"],
                      bytes=sum(f["bytes"] for f in manifest["files"].values()))
        print(f"off-site -> {target.describe()}{stamp}/ ({report['bytes']} bytes, encrypted)", flush=True)
        try:
            kept = offsite.prune(target, daily, weekly, s3)
            report.update(kept=len(kept["kept"]), removed=kept["removed"])
            if kept["removed"]:
                print(f"off-site retention ({daily} daily, {weekly} weekly): removed {', '.join(kept['removed'])}")
        except Exception as exc:  # noqa: BLE001 - the copy is up; a failed prune is reported, not fatal
            report["retention_error"] = offsite.redact(str(exc), target)
    except Exception as exc:  # noqa: BLE001 - any failure is the finding
        report["error"] = offsite.redact(str(exc) if isinstance(exc, offsite.OffsiteError)
                                         else f"{type(exc).__name__}: {exc}", target)
        print(f"OFF-SITE COPY FAILED (the local backup is kept): {report['error']}", file=sys.stderr, flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="Directory to write backup files into (default: backups/ at the repo root, gitignored).")
    parser.add_argument(
        "--oxigraph-url",
        default=os.environ.get("OXIGRAPH_URL", "http://localhost:7878"),
        help="Oxigraph's HTTP base URL (default: $OXIGRAPH_URL or http://localhost:7878, i.e. the docker-compose published port).",
    )
    parser.add_argument(
        "--compose-project-dir",
        default=os.path.join(os.path.dirname(__file__), ".."),
        help="Directory containing the docker-compose.yml to run `docker compose exec postgres pg_dump` against (default: the repo root).",
    )
    parser.add_argument("--prune", action="store_true",
                        help="Also apply the retention policy (BALER_BACKUP_KEEP_DAILY/_WEEKLY, default 7 daily and "
                             "4 weekly) to the local backups in --out-dir. The newest is always kept.")
    parser.add_argument("--no-offsite", action="store_true",
                        help="Skip the off-site copy even when BALER_BACKUP_S3_BUCKET is set.")
    parser.add_argument("--no-status", action="store_true",
                        help="Don't report the result to the database (/ops/status) or BALER_BACKUP_ALERT_COMMAND.")
    args = parser.parse_args()

    offsite, status = _baler()
    os.makedirs(args.out_dir, exist_ok=True)
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prev = {} if args.no_status else (status.previous("backup", args.compose_project_dir) or {})
    report: dict = {"ok": False, "stamp": timestamp, "started_at": status.now_iso(), "host": socket.gethostname(),
                    "last_ok_at": prev.get("last_ok_at"),
                    "offsite": {"enabled": False, "last_ok_at": (prev.get("offsite") or {}).get("last_ok_at")}}

    def finish(event: str | None) -> None:
        report["finished_at"] = status.now_iso()
        status.write_file(os.path.join(args.out_dir, "backup-status.json"), report)
        if not args.no_status:
            status.record("backup", report, args.compose_project_dir)
            if event:
                status.alert(event, report)

    files: dict[str, str] = {}
    try:
        files["postgres"] = backup_postgres(args.out_dir, timestamp, args.compose_project_dir)
        print(f"postgres -> {files['postgres']}")
        files["oxigraph"] = backup_oxigraph(args.out_dir, timestamp, args.oxigraph_url)
        print(f"oxigraph -> {files['oxigraph']}")
    except BaseException as exc:
        report["local"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finish("backup_failed")
        raise
    report["local"] = {"ok": True, "dir": os.path.abspath(args.out_dir),
                       "files": {k: {"name": os.path.basename(p), "bytes": os.path.getsize(p)} for k, p in files.items()}}
    report["last_ok_at"] = report["finished_at"] = status.now_iso()

    if args.prune and offsite is None:
        report["local"]["retention_error"] = "--prune needs baler importable (run from the checkout)"
    elif args.prune:
        try:
            daily, weekly = offsite.retention_from_env()
            pruned = offsite.prune_local(args.out_dir, daily, weekly)
            report["local"].update(kept=len(pruned["kept"]), removed=pruned["removed"])
            if pruned["removed"]:
                print(f"local retention ({daily} daily, {weekly} weekly): removed {', '.join(pruned['removed'])}")
        except Exception as exc:  # noqa: BLE001 - reported; the new backup stands
            report["local"]["retention_error"] = str(exc)

    if not args.no_offsite and os.environ.get("BALER_BACKUP_S3_BUCKET", "").strip() and offsite is None:
        report["offsite"] = {"enabled": True, "ok": False, "error": "baler isn't importable: run backup.py from the "
                             "checkout with the offsite extra installed"}
        print(f"OFF-SITE COPY FAILED (the local backup is kept): {report['offsite']['error']}", file=sys.stderr)
    elif not args.no_offsite and os.environ.get("BALER_BACKUP_S3_BUCKET", "").strip():
        report["offsite"] = {**offsite_copy(offsite, args.out_dir, timestamp, files),
                             "last_ok_at": report["offsite"]["last_ok_at"]}
        if report["offsite"]["ok"]:
            report["offsite"]["last_ok_at"] = status.now_iso()

    report["ok"] = not report["offsite"]["enabled"] or report["offsite"]["ok"]
    finish(None if report["ok"] else "offsite_upload_failed")
    # Non-zero so a timer/cron run shows as failed; the local files stay.
    sys.exit(0 if report["ok"] else 2)


if __name__ == "__main__":
    main()

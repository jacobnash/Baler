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
"""

import argparse
import datetime
import gzip
import os
import shutil
import subprocess

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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
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
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    pg_path = backup_postgres(args.out_dir, timestamp, args.compose_project_dir)
    print(f"postgres -> {pg_path}")

    ox_path = backup_oxigraph(args.out_dir, timestamp, args.oxigraph_url)
    print(f"oxigraph -> {ox_path}")


if __name__ == "__main__":
    main()

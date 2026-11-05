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
import subprocess

import requests

DEFAULT_OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "backups")


def backup_postgres(out_dir: str, timestamp: str, compose_project_dir: str) -> str:
    path = os.path.join(out_dir, f"postgres-{timestamp}.sql.gz")
    dump = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "sh", "-c", 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"'],
        cwd=compose_project_dir,
        check=True,
        capture_output=True,
    ).stdout
    with gzip.open(path, "wb") as f:
        f.write(dump)
    return path


def backup_oxigraph(out_dir: str, timestamp: str, oxigraph_url: str) -> str:
    path = os.path.join(out_dir, f"oxigraph-{timestamp}.ttl.gz")
    resp = requests.get(f"{oxigraph_url}/store", params={"default": ""}, headers={"Accept": "text/turtle"})
    resp.raise_for_status()
    with gzip.open(path, "wb") as f:
        f.write(resp.content)
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

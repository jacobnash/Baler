"""
Inverse of scripts/backup.py. Assumes a fresh/empty postgres+oxigraph
(disaster recovery, not a merge onto live data): pg_dump's plain-text
output has no `IF NOT EXISTS`/`--clean` DROP statements, so restoring
onto tables that already exist will error; Oxigraph's bulk POST /store
adds triples rather than replacing the graph, so restoring onto a
non-empty graph would merge instead of overwrite.
ponytail: no --clean/wipe-first flag - add one if restoring onto a live,
non-empty stack becomes a real (not disaster-recovery) use case.
"""

import argparse
import gzip
import os
import subprocess

import requests


def restore_postgres(path: str, compose_project_dir: str) -> None:
    with gzip.open(path, "rb") as f:
        sql = f.read()
    # -v ON_ERROR_STOP=1 - without it, psql prints errors to stderr but
    # keeps going and exits 0, so a restore onto tables that already exist
    # (the documented failure case) would look like it succeeded.
    subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "sh", "-c", 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" "$POSTGRES_DB"'],
        cwd=compose_project_dir,
        input=sql,
        check=True,
    )


def restore_oxigraph(path: str, oxigraph_url: str) -> None:
    """`?default` is required here for the same reason
    shacl_validate.graph_from_remote_store's GET needs it: under
    --union-default-graph (how docker-compose.yml runs Oxigraph), a bare
    POST /store lands triples somewhere queryable via a plain SPARQL
    SELECT but NOT via GET /store?default - so without this param,
    restore would "succeed" (201) while writing to a different place
    than backup.py's GET /store?default reads from, silently breaking
    the backup/restore round trip."""
    with gzip.open(path, "rb") as f:
        turtle = f.read()
    resp = requests.post(f"{oxigraph_url}/store", params={"default": ""}, data=turtle, headers={"Content-Type": "text/turtle"})
    resp.raise_for_status()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-file", help="e.g. backups/postgres-20260825T120000Z.sql.gz")
    parser.add_argument("--oxigraph-file", help="e.g. backups/oxigraph-20260825T120000Z.ttl.gz")
    parser.add_argument(
        "--oxigraph-url",
        default=os.environ.get("OXIGRAPH_URL", "http://localhost:7878"),
        help="Oxigraph's HTTP base URL (default: $OXIGRAPH_URL or http://localhost:7878, i.e. the docker-compose published port).",
    )
    parser.add_argument(
        "--compose-project-dir",
        default=os.path.join(os.path.dirname(__file__), ".."),
        help="Directory containing the docker-compose.yml to run `docker compose exec postgres ...` against (default: the repo root). "
        "Only needed to target a different compose project than this checkout's own.",
    )
    args = parser.parse_args()

    if not args.postgres_file and not args.oxigraph_file:
        parser.error("pass --postgres-file and/or --oxigraph-file")

    if args.postgres_file:
        restore_postgres(args.postgres_file, args.compose_project_dir)
        print(f"postgres restored from {args.postgres_file}")

    if args.oxigraph_file:
        restore_oxigraph(args.oxigraph_file, args.oxigraph_url)
        print(f"oxigraph restored from {args.oxigraph_file}")


if __name__ == "__main__":
    main()

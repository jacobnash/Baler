"""
Inverse of scripts/backup.py, for disaster recovery into a fresh stack:
bring up only postgres and oxigraph on new, empty volumes, then run this.
It refuses a Postgres database that already has tables in it (pg_dump's
plain output has no DROP/IF NOT EXISTS, so it could only fail halfway or
merge), and Oxigraph's bulk POST /store adds triples rather than replacing
the graph, so restoring onto a non-empty graph would merge.

TimescaleDB (#241): a dump of a database with hypertables only loads
between `timescaledb_pre_restore()` and `timescaledb_post_restore()`
(without them it stops at "could not find hypertable with id 1"), and only
into the same TimescaleDB extension version as the source. The dump itself
records that version (`_timescaledb_catalog.metadata`), so this reads it
from the file and, if the target's version differs, installs the dump's
version from the versions the image ships, restores, then updates the
extension to the image's own version - TimescaleDB's documented
procedure. Compressed chunks and continuous aggregates come back as they
were: the drill (scripts/restore_drill.py) checks both.
"""

import argparse
import gzip
import os
import re
import shutil
import subprocess
import sys

import requests

# -X: no ~/.psqlrc. ON_ERROR_STOP: without it psql prints errors and keeps
# going and exits 0, so a failed restore would look like it succeeded.
PSQL = 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" "$POSTGRES_DB"'
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


class RestoreError(SystemExit):
    """Exits non-zero with the message, as a failed restore must."""


def psql_query(sql: str, compose_project_dir: str) -> str:
    """One statement in its own session, through the postgres container's
    local socket (no password, no published port needed); unaligned output."""
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "sh", "-c", PSQL + ' -At -c "$1"', "sh", sql],
        cwd=compose_project_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def dump_timescaledb_version(path: str) -> str | None:
    """The TimescaleDB version the dump was taken from, read from the
    extension's own metadata table in it; None for a dump of a database
    without TimescaleDB. Streams, so a large dump isn't held in memory."""
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        in_block = False
        for line in f:
            if not in_block:
                in_block = line.startswith("COPY _timescaledb_catalog.metadata ")
                continue
            if line.startswith("\\."):
                return None
            key, _, rest = line.partition("\t")
            if key == "timescaledb_version":
                return rest.split("\t", 1)[0].strip()
    return None


def _check_empty(compose_project_dir: str) -> None:
    n = psql_query("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'", compose_project_dir)
    if n != "0":
        raise RestoreError(
            f"the target database already has {n} tables in its public schema: restore needs a fresh, empty one "
            "(stop the stack, move or remove its postgres volume, `docker compose up -d postgres oxigraph`, "
            "then run this again)"
        )


def _match_extension_version(want: str, compose_project_dir: str) -> str:
    """Make the (empty) target's TimescaleDB the dump's version; returns the
    version it had, to update back to after the load."""
    have = psql_query("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'", compose_project_dir)
    if have == want:
        return have
    if not _VERSION.match(want):
        raise RestoreError(f"unexpected TimescaleDB version {want!r} in the dump")
    shipped = psql_query(
        f"SELECT count(*) FROM pg_available_extension_versions WHERE name = 'timescaledb' AND version = '{want}'",
        compose_project_dir,
    )
    if shipped != "1":
        raise RestoreError(
            f"the dump is from TimescaleDB {want}, and this postgres image (TimescaleDB {have or 'not installed'}) "
            f"doesn't ship {want}: restore with an image that does, e.g. the one the backup was taken with"
        )
    print(f"target has TimescaleDB {have or '(none)'}, the dump is from {want}: installing {want} for the load")
    # Separate sessions: the loader has already loaded the old version's
    # library into a session that ran anything against the extension.
    psql_query("DROP EXTENSION IF EXISTS timescaledb CASCADE", compose_project_dir)
    psql_query(f"CREATE EXTENSION timescaledb VERSION '{want}'", compose_project_dir)
    return have


def restore_postgres(path: str, compose_project_dir: str, update_extension: bool = True) -> None:
    _check_empty(compose_project_dir)
    want = dump_timescaledb_version(path)
    have = _match_extension_version(want, compose_project_dir) if want else None
    # One psql session, streamed: the dump sets search_path to '' (hence the
    # schema-qualified calls), and gzip -> psql never holds the whole dump in
    # memory. -o /dev/null drops the per-statement results; errors still go
    # to stderr, and ON_ERROR_STOP keeps post_restore from running after one.
    proc = subprocess.Popen(
        ["docker", "compose", "exec", "-T", "postgres", "sh", "-c", PSQL + " -q -o /dev/null"],
        cwd=compose_project_dir,
        stdin=subprocess.PIPE,
    )
    assert proc.stdin is not None
    try:
        if want:
            proc.stdin.write(b"SELECT public.timescaledb_pre_restore();\n")
        with gzip.open(path, "rb") as f:
            shutil.copyfileobj(f, proc.stdin, 1 << 20)
        if want:
            proc.stdin.write(b"\nSELECT public.timescaledb_post_restore();\n")
        proc.stdin.close()
    except BrokenPipeError:
        pass  # psql stopped on an error; its exit code says so below
    if proc.wait() != 0:
        raise RestoreError(
            f"psql stopped with exit code {proc.returncode} (the error is above). The database is partly restored"
            + (" and still in TimescaleDB restore mode" if want else "")
            + ": remove its volume and restore again into a fresh one."
        )
    if want and psql_query("SHOW timescaledb.restoring", compose_project_dir) != "off":
        raise RestoreError("timescaledb_post_restore() ran but the database is still in restore mode")
    if want and have and have != want and update_extension:
        # A fresh session, before anything else touches the extension, as
        # ALTER EXTENSION ... UPDATE requires.
        psql_query("ALTER EXTENSION timescaledb UPDATE", compose_project_dir)
        print(f"TimescaleDB updated {want} -> "
              + psql_query("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'", compose_project_dir))


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
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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
        "COMPOSE_PROJECT_NAME/COMPOSE_FILE/COMPOSE_ENV_FILES in the environment pick another project, as for docker compose itself.",
    )
    parser.add_argument(
        "--no-extension-update",
        action="store_true",
        help="When the dump is from an older TimescaleDB than the image's, leave the restored database on the dump's version "
        "instead of running ALTER EXTENSION timescaledb UPDATE after the load.",
    )
    args = parser.parse_args()

    if not args.postgres_file and not args.oxigraph_file:
        parser.error("pass --postgres-file and/or --oxigraph-file")

    if args.postgres_file:
        restore_postgres(args.postgres_file, args.compose_project_dir, update_extension=not args.no_extension_update)
        print(f"postgres restored from {args.postgres_file}")

    if args.oxigraph_file:
        restore_oxigraph(args.oxigraph_file, args.oxigraph_url)
        print(f"oxigraph restored from {args.oxigraph_file}")
    sys.stdout.flush()


if __name__ == "__main__":
    main()

"""
One-shot, idempotent migrations, run by docker-compose's `migrate` service
before any daemon starts (every daemon `depends_on` it completing):

  1. Postgres: baler.schema's numbered schema migrations (#194), whatever
     is pending, under their lock. On a current database it does nothing.
  2. Oxigraph: the Timberdoodle-to-Baler graph rewrite below.

So a deploy brings its own schema and fixes its own stored data. On data
that's already migrated - or on a fresh install - each step finds nothing
to do and exits 0.

What the graph step migrates, and only this:

  - Oxigraph default graph: every IRI (subject, predicate or object) that
    starts with `urn:timberdoodle:` is rewritten to `urn:baler:`. That's
    Baler's own vocabularies (td:, haystack:, proj:) as ingest wrote them.
    Point and equip ids (`urn:point:...`, `urn:equip:...`) never carried
    the product name and are untouched, so no history is re-keyed.
  - The named ontology graph `urn:timberdoodle:ontology` is moved to
    `urn:baler:ontology`, unless the new graph already has data (then the
    old one is left alone for a human to look at, never merged).

No Postgres value names the product, so the rename needs nothing there: the
role and database keep their old names on migrated installs via
BALER_PG_USER / BALER_PG_DB (see deploy/MIGRATING-TO-BALER.md), and the
schema migrations connect through BALER_PG_DSN built from them.

    python -m baler.migrate          # what the compose service runs
"""

from __future__ import annotations

import sys
import time

from baler import tracing

tracer = tracing.get_tracer(__name__)

OLD = "urn:timberdoodle:"
NEW = "urn:baler:"
OLD_ONTOLOGY_GRAPH = OLD + "ontology"
NEW_ONTOLOGY_GRAPH = NEW + "ontology"


def _renamed(var: str) -> str:
    # isIRI(...) first: `&&` short-circuits on false, so STR() is never asked
    # of a blank node, and a bnode/literal term passes through unchanged
    # instead of erroring out of the BIND (which would leave the variable
    # unbound and silently drop the triple from the INSERT).
    return (
        f'IF(isIRI({var}) && STRSTARTS(STR({var}), "{OLD}"), '
        f'IRI(CONCAT("{NEW}", STRAFTER(STR({var}), "{OLD}"))), {var})'
    )


REWRITE_DEFAULT_GRAPH = f"""
DELETE {{ ?s ?p ?o }}
INSERT {{ ?s2 ?p2 ?o2 }}
WHERE {{
  ?s ?p ?o .
  FILTER(
    (isIRI(?s) && STRSTARTS(STR(?s), "{OLD}")) ||
    STRSTARTS(STR(?p), "{OLD}") ||
    (isIRI(?o) && STRSTARTS(STR(?o), "{OLD}"))
  )
  BIND({_renamed("?s")} AS ?s2)
  BIND({_renamed("?p")} AS ?p2)
  BIND({_renamed("?o")} AS ?o2)
}}
"""

COUNT_OLD = f"""
SELECT (COUNT(*) AS ?n) WHERE {{
  ?s ?p ?o .
  FILTER(
    (isIRI(?s) && STRSTARTS(STR(?s), "{OLD}")) ||
    STRSTARTS(STR(?p), "{OLD}") ||
    (isIRI(?o) && STRSTARTS(STR(?o), "{OLD}"))
  )
}}
"""


def _count(store, sparql: str) -> int:
    rows = list(store.query(sparql))
    return int(getattr(rows[0], "n", 0)) if rows else 0


def _graph_size(store, graph: str) -> int:
    return _count(store, f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{graph}> {{ ?s ?p ?o }} }}")


def migrate_graph(store) -> dict:
    """Returns what it did: {"rewritten": int, "ontology": "moved"|"absent"|"kept-both"}."""
    out: dict[str, int | str] = {}
    before = _count(store, COUNT_OLD)
    if before:
        store.update(REWRITE_DEFAULT_GRAPH)
    out["rewritten"] = before

    if not getattr(store, "supports_named_graphs", True):
        out["ontology"] = "absent"
        return out
    old_n = _graph_size(store, OLD_ONTOLOGY_GRAPH)
    if not old_n:
        out["ontology"] = "absent"
    elif _graph_size(store, NEW_ONTOLOGY_GRAPH):
        out["ontology"] = "kept-both"
    else:
        store.update(f"MOVE SILENT GRAPH <{OLD_ONTOLOGY_GRAPH}> TO <{NEW_ONTOLOGY_GRAPH}>")
        out["ontology"] = "moved"
    return out


def _wait_for(store, attempts: int = 60) -> None:
    """Oxigraph has no healthcheck in compose (its image has no shell to
    run one in), so `service_started` can win the race with its HTTP port."""
    for i in range(attempts):
        try:
            store.query("SELECT ?s WHERE { ?s ?p ?o } LIMIT 1")
            return
        except Exception:  # any connection error just means "not up yet"
            if i == attempts - 1:
                raise
            time.sleep(1)


def _wait_for_postgres(dsn: str, attempts: int = 60, connect=None) -> None:
    """compose's postgres healthcheck (pg_isready) can pass during the
    image's first-start init, which then restarts the server: a connection
    right after can be refused. Retry until a real query works."""
    import psycopg

    connect = connect or psycopg.connect
    for i in range(attempts):
        try:
            with connect(dsn, autocommit=True, connect_timeout=5) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.OperationalError:
            if i == attempts - 1:
                raise
            time.sleep(1)


def main() -> int:
    from baler import schema, timeseries
    from baler.remote_store import RemoteStore

    tracing.init_tracing("baler-migrate")
    with tracer.start_as_current_span("migrate.schema"):
        _wait_for_postgres(timeseries.DEFAULT_DSN)
        schema.main(["upgrade"])
    store = RemoteStore()
    with tracer.start_as_current_span("migrate.timberdoodle_to_baler") as span:
        _wait_for(store)
        result = migrate_graph(store)
        span.set_attribute("rewritten", result["rewritten"])
        span.set_attribute("ontology", result["ontology"])
    print(f"migrate: rewrote {result['rewritten']} triple(s) urn:timberdoodle: -> urn:baler:; "
          f"ontology graph: {result['ontology']}")
    if result["ontology"] == "kept-both":
        print(f"migrate: WARNING both <{OLD_ONTOLOGY_GRAPH}> and <{NEW_ONTOLOGY_GRAPH}> have data - "
              "left the old one in place; drop it once you've checked it", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

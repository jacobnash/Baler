"""
One-shot sweep: find every point/equip entity with Haystack tags but no
Brick class yet, run the rules on each, then the model step
(model_step.run) on what the rules couldn't place. Not a daemon - run it
once after a batch of ingest.

The model step asks the configured model (BALER_LLM_PROVIDER, see
llm_classifier) twice per naming pattern and writes a class only when both
asks agree and the unit veto passes. Everything else becomes a mapping
proposal in Postgres (mapping_proposals) for a person to decide. With
--no-llm, or BALER_LLM_PROVIDER=none, the proposals still carry the
deterministic candidates.

    python -m baler.autotag                 # rules, model, proposals
    python -m baler.autotag --no-llm        # rules and proposals only
    python -m baler.autotag --no-proposals  # don't touch Postgres
"""

import argparse
import os
import time

import requests
from rdflib import URIRef

from baler import llm_classifier, mapping, model_step, tracing
from baler.commissioning import llm
from baler.remote_store import RemoteStore
from baler.store import BRICK, TD

tracer = tracing.get_tracer(__name__)

# A batch sweep over hundreds of independent HTTP writes shouldn't die on
# one transient failure - confirmed live against a busy Oxigraph instance
# (several other processes writing concurrently) that an isolated retry of
# the exact same request succeeds; this is a load/timing hiccup on
# Oxigraph's HTTP layer, not a bad request.
_MAX_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 0.5

POINT_RULES_PATH = mapping.DEFAULT_RULES_PATH
EQUIP_RULES_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "rules", "haystack_equip_to_brick.yaml")


def find_unclassified(store) -> list[URIRef]:
    """Points and equip with no rdf:type in the Brick namespace - covers
    both the "miss" (TD.UnmappedPoint) and "fallback" (PROJ:*) outcomes
    classify_point already produced. A point counts once it has a marker
    tag or a reading (TD.RawPoint), and an equip once it has a marker tag
    or a point: a BACnet `AI-17` with only a unit, or a Windrow equip with
    no markers, is exactly what the model step is for, and used to be
    skipped for having no hasTag triple (#182).

    URIRef, not bare str - RemoteStore treats a plain Python str as a
    Literal, not a URI (see remote_store._to_sparql_term), so every write
    for one of these would silently corrupt the subject position and 400.
    Confirmed live: this was dropping ~99% of a real sweep on the floor
    before being caught here."""
    rows = store.query(f"""
        PREFIX haystack: <urn:baler:haystack#>
        SELECT DISTINCT ?entity WHERE {{
            {{ ?entity haystack:hasTag ?tag }}
            UNION {{ ?entity a <{TD.RawPoint}> }}
            UNION {{ ?entity <{BRICK.hasPoint}> ?point }}
            FILTER NOT EXISTS {{
                ?entity a ?type .
                FILTER(STRSTARTS(STR(?type), "https://brickschema.org/schema/Brick#"))
            }}
        }}
    """)
    return [URIRef(row.entity) for row in rows]


def _kind(uri) -> str:
    return "equip" if str(uri).startswith("urn:equip:") else "point"


def _with_retries(what: str, fn):
    """fn() up to _MAX_ATTEMPTS times on the transient failures above, and
    on a model reply that can't be read. None once attempts run out, so
    the sweep reports and skips rather than aborting."""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return fn()
        except (
            requests.exceptions.HTTPError,
            requests.exceptions.ConnectionError,
            llm_classifier.LLMClassificationError,
            llm.LLMError,
        ) as exc:
            if attempt == _MAX_ATTEMPTS:
                print(f"{what}: FAILED after {_MAX_ATTEMPTS} attempts ({exc}) - skipping")
            else:
                time.sleep(_RETRY_DELAY_SECONDS)
    return None


class _RetryingModel:
    def __init__(self, model):
        self.model = model
        self.name = model.name

    def ask(self, prompt: str) -> dict:
        reply = _with_retries("model ask", lambda: self.model.ask(prompt))
        return reply if reply is not None else {}


def run(store, model=None, proposals=None, stats=None, site_rules=None, recall=None, conventions=None) -> list[model_step.Decision]:
    point_rules = mapping.load_rules(POINT_RULES_PATH)
    equip_rules = mapping.load_rules(EQUIP_RULES_PATH)

    pending = find_unclassified(store)
    # Rung 2, before the rules: site naming conventions a person wrote (#186).
    if conventions:
        from baler import conventions as conv_mod

        settled = conv_mod.run_ladder(conventions, store, [str(u) for u in pending])
        for u in sorted(settled):
            print(f"{u}: convention")
        pending = [u for u in pending if str(u) not in settled]

    unresolved = []
    for uri in pending:
        rules = equip_rules if _kind(uri) == "equip" else point_rules
        result = _with_retries(str(uri), lambda uri=uri, rules=rules: mapping.classify_point(store, uri, rules))
        if result is None:
            continue
        outcome, brick_class = result
        if outcome == "direct":
            print(f"{uri}: direct -> {brick_class}")
        else:
            unresolved.append((uri, _kind(uri)))

    model = _RetryingModel(model) if model is not None else None
    decisions = model_step.run(store, unresolved, model, proposals=proposals, stats=stats, site_rules=site_rules, recall=recall)
    for d in decisions:
        if d.accepted:
            print(f"{d.uri}: model -> {d.accepted}")
        else:
            top = ", ".join(c["brick_class"] for c in d.candidates) or "no candidates"
            print(f"{d.uri}: proposed ({top}) - {d.refused_because}")
    return decisions


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-llm", action="store_true", help="rules and proposals only, skip the model")
    parser.add_argument("--no-proposals", action="store_true", help="don't write mapping proposals (no Postgres needed)")
    args = parser.parse_args()

    tracing.init_tracing("baler-autotag")
    store = RemoteStore()

    model = None if args.no_llm else llm_classifier.model_from_env()
    proposals = stats = site_rules = recall = convs = None
    if not args.no_proposals:
        from baler import mapping_proposals, timeseries

        conn = timeseries.connect()
        proposals = mapping_proposals.PgProposals(conn)
        proposals.ensure_schema()
        stats = model_step.pg_stats(conn)
        site_rules = proposals.site_rules()
        from baler import memory

        memory.ensure_schema(conn)
        mem = memory.load(conn)
        print(f"memory: {len(mem.examples)} confirmed example(s)")
        recall = memory.recall_for(mem, memory.pg_locator(conn)) if mem.examples else None
        from baler import conventions as conv_mod

        conv_mod.ensure_schema(conn)
        convs = conv_mod.load_all(conn)
    run(store, model=model, proposals=proposals, stats=stats, site_rules=site_rules, recall=recall, conventions=convs)


if __name__ == "__main__":
    main()

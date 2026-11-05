"""
One-shot sweep: find every point/equip entity with Haystack tags but no
direct Brick classification yet, and run classify_point_with_fallback on
each (rule engine first, LLM fallback second). Not a daemon - run it once
after a batch of ingest, same shape as derivation_engine.py's --dry-run
precedent for a runnable one-off tool.

Requires ANTHROPIC_API_KEY (see llm_classifier.classify_with_llm) unless
run with --no-llm, in which case it's just a report of what the rule
engine alone couldn't place.
"""

import argparse
import os
import time

import requests
from rdflib import URIRef

from baler import llm_classifier, mapping, tracing
from baler.remote_store import RemoteStore

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
    """Entities with at least one hasTag triple but no rdf:type in the
    Brick namespace - covers both the "miss" (TD.UnmappedPoint) and
    "fallback" (PROJ:*) outcomes classify_point already produced, points
    and equip alike (both are tagged the same way, see ingest.py).

    URIRef, not bare str - RemoteStore treats a plain Python str as a
    Literal, not a URI (see remote_store._to_sparql_term), so every write
    classify_point_with_fallback makes for one of these would silently
    corrupt the subject position and 400. Confirmed live: this was
    dropping ~99% of a real sweep on the floor before being caught here."""
    rows = store.query("""
        PREFIX haystack: <urn:baler:haystack#>
        SELECT DISTINCT ?entity WHERE {
            ?entity haystack:hasTag ?tag .
            FILTER NOT EXISTS {
                ?entity a ?type .
                FILTER(STRSTARTS(STR(?type), "https://brickschema.org/schema/Brick#"))
            }
        }
    """)
    return [URIRef(row.entity) for row in rows]


def _classify_with_retries(store, uri, rules, llm_classify):
    """Retries classify_point_with_fallback up to _MAX_ATTEMPTS times on
    the transient failures _MAX_ATTEMPTS's own comment explains (a load/
    timing hiccup, not a bad request). Returns None once attempts are
    exhausted instead of raising, so run() reports and skips that one
    entity rather than aborting the whole sweep."""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return mapping.classify_point_with_fallback(store, uri, rules=rules, llm_classify=llm_classify)
        except (
            requests.exceptions.HTTPError,
            requests.exceptions.ConnectionError,
            llm_classifier.LLMClassificationError,
        ) as exc:
            if attempt == _MAX_ATTEMPTS:
                print(f"{uri}: FAILED after {_MAX_ATTEMPTS} attempts ({exc}) - skipping")
            else:
                time.sleep(_RETRY_DELAY_SECONDS)
    return None


def _propose_rule_for_llm_classification(store, uri, brick_class: str, rules_path: str) -> None:
    tags = mapping.read_tags(store, uri)
    if llm_classifier.propose_rule(rules_path, tags, brick_class):
        print(f"  proposed new rule for {sorted(tags)} -> {brick_class} in {rules_path}")


def _rules_for_entity(uri, point_rules, equip_rules, known_point_classes, known_equip_classes):
    """Points and equip share the same tagging shape (see ingest.py) but
    each has its own rule file/known-class-set - the only thing that
    differs by uri's own urn:equip: vs urn:point: prefix."""
    if uri.startswith("urn:equip:"):
        return equip_rules, EQUIP_RULES_PATH, known_equip_classes
    return point_rules, POINT_RULES_PATH, known_point_classes


def run(store, llm_classify=None, propose_rules: bool = True) -> None:
    point_rules = mapping.load_rules(POINT_RULES_PATH)
    equip_rules = mapping.load_rules(EQUIP_RULES_PATH)
    known_point_classes = {r["brick_class"] for r in point_rules}
    known_equip_classes = {r["brick_class"] for r in equip_rules}

    for uri in find_unclassified(store):
        rules, rules_path, known_classes = _rules_for_entity(uri, point_rules, equip_rules, known_point_classes, known_equip_classes)

        result = _classify_with_retries(store, uri, rules, llm_classify)
        if result is None:
            continue
        outcome, brick_class = result
        print(f"{uri}: {outcome}" + (f" -> {brick_class}" if brick_class else ""))

        if propose_rules and outcome == "llm" and brick_class in known_classes:
            _propose_rule_for_llm_classification(store, uri, brick_class, rules_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-llm", action="store_true", help="rule engine only, skip the LLM fallback")
    parser.add_argument("--no-propose-rules", action="store_true", help="skip appending llm-proposed rule entries")
    args = parser.parse_args()

    tracing.init_tracing("baler-autotag")
    store = RemoteStore()

    llm_classify = None if args.no_llm else llm_classifier.classify_with_llm
    run(store, llm_classify=llm_classify, propose_rules=not args.no_propose_rules)


if __name__ == "__main__":
    main()

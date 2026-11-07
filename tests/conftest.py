"""
Shared setup for the OpenAPI contract tests (test_*_openapi.py) - see
PLAN.md Stage 1.3. AUDIT.md's jscpd pass found this exact
schema-validation helper duplicated verbatim across files; extracted
here so a future OpenAPI-contract-test file doesn't have to copy it a
sixth time. Not a place for per-service fixtures (live_server, DB pool
setup, etc.) - each service's handler takes different constructor args,
and forcing those into one shared fixture would trade a small amount of
duplication for real coupling between otherwise-independent test files.
"""

import yaml
from jsonschema.validators import validator_for
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012


def load_spec(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def assert_matches_schema(instance, schema: dict, spec: dict) -> None:
    resource = Resource.from_contents(spec, default_specification=DRAFT202012)
    registry = Registry().with_resource(uri="spec", resource=resource)
    if "$ref" in schema and schema["$ref"].startswith("#"):
        schema = {"$ref": f"spec{schema['$ref']}"}
    validator_cls = validator_for(schema)
    validator_cls(schema, registry=registry).validate(instance)


# --- shared-DB hygiene for integration tests ---------------------------------
#
# Site scoping (#16) switches on for the whole database the moment any site
# has a topic prefix: from then on a caller with no token sees nothing. Many
# integration tests read without a token, so one test that leaves a prefixed
# site behind silently empties every later test's results (CI's integration
# job was red from fd6d208 to the fix for exactly this). After each
# integration test, sites it created keep no prefixes, with a warning naming
# the test, so a leak can't spread.

import warnings

import pytest


def _site_ids(conn) -> set[str] | None:
    try:
        return {r[0] for r in conn.execute("SELECT id FROM sites").fetchall()}
    except Exception:  # noqa: BLE001 - no sites table yet, or no DB: nothing to guard
        return None


@pytest.fixture(autouse=True)
def _no_leaked_site_prefixes(request):
    if request.node.get_closest_marker("integration") is None:
        yield
        return
    try:
        from baler.timeseries import connect

        conn = connect()
    except Exception:  # noqa: BLE001 - DB unreachable: the test itself will say so
        yield
        return
    before = _site_ids(conn)
    try:
        yield
    finally:
        try:
            if before is not None:
                leaked = conn.execute(
                    "UPDATE sites SET topic_prefixes = '{}' WHERE cardinality(topic_prefixes) > 0 "
                    "AND NOT (id = ANY(%s)) RETURNING name",
                    (list(before),),
                ).fetchall()
                if leaked:
                    warnings.warn(
                        f"{request.node.nodeid} left site(s) {[r[0] for r in leaked]} with topic prefixes; "
                        "cleared them so later tests aren't scoped (clean up in the test's teardown)",
                        stacklevel=1,
                    )
        except Exception as exc:  # noqa: BLE001 - the guard must never fail a test itself
            warnings.warn(f"site-prefix guard skipped: {exc!r}", stacklevel=1)
        conn.close()


# --- CI sharding (#27) ---------------------------------------------------------
# BALER_SHARD=i/n keeps only shard i of n (whole files, so module-scoped
# fixtures stay together), so n CI jobs - each with its own stack - split
# the integration suite. Shards are balanced by estimated duration.


# Files much slower than their test count suggests (seconds, from CI's
# --durations): they're placed first so the shards come out even. Every
# other file counts SECONDS_PER_TEST per test.
SLOW_FILES = {
    "tests/test_gateway_auth.py": 50.0,  # waits out the gateway's 30 s revocation poll
    "tests/test_haystack_brick_e2e.py": 40.0,
    "tests/test_broker_accounts.py": 8.0,
}
SECONDS_PER_TEST = 0.4


def _shard_files(counts: dict[str, int], total: int) -> list[set[str]]:
    """Greedy: heaviest file first, each into the currently lightest shard."""
    weight = {f: SLOW_FILES.get(f, n * SECONDS_PER_TEST) for f, n in counts.items()}
    shards: list[set[str]] = [set() for _ in range(total)]
    load = [0.0] * total
    for f in sorted(weight, key=lambda f: (-weight[f], f)):
        i = load.index(min(load))
        shards[i].add(f)
        load[i] += weight[f]
    return shards


def pytest_collection_modifyitems(config, items):
    import os
    from collections import Counter

    spec = os.environ.get("BALER_SHARD", "").strip()
    if not spec:
        return
    index, total = (int(x) for x in spec.split("/"))
    counts = Counter(item.nodeid.split("::")[0] for item in items)
    mine = _shard_files(dict(counts), total)[index - 1]
    keep = [item for item in items if item.nodeid.split("::")[0] in mine]
    dropped = [item for item in items if item.nodeid.split("::")[0] not in mine]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = keep

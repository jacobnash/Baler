# SHACL validation against Brick's own shapes

**Status: implemented (2026-08-25)**, per the design below — `ontology/Brick-only.ttl`,
`src/baler/shacl_validate.py`, `src/baler/validate_api.py` (port 8005),
`tests/test_shacl_validate.py`, `tests/test_validate_openapi.py`. Kept as the design record;
not deleted post-implementation. Originally saved here instead of relying on Claude's
per-conversation plan-file slot (`~/.claude/plans/*.md`), which gets overwritten on the next
unrelated planning session — this was meant to survive that, and did.

## Context

A survey of github.com/BrickSchema (prompted by noticing "a lot of good things" in that org)
found one concrete, well-scoped capability gap worth closing: baler has no structural
validation of its RDF graph today. Brick itself moved to a SHACL-based ontology definition (its
release `.ttl` files embed `sh:NodeShape`/`sh:PropertyShape` declarations alongside the class
hierarchy), so `pyshacl` can validate any graph typed with Brick classes directly against Brick's
own real constraints — without adopting the full `brickschema` Python package (which requires
Python ≥3.11 vs. this project's ≥3.10 floor, and pulls in `pyshacl`+`owlrl`+`pyontoenv` plus
optional Docker/Rust reasoners as a bundle). This keeps the existing "ontology-agnostic,
hand-verify everything, minimal dependencies" design (`store.py`, `mapping.py`) intact while
adding a real, independent check: does the hand-curated `rules/haystack_to_brick.yaml`/
`rules/haystack_equip_to_brick.yaml` classification actually produce structurally valid Brick
instances, per Brick's own authority, not just per manual review?

Confirmed via direct research against the real v1.4.4 release (latest stable tag): the release
ships `Brick.ttl` (~1.7MB), `Brick-only.ttl` (~1.5MB), `Brick+extensions.ttl` (~2.8MB),
`Brick+imports.ttl` (~7.5MB) — all Turtle, SHACL shapes embedded inline in all of them (confirmed
via fetching `Brick-only.ttl`'s raw content: `sh:NodeShape` appears 100+ times, essentially one
per class), **no separate shapes-only asset exists**. `Brick-only.ttl` is the right one to vendor:
same embedded shapes as the others, without `+extensions`/`+imports` bloat this project doesn't
need.

## Design

1. **Vendor `Brick-only.ttl` into the repo**, pinned at v1.4.4, from
   `https://github.com/BrickSchema/Brick/releases/download/v1.4.4/Brick-only.ttl`. New top-level
   `ontology/` directory (sibling to `rules/`/`src/`/`tests/`, matching the project's existing
   one-top-level-dir-per-artifact-kind convention) — this is ontology *data*, not a hand-curated
   rule file (`rules/`) or a trimmed test fixture (`tests/fixtures/`), and unlike both of those it
   needs to be loadable by production code at runtime, not just tests.
2. **New optional dependency group** in `pyproject.toml`: `validate = ["pyshacl>=0.30.0"]` —
   matching the existing `[project.optional-dependencies]` pattern (`dev`, `llm`) rather than a
   core dependency, since not every process needs validation capability.
3. **New library module `src/baler/shacl_validate.py`** (no CLI of its own — the service
   below is the "run this" surface):
   - `load_shapes(path=DEFAULT_SHAPES_PATH) -> Graph` — parses the vendored
     `ontology/Brick-only.ttl` once.
   - `graph_from_remote_store(store: RemoteStore) -> Graph` — bulk-fetches the live Oxigraph
     default graph as Turtle (`GET {base_url}/store` with `Accept: text/turtle` — the same
     graph-store-protocol endpoint `RemoteStore.load_ontology` already POSTs to for bulk loads,
     used here in reverse) and parses it into an in-memory `rdflib.Graph`, since
     `pyshacl.validate()` needs an in-memory graph to check, not a live SPARQL endpoint. For the
     in-memory `Store`, its `.graph` attribute is already usable directly — no helper needed.
   - `validate_graph(data_graph, shapes_graph=None) -> dict` — wraps
     `pyshacl.validate(data_graph, shacl_graph=shapes_graph or load_shapes())`, returns
     `{"conforms": bool, "violations": [{"focus_node": ..., "message": ..., "severity": ...}, ...]}`
     parsed from pyshacl's results graph, not pyshacl's raw text report.
4. **New service `src/baler/validate_api.py`** — its own standalone process/port, matching
   `ingest_api.py`/`fault_api.py`/`derivation_api.py`'s exact shape (stdlib `http.server`,
   `ThreadingHTTPServer`, OTel span per request, spec served straight off disk, `docs_ui.serve`
   for `/docs`). Decided to be a real service, not just a CLI tool — it's the remotely-triggerable
   surface (CI hitting an HTTP endpoint, a future UI button) that a pure shell tool wouldn't give.
   - `POST /validate` — calls `graph_from_remote_store` + `validate_graph` against the *current
     live* Oxigraph state (no request body needed — always validates live state, not an arbitrary
     posted graph; keeps v1 simple), returns `{"conforms": bool, "violations": [...]}`. `200`
     either way (conformance is data in the response, not an error status) — matching how
     `derivation_api.py`'s `/dry-run` returns `200` with a trace regardless of what the trace shows.
   - `GET /openapi.yaml`, `GET /docs` — same as every other API here.
   - New port **`8005`** (8000 = ingest, 8002 = fault, 8003 = derivation, 8004 = the demo UI's
     static file server per the README — check this is still accurate/free before implementing,
     given other work has landed in this repo since this was written).
   - `validate_api.py`'s `main()` is the only process entry point — no separate ad-hoc CLI script.
5. **New test `tests/test_shacl_validate.py`** (no `@pytest.mark.integration` — pure in-memory
   rdflib, no live Postgres/Oxigraph needed, so it runs in the fast default suite):
   - A deliberately-broken synthetic graph (e.g. an entity typed into a Brick class while missing
     a property Brick's shapes require) confirming `validate_graph` reports `conforms: False` with
     a specific violation — proves the mechanism actually catches something, not just "runs
     without crashing."
   - **The real regression value**: build a small graph via the actual `mapping.classify_point()`
     rule engine against a few real tag-sets from `rules/haystack_to_brick.yaml` (same idiom
     `test_mapping.py` already uses), then validate that output against the real vendored Brick
     shapes. This checks, via Brick's own independent authority rather than manual review, whether
     the hand-curated classification rules actually produce structurally valid Brick instances —
     a genuinely new safety net that didn't exist before, catching a class of mistake (e.g. a rule
     assigning a class that requires a property the ingested data doesn't carry) that pure "does
     the class name exist in the ontology" checking (what `test_mapping.py` already does) can't
     catch.

**Note on the growing number of services** (ingest, fault, derivation, now validate — plus the
existing demo static UI on :8004): these should eventually converge into one UI rather than stay
as separately-browsed APIs forever. Nothing in this design needs to change to accommodate that
later — every one of these services already serves a real `GET /openapi.yaml` + `GET /docs`
(Swagger UI), which is exactly the uniform surface a future unified dashboard would consume to
talk to all of them. Building that unified UI is a separate, bigger effort — not part of this.

## Explicitly out of scope for v1

- **Validating `derivation_engine.py`'s own output** against Brick shapes — a derivation's output
  point is typed either with a user-supplied `brick_class` (arbitrary, not meaningfully checkable
  in general) or `TD.ComputedPoint` (a project-internal type outside Brick's namespace entirely,
  so Brick's shapes don't apply to it). Not a clean fit for this feature; skip rather than force it.
- **The full `brickschema` package, `owlrl` reasoning, or the `topquadrant`/`allegro` alternate
  SHACL engines** — `pyshacl`'s default engine is sufficient; nothing here needs OWL inference or
  an alternate reasoner.
- **Auto-updating the vendored file** — `ontology/Brick-only.ttl` is pinned, updated manually
  if/when the project bumps its Brick version (same "hand-vendor, verify, commit" pattern as
  `tests/fixtures/brick_subset.ttl`, not an auto-fetch-on-install step).
- **Accepting an arbitrary posted graph to validate** (vs. always validating current live state) —
  keeps `POST /validate`'s request shape trivial for v1; add if a real need for validating a
  candidate/draft graph before it goes live shows up.

## Files to create/modify

- `ontology/Brick-only.ttl` (new, vendored, ~1.5MB, pinned v1.4.4) + `ontology/README.md` (new,
  small — source URL, version, date vendored, BSD-3-Clause license note per Brick's upstream
  `LICENSE` file, and why `Brick-only.ttl` specifically was chosen over the other three release
  variants).
- `pyproject.toml` (modified) — add the `validate` optional-dependency group.
- `src/baler/shacl_validate.py` (new).
- `src/baler/validate_api.py` (new).
- `validate-api-openapi.yaml` (new, repo root, matching `fault-api-openapi.yaml`/
  `derivation-api-openapi.yaml`'s placement).
- `tests/test_shacl_validate.py` (new).
- `README.md` — add a `validate_api` row to the process table, matching the existing
  `derivation_api`/`derivation_engine` rows.

No changes needed to `store.py`/`remote_store.py`/`mapping.py` — this reuses their existing public
interfaces (`Store.graph`, `RemoteStore`'s base URL, `mapping.classify_point`) without modification.

## Verification (when this gets picked up)

- `pip install -e ".[validate,dev]"` then `pytest tests/test_shacl_validate.py -v` — both the
  deliberately-broken-graph case and the real-rules-vs-real-shapes regression case should pass
  (the second one only *proves* something if it currently conforms; if it doesn't, that's a
  genuine finding about the existing hand-curated rules worth surfacing, not a reason to weaken
  the test).
- Start `validate_api.py` for real, `POST /validate` against the live `docker-compose`-managed
  Oxigraph (with some real classified data in it, e.g. after running the existing test suite or
  `autotag.py`) — confirm it reports a sensible conforms/violates result.
- Full `pytest` run to confirm no regressions elsewhere.
- **Before starting implementation**: re-check port 8005 is still free and the `Files` list above
  still matches reality — other work has been landing in this repo concurrently (a `gateway/` and
  `ui/` directory and a `Dockerfile` appeared since this was written, none of which were part of
  this plan's own scope).

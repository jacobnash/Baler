# Baler — Remediation Plan

Companion to `AUDIT.md` — read that first for evidence and rationale. This plan turns its findings into staged, incremental work. No stage requires the build to go red at any point; every item ships independently.

## Executive summary — the five things that matter most

1. **Stage 0 first, always**: wire `ruff`/`mypy`/`coverage` into CI as non-blocking before anything else touches this code — there is currently zero standing guardrail, so any refactor risks silently regressing.
2. **Fix the `llm_classifier.py` unhandled-`None` bug** (Theme D) — smallest, highest-confidence real bug in the audit, no design decision needed.
3. **Document and enforce the gateway-restart step** (Theme B) — a one-line README fix closes a gap a first-time user can hit on the documented quick-start path *today*.
4. **Extract the shared HTTP-handler boilerplate** (Theme A) — the only structural item in this plan, and it's additive (a new small module each service opts into), not a rewrite.
5. **Add targeted tests, not a coverage mandate** (Theme C) — `autotag.py:run`'s untested retry loop and `mapping.py`'s untested fallback path, specifically, not "increase % coverage" as a goal in itself.

## Stage 0 — Safety net

Nothing in Stage 1+ touches code without this in place first, per the audit's own complexity/coverage findings (`AUDIT.md` §4.1, §4.6).

### 0.1 — Record baseline metrics
- **Rationale:** need a "before" snapshot to know Stage 1+ actually improved things, not just moved them.
- **What:** commit the raw tool outputs already gathered for this audit (`ruff check --select ALL` count: 3,716; default-ruleset count: 63; `radon cc -a`: 2.73 average, 214 blocks; `radon mi`: 0 files below grade A; coverage: 81%, 2,712 stmts/524 missed; `jscpd`: 6.06% Python / 3.26% JS duplication; `mutmut` on `mapping.py`+`webhooks.py`: 70.6% mutation score) as a short `docs-site/pages/`-adjacent note or a `BASELINE.md`, dated.
- **Files:** new file only, no code touched. **Size:** XS. **Risk:** none.

### 0.2 — Wire `ruff` + `mypy` + `pytest-cov` into CI, non-blocking
- **Rationale:** Theme (implicit) — zero tooling today means every future PR is unguarded. `.github/workflows/ci.yml`'s `unit` job already runs `pytest -m "not integration"`; add lint/type/coverage steps alongside it.
- **What:** add `ruff.toml` (or `[tool.ruff]` in `pyproject.toml`) pinned to the **default rule set only** for now (63 findings, not the 3,716-finding `ALL` set — ratchet later, see Guardrails); add `[tool.mypy]` with `ignore_missing_imports = true` as a starting baseline; add `--cov=baler --cov-report=term-missing` to the existing `pytest` CI step. Every new check reports but does not fail the build (`|| true` or a soft-fail annotation) until Stage 1 cleans up the existing findings.
- **Files:** `.github/workflows/ci.yml`, new `ruff.toml`/`pyproject.toml` `[tool.ruff]`/`[tool.mypy]` sections, `pyproject.toml` `dev` extra (add `ruff`, `mypy`, `radon`, `coverage` — no new runtime deps).
- **Acceptance criteria:** CI runs all three tools and prints results; build stays green even with today's 63 ruff findings / 19 mypy errors present.
- **Test strategy:** push to a branch, confirm CI output shows the new steps without failing the job.
- **Size:** S. **Dependencies:** none. **Risk:** low — additive CI steps only.

### 0.3 — Characterization tests for the two audit-flagged untested hotspots
- **Rationale:** `AUDIT.md` Theme C — `autotag.py:run` (34% covered, CC 16) and `mapping.py:classify_point`'s `rules=None` fallback (100% line coverage but 0% mutation coverage on that branch) are exactly the "don't refactor untested code" case the rubric warns about, and Theme A's extraction work will eventually touch `derivation_api.py`/`fault_api.py`/etc. which currently have no characterization beyond their existing (adequate — 81%+ each) coverage, so this item is scoped to the two specific gaps, not a blanket new-test sweep.
- **What:** add tests in `tests/test_autotag.py` covering `run()`'s retry-exhaustion path (mock `mapping.classify_point_with_fallback` to raise `requests.exceptions.ConnectionError` `_MAX_ATTEMPTS` times, assert it's skipped not crashed) and the rule-proposal branch (`outcome == "llm"` triggering `llm_classifier.propose_rule`); add a test in `tests/test_mapping.py` that calls `classify_point(store, uri, rules=None)` and asserts it actually falls through to `load_rules()` (e.g., by pointing `POINT_RULES_PATH` at a temp file and asserting the loaded rule takes effect) — this is the exact case `mutmut` showed passes even when the fallback is deleted.
- **Files:** `tests/test_autotag.py`, `tests/test_mapping.py`. **Size:** S. **Dependencies:** none. **Risk:** none (tests only).

## Stage 1 — Low-risk, high-leverage

### 1.1 — Fix the `llm_classifier.py` unhandled-`None` bug (Theme D)
- **Rationale:** `AUDIT.md` Theme D — real, unhandled `AttributeError` if the Anthropic SDK's structured-output parsing ever returns `None` for `response.parsed_output`.
- **What:** add a `None` check after `parsed = response.parsed_output` (around line 75) that raises a clear, caught-upstream exception or returns a "classification failed" sentinel the caller (`autotag.py:run`) already knows how to skip past (it already has a `result is None: continue` path for the retry-exhaustion case — reuse that shape).
- **Files:** `src/baler/llm_classifier.py` (~line 75), `src/baler/autotag.py` (only if the sentinel/exception shape needs a matching `except`/`continue`).
- **Acceptance criteria:** a test (added alongside, in `tests/test_llm_classifier.py`) that mocks `response.parsed_output = None` and asserts the function fails gracefully instead of raising `AttributeError`.
- **Size:** S. **Dependencies:** none. **Risk:** low.

### 1.2 — Document the gateway-restart requirement more prominently (Theme B, cheap half)
- **Rationale:** `AUDIT.md` Theme B — live-reproduced from the documented quick-start path itself, not an edge case.
- **What:** add `docker compose restart gateway` as an explicit last step of README's Quick Start (not just in the existing `CLAUDE.md` gotchas section, which agents read but first-time human users may not).
- **Files:** `README.md`. **Size:** XS. **Risk:** none.

### 1.3 — Add `tests/conftest.py` for the openapi-contract-test duplication
- **Rationale:** `AUDIT.md` §4.4 — `jscpd` found real duplication clustered across `test_*_openapi.py` files and e2e setup, and `tests/` has no `conftest.py` today despite this.
- **What:** extract the shared setup (spec-loading, common fixtures used across `test_openapi.py`/`test_auth_openapi.py`/`test_fault_openapi.py`/`test_validate_openapi.py`/`test_derivation_openapi.py`) into `tests/conftest.py` fixtures. Keep this additive — existing tests migrate to use the fixture one file at a time, nothing needs to change all at once.
- **Files:** new `tests/conftest.py`; incremental edits to the 5 `test_*_openapi.py` files.
- **Acceptance criteria:** `pytest -m "not integration"` still green after each file's migration; `jscpd` duplication % on `tests/` drops (measure before/after).
- **Size:** S. **Dependencies:** none. **Risk:** low — pure test refactor, covered by the tests themselves passing.

### 1.4 — Turn on the highest-value narrow `ruff` rules beyond the default set
- **Rationale:** `AUDIT.md` §4.1 — `BLE001` (blind except) and `RUF013` (implicit Optional) are both real, narrowly-scoped, and already have a small enough hit count (9 and 3) to fix immediately rather than defer to the "eventually ratchet toward `ALL`" guardrail.
- **What:** add `BLE001` and `RUF013` to the `ruff.toml` select list from 0.2; either fix each of the 9+3 hits (narrow the except clause or add a comment explaining why broad-catch is intentional per `AUDIT.md`'s "explicitly-fine" note on the pollers; make the `Optional` explicit) or add a scoped `# noqa: BLE001` with a one-line reason where the broad catch is the audit-confirmed-correct pattern (the 4 pollers + `sandbox.py`).
- **Files:** `src/baler/{derivation_engine,energystar_puller,haystack_puller,openmeteo_puller,sandbox,sql_puller}.py` (BLE001), `src/baler/{haystack_client,remote_store}.py` (RUF013).
- **Size:** S. **Dependencies:** 0.2 (ruff config must exist first). **Risk:** low.

## Stage 2 — Structural

### 2.1 — Extract shared HTTP-handler boilerplate (Theme A)
- **Current state:** `auth_api.py`, `derivation_api.py`, `fault_api.py`, `ingest_api.py`, `validate_api.py` each independently define a `BaseHTTPRequestHandler` subclass with its own `_handle(span_name, fn)`-shaped tracer-wrapping, JSON-response helper, and `log_message` override (`AUDIT.md` Theme A, `jscpd`-confirmed).
- **Target state:** a new `src/baler/http_handler_base.py` providing the shared pieces (a `traced_handler(span_name, fn)` decorator/wrapper, a `respond_json(handler, status, body)` helper, a `QuietRequestHandler` mixin with the shared `log_message` override) that each of the 5 API modules imports and composes into their own handler class — **no framework, no shared server bootstrap, no change to the one-container-per-service model.**
- **Migration steps (build stays green throughout):**
  1. Write `http_handler_base.py` + its own unit tests, with no other file importing it yet.
  2. Migrate **one** API (`validate_api.py` — smallest, 110 lines) to use it; run its existing tests + the relevant `test_validate_openapi.py`/integration tests; commit.
  3. Repeat one at a time for `ingest_api.py`, `auth_api.py`, `fault_api.py`, `derivation_api.py` — each is an independent commit, each keeps that service's existing test suite as the safety net, no cross-service coordination needed since they don't call each other.
  4. Re-run `jscpd` after all 5 are migrated; confirm the Python duplication % (baseline 6.06%, §0.1) has measurably dropped.
- **Rollback plan:** each migration is a single-file, single-commit change; revert the one commit for the affected service if its tests or the integration suite regress — no other service is touched by any one step.
- **Files touched:** new `src/baler/http_handler_base.py` + `tests/test_http_handler_base.py`; then one at a time, `validate_api.py`, `ingest_api.py`, `auth_api.py`, `fault_api.py`, `derivation_api.py`.
- **Acceptance criteria:** full test suite green after each step; `jscpd` Python duplication % drops from the 6.06% baseline; no behavior change (existing tests are the proof).
- **Size:** M (spread across 6 small commits). **Dependencies:** Stage 0 (needs the coverage baseline to confirm no regression), Stage 1.3 done first is not required but is complementary. **Risk:** medium-low — mechanical extraction with a real regression test (the existing suite) at every step; the risk is entirely in "did I preserve exact behavior," which the existing per-service tests plus the gateway integration tests directly check.

## Stage 3 — Longer-horizon (options, not decrees)

### 3.1 — Gateway upstream-resolution fix (Theme B, real half)
Two options, tradeoffs only:
- **(a) Keep the README fix (1.2) as the permanent answer.** Zero risk, but relies on every operator remembering the extra step forever.
- **(b) Switch `gateway/njs`'s upstream resolution to per-request via the existing `resolver 127.0.0.11` mechanism** (already used elsewhere per `CLAUDE.md`) instead of caching at nginx startup. Removes the class of bug entirely, but touches `gateway/nginx.conf`/`gateway/njs/main.js`, needs `nginx -t` + the existing `node gateway/njs/test_*.mjs` self-checks plus a live re-run of `tests/test_gateway_auth.py`'s full suite (28 integration tests) to confirm no auth-policy regression, and njs's runtime restrictions (`CLAUDE.md`'s documented gotchas) make this a slower, more careful change than it looks. **Needs a product/ops decision on whether the operational cost of (a) is actually worth (b)'s engineering cost** — not decided here.

### 3.2 — `tests/` shared-fixture consolidation beyond openapi tests
Stage 1.3 only covers the openapi-contract-test cluster. `AUDIT.md` §4.4 also found duplication across `test_e2e_journey.py`/`test_haystack_brick_e2e.py`/`test_seed_script.py` (shared org/login/seed-data setup). Worth a second `conftest.py` pass once 1.3 proves the pattern works, but not urgent — these are the least-frequently-run tests (integration-only, e2e).

### 3.3 — Broader `ruff` rule adoption beyond BLE001/RUF013
The `--select ALL` pass found 3,716 findings, dominated by docstring/annotation/line-length categories. This is a real style-consistency decision (does the team want mandatory docstrings on every function? type annotations everywhere given `mypy`'s current 19-error baseline?) that deserves a explicit team choice, not a silent tool default — framed as options for whoever owns this decision, not prescribed here.

## Guardrails

- **CI, from Stage 0.2 onward**: `ruff check` (default set, then BLE001+RUF013 from 1.4) and `mypy` run on every PR, non-blocking initially; **flip to blocking once the current 63 ruff / 19 mypy findings reach 0** (tracked via the Stage 0.1 baseline file).
- **Coverage floor that ratchets, not blocks**: record the 81% baseline (0.1); CI reports the diff-covered % on each PR (e.g. via `pytest-cov`'s `--cov-report` diff mode or a simple before/after comparison script) so coverage can only go up, never silently down, without requiring an arbitrary hard floor that blocks legitimate low-coverage infra work.
- **Clone-detection gate**: re-run `jscpd` in CI (same flags as this audit) as a reporting-only step; alert (not block) if Python duplication % rises above the 6.06% baseline — catches new instances of the Theme A pattern before they multiply to a 6th service.
- **Pre-commit hook**: `ruff check --fix` (safe fixes only) + `ruff format --check` if/when a formatter is adopted — deferred to whoever picks up 3.3's style decision.
- **One ADR worth writing**: a short `todo/`-style note (matching the project's existing pattern, per `AUDIT.md` §1) recording the Theme A decision — "why a shared `http_handler_base.py`, not a framework" — so a future contributor doesn't propose re-litigating the framework question when they hit the boilerplate.

## Metrics to track — 30/90 days

| Metric | Baseline (this audit) | 30-day target | 90-day target |
|---|---|---|---|
| `ruff check` (default set) findings | 63 | ≤ 20 (Stage 1 cleanup) | 0 |
| `mypy` errors | 19 | ≤ 10 | 0 or explicitly suppressed with reasons |
| Python duplication (`jscpd`) | 6.06% | 6.06% (Stage 2 not yet done) | < 3% (post Theme A extraction) |
| Line coverage (`pytest-cov`) | 81% | 82%+ (targeted additions, not a blanket push) | 85%+ |
| `mapping.py` mutation score | 70.6% (96 survivors) | 96 survivors → 0 on the specific fallback path (0.3) | full-file re-run, track trend |
| Known `AttributeError` risk (Theme D) | present | fixed | — |

**Success at 30 days** looks like: CI has the new non-blocking lint/type/coverage steps and nobody's workflow broke; Theme D is fixed; the README gap (1.2) is closed; the two characterization tests (0.3) exist and pass. **Success at 90 days** looks like: Theme A's extraction is complete and duplication has measurably dropped; the ruff/mypy gates have flipped from non-blocking to blocking because the counts actually hit zero, not because the gate was loosened.

# Code quality baseline — 2026-09-05

Recorded per `PLAN.md` Stage 0.1, from the audit in `AUDIT.md`. Numbers here are the "before"
snapshot for the metrics tracked in `PLAN.md`'s Guardrails/Metrics sections — re-run the same
commands to compare after later stages land.

| Metric | Command | Baseline |
|---|---|---|
| `ruff check` (default rule set) | `ruff check src/ tests/` | 63 errors |
| `mypy` errors | `mypy --ignore-missing-imports src/baler` | 19 errors in 11 files |
| Cyclomatic complexity (average) | `radon cc -s -a src/` | A (2.73), 214 blocks, nothing above CC 18 |
| Maintainability index | `radon mi -s -n B src/` | no file below grade A |
| Line coverage | `pytest -m "integration or not integration" --cov=baler` | 81% (2,712 stmts / 524 missed) |
| Python duplication | `jscpd src/baler tests ui gateway/njs` | 6.06% (645 duplicated lines, 50 clone pairs) |
| JS duplication | same `jscpd` run | 3.26% (55 duplicated lines, 5 clone pairs) |
| `mapping.py` mutation score | `mutmut run` scoped to `mapping.py`+`webhooks.py` | 70.6% (230 killed / 96 survived, all 96 in `mapping.py`) |
| `webhooks.py` mutation score | same `mutmut run` | 100% (0 survivors) |

See `AUDIT.md` for full methodology, per-finding evidence, and what's explicitly out of scope for these numbers (e.g. `ui/` has no coverage/mutation tooling by design — buildless, no test runner).

## After Stage 0-2 (PLAN.md)

| Metric | Before | After |
|---|---|---|
| `ruff check` (default rule set) | 63 | 62 (one finding was in a since-stashed WIP file, not fixed by this work) |
| `mypy` errors | 19 | 9 |
| `ruff` `BLE001`/`RUF013` | 9 / 3 | 0 / 0 (Stage 1.4) |
| Python duplication | 6.06% (645 lines, 50 clones) | 3.79% (404 lines, 30 clones) (Stage 1.3 + Stage 2.1) |
| `mapping.py` mutation score | 70.6% (96 survivors) | closed the specific `classify_point` fallback-path gap `mutmut` flagged (Stage 0.3); full re-run not repeated here |

Stage 2.1 (extracting `http_handler_base.py` for the 5 stdlib HTTP APIs) is the largest single contributor to the duplication drop. Full audit-tool re-run (radon/vulture/full mutmut) not repeated here — see `AUDIT.md` for the original methodology if a fresh full pass is wanted later.

## After the follow-up hardening pass (same day)

| Metric | Before this pass | After |
|---|---|---|
| `mypy` errors | 9 | **0** (2 documented false positives suppressed with `# type: ignore[index]` + reason; CI flipped from non-blocking to blocking) |
| `ruff check` (default rule set) | 62 | 3 (all 3 are in files with your own in-flight, uncommitted feature work — not touched; every finding in a committed file is fixed) |
| Flaky test | `test_e2e_derivation_averages_bacnet_and_modbus_sourced_sensors_over_real_broker` intermittently failed under load | Fixed a real race (wait-for-ingest check only verified one of two points' tags) — 5/5 clean runs in the exact batch that reproduced it |
| CI guardrails | mypy/ruff both non-blocking, no clone-detection gate, no pre-commit hook | mypy blocking; `jscpd` reporting-only step added; `.pre-commit-config.yaml` added (ruff, optional local install) |
| `todo/http-handler-base-extraction.md` | didn't exist | written — the design-record note `PLAN.md`'s own Guardrails section called for |

`ruff` stays non-blocking until the 3 remaining findings (all in uncommitted WIP) are resolved — flipping it now would immediately break CI for that work.

## ruff to zero, gate flipped to blocking — 2026-09-09

The WIP above has since landed (`axon_convert.py`, the Query feature, the Brick-extension work), bringing the committed-tree count to 8 under ruff 0.16.6's default set + `BLE001`/`RUF013`. All 8 resolved:

| Finding | Where | Resolution |
|---|---|---|
| `I001` unsorted imports | `derivation_engine.py` | `ruff --fix` |
| `F401` unused imports (×2) | `tests/test_fault_openapi.py` | `ruff --fix` |
| `RUF012` mutable class attribute | `axon_convert.py` `_COMPARE_OPS` | `frozenset` — it was only ever read |
| `DTZ011` naive `date.today()` | `energystar_puller.py` `pull_once` | `datetime.now(timezone.utc).date()` — same UTC clock as the reading's own `time.time()` timestamp; the containers already run in UTC, so no behavior change in the deployed configuration |
| `S102` `exec()` (×3) | `tests/test_axon_convert.py` | `per-file-ignores` in `ruff.toml` with the reason — the module under test emits Python source, exec'ing it is the test |

| Metric | Before | After |
|---|---|---|
| `ruff check src/ tests/` | 8 | **0** |
| `mypy src/baler` | 0 | 0 |
| CI | ruff `continue-on-error: true` | **ruff blocking** — both lint gates are now hard gates, per `PLAN.md` Guardrails ("flip to blocking once the counts actually hit zero, not because the gate was loosened") |

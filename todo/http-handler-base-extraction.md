# Shared HTTP-handler boilerplate for the 5 stdlib API services

**Status: implemented (2026-09-05)**, per the design below —
`src/baler/http_handler_base.py`, all 5 of `auth_api.py`/`derivation_api.py`/
`fault_api.py`/`ingest_api.py`/`validate_api.py`, `tests/test_http_handler_base.py`. Kept as the
design record per this project's own convention (see `shacl-validation-and-service.md`'s note on
why `todo/*.md` survives past implementation instead of a per-conversation plan file).

## Context

A staff-level architecture/code-quality audit (`AUDIT.md`, `PLAN.md` in this repo — a one-off
audit pass, not itself kept as a standing doc) found real, `jscpd`-confirmed duplication: all 5
stdlib-`http.server` API services independently reimplemented the same request-handling glue —
JSON response encoding, a tracer-span-wrapped error-to-400 handler, `GET /openapi.yaml` file
serving, CORS/`OPTIONS` handling, and quiet request logging. 15 of `jscpd`'s 50 Python clone pairs
at audit time were among these 5 files; duplication dropped from 6.06% to 3.72% of the Python
codebase after this extraction.

The risk this design had to avoid: re-litigating the already-settled "stdlib `http.server`, not a
framework" decision. Each service being its own process/container with its own port is
deliberate (independent restartability, no shared failure domain) — a shared *library* module
that each service imports and composes into its own handler class doesn't compromise that, the
way adopting Flask/FastAPI as a shared dependency and server bootstrap would.

## Design

`src/baler/http_handler_base.py`: a `BaseAPIHandler(BaseHTTPRequestHandler)` mixin plus
two free functions (`respond_json`, `respond_error`). Each service's own `make_handler()` still
builds its own handler class and its own `ThreadingHTTPServer` — nothing about server bootstrap,
port binding, or process lifecycle changed.

- `TRACER` (class attribute, set once per subclass to that module's own `tracing.get_tracer(__name__)`)
  keeps spans tagged by the emitting service, not collapsed into one shared tracer.
- `ALLOWED_METHODS`/`ALLOWED_HEADERS` (class attributes) parametrize `do_OPTIONS` — only
  `auth_api.py` needed `Authorization` in `ALLOWED_HEADERS`, everything else uses the base
  defaults untouched.
- `ERROR_TYPES` (class attribute, a tuple) parametrizes `handle_traced`'s except clause —
  `derivation_api.py` extends it with `sandbox.SandboxError` since it alone executes submitted
  Python as part of validation; every other service uses the base default
  `(KeyError, TypeError, ValueError, json.JSONDecodeError)` unchanged.
- `respond_json` takes an optional `default=` passthrough for `json.dumps` — `derivation_api.py`
  binds it via `functools.partial(respond_json, default=str)` since derivation traces can carry
  datetimes; every other service's payloads are plain JSON-native types and pass nothing.
- `read_json_body`/`handle_traced`/`serve_openapi_spec`/`do_OPTIONS`/`end_headers`/`log_message`
  are inherited as-is by every service that used the exact matching shape (4 of 5 — `validate_api.py`
  didn't even have `handle_traced`'s pattern, being a single parameterless route).

**`ingest_api.py` was deliberately only partially migrated.** Its per-route error handling
(`_post_ingest`/`_post_tags`/`_post_equip_merge`/`_post_part`/`_get_history`/`_delete_history`)
looked superficially similar to the other 4 services' `_handle(span_name, fn)` pattern but isn't
the same thing: each route has a genuinely different exception tuple and a custom, specific error
message (e.g. `f"reading missing required field: {exc}"` vs. a generic `str(exc)`). Forcing this
into `handle_traced` would have flattened real, useful behavior for a DRY win that didn't actually
apply — this is the "coincidental similarity, not true duplication" case. Only the byte-identical
pieces (`respond_json`/`respond_error` imports, the `openapi.yaml`-serving block, `do_OPTIONS`/
`end_headers`/`log_message`) were extracted there.

## Explicitly out of scope

- **A shared server bootstrap or a framework.** Each service keeps its own `ThreadingHTTPServer`
  instantiation and `main()`. This was never on the table — see Context above.
- **Forcing `ingest_api.py`'s per-route error handling into `handle_traced`.** See above.
- **Collapsing the 5 `tracer = tracing.get_tracer(__name__)` module-level instances into one.**
  Each service's spans should stay attributable to that service in Jaeger.
- **A second pass to further reduce the ~3.7% remaining Python duplication** (mostly test-file
  boilerplate — `tests/conftest.py` already absorbed the OpenAPI-contract-test cluster; the
  `test_e2e_journey.py`/`test_haystack_brick_e2e.py`/`test_seed_script.py` cluster is a separate,
  lower-priority follow-up per `PLAN.md` Stage 3.2, not done here).

## Files created/modified

- `src/baler/http_handler_base.py` (new) + `tests/test_http_handler_base.py` (new, 13
  unit tests, no live services — `BaseHTTPRequestHandler` instances built via `__new__` to bypass
  its request-dispatching `__init__`).
- `src/baler/{validate_api,ingest_api,auth_api,fault_api,derivation_api}.py` (modified,
  one commit each, smallest service first).

## Verification (already done, kept for anyone touching this area later)

Each service's migration commit was verified against that service's full test suite
(unit + integration) before moving to the next, plus a full-repo `pytest` run at the end (322
passed at the time). `jscpd` re-run after all 5 confirmed the duplication drop. See `PLAN.md`
Stage 2.1 for the exact migration order and per-step acceptance criteria.

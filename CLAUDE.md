# Baler — agent notes

Full docs: [README.md](README.md) (setup, ports, tests) and
[docs-site/](docs-site) (architecture, fault detection, webhooks, Haxall
integrations). This file is gotchas only — don't duplicate those.

- **Src layout**: package is `src/baler/`, installed editable
  (`pip install -e .`; `pip install -e ".[dev,llm]"` for tests + autotag;
  add `,validate` for SHACL validation — `tests/test_shacl_validate.py`
  needs `pyshacl` installed or it errors at import).
- **`haystack_api.py` needs `openhay`** (`pip install -e ".[haystack]"`),
  which builds from git with maturin and so needs a Rust toolchain
  (`cargo`). Without it `tests/test_haystack_api.py` *skips* rather than
  failing. CI's unit job installs it; the Dockerfile builds it in a Rust
  stage. The openhay git ref is pinned in two places, `pyproject.toml` and
  the Dockerfile's `OPENHAY_REF`, so bump both together.
- **Renamed from Timberdoodle (FBF -> Windrow).** The legacy shims are
  load-bearing for the live nash-srv install, not leftovers: compose's
  `${BALER_X:-${TIMBERDOODLE_X}}` fallbacks, the `BALER_PG_USER`/`BALER_PG_DB`/
  `BALER_VOLUME_PREFIX` variables, and the `migrate` service
  (`baler.migrate`, rewrites `urn:timberdoodle:` IRIs, and now also applies
  the Postgres schema migrations). The `fbf/` MQTT
  topic prefix and `urn:point:fbf/...` ids are deliberately unchanged
  (point identity). See `deploy/MIGRATING-TO-BALER.md` before touching any
  of these. A blanket `TIMBERDOODLE`->`BALER` find/replace will silently
  break the fallbacks (it did once).
- **`commissioning/vendor_constrain/` is vendored PNNL ConStrain (BSD-2),
  not ours to restyle**: `verify()` bodies stay upstream's so results mean
  what they mean in ConStrain. `constrain` itself can't be a dependency
  (it pins rdflib 6). `tests/test_commissioning_gl36.py` skips without
  pandas (`pip install -e ".[cx-gl36]"`); CI's unit job installs it.
- **No Makefile/justfile.** Every command is literally
  `python -m baler.<module>` or `docker compose ...` — check
  `docker-compose.yml`'s `command:` fields or a module's
  `if __name__ == "__main__"` block for the real invocation, don't assume
  a wrapper script exists. There is CI (`.github/workflows/ci.yml`, two
  jobs: `unit` needs no services, `integration` runs `docker compose up
  -d` first) — it just doesn't wrap anything, it runs the same bare
  commands above.
- **App containers run as root with the checkout bind-mounted (`.:/app`).**
  Anything they write into `/app` is root-owned on the host, and on nash-srv
  a root-owned directory breaks the next deploy's `actions/checkout` (EACCES,
  #29). The shared daemon env sets `PYTHONPYCACHEPREFIX` so bytecode stays
  inside the container; a new service that bind-mounts `.:/app` needs that
  env too, and don't add code that writes new directories under `/app`. CI's
  integration job fails if any appear.
- **The `jupyter` service (profile `notebook`) deliberately doesn't
  bind-mount the checkout**: `baler_fn` and `baler/haystack_client.py`
  are baked into `baler-notebook:local` (`docker/notebook.Dockerfile`), so
  a change there needs `--build`. Function-project images build `FROM
  baler-notebook:local` (`templates/function-project/`). Never make Baler
  itself import or run function-package code; the docs-site index
  (`scripts/gen_function_index.py`, run by docs-site's `prebuild`/`predev`)
  reads only `baler.toml` and escapes it for MDX. Its output
  `docs-site/pages/code/functions.mdx` is generated and git-ignored.
- **Schema changes are numbered migrations** (`src/baler/migrations/`,
  `python -m baler.schema`, docs-site `schema-migrations.mdx`), never DDL in
  a module: module `ensure_schema()`s just call `schema.ensure`, once per
  process. Released migration files are hash-pinned in
  `tests/test_schema.py`; add a new one, don't edit. TimescaleDB refuses
  `ALTER COLUMN TYPE` on compressed chunks: use
  `migrations.hypertable.with_decompressed`.
- **Env vars**: `.env.example` at the repo root is the single source of
  truth for every var any process reads — check there before grepping
  source.
- **Gateway auth requires `BALER_JWT_SECRET`/`BALER_GATEWAY_SECRET`
  in `.env` — `docker compose up` refuses to start the `gateway` service
  without both (see README quick start).** No more htpasswd files — every
  route on every API is JWT-verified in nginx itself (`gateway/njs/`,
  policy in `gateway/njs/policy.js`), not per-backend Python auth code.
  `POST /auth/orgs` (public) creates the first admin account;
  `POST /auth/login` exchanges credentials for a token.
- **Gateway upstreams re-resolve at runtime; keep it that way.** Each
  backend is an `upstream { server x:port resolve max_fails=0; }` block in
  `gateway/nginx.conf` (or a variable `proxy_pass`), resolved through
  `resolver 127.0.0.11`, so `docker compose up -d --build ingest_api` needs
  no gateway restart (#108). A new location written as a literal
  `proxy_pass http://host:port/` brings back the old bug (address pinned at
  boot, 502s after that backend is recreated). Don't switch to
  `proxy_pass http://$var` plus `rewrite`: njs's policy reads `r.uri`,
  which a rewrite changes. Gateway config changes need `nginx -s reload`
  (the deploy does `exec gateway nginx -t && nginx -s reload`), not a
  restart.
- **`pytest -m "integration or not integration"` runs everything**, not a
  filtered subset — it's a tautology. It needs `docker compose up -d`
  first (hits live Postgres/Oxigraph/MQTT). Use
  `pytest -m "not integration"` for a services-free run.
- **Sources are configured in Ops → Sources** (`baler.sources` registry,
  run by the always-on `source_runner`). The per-kind `*_puller` services
  are the older env-configured path. Changing `BALER_GATEWAY_SECRET` makes
  stored source secrets unreadable unless `BALER_SOURCES_KEY` is set.
- **`haystack_puller` is profile-gated**: `docker compose up -d` does not
  start it. Needs `docker compose --profile haystack-pull up -d
  haystack_puller`. Set `REMOTE_HAYSTACK_URL`/`REMOTE_HAYSTACK_USER` first
  — the container does NOT refuse to start without them (compose always
  passes them through, blank if unset), it just fails every pull cycle
  silently against an empty URL instead of erroring at startup.
- **Webhook URLs must be `https://` and resolve to a public address** —
  `fault_api.py`'s `POST /webhooks` and `fault_detector.py`'s delivery
  both reject loopback/private/reserved targets (SSRF protection,
  `webhooks.validate_url`). Testing against a local receiver (e.g.
  `http://127.0.0.1:<port>/`) needs `BALER_ALLOW_PRIVATE_WEBHOOKS=1`
  set for whichever process is doing the registering/delivering — it's a
  deployment-level env var, not a per-webhook field, so it can't be set
  through the API itself.
- `todo/shacl-validation-and-service.md`'s design is now implemented
  (`ontology/`, `shacl_validate.py`, `validate_api.py` on port 8005) — the
  doc itself wasn't deleted (kept as design rationale), don't treat its
  "planned, not started" status line as current.
- **`gateway/njs/*.js` are ES modules regardless of file extension** —
  njs always treats them as modules. Plain `node` needs
  `gateway/njs/package.json`'s `"type": "module"` to agree, or
  `test_jwt.mjs`/`test_policy.mjs` (`node gateway/njs/test_*.mjs`, no
  pytest equivalent exists for this code) fail to import them. Only
  `export default { ... }` works in njs 1.0.0 (bundled with
  `nginx:alpine`) — named exports (`export const X = ...`), array/object
  destructuring, `for...of`, spread, default params, and `class` all
  throw `SyntaxError`/`TypeError` at nginx config load time, not at
  runtime — `nginx -t` catches these, plain review doesn't. Also: a
  `js_shared_dict_zone` needs an explicit `timeout=` for `.incr()`'s own
  TTL argument to work at all, `ngx.fetch()` needs a `resolver` directive
  (Docker Compose: `127.0.0.11`) to resolve container hostnames — neither
  error surfaces at `nginx -t`, only at request/periodic-tick time, and
  only visibly if `error_log` is at `warn` or lower (the nginx default,
  `error`, silently swallows both `ngx.log(ngx.WARN, ...)` output and the
  underlying failures unless you turn it up).

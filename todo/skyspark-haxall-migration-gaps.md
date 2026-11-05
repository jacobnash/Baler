# Closing the SkySpark/Haxall migration gaps

**Status:** items 1, 4, 5, and 7 done (item 4/5 as of 2026-08-28); item 2 won't do (no SkySpark
access, see below); items 3, 6 not started.
Backlog from a 2026-08-26 critical review
specifically asking "what would it cost an existing SkySpark/Haxall customer to migrate away to
Baler" (as opposed to general feature parity, which was already tracked informally).
Full comparison detail, evidence, and the "Verdict" this backlog is ranked against live in the
Field Report Artifact (`https://claude.ai/code/artifact/916878fb-8991-4a79-a1fa-fd4f39ff543d`,
"Migrating away — the honest version" section) — this file exists so the backlog itself survives
in the repo rather than only in a Claude conversation/artifact. Saved here per the same reasoning
as `todo/shacl-validation-and-service.md`: this is meant to survive past the current
conversation's plan-file slot.

## Context

Baler's technical core (Brick/RDF, a real historian, the Haxall `hx::IHisExt` drop-in) is
solid, but the review found the *migration* story specifically overclaims relative to what's
actually tested: the one integration that lets a customer migrate gradually
(`fantom-his-ext`/`fantom-windrow-conn`) has a currently-broken default config, has never been
verified against SkySpark itself (only open-source Haxall), and has zero automated test coverage.
Separately, the things a migrated customer would *lose* on day two (SSO, alarm escalation,
scheduling, automated backups) aren't tracked anywhere as backlog — this file is that tracking.

Ranked by how directly each item turns an asserted claim into a tested one, or closes a concrete
day-two gap.

## Backlog

### 1. Fix the Haxall drop-in's dead URL, and add a smoke test

- **Status:** fully done (2026-08-27) — fixed, live-verified, and an automated smoke test exists.
- **Why:** `BalerHisExt.fan` hardcoded `http://host.docker.internal:8000/`, a port
  `ingest_api` stopped publishing once it moved behind the gateway (JWT auth rewrite,
  `6d2a61b`/`0c553c1`). Anyone following `docs-site/pages/haxall-drop-in.mdx` got a dead
  connection — the doc said so ("neither [fix] is done for you yet") but shipped no fix. This
  was the one piece of the migration pitch that was actively false, not just incomplete.
- **What was done:** new `fantom-his-ext/fan/BalerHisSettings.fan` (a `Settings` subclass,
  the same `@Setting`/`Ext.settings()` mechanism confirmed live in `haxall/haxall`'s own
  `hxHttp`/`HttpSettings`) adds real `baseUri`/`authToken` fields, editable via Axon's real
  `extSettingsUpdate("his", {...})` function (confirmed against `haxall/haxall`'s
  `src/xeto/hx/funcs.xeto`). `baseUri` now defaults to the gateway
  (`http://host.docker.internal:8080/ingest/`); `BalerHisExt.fan`'s `read`/`write` attach
  `Authorization: Bearer <authToken>` when one is configured. `docs-site/pages/haxall-drop-in.mdx`
  updated to match.
- **Live-verified (2026-08-27):** built `tdHis`/`his.xetolib` against a real Haxall 4.0.6 install
  (the `windrow-haxall` image's compiled release SDK, run as a throwaway `td-verify` container —
  `~/haxall`'s own source-build path turned out to be separately broken, see note below), ran
  `libAdd(["his"])` and a real `extSettingsUpdate("his", {authToken, baseUri})`, wrote a value
  through a live curVal transition (`hisCollectCov` fired, `hisStatus: "ok"`), confirmed it landed
  via this API's own `GET /ingest/history` through the gateway with a real Bearer token, then read
  it back via Haxall's `hisRead` op (which calls `BalerHisExt.read()` directly) and got the
  same value/timestamp back. Both directions confirmed end-to-end, not just cross-checked against
  source. `docs-site/pages/haxall-drop-in.mdx` updated to state this as proven, not pending.
- **Automated smoke test added (2026-08-27):** `tests/test_haxall_drop_in_smoke.py`, same opt-in
  convention as `test_haystack_puller.py`'s real-Haxall test (`@pytest.mark.integration` +
  `skipif` unless `HAXALL_SU_USERNAME` is set, so it's silent in the normal/CI suite). Runs the
  exact same enable/configure/write/read sequence used for live verification above, reusing the
  existing `HaystackClient` (`src/baler/haystack_client.py`) for auth rather than adding a
  new dependency — confirmed passing against a real Haxall instance, and confirmed it skips
  cleanly (doesn't affect `pytest -m "not integration"` or break anything, ran the full suite:
  158 passed). Not wired into GitHub Actions CI — that would mean vendoring the whole Haxall
  build pipeline into CI, a separate, bigger decision than "add a smoke test" for this bug fix;
  this is the developer-runnable, repeatable form.
- **Side finding, fixed (2026-08-27):** rebuilding `~/haxall`'s own Docker image (the
  aggregate-source-build path, distinct from the `windrow-haxall` release-SDK path used for the first
  live-verification pass above) was broken — its Dockerfile did unpinned `git clone -b master` of
  `Project-Haystack/xeto` and `Project-Haystack/haystack-defs` at build time, and current upstream
  `master` no longer parsed cleanly against this pinned Haxall commit's build-var substitution,
  breaking `hx init`/`hx run` entirely (`sys::Err: Must include 'sys' lib`, later
  `Invalid 'sys::UnitQuantity' string value: specificEnthalpy`) for every database, not just a
  fresh one. Root-caused to two separate issues (missing `ph.*` build vars under the filename this
  pinned `xetoc` build actually reads, and xeto's `master` having renamed a unit quantity after
  this Haxall commit's `UnitQuantity` enum was built) and fixed directly in `~/haxall`'s own
  `docker/Dockerfile` (not this repo): pinned both clones to the latest commit at or before
  Haxall's own pin date instead of floating `master`, and added the missing `build.props` values.
  `docker compose build && docker compose up -d` in `~/haxall` now boots cleanly again, confirmed
  against both a fresh db and the pre-existing `var` db. Unrelated to tdHis itself, but was
  blocking the most natural way to test it.
- **Files:** `fantom-his-ext/fan/BalerHisExt.fan`,
  `fantom-his-ext/fan/BalerHisSettings.fan` (new), `tests/test_haxall_drop_in_smoke.py`
  (new), `docs-site/pages/haxall-drop-in.mdx`.

### 2. Verify the drop-in against a real SkySpark instance

- **Status:** won't do — no access to a licensed SkySpark instance to test against. Not a
  priority call, a hard constraint; revisit if that access ever becomes available.
- **Why it mattered:** every live-verification claim in `haxall-drop-in.mdx` is against
  open-source Haxall 4.0.6, never SkySpark. SkySpark is the commercial product with the actual
  paying customers worth migrating. `hx::IHisExt` being shared, open infrastructure is a
  reasonable bet that SkySpark behaves the same way, but it's a bet, not a tested claim — per the
  standing rule in memory (`feedback_vendor_marketing_claims`), don't assert it until it's
  verified. `haxall-drop-in.mdx`'s existing "Verified on plain, open-source Haxall... has not been
  verified — don't assume it without testing" line already states this honestly and needs no
  change either way.
- **What it would have taken:** the same live pass (`libAdd(["his"])`, confirm `hisWrite`/
  `hisRead` round-trip through Baler's Postgres) against a real SkySpark instance.

### 3. Measure real-world Haystack→Brick tag-mapping accuracy

- **Status:** not started
- **Why:** `mapping.py`'s rule engine plus the LLM fallback (`autotag.py`/`llm_classifier.py`)
  has only ever run against the mock hospital fixture (~500 clean, synthetic points). The
  fraction of a real, messy, years-old Haystack tag set that lands as `TD.UnmappedPoint` is
  genuinely unmeasured — a migrating customer can't budget cleanup time against an unknown.
- **What:** run `haystack_puller.py --once` + `autotag.py` against a real (or realistically
  messy) Haystack export, not another synthetic fixture, and publish the resulting
  direct/fallback/unmapped breakdown.
- **Files:** `src/baler/mapping.py`, `rules/haystack_to_brick.yaml`,
  `rules/haystack_equip_to_brick.yaml`, `src/baler/autotag.py`,
  `src/baler/llm_classifier.py`.

### 4. Live in-app Python rule-authoring surface

- **Status:** done (2026-08-28) — built together with item 5 (shared UI shell: `auth.js`,
  `console.css`, `top-nav`), verified live in a real browser.
- **Why:** unchanged from the first (2026-08-24) review pass — still the one piece of daily
  SkySpark workflow with no Baler equivalent. Not about Axon vs. Python (Python's the
  right call, see `feedback_vendor_marketing_claims`/`project_baler_haxall_context`); it's
  the live in-browser shell with autocomplete and inline eval against real data that SkySpark has
  and Baler doesn't.
- **What was done:** `ui/derivations.html`/`ui/derivations.js` — no new expression language, purely
  a browser client over `derivation_api.py`'s existing endpoints (`POST /derivations/test`,
  `/dry-run`, `/derivations`, `DELETE /derivations/{id}`). Three panes (config JSON, `fn_source`,
  `test_cases` JSON) plus Run tests/Dry run/Save/Clear actions and a results pane; a table of
  existing derivations with "Load into editor"/"Delete". No structured per-`kind` form (formula
  vs. rollup have different field sets) — one JSON config textarea covers both kinds rather than
  building conditional UI for marginal benefit; noted as the corner cut, not silently skipped.
  Autocomplete and a plain-language-to-Python stretch goal from the original "What" are still
  undone — out of scope for this pass, no autocomplete library evaluated.
- **Live-verified (2026-08-28):** in a real Chrome session against the running stack — loaded the
  page, ran the seeded default template's tests (real sandbox pass), ran a real dry-run against
  live Oxigraph/point_history data (got a real trace back), loaded an existing derivation into the
  editor, created a new one via Save (confirmed it round-tripped correctly), deleted it, confirmed
  the list reflected each change.
- **Files:** `ui/derivations.html`, `ui/derivations.js` (new), `ui/auth.js`, `ui/console.css`
  (new, shared with item 5), `ui/style.css`, `ui/index.html`, `ui/devices.html` (nav links).

### 5. Alarm console — acknowledge and escalate, not just open/resolved

- **Status:** done (2026-08-28) — ack/snooze/severity shipped; full escalation (paging chains,
  auto-reassignment) explicitly not attempted, see below.
- **Why:** `faults.py`'s schema is open/resolved only — no ack, snooze, severity, or escalation
  columns (confirmed via grep, zero hits for any of those terms in `src/`). This is both a
  Section 02 market-position gap (SkySpark's alarm console is the app an operator opens daily)
  and a Section 03 day-two-regression item (a migrated customer loses alarm handling they had).
- **What was done:** `faults` table gained `severity`/`acked_at`/`acked_by`/`snoozed_until`
  columns (idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, no new migration framework). A
  rule can carry a free-form `severity` (defaults `"warning"`), copied onto each fault it opens at
  open time. Two new endpoints, `POST /fault/faults/{id}/ack` and `POST /fault/faults/{id}/snooze`
  (`{"minutes": N}`), both pure annotation — neither stops rule evaluation, closes the fault, or
  touches webhook delivery. `ui/alarms.html`/`ui/alarms.js`: a live-polling (5s) table with a
  status filter and Ack/Snooze buttons per row. Explicitly not built: paging/escalation chains,
  auto-reassignment, or any severity-driven routing — "severity" here is a displayed label, not a
  policy input to anything yet. That's a real gap left for a future pass if SkySpark-parity
  alarm routing becomes the priority, not silently folded into "done."
- **Live-verified (2026-08-28):** in a real Chrome session — real faults from the live DB
  rendered with correct severity badges, clicked Ack on a real open fault and confirmed it flipped
  to "Acked (`<user>`)" with the real gateway-forwarded `X-User` identity, confirmed a
  previously-snoozed fault's button correctly showed its countdown, and confirmed the status
  filter (open/resolved/all) round-tripped through the real API.
- **Also fixed as a blocker to verifying this live:** `gateway/nginx.conf`'s `http {}` block never
  had `include mime.types;` — every static file under `/ui/` (all of it, not just the new pages)
  was served as `Content-Type: text/plain`, which makes Chrome silently refuse to apply any
  linked stylesheet. Every page in `ui/` rendered completely unstyled before this fix. Also fixed
  `ui/auth.js`'s login-gate show/hide: it toggled the `hidden` attribute, but CSS cascade rules
  mean a UA-stylesheet `[hidden] { display: none }` always loses to *any* author rule setting
  `display` on the same element (origin is compared before specificity) — so a page-specific rule
  like `.console-main { display: block }` silently defeated `mainEl.hidden = true`, and the login
  form and page content rendered on top of each other simultaneously. Switched to inline
  `style.display` toggling, which is immune to this regardless of what a given page's CSS says.
- **Files:** `src/baler/faults.py`, `src/baler/fault_api.py`,
  `src/baler/fault_detector.py`, `fault-api-openapi.yaml`, `gateway/njs/policy.js`,
  `gateway/nginx.conf`, `ui/alarms.html`, `ui/alarms.js` (new), `ui/auth.js`, `ui/console.css`,
  `tests/test_faults.py`, `tests/test_fault_openapi.py`.

### 6. Axon → Python converter

- **Status:** v1 done (2026-09-06) — the structural/expression subset described below, as a
  from-scratch native Python parser, not the vendored-Fantom path this item originally proposed.
- **Why:** Section 03 of the review calls manual Axon-to-Python rewrite the single biggest cost
  center in a migration, bigger than any infrastructure step. Automating even a large *subset* of
  it directly attacks that cost, more so than any other item in this backlog.
- **What's actually bounded vs. not:** confirmed via `haxall/haxall`'s real GitHub source
  (`src/core/axon/fan/`) that Axon's grammar is not the obstacle — it's a small, open-source,
  recursive-descent parser (`parser/Tokenizer.fan` + `parser/Parser.fan`, ~40KB combined) over a
  clean AST (`ast/Call.fan`, `ast/Fn.fan`, `ast/Operators.fan`, etc.), and there's already an
  AST→Axon-source path (`ast/Printer.fan`) proving the AST is structured enough to walk. A v1
  converter is a bounded task: either shell out to Fantom to parse Axon into that AST (dumped as
  JSON) and write a Python code generator over it, or port the (small) grammar directly. The real
  scope driver is Axon's **builtin-function library** — `hisRead`, `readAll`, Folio filter
  queries, and the rest of Axon's standard functions have no 1:1 Baler equivalent (SPARQL
  over RDF vs. Folio's tag-filter query language) and need per-function mapping decisions, one at
  a time. That's real, enumerable work, not an open-ended unknown.
- **What (v1 scope):** convert the structural/expression subset that maps cleanly (arithmetic,
  comparisons, conditionals, local `def`s, lambdas) directly to Python AST nodes; for any call to
  an unmapped builtin, emit a flagged stub (e.g. a `# AXON: <original call>` comment plus a
  `NotImplementedError`) for manual completion instead of silently guessing. Even partial,
  review-required conversion is a large win over rewriting every rule from a blank file.
- **What was done:** `src/baler/axon_convert.py` — a hand-rolled recursive-descent
  tokenizer/parser for the v1 subset (arithmetic, comparisons, `if/else`, local `def`s via
  `do...end`, lambdas), building a Python `ast.Module` directly and rendering it with stdlib
  `ast.unparse()`. Deliberately **not** the vendored-Fantom-parser path this item originally
  proposed: this repo's CI has no Fantom toolchain to run a parse step in (confirmed —
  `fantom-his-ext/` is built by hand, never in CI), and there's no in-repo or fetchable Axon
  corpus to validate a vendored parser against either way (confirmed — no `.axon` files anywhere,
  Baler's own rules are already Python). A native port is the smaller, self-contained bet
  for a v1 whose real cost driver is builtin-function mapping, not the grammar. Any call to a
  builtin outside `MAPPED_BUILTINS` (starts with just `abs`) emits the flagged stub this item
  called for — a `# AXON: <original call>` comment plus a `NotImplementedError` — rather than
  guessing at `hisRead`/`readAll`/Folio-filter semantics.
- **Test coverage:** `tests/test_axon_convert.py`, 112 cases — every operator's precedence and
  associativity, all grammar productions, mapped vs. unmapped builtins (including nesting inside
  arithmetic, do-block bindings, both branches of a conditional, and as an argument to another
  call), string escapes, `null`, and 14 malformed-input cases. No real customer Axon corpus
  exists to validate against; this proves the converter handles the examples it was built
  against, not real migration-target rule bodies.
- **Known gap, verified live — null semantics don't match:** confirmed against a real Haxall
  instance (`docker run --entrypoint /haxall/bin/axon windrow-haxall:latest '<expr>'`, axon shell
  v4.0.6) that Axon's null handling is inconsistent by operator family, and this converter
  reproduces none of it. Arithmetic silently propagates null (`1 + null` → `null`, no error);
  ordering treats null as sorting below every number (`null < 1` → true, `1 < null` → false,
  `null <= null` → true); boolean logic does the *opposite* of arithmetic and errors on null
  (`not null`, `true and null` both raise `sys::NullErr`), except where short-circuiting skips
  the null (`true or null` → true). The converter maps every one of these straight to Python's
  operator, which either raises `TypeError` where Axon would silently produce `null`, or silently
  produces a value where Axon would raise. Fixing this needs a runtime null-aware layer for every
  operator, not just the arithmetic ones — bigger than v1's scope, so it's documented (see
  `axon_convert.py`'s module docstring) rather than fixed, and left here as a real backlog item
  for whoever picks this converter up next.

### 7. Put backup/restore on a schedule

- **Status:** done (2026-08-27)
- **Why:** `scripts/backup.py`/`restore.py` are solid, tested one-shot CLI scripts, but nothing
  runs them automatically — a real gap for anyone trusting a live migrated site to them. Least
  visible in a demo, most visible six months into production.
- **What was done:** a compose sidecar was ruled out — `backup.py` shells out to the *host's*
  `docker compose exec postgres pg_dump`, so running it inside a container would mean mounting
  the Docker socket/CLI into a new image just to keep that working, more attack surface than a
  backup job warrants. Went with a host-level scheduled job instead, reusing `backup.py`
  completely unchanged: `scripts/systemd/baler-backup.{service,timer}` (new, daily by
  default, `Persistent=true` so a missed run catches up on next boot) plus a cron one-liner
  documented as the non-systemd fallback. README's "Backup and restore" section has a new
  "Automating backups" subsection covering both. `restore.py`'s refuse-on-non-empty-target
  behavior is untouched. Not done: retention/pruning of `backups/` — flagged in the README as a
  known, deliberate gap, not implemented since it wasn't part of this item's scope.
- **Files:** `scripts/systemd/baler-backup.service` (new),
  `scripts/systemd/baler-backup.timer` (new), `README.md`,
  `docs-site/pages/backups.mdx` (new), `docs-site/zudoku.config.tsx`.

### 8. Ad-hoc `readAll(filter).hisRead(span)` — the SkySpark shell reflex

- **Status:** v1 done (2026-09-08)
- **Why:** the single most-used thing in a SkySpark shell isn't a rule or a view, it's typing
  `readAll(ahu and air).hisRead(thisMonth)` and looking at the charts. Item 6's converter
  deliberately stubs `hisRead`/`readAll` because there was no Baler equivalent to map
  *to* — the equivalent was "write a SPARQL query, then N `GET /history` calls, then chart it
  yourself," which is exactly the loss a migrating SkySpark user feels first and most often.
  Nothing in the UI let you say "show me this AHU" without already knowing its point URIs.
- **What was done:** `src/baler/hisquery.py` — a native parser for the two-call subset
  (`read`/`readAll` + Haystack filter grammar, `.hisRead(span)`, `.hisRollup(fold, interval)`),
  a filter→SPARQL compiler (marker tags as `haystack:hasTag`, CapitalCase names as Brick/PROJ
  `rdf:type`, `point`/`equip`/`site` structural aliases, `id ==`/`equipRef ==` refs, missing-tag
  comparisons false as in Haystack), and Axon `DateSpan` resolution (`today`...`pastYear`,
  `YYYY-MM-DD`, `YYYY-MM`, `YYYY`, `a..b`) to local-midnight half-open ranges in a request
  timezone. `timeseries.read_range_many`/`read_rollup` do multi-point range reads and
  `date_bin`/`date_trunc` server-side aggregation (booleans fold as 0/1 → duty cycle). Exposed as
  `GET /ingest/his` (`viewer` in `gateway/njs/policy.js`, documented in `openapi.yaml`), rendered
  by `ui/his.html` (canvas charts grouped by unit, per-point, grid + CSV, live refresh, span
  chips/rollup dropdown that rewrite the visible expression so the text is always the whole
  truth), deep-linked from every equipment in the tree and every point detail panel. Default
  span timezone via `BALER_TZ`. The one deliberate non-Axon behavior: matched
  *equipment* expands to its points (Axon errors on `read(ahu).hisRead(...)`) - that's the
  whole point of the feature.
- **Brick without the Brick spelling (2026-09-08, same pass):** the first cut matched a Brick
  class only by exact, case-sensitive `rdf:type` and a plain word only as a Haystack marker —
  so a Brick-only entity (loaded model, derivation output) was unreachable unless you knew
  `Zone_Air_Temperature_Sensor` verbatim, and `Temperature_Sensor` found no subclasses. Now
  `src/baler/brick_vocab.py` parses the vendored `ontology/Brick-only.ttl` (1,428
  classes, 531 tag words, ~70 ms, regex over the release's rdflib serialization) and the
  compiler matches every name three ways: as Brick's per-class tag words
  (`brick:hasAssociatedTag` — `zone and air and temperature and sensor`, `ahu`, with Haystack
  `temp`/`sp`/`cmd`/`equip` mapped, and `rules/haystack_*_to_brick.yaml` inverted so `run` →
  `Fan_Status`), as a case-insensitive class with its full subtree and `owl:equivalentClass`/
  `brick:isReplacedBy` aliases (`AHU` = `air_handling_unit` ⊇ `Rooftop_Unit`), and as a marker.
  Every hierarchy hop is resolved in Python and handed to SPARQL as a bound VALUES list joined
  once per leaf test (`OPTIONAL { SELECT DISTINCT ?s (true AS ?m) ... }` + `BOUND(?m)` flags):
  measured against the full ontology loaded into Oxigraph, an unbound `rdfs:subClassOf*` inside
  a filter takes 50+ s, per-candidate `EXISTS` with a VALUES list 0.4–5 s, this shape a flat
  ~0.15 s for any list size. Empty results carry `hints` (`Air_Handeling_Unit` →
  `Air_Handling_Unit`) that the UI renders as did-you-mean links. `tests/test_brick_vocab.py`
  pins the file facts relied on.
- **Brick extensions (2026-09-08, follow-up pass):** probing the above against the live stack
  with Brick *and* a Brick extension in its own namespace loaded (the way Brick's extension
  guidance says to write one) showed the extension was only half-reachable: its entities were
  found under the Brick parent, but not by the extension class's own name, not through an
  `owl:equivalentClass` alias it declared, and not by any `brick:hasAssociatedTag` word it
  declared — 16 of 24 probes failed. Separately, the "excluded by type" candidate filter
  (`FILTER NOT EXISTS { ?s a owl:Class/brick:Tag/sh:NodeShape }`) was where ~0.4 s of every
  query went with Brick loaded, and it still let Brick's quantity individuals (`a
  brick:Quantity`) and the embedded REC classes through, so `readAll(Quantity)` returned
  vocabulary and any `not ...` filter returned ~470 ontology subjects. Now
  `hisquery.graph_vocab` reads, in one query, every subClassOf/equivalentClass/hasAssociatedTag
  edge and schema-typed subject whose subject isn't Brick's own; the compiler expands class
  lists through it and resolves names and tag words against it; ontology subjects are dropped
  in Python after the query by namespace and by declaration. Measured with the full ontology
  loaded: 0.42 s → 0.07 s per query, 28/28 probes passing with and without Brick loaded.
  `docs-site/pages/his-query.mdx` now has a "Your own Brick extension" section including how to
  load one (a named graph of its own — `RemoteStore.load_ontology` replaces a single graph).
  Known, documented limit: subjects in the `brick:` namespace are skipped when reading the live
  graph, so a newer Brick loaded live doesn't extend the vendored 1.4.4 — bump the file.
- **Test coverage:** `tests/test_hisquery.py` (parser, compiler against an in-memory rdflib
  graph including Brick-only/PROJ-typed/ontology-subject fixtures, span math including leap
  years/year rollover, interval parsing, documented 400s, hints — no services),
  `tests/test_brick_vocab.py` (hierarchy/alias/tag facts against the vendored file), and
  `tests/test_his_route.py` (integration: equipment expansion, `read` 404, rollup arithmetic
  incl. boolean folding, per-point `limit`/`truncated`, `tz` shifting, derivation-engine points
  taking unit/label from history rows, a never-tagged `brick:RTU` found by `AHU`/`rooftop_unit`/
  `zone and temp and sensor` through live Oxigraph).
- **Not done, deliberately:** tag paths (`equipRef->dis`), arithmetic, any Axon function beyond
  the two, unit-aware comparisons (`temp > 72°F` compares the bare 72), time-of-day spans. Each
  is an explicit 400 naming what was rejected, never a silent approximation. Item 6's converter
  still stubs `hisRead`/`readAll` — wiring it to emit `hisquery.run_query(...)` calls for the
  subset this now covers is the obvious follow-on, not done here.
- **Files:** `src/baler/hisquery.py`, `src/baler/brick_vocab.py` (new),
  `src/baler/timeseries.py`, `src/baler/ingest_api.py`, `openapi.yaml`,
  `gateway/njs/policy.js`, `ui/his.html`, `ui/his.js`, `ui/his.css` (new), `ui/app.js`,
  `ui/style.css`, every `ui/*.html` nav, `tests/test_hisquery.py`, `tests/test_brick_vocab.py`,
  `tests/test_his_route.py` (new), `docs-site/pages/his-query.mdx` (new),
  `docs-site/pages/architecture.mdx`, `docs-site/zudoku.config.tsx`, `README.md`,
  `.env.example`, `docker-compose.yml`.

## Explicitly out of scope for this backlog

- **SSO/LDAP and encryption at rest** — real day-two gaps (Section 02/03 of the review), but
  bigger, more architecturally invasive efforts than the items above; not scoped here.

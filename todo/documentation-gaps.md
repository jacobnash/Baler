# Closing feature/documentation gaps

**Status:** item 1 (windrow.mdx nav) done (2026-08-27); items 2-6 not started. Backlog from a
2026-08-27 gap analysis of every feature (src/baler modules, scripts, compose services)
against every doc surface (docs-site/, README, OpenAPI specs), using the Diátaxis framework
(tutorial/how-to/reference/explanation) as the lens. Driven by two standing rules from this
session: every feature needs a how-to or API doc that lives in `docs-site/` (README-only isn't
enough — see `.claude` memory `feedback_docs_required_per_feature`), and every how-to should
carry a "why this design" beat, not just steps.

Motivation: if this project is open-sourced, docs are the first thing a skeptical evaluator
judges it by — a feature with zero documentation reads as unfinished or abandoned even when the
code itself is solid. Ranked by how much an evaluator finding nothing would hurt.

## Backlog

### 1. Register `windrow.mdx` in the docs-site nav

- **Status:** done (2026-08-27)
- **Why:** `docs-site/pages/windrow.mdx` existed and had a working OpenAPI spec wired at
  `/api/windrow`, but the page itself was never added to `zudoku.config.tsx`'s `navigation.items`
  array — reachable only by guessing the URL, invisible from the site.
- **What was done:** added `/windrow` to the nav array alongside the other 12 pages.
- **Files:** `docs-site/zudoku.config.tsx`.

### 2. Document `sql_puller` and `energystar_puller` — currently zero docs anywhere

- **Status:** not started
- **Why:** highest-risk gap. Both are real `docker-compose.yml` services with real
  `.env.example` entries, but nothing — not docs-site, not README, not even a code comment
  pointing outward — tells a human they exist, how to enable them, or why. These are also the
  two newest features in the repo (currently uncommitted). An evaluator who greps the compose
  file and finds an undocumented service reads that as "unfinished," regardless of code quality.
- **What:** a docs-site how-to for each (or one combined "SQL/ENERGY STAR pullers" page if
  they're similar enough in shape to `haystack-puller.mdx`), covering enable steps, required
  env vars, and *why* each source exists (what gap it closes vs. the Haystack/MQTT/HTTP paths).
- **Files:** new `docs-site/pages/sql-puller.mdx` and/or `docs-site/pages/energystar-puller.mdx`
  (register in `zudoku.config.tsx`), `src/baler/sql_puller.py`,
  `src/baler/sql_pull_state.py`, `src/baler/energystar_puller.py`,
  `src/baler/energystar_client.py`.

### 3. Document the derivation engine (`derivation_api.py`/`derivation_engine.py`/`sandbox.py`)

- **Status:** not started
- **Why:** "write custom rules in real Python" is a genuine differentiator vs. SkySpark's Axon
  (see `todo/skyspark-haxall-migration-gaps.md` item 4/6) — but it's currently reference-only
  (an OpenAPI spec at `/api/derivation`, a passing mention in `architecture.mdx`). There's no
  how-to explaining how to write/dry-run/deploy a rule, or an explanation of what `sandbox.py`
  actually restricts (and why that matters for anyone about to trust it with real building
  control logic). The single biggest pitch this project has is the hardest one to discover.
- **What:** a `derivation.mdx` how-to (write a rule, dry-run it, deploy it) plus an explanation
  beat on the sandbox's actual guarantees/limits.
- **Files:** new `docs-site/pages/derivation.mdx`, `src/baler/derivation_api.py`,
  `src/baler/derivation_engine.py`, `src/baler/sandbox.py`.

### 4. Document the tag-mapping engine (`mapping.py`/`autotag.py`/`llm_classifier.py`)

- **Status:** not started
- **Why:** core to the Brick-mapping pitch (rules-first, LLM fallback for anything the rule
  engine can't place) but only exists today as a passing mention inside `haystack-puller.mdx`
  and `adding-a-data-source.mdx`, which point at "the README's autotag section" — a section that
  doesn't actually exist as its own header, it's folded into "Real building data." No standalone
  explanation of the rules-first/LLM-fallback design exists anywhere.
- **What:** a `tag-mapping.mdx` explaining the rule engine, the LLM fallback, and how
  `rules/haystack_to_brick.yaml`/`rules/haystack_equip_to_brick.yaml` get extended.
- **Files:** new `docs-site/pages/tag-mapping.mdx`, `src/baler/mapping.py`,
  `src/baler/autotag.py`, `src/baler/llm_classifier.py`.

### 5. Move `load_bdg2_sample.py` off README-only

- **Status:** not started
- **Why:** direct violation of the standing "how-tos live in docs-site, not README-only" rule —
  currently only in README's "Real building data (optional)" section.
- **What:** either a small dedicated docs-site page, or fold it into whichever page ends up
  covering data-loading/demo setup broadly (don't create a page just for this alone if it fits
  naturally elsewhere — check before adding a new file).
- **Files:** `scripts/load_bdg2_sample.py`, README.md's existing section.

### 6. Document the web UI (`docs_ui.py`/`ui_server.py`)

- **Status:** not started
- **Why:** no docs-site page, no README section — the UI only appears as a line inside
  `architecture.mdx`'s service-topology description. If there's a UI worth demoing to a
  prospective adopter, it currently has no how-to at all.
- **What:** a how-to covering what the UI does today and how to reach it locally.
- **Files:** new `docs-site/pages/ui.mdx` (or similar), `src/baler/docs_ui.py`,
  `src/baler/ui_server.py`.

## Cross-cutting, not a numbered item

Most existing how-to pages (`haystack-puller.mdx`, `backups.mdx`, `weather-stations.mdx`,
`gateway-auth.mdx`, `validate.mdx`) read as step-list-first with little "why this design" —
only `architecture.mdx`/`introduction.mdx` carry real explanation content today. This is a
breadth problem across most pages, not a single item to fix once — apply the "why" beat as new
pages get written (items 2-4 above) and retrofit older pages opportunistically rather than as
one big pass.

## Confirmed clean (no action needed)

All 5 OpenAPI specs at the repo root are correctly wired into `zudoku.config.tsx`'s `apis`
array — nothing orphaned. `fault-detection.mdx`, `webhooks.mdx`, `validate.mdx`,
`gateway-auth.mdx`, `haxall-drop-in.mdx`, `weather-stations.mdx`, `adding-a-data-source.mdx`,
`multi-site.mdx` all correctly cover their feature with no README-only duplicate.

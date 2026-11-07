# Commissioning agent: research and plan (Baler#32)

Status: **slices Cx 1-10 built** (2026-11-06). Jacob answered the open questions on
2026-11-06 (see [Decisions](#0-decisions-jacob-2026-11-06)), and the
work is split into the shippable slices in [§0.2](#02-slices). This is a
living doc: keep it current as slices land. Where §5 and §11 below
disagree with §0, §0 wins. Those sections are kept as the original
research-time reasoning.

Supersedes nothing. The earlier attempt, PR #5 (branch
`cursor/commissioning-agent-6550`, closed as stale), is reviewed in
[§3](#3-what-pr-5-got-right-and-wrong) and mined for design and tests,
not rebased.

---

## 0. Decisions (Jacob, 2026-11-06)

Jacob's words are in quotes; the effect on the plan follows each one.

| # | Question | Decision | Effect on the plan |
|---|---|---|---|
| 1 | Scope | "agentic commissioning" | Build toward the full product: an agent that drives discovery, verification, issue tracking and test execution. The MVP is still the foundation, but every slice is designed as a tool the agent can call (Cx 10). |
| 2 | Placement | New `commissioning_api` on **8009** at `/cx/` | As proposed (§8). |
| 3 | Model | "inter-op preferred brick then haystack" | Brick classes first. Haystack markers and the ph Xeto spec are the second vocabulary on every inventory row, from `hisquery`'s Xeto table and the mapping rules reversed (the code `haystack_api` already used for `spec`, now lifted into `hisquery`). A point with no Brick class can still satisfy a template through its Haystack markers. No third model. |
| 4 | Phases | Explicit, "but make them manageable" | Phases are first-class rows per project (`cx_phases`): editable, reorderable and visible, with status and timestamps. Default list: design → construction → start-up → functional testing → acceptance → warranty → operations. Every run records the phase it ran in, and every phase change is an audited event. |
| 5 | Active tests | Yes, with the safeguards | `BALER_CX_ALLOW_WRITES`, admin only, a scheduled window, `expires_at`, guaranteed relinquish via `point_write` (§6.4, Cx 9). |
| 6 | Spec ingestion | "yes AI extraction" | LLM extraction from PDFs via the Claude API (latest model, `llm` extra, the `autotag` pattern), stored as proposals a human reviews. CSV/XLSX as the simple path (Cx 7). |
| 7 | Faults | Opt-in, confirmed equip only, "or allow you to have something to be ingested" | Mirroring as proposed (Cx 8), plus import of external issues and punch items (Cx 4) and of external or `fault_detector` faults as Cx issues (Cx 8). |
| 8 | Re-runs | "think about Power on Testing and all that start-up" | Start-up / power-on and functional tests become explicit procedures (Cx 6). On demand first, then scheduled. |
| 9 | Templates | SHACL in `ontology/` | `ontology/cx-expected-points.ttl` (Cx 1). The same file is valid SHACL for pyshacl and is evaluated subclass-aware by `commissioning.templates`. |
| 10 | GL36 | "YES YES YES": adopt PNNL ConStrain | Spike early (Cx 3): licence (BSD-2), how it runs, mapping its verification items onto Baler history. |
| 11 | Integrations | Unanswered | Keep CSV/JSON export first (Cx 4). |
| 12 | #5's tests | "GOOOD" | Port them as planned: ladder and points in Cx 2, spec/alignment/reconcile in Cx 7. |

### 0.1 Re-plan around the agentic goal

The agent (Cx 10) is the product. Every earlier slice is a capability it
calls through the same `/cx/` API a person uses:

| Capability | Slice |
|---|---|
| Inventory | Cx 1 |
| Templates and gaps | Cx 1 |
| Runs | Cx 1, Cx 2 |
| GL36 checks | Cx 3 |
| Issues and risks | Cx 4 |
| Test procedures | Cx 6, Cx 9 |
| Spec items and alignment | Cx 7 |

Design rules that follow from this:

- **Every capability is an API call that returns structured evidence.**
  The agent never reads raw SQL or SPARQL, so it is bounded by the same
  scope and role checks as a human.
- **Every write the agent makes is either a proposal** (spec items,
  mappings, issues, test procedures) that a human accepts, **or happens
  inside an existing safety envelope.** Passive runs are always allowed.
  Active tests only through Cx 9.
- **Every agent and human action lands in `cx_events`**, so "what did
  the agent do on this project" is one query.
- **The commissioning lifecycle is the phase list.** The agent's next
  step comes from the current phase:
  - *start-up*: power-on procedures, template gaps;
  - *functional testing*: FPT procedures, GL36 items;
  - *warranty and operations*: scheduled re-runs, fault ingest.

### 0.2 Slices

Each slice is a GitHub issue, and each is independently mergeable.

| Slice | Issue | What | Depends on |
|---|---|---|---|
| Cx 1 ✅ | #37 | Foundation: `src/baler/commissioning/`; `read_latest_many` moved to `timeseries`; Xeto matching lifted into `hisquery`; scoped inventory; SHACL expected-point templates (6 classes); `cx_projects`/`cx_phases`/`cx_events`/`cx_runs`/`cx_results`; `commissioning_api` :8009 with project/phase/inventory/run endpoints; rung-1 existence; compose, gateway, policy, OpenAPI | none |
| Cx 2 ✅ | #38 | Ladder rungs 1–3 over trends; stale/stuck predicates shared with `fault_detector`; port #5 ladder and points tests and `cx_fixtures` | Cx 1 |
| Cx 3 ✅ | #39 | PNNL ConStrain spike: GL36 items over Baler history, `gl36:<item>` results. As built: vendored subset (ConStrain can't co-install - rdflib 6 pin), `commissioning/gl36.py`, `cx-gl36` extra; write-up `todo/cx3-constrain-gl36-spike.md` | Cx 1 |
| Cx 4 ✅ | #40 | Issues log (close on evidence), accepted risks, CSV/JSON import of external issues and punch items, export. As built: `commissioning/issues.py` (rules, import header aliases, export shapes `baler`/`cxalloy`/`facilitygrid`), `cx_issues`/`cx_issue_events`/`cx_risks`, reconcile after every completed run (`runner.reconcile_issues`), risks lapse on read too; issues are opened from failing results by a person (one or all of a run), not automatically | Cx 1 |
| Cx 5 ✅ | #41 | UI: `cx.html`, phase bar, runs grid, tree badge, printable report. As built: `ui/cx.html`/`cx.js`/`cx.css`; `GET /cx/runs/{id}/report?format=json\|html` (`commissioning/report.py`, CSP `default-src 'none'`), `GET /cx/projects/{id}/gaps`, `GET /cx/equipment-status` (the tree's dot) | Cx 1 |
| Cx 6 ✅ | #42 | Start-up / power-on and functional test procedures as data; passive execution on demand, then scheduled; run diff (reconcile). As built: `commissioning/procedures.py` (12 seeds: power-on and start-up for AHU, VAV, FCU, chiller, boiler, pump; criteria are Cx checks or value ranges, incl. a named derivation output point), `cx_test_procedures`/`cx_test_runs`, `runner.Scheduler` thread (procedure `schedule.every_hours`, project `settings.schedule.every_hours`), `GET /runs/{id}/diff` | Cx 2, Cx 4 |
| Cx 7 ✅ | #43 | Spec ingestion: Claude PDF extraction as proposals, CSV/XLSX; spec↔device alignment; `record_edit` write-back. As built: `commissioning/spec.py` (tags, CSV/XLSX, alignment, points), `commissioning/spec_llm.py` (PDF via the Claude API, structured output, server-side refusal fallbacks, `BALER_CX_MODEL`), `cx_spec_documents`/`cx_spec_items`/`cx_mappings`; alignment is computed on request, only a person's confirm/reject is stored; write-back sets `cxSpecTag` (and the class when the equipment has none) via `record_edit` + `record_edits` | Cx 1, Cx 4 |
| Cx 8 ✅ | #44 | Opt-in fault mirroring (real URIs); ingest of faults as Cx issues. As built: `commissioning/mirror.py`; `settings.fault_mirroring` (`equipment: confirmed` = confirmed against the spec in Cx 7, or `all`); rule ids `cx:<check_id>`; webhooks via fault_detector's delivery; accepted risks suppress; `POST .../faults/ingest` for `fault_detector` (closes issues when the fault resolves) or an outside FDD tool | Cx 2, Cx 4 |
| Cx 9 ✅ | #45 | Active tests via `point_write` inside the safety envelope. As built: `commissioning/active.py`; `cx_active_runs`; plan (dry run) → admin confirms the run id inside its window → background executor writes at priority 8 as `cx:<run>`, relinquishes per step and per run in `finally`; sweeper on the scheduler tick; per-run abort and org-wide kill switch; before-state; `BALER_CX_ALLOW_WRITES=1`; rung 4 = `command_response` | Cx 6 |
| Cx 10 ✅ | #46 | The commissioning agent (Claude tool use over `/cx/`). As built: `commissioning/agent.py` (18 tools: reads incl. phase gates and open faults, passive runs and test runs, and `propose`; an accepted phase proposal passes the #86 gate like POST .../phase), `cx_agent_sessions` (API transcript replayed unchanged) and `cx_agent_proposals` (issue, procedure, phase, spec_item, mapping, active_test - accepted as the person; an active test becomes a *planned* Cx 9 run); every tool call is `agent.<tool>` in `cx_events`; Assistant panel on cx.html | Cx 1–Cx 9, can start after Cx 4 |

**As built (Cx 2):** rungs are Brick-keyed (`commissioning/ladder.py`).
A point's role comes from its Brick class family (Sensor, Setpoint,
Command, Status), with Haystack markers as the fallback. Pairs found on
one piece of equipment are command↔status (fan, pump, run) and
sensor↔setpoint (zone air temperature, supply air temperature, duct
static pressure, airflow). When a pair is ambiguous, for example heating
and cooling setpoints with neither an exact match, the result is
*inconclusive* rather than a guess. Stale and stuck share one definition
with `fault_detector` (`baler/his_checks.py`). #5's name-classification
tests (`points`/`vocabulary`) move to Cx 7, where vendor names are
matched to spec tags. Their role expectations are ported now, against
Brick classes.

**Slice 1 deviations from §6.1:**

- `cx_projects.site_id` is nullable. A site-less project is org-wide,
  hidden from site-restricted logins, and its runs use the caller's
  scope.
- `cx_issues`/`cx_issue_events`/`cx_risks` move to Cx 4.
- Phases are in `cx_phases` instead of a `phase` column.

---

## 1. How commissioning works in practice

### 1.1 The process standards

- **ASHRAE Standard 202** (first published 2013, current edition
  202-2024, *The Commissioning Process Requirements for New Buildings
  and New Systems*) sets the *minimum* Cx process. It is a process
  document, not a technical one. It defines 13 steps, each with
  deliverables. They run from roles and the Owner's Project
  Requirements (OPR), through the Cx plan, Basis of Design (BoD),
  contractor Cx requirements, design and submittal review, and on to
  construction-phase verification and the Cx report. The 2024 edition
  clarifies construction-phase activities, ongoing Cx, and preliminary
  versus final reports. [CSE][s202-cse], [HPAC][s202-hpac],
  [Nimonik listing][s202-2024]
- **ASHRAE Guideline 0-2019** describes how to carry that process out,
  verifying "that the building and its systems meet the OPR". The OPR
  is the baseline everything later is checked against: BoD review, Cx
  plan, pre-functional checklists, functional performance tests,
  systems manual. The BoD is the design's response to the OPR.
  [ASHRAE news][g0-news], [CxPlanner summary][g0-cxplanner]
- **Guideline 1.1** adds HVAC&R technical detail to Guideline 0 and
  Standard 202 for new systems. Its annexes hold more than 100 pages of
  sample forms and checklists. Existing buildings are covered by
  **Guideline 0.2** (process) and **Guideline 1.2** (technical), which
  is the retro-/re-Cx track. [CSE][g11-cse], [1.1-2007R review
  draft][g11-draft]
- **Guideline 36** (high-performance sequences of operation) is the
  sequence library that functional tests increasingly check against.
  NIST built and validated GL36 functional tests with the HVAC-Cx tool
  in a virtual testbed. [NIST][g36-nist]

These are paid ASHRAE documents. Summaries above are from secondary
sources. Check exact definitions against the standard text before
quoting them in product copy.

### 1.2 The verification steps, in order

| Step | What happens | Evidence produced | Who |
|---|---|---|---|
| **Pre-functional checklists (PFC)** | Equipment installed, wired, powered, started up. "Completed as part of startup and initial checkout, preparatory to functional testing." Typically due before FPTs (one spec: at least one week before). | Signed checklist rows, photos, nameplate data | Installing contractors, witnessed by the CxA |
| **Point-to-point (P2P) checkout** | Each physical I/O point (AI/AO/BI/BO) reads or strokes correctly *at the BAS front end*: a sensor compared to a reference instrument, an actuator commanded and observed. Points inside packaged-equipment controllers are usually exempt except actuator positions. | Per-point pass/fail with the measured versus BAS value | Controls contractor, CxA spot-checks |
| **Trend-based pre-verification** | The CxA asks the controls contractor to trend each system for a period *before* FPTs, then reviews the trends to catch obvious problems before scheduling field time. | Trend logs per controller | Controls contractor plus CxA |
| **Functional performance tests (FPT)** | Systems and sequences verified "as specified, and operate according to the OPR, BoD, and plans". Traditionally scripted, hands-on, and **sampled** (a percentage of VAV boxes, for example). | Test scripts with expected and actual results, issues | CxA leads, contractors operate |
| **Issues log and resolution** | Every deficiency is tracked to closure, often with re-test. | Issues log | Everyone |
| **Cx report, systems manual, training, seasonal and deferred testing** | Final report, plus tests that need weather the project never saw. | Report | CxA |
| **Ongoing / monitoring-based Cx (MBCx)** | Continuous FDD, energy information systems (EIS) and trend analysis after handover. | FDD findings, savings | Owner/operator, MBCx provider |

Sources: [Northwestern 25 08 00 spec][nu-spec], [UH 25 08 00
spec][uh-spec], [Everett spec][everett], [Energy Code Ace
FPT][eca-fpt], [ACHR "unsampled systems"][achr-unsampled], [LBNL MBCx
FAQ][lbnl-mbcx].

Data centers formalize the same ladder as **L1–L5**: L1 is factory
witness, L2 installation, L3 start-up / pre-functional, L4 functional
performance, and L5 integrated systems test. Labels vary by client.
[Archdesk][dc-levels]

### 1.3 Where software fits today

**Cx management platforms.** These are workflow tools: checklists,
tests, issues, documents, and field apps.

- **CxAlloy.** Cloud plus an iOS app that works offline. Its core
  objects are checklists, tests, issues and asset tracking.
  [CxAlloy][cxalloy], [App Store][cxalloy-ios]
- **Bluerithm.** Customizable forms and workflows, data centers L1–L5,
  AI-generated checklists and test procedures, offline logging. It
  frames P2P checkout as per-device progress, results and issues on a
  shared platform. [Bluerithm platform][bluerithm],
  [Bluerithm P2P][bluerithm-p2p]
- **Facility Grid.** "Operational readiness": it ties installation
  status, functional testing, acceptance milestones and documentation
  into system-level readiness, and imports existing spreadsheets.
  [BuiltWorlds][facilitygrid]
- **CxPlanner.** Presented a "CxAI (Commissioning AI Agent)" session
  at BCxA 2025. [CxPlanner][cxplanner-bcxa]

**What they share:** they record what a human did and saw. None of
them is the system of record for the BAS data itself. Verification
evidence arrives as typed values, photos and attached trend exports.

**Analytics/FDD tools (MBCx).** These cover SkySpark, Clockworks, KGS,
Cimetrics and similar. Rules run continuously over BAS trends.
SkyFoundry's case studies include SkySpark rules catching VAV boxes
that could not reach commanded airflow, "missed in that commissioning
process", and MBCx after a retro-Cx study. [SkySpark Cx case
study][skyspark-cx], [Denver case study][skyspark-denver]. I found no
primary sources for Clockworks, KGS or Cimetrics in this pass, so
treat them as category examples only. LBNL/DOE's Smart Energy
Analytics Campaign covered 96 organizations and 518M sq ft. It
reported median savings of **9% for FDD** and 3–4% for EIS, with
roughly 2-year simple payback. [LBNL SEAC year 3][seac-y3],
[DOE][seac-doe]

**Automated functional testing (AFT).** Scripts force modes or
setpoints through the BAS and judge the trend response. AFT removes
sampling (100% coverage) and backs every result with data. The I2SL
material recommends Brick or Haystack metadata as the prerequisite for
doing this at scale. [NREL slides via search summary][nrel-aft]
(docs.nrel.gov is not reachable from this sandbox, so this cites the
search result, not the PDF). Examples:

- **pingCx (Ping Building Systems)** automates start-up, P2P and Cx
  from an uploaded BACnet points database "regardless of naming", and
  claims up to 25% labor savings (vendor claim). [pingCx][pingcx]
- **PNNL ConStrain** (formerly ANIMATE, **BSD-2-Clause**, Python) is a
  library of control-verification algorithms built on GL36-2021 and
  90.1-2016. It covers supply air temperature control, economizer high
  limit, integrated economizer, zone deadband and setback, and HW/CHW
  reset. It ships a Brick demo notebook. [GitHub][constrain],
  [JOSS][constrain-joss]
- **LBNL OpenBuildingControl** aims to standardize how sequences and
  functional tests are expressed, for automated implementation and
  automated testing. [DOE slides][obc]

### 1.4 What this means for Baler

The gap between the two software categories is the opportunity. Cx
platforms hold the *process* but not the *data*. FDD tools hold the
data but not the *process*: there are no OPR, issues-to-closure or
acceptance concepts. Baler already sits on the data (Windrow
discovery, a Brick graph, full point history) and on part of the
process: faults with ack/snooze, record audit, per-org scoping.

The cheapest high-value slice is **trend-based verification with an
issues log**. It is the step the CxA already does by eye
(§1.2, "trend-based pre-verification"), it needs no field hardware and
no writes, and it covers 100% of points instead of a sample. It does
not replace PFCs, the physical half of P2P, or the Cx management
platforms. Don't build a forms or checklist product to compete with
CxAlloy.

---

## 2. What Baler already provides

| Cx need | Baler today | Where |
|---|---|---|
| Device and point inventory | Windrow BACnet/Modbus discover → learn → connection; periodic discovery with "built for new-construction commissioning" factory-default credential checks | `windrow/src/windrow/discovery.py`, `docs-site/pages/windrow-periodic-discovery.mdx` |
| A semantic model of equip and points | Brick classes plus Haystack tags in Oxigraph; `brick:hasPoint`/`hasPart`/`feeds`; `mapping.classify_point`; vendored `ontology/Brick-only.ttl` with `brick_vocab` for subclass lists | `mapping.py`, `brick_vocab.py`, `store.py` |
| Trend data | `point_history` (Timescale); `timeseries.read_range_many`, `read_rollup`; a batched latest-value read **exists but lives in `haystack_api.read_latest_many`**, not in `timeseries` | `timeseries.py`, `haystack_api.py:413` |
| Rule evaluation over trends | `fault_detector`: `range` (cur), `stuck`/`stale` (his), `applies_to.brick_class`; faults with severity, ack, snooze and webhooks | `fault_detector.py`, `faults.py`, `fault_api.py` |
| Computed quantities | Derivations (`formula`/`rollup`, sandboxed `fn_source`, `/dry-run` against live data) | `derivation_api.py`, `derivation_engine.py` |
| Model completeness checks | SHACL over the live graph, results filtered by scope | `shacl_validate.py`, `validate_api.py` (8005) |
| Multi-tenant scoping | `Scope` from the caller's own JWT; `Scope.sparql(var)` inside every graph read; `Scope.allows(uri)` for points and equip | `scope.py`, `hisquery.py` |
| Corrections to the model, audited | `record_edit.apply_edit`/`create_record` plus the `record_edits` log (who, when, detail) | `record_edit.py`, `ingest_api` `/records` |
| Commanding points | Haystack `pointWrite` → Windrow `point_write` over MQTT, a 16-level priority array, `expires_at` so a stale command is refused, Baler-side record of who wrote what | `point_write.py`, `windrow/src/windrow/api.py` |
| Background jobs and configured integrations | Source registry plus `source_runner` (per-source interval and health, Fernet-sealed secrets) | `sources.py`, `source_runner.py` |
| Auth and roles | nginx/njs JWT, viewer < operator < admin, per-route policy table | `gateway/njs/policy.js` |
| UI patterns | Ops tabs (`ops.html` + `users.js`/`sources.js`), equipment tree (`app.js`), alarms console, record editor | `ui/` |

**Missing for Cx:**

1. A **project/run/result** concept, meaning "verification of site X as
   of time T". Faults are open/closed state, not a dated, reproducible
   report.
2. **Pair and relationship checks** (status follows command, sensor
   tracks setpoint). `fault_detector` only looks at one point at a
   time.
3. **Expected-point templates**: "an AHU should have SAT, SAT-SP, SF
   status/command, DSP". SHACL is the natural home for this and
   partly covers it.
4. An **issues log** (punch list) with states through re-test and
   closure, and **accepted risks** kept apart from faults.
5. **Spec ingestion** (point lists, equipment schedules, sequences) and
   spec↔device alignment.
6. **Active tests** (scripted overrides), now *possible* thanks to
   `point_write`, but with no safety envelope for test use.
7. **Org-scoped configuration.** `rules.json`/`derivations.json` are
   global files shared by every org. Cx definitions can't live there.

---

## 3. What PR #5 got right and wrong

Branch `cursor/commissioning-agent-6550`, 7 commits, about 5.7k lines
under `src/timberdoodle/commissioning/`. It predates the rename,
scoping (#16), record audit (#19/#24), sources (#20) and `point_write`.

### Right (keep the ideas)

- **Honesty rules.** Symptoms are certain and causes are candidates,
  each with "what would distinguish it". No data counts as
  *inconclusive*, never *pass*. Freshness statements say what was
  observed ("1 of 2 fresh, 0 stale, 1 never seen"), never "all good".
- **The four-rung ladder**, climbed in order: existence → liveness →
  responsiveness (status follows command, sensor near setpoint) →
  command response. A rung-3/4 failure whose sensor is flat is reported
  as the rung-2 fault it really is (`fell_to_rung`). Rung 4 was
  *passive*: it used the building's own setpoint changes (setback,
  warm-up) as the stimulus.
- **Explicit phase** (new construction / warranty / operations), stated
  or marked `ASSUMED`. Absence means opposite things in each phase.
  `cidr_scopes` is a claim about what was scanned, not about the
  building.
- **Alignment confidence** (`confirmed`/`high`/`medium`/`low`/
  `unmatched`) with named alternatives and an evidence ladder: tag
  variants, point signature, unit plausibility, topology, vendor
  naming. A different number in the candidate's name scores *down*.
- **Reconcile states**: `unchanged`/`new`/`drifted`/`re_addressed`/
  `missing` (held two passes and 12 h before `absent`)/`absent`/
  `returned`, compared by signature. Nothing is dropped on one miss.
- **Faults versus accepted risks are never merged.** A covered fault is
  reported under the risk with its owner and review date.
- **Punch items** carry `where`/`what_to_find`/`what_to_capture`/
  `what_would_resolve`, route-ordered. Questions carry ready-to-POST
  `accept`/`correct`/`reject` bodies, and corrections re-score the
  whole building and report `confidence_movement`.
- **A `History` protocol seam** (`latest`, `window`) with an
  `InMemoryHistory`. That made every rung unit-testable without
  Postgres.
- **Deterministic passes** given the same inputs.

### Wrong (don't repeat)

- **No site scoping.** Discovery read the whole graph. Fault mirroring
  used `point_uri = cx:<project>:<entity>`. Under today's `Scope`, a
  non-`urn:point:`/`urn:equip:` URI is *visible to every org without an
  allow-list* (`Scope.allows` returns `prefixes is None`) and hidden
  from orgs with one. That is a cross-org leak in the alarm console in
  both directions. Mirror faults against real `urn:equip:…` /
  `urn:point:…` URIs and do every graph read through `Scope.sparql`.
- **Its own timeseries helpers.** It added a second
  `read_latest_many` to `timeseries.py` while `haystack_api` grew its
  own. Today there is one (in `haystack_api.py`), and it should move
  into `timeseries.py` and be shared, not duplicated a third time.
- **Port 8008**, now `haystack_api`.
- **A parallel vocabulary.** `vocabulary.py` (730 lines) plus a
  "canonical model is not Brick" with Brick as a lossy projection. That
  duplicates `mapping.py`/`brick_vocab.py`/`rules/*.yaml` and creates a
  second source of truth that has to be synced into the graph.
  Recommendation (Q3): Brick classes *are* the vocabulary. Keep
  confidence, alternatives and provenance in Cx tables keyed by the
  equip/point URI, not in a shadow model.
- **Writes to the graph bypassed `record_edit`**, so they had no audit
  trail. Any write-back now goes through `apply_edit`/`create_record`
  plus `log_edit`.
- **Too much at once.** Spec model, alignment, ladder, reconcile,
  captures, projections, a daemon, an API, a gateway route and docs
  all landed in one PR, before anyone could use the smallest piece.
  This is why it went stale.
- **A separate reconciler daemon** (`COMMISSIONING_PASS_INTERVAL`)
  before there was a reason to re-run unattended.
- **"No point-write transport"** is no longer true (`point_write.py`).
  Rung 4 can become active later, but needs its own safety design
  (§6.4).

### Tests

The branch has 11 `tests/test_commissioning_*.py` files. #32 cites 151
tests; I count 92 `test_` functions plus a few `parametrize` cases.
Reconcile the number when porting. They also include
`tests/cx_fixtures.py`, a small realistic building: AHU-1 with
VAV-1-01/-02, a chiller the network hasn't seen, a non-networked EF,
field names that differ from spec tags (`V1_1`), one wrong box
(`V1_3` ≠ `VAV-1-02`), and hourly history with one flat sensor. Reuse
plan in §8.

---

## 4. Goals and non-goals

**Goals**

1. Answer "is this site's BAS verified, and how sure are we?" from data
   Baler already has, for 100% of points, with a dated, reproducible
   report.
2. Turn every failure into a tracked **issue** that closes only on
   evidence (a later passing run) or on an explicit, owned, dated
   **accepted risk**.
3. Respect every existing boundary: per-org scoping, gateway roles,
   the audit log, and stdlib HTTP services.
4. Reuse fault-rule semantics and derivations instead of a third
   evaluation engine.
5. Grow, in later phases, toward spec↔device alignment and agent-driven
   Cx (the "agentic commissioning" pivot) without re-architecting.

**Non-goals (for now)**

- A general Cx management platform: OPR/BoD authoring, design and
  submittal review, PFC forms, training records, document control.
  Integrate with CxAlloy, Facility Grid and similar instead (export
  issues).
- Replacing physical checks: reference-instrument calibration,
  nameplate verification, the "stroke and look" half of P2P.
  Field captures come later (§5, Phase 2).
- Writing to controllers in the MVP.
- Contractual sign-off. The report is evidence for the CxA, not a
  certificate.
- LLM anything in the MVP.

---

## 5. User stories and phasing

### Personas

- **Cx agent (CxA)**, third-party or MEP firm: needs coverage, evidence
  and an issues log they can hand to contractors and the owner.
- **Operator** (facilities tech, controls contractor): fixes issues and
  wants to know exactly what to look at and whether the fix worked.
- **Owner / FM manager**: wants status, open risk, and proof at
  handover and warranty end.

### Stories

| # | As a… | I want… | Phase |
|---|---|---|---|
| U1 | CxA | to start a Cx project for a site I have access to, with a stated phase, and run verification over the last N hours | MVP |
| U2 | CxA | each equip's points checked for existence, liveness and pair behaviour, with pass/fail/**inconclusive** and the evidence (window, sample count, values) | MVP |
| U3 | CxA | to see which expected points each equip class is missing ("AHU without DSP") | MVP |
| U4 | Operator | each failure as an issue with symptom, candidate causes, and what would resolve it | MVP |
| U5 | Operator | to mark an issue *ready for retest* and have the next run close it if the check passes | MVP |
| U6 | Owner | to accept a risk on an issue with owner, reason and review date, kept visibly separate from faults | MVP |
| U7 | Owner/CxA | a printable/JSON report: coverage, pass rates by system, open issues, accepted risks, freshness statement | MVP |
| U8 | Operator | Cx failures on confirmed equipment in the alarm console and webhooks | MVP (optional, Q7) |
| U9 | CxA | scheduled re-runs and a diff against the last run (reconcile states) | Phase 2 |
| U10 | CxA | to import a point list / equipment schedule (CSV/XLSX) and see spec↔field alignment with confidence and alternatives | Phase 2 |
| U11 | CxA | to confirm/correct a mapping and have it written to the graph through `record_edit` with audit | Phase 2 |
| U12 | Field tech | a route-ordered punch list and photo/nameplate captures that close items | Phase 2 |
| U13 | CxA | to run a scripted active FPT (override, observe, auto-release) on a scheduled window with explicit approval | Phase 3 |
| U14 | CxA | GL36 sequence verification (economizer, SAT reset, …) over trends | Phase 3 |
| U15 | CxA | an agent that drafts mappings, FPT scripts and issue text for my approval, and exports to CxAlloy/Facility Grid | Phase 4 |

### MVP: "trend verification report + issues log"

Smallest shippable slice, read-only towards the BAS, on existing data:

1. **Library `src/baler/commissioning/`** (pure where possible):
   - `history.py`: the #5 `History` protocol with a
     `PostgresHistory` over `timeseries.read_range_many` and
     `read_latest_many`. As part of this, move `read_latest_many` from
     `haystack_api.py` into `timeseries.py` and have `haystack_api`
     import it.
   - `inventory.py`: equip → points for a site via SPARQL with
     `Scope.sparql` applied to both equip and point variables (Brick
     class of each point from the graph).
   - `checks.py`: rungs 1–3, ported from #5's `ladder.py` and
     re-keyed on Brick classes. Rung 1 *existence* and rung 2
     *liveness* share predicates with `fault_detector`'s `stale`/
     `stuck` (§7). Rung 3 *pairs* cover Command↔Status (fan, pump,
     enable) and Sensor↔Setpoint (zone, SAT, DSP), with tolerances
     from defaults per class.
   - `templates.py`: expected points per equip class (a short list
     for AHU, VAV, FCU, chiller, boiler, pump), as data. These may be
     emitted as SHACL shapes so `validate_api` can report them too
     (§7.3).
   - `report.py`: summary, coverage, freshness statement (from #5).
2. **Service `commissioning_api` on port 8009**, routed at `/cx/`
   (Q2). ~~Synchronous `POST …/runs`. No daemon.~~ Runs became
   asynchronous in #57 (55 s on nash-srv against a 60 s gateway timeout):
   `POST …/runs` → 202 `queued`, worker threads in `commissioning_api`
   (`commissioning/runner.py`), poll `GET /cx/runs/{id}`. Still no daemon.
3. **Postgres tables** (§6.1): projects, runs, results, issues, issue
   events, risks.
4. **UI `ui/cx.html` + `ui/cx.js`**: project list, run button, results
   grid by equip, issue list with state transitions, and an accept-risk
   dialog. Plus a Cx status badge per equip in the `app.js` tree.
5. **Optional fault mirroring** (Q7): a failed check on equip with
   `confirmed` identity (in the MVP, all graph equip counts as
   confirmed) opens a fault with `rule_id = cx:<check_id>` and
   `point_uri` set to the real `urn:equip:`/`urn:point:` URI. Accepted
   risks suppress mirroring.

Explicitly not in the MVP: spec import, alignment, reconcile states
across runs (only "issue still failing / now passing"), captures,
active tests, LLM, scheduled runs, and graph write-back.

**Size estimate:** library about 800–1,000 lines, API about 400 lines,
UI about 500 lines, tests about 60. That is roughly two to three
focused sessions, each landing independently (library+tests →
API+gateway+compose → UI).

### Phase 2: spec and alignment

- Spec import as point list / equipment schedule CSV/XLSX, stored as
  `cx_spec_items`. Optionally later a `sources` kind (`cx_spec`) if it
  needs to re-pull from a shared drive.
- Port #5's `spec_model`, `alignment` (with its tests) and `reconcile`,
  re-keyed on Brick classes and equip URIs. Confidence and
  alternatives live in `cx_mappings`.
- Confirm/correct writes `brick:` relations/tags through
  `record_edit.apply_edit` + `log_edit` (`user_sub` from the JWT).
- Punch list with route ordering (from #5's `punchlist`), field
  captures (photos and legible-only fields, the #5 rules), and a
  read-only offline-friendly page.
- Scheduled re-runs: a `cx` job in an existing loop, or a small
  `commissioning_runner` (Q8).
- Issue export (CSV/JSON) shaped for CxAlloy/Facility Grid import.

### Phase 3: active and sequence testing

- FPT scripts as data: precondition, override(s) via `point_write` at a
  fixed priority (8, manual operator) with `expires_at`, observe
  window, expected response (a check from `checks.py`), and an
  always-run release step. Requires admin, a per-run confirmation, a
  schedule window, a kill switch, and "before" state captured. Every
  write is logged and the run report shows exactly what was commanded
  (§6.4).
- Rung 4 becomes active when allowed and stays passive otherwise.
- GL36 sequence checks: evaluate PNNL ConStrain (BSD-2, so compatible
  with AGPL) as a library for economizer, SAT/CHW/HW reset and zone
  deadband verification, fed from `PostgresHistory` + Brick bindings,
  rather than writing those algorithms ourselves.

### Phase 4: agentic Cx

- An LLM agent (the existing `llm` extra) proposes alignments, drafts
  FPT scripts from sequence text, writes issue narratives, and drafts
  the Cx report. **Every proposal is a pending item a human accepts**,
  the same pattern as `autotag` (proposes, never edits live).
- Integrations with Cx platforms (API push of issues/results) if a
  customer needs them.

---

## 6. Data model

All Postgres, idempotent `ensure_schema` DDL (the house style), in the
same database. All rows carry `org_id` and `site_id`. Scoping is
enforced twice: projects are filtered by the caller's org/sites, and
every entity URI by `Scope.allows`. Check definitions are **not** in a
global JSON file (unlike `rules.json`), because that would leak across
orgs.

### 6.1 MVP tables (sketch)

```sql
CREATE TABLE IF NOT EXISTS cx_projects (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL REFERENCES orgs(id),
    site_id     TEXT NOT NULL REFERENCES sites(id),
    name        TEXT NOT NULL,
    phase       TEXT NOT NULL,          -- new_construction | warranty | operations
    phase_assumed BOOLEAN NOT NULL DEFAULT false,
    settings    JSONB NOT NULL DEFAULT '{}',  -- window_hours, tolerance overrides
    created_by  TEXT, created_at TIMESTAMPTZ NOT NULL, archived_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS cx_runs (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES cx_projects(id),
    started_at  TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ,
    window_start TIMESTAMPTZ NOT NULL, window_end TIMESTAMPTZ NOT NULL,
    triggered_by TEXT,                  -- user_sub or 'schedule'
    summary     JSONB,                  -- counts, coverage, freshness statement
    check_versions JSONB                -- which check/template versions ran
);
CREATE TABLE IF NOT EXISTS cx_results (
    run_id      TEXT NOT NULL REFERENCES cx_runs(id),
    equip_uri   TEXT NOT NULL,          -- urn:equip:... (scope-checkable)
    check_id    TEXT NOT NULL,          -- e.g. exists, live, pair.cmd_status, template.ahu
    subject     TEXT NOT NULL,          -- point uri, or pair "a|b", or equip uri
    rung        SMALLINT,
    status      TEXT NOT NULL,          -- pass | fail | inconclusive | not_applicable
    fell_to_rung SMALLINT,
    evidence    JSONB NOT NULL,         -- window, n_samples, stats, values used
    symptom     TEXT, candidates JSONB, -- certain symptom; candidate causes + distinguisher
    PRIMARY KEY (run_id, check_id, subject)
);
CREATE TABLE IF NOT EXISTS cx_issues (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES cx_projects(id),
    equip_uri   TEXT NOT NULL, check_id TEXT, subject TEXT,   -- NULL check = manual issue
    status      TEXT NOT NULL,          -- open | in_progress | ready_for_retest | closed | risk_accepted
    title       TEXT NOT NULL, detail JSONB,
    assignee    TEXT,
    opened_run  TEXT, closed_run TEXT,  -- evidence for open/close
    created_at  TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS cx_issues_open_unique
    ON cx_issues (project_id, check_id, subject) WHERE status <> 'closed';
CREATE TABLE IF NOT EXISTS cx_issue_events (   -- audit, like record_edits
    id BIGSERIAL PRIMARY KEY, issue_id TEXT NOT NULL REFERENCES cx_issues(id),
    ts TIMESTAMPTZ NOT NULL DEFAULT now(), action TEXT NOT NULL,
    user_sub TEXT, user_email TEXT, detail JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS cx_risks (
    id          TEXT PRIMARY KEY,
    issue_id    TEXT NOT NULL REFERENCES cx_issues(id),
    accepted_by TEXT NOT NULL, reason TEXT NOT NULL,
    review_by   DATE NOT NULL, revoked_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL
);
```

Rules:

- A result is immutable once its run finishes. Issues reference runs as
  evidence.
- An issue closes only when a later run shows `pass` for the same
  `(check_id, subject)`. *Inconclusive* never closes it. Manual issues
  close by hand with an event.
- `risk_accepted` issues stay visible. When their check fails again it
  is reported under the risk ("covered this run"), not as a new issue,
  until `review_by` passes. After that the issue reopens.

### 6.2 Phase 2 additions

- `cx_spec_items` (tag, type → Brick class, location, expected points,
  source doc/page).
- `cx_mappings` (spec item ↔ equip URI, confidence, basis,
  alternatives, confirmed_by).
- `cx_captures` (location, legible/partial/unreadable, photo refs).
- `cx_entity_state` (reconcile status per equip per run).

### 6.3 Graph writes

The MVP makes none. In Phase 2, confirmed mappings and corrected
classes go into Oxigraph via `record_edit` only, so they show up in
`GET /ingest/records/edits` like any other edit. No `cx:` namespace
in the graph. Provenance stays in Postgres.

### 6.4 Active-test safety (Phase 3, design constraint now)

Writes only through `point_write` (Windrow `point_write`, priority
array, `expires_at`). Enforced constraints:

- admin role;
- the project's org owns the point (`Scope.allows`);
- a scheduled window;
- maximum override duration (`expires_at` ≤ window end);
- automatic relinquish in a `finally`, plus a sweeper that relinquishes
  any Cx-priority level older than its run;
- a dry-run that lists every write before confirmation;
- a global `BALER_CX_ALLOW_WRITES=1` deployment switch, the same
  pattern as `BALER_ALLOW_PRIVATE_WEBHOOKS`.

---

## 7. How verification reuses fault rules and derivations

### 7.1 Fault-rule semantics

`fault_detector._evaluate_stale`/`_evaluate_stuck` are the rung-1 and
rung-2 predicates, but they read Postgres per point. Refactor them into
pure functions over a sample list (`is_stale(samples, now, max_age)`,
`is_stuck(samples, window)`), called by both `fault_detector` (via
`read_range`) and `commissioning.checks` (via `History`). This is one
definition of "stuck", with the documented edge cases kept:

- a point that never reported is *not stale*. That is Cx *inconclusive
  / missing*, which the Cx layer reports explicitly, closing the
  documented `stale` gap for Cx purposes.
- zero samples is *not stuck*.

`range` rules with `applies_to.brick_class` are reused as plausibility
checks. A Cx run lists the fault rules that apply to each point and
reports their current open faults as evidence ("already faulted by
rule X since T"). It does not re-evaluate them.

Rung 3 (pairs) is new logic. `fault_detector` is single-point.
Rung-3 checks stay Cx-only in the MVP. If they prove useful, they can
later become a fault rule type (`type: "pair"`).

### 7.2 Derivations

- A check can name a **derivation output point** as its subject. For
  example, a `formula` derivation computing SAT − SAT-SP lets a range
  check verify "SAT tracks setpoint within ±2°F" without new Cx code.
  Cx reads the derived history like any other point.
- `derivation_api`'s `/dry-run` is the template for a Cx **dry run**:
  show which equip/points a run will bind and what it will skip
  before running.
- Rollups (`hasPart` trees) give system-level coverage numbers
  (floor → building) if we want them in the report. Optional.

### 7.3 SHACL

Expected-point templates can be expressed as SHACL shapes in
`ontology/` (for example "a `brick:AHU` must `hasPoint` some
`brick:Supply_Air_Temperature_Sensor`"). Then `validate_api` reports
model gaps everywhere and Cx reports them per project, from one
definition. Recommendation: do this in the MVP if the shape set stays
small. Otherwise use Python templates first and convert later.

---

## 8. APIs, placement, gateway, UI

### 8.1 Placement

Recommend a **new `commissioning_api` service on port 8009**, routed
at `/cx/` (Q2).

- 8000 and 8002–8008 are taken: 8001 is Windrow, 8004 is `ui_server`
  and 8008 is `haystack_api`. Nothing in `docker-compose.yml`,
  `gateway/nginx.conf` or `src/` uses 8009.
- It follows the house shape: stdlib `ThreadingHTTPServer`, its own
  `commissioning-api-openapi.yaml`, a `/docs` route, a healthcheck on
  `/openapi.yaml`, `connect_pool()`, `tracing.init_tracing`.
- Why not fold it into `ingest_api` or `fault_api`? Cx has its own
  tables, lifecycle and roles. Adding about 15 routes to either would
  break the "one concern per API" layout described in
  `architecture.mdx`.
- Why not have no service at all? An MVP could ship as a CLI
  (`python -m baler.commissioning.report --site …`) as a first step,
  but the issues log needs an API and UI to be useful.

### 8.2 Routes (MVP)

```
GET    /cx/projects                       viewer   (filtered to caller's org/sites)
POST   /cx/projects                       operator {site_id, name, phase}
GET    /cx/projects/{id}                  viewer
DELETE /cx/projects/{id}                  admin    (archive, not delete)
POST   /cx/projects/{id}/runs             operator {window_hours?} -> 202 queued run (#57); 409 if one is in flight
GET    /cx/projects/{id}/runs             viewer
GET    /cx/runs/{id}                      viewer   ?equip=&status=
GET    /cx/runs/{id}/report               viewer   ?format=json|html
GET    /cx/projects/{id}/issues           viewer   ?status=
POST   /cx/projects/{id}/issues           operator (manual issue)
PATCH  /cx/issues/{id}                    operator {status, assignee, note}
GET    /cx/issues/{id}/events             viewer
POST   /cx/issues/{id}/risk               admin    {reason, review_by}
DELETE /cx/risks/{id}                     admin    (revoke)
GET    /cx/templates                      viewer   (expected points per class)
GET    /cx/openapi.yaml, /cx/docs         public
```

The author of every write is taken from the verified JWT (`sub`,
`email`), as `record_edit.log_edit` does, not from a body field.

### 8.3 Compose and gateway (at implementation time, not now)

- A `commissioning_api` service using `*daemon-env`, a healthcheck on
  8009, and `restart: unless-stopped` if it connects eagerly (the
  fault_api lesson).
  `*daemon-env` also carries `PYTHONPYCACHEPREFIX`, which a service
  bind-mounting `.:/app` needs (CLAUDE.md, #29). Nothing the service
  does may create directories under `/app`, which rules out a
  report-export or photo directory in the checkout. Phase 2 capture
  photos go in Postgres (`bytea`, size-capped) or a named volume, never
  under `/app`.
- Add the service to the gateway's `depends_on`.
- An nginx `location /cx/` with `js_access` and policy rows in
  `gateway/njs/policy.js`, following the njs 1.0 rules in CLAUDE.md:
  only `export default`, no destructuring or `for…of`. Run `nginx -t`.
- Add cases to `test_policy.mjs`.
- Note the gateway-restart gotcha in the PR.

### 8.4 UI

- `ui/cx.html` + `ui/cx.js`, styled like `ops.html`:
  - **Projects** list;
  - **Run** view: a grid with equip rows and check columns
    (pass/fail/inconclusive chips), and a drawer with evidence plus an
    inline sparkline (`chart.js`) of the window;
  - **Issues** table with state buttons and an events history;
  - **Risks** tab (separate on purpose).
- `app.js` equipment tree: a small Cx status dot per equip from the
  latest run, linking to the run drawer.
- The report is HTML via `GET …/report?format=html`, which is
  printable. PDF comes later.
- Phase 2: a mobile-friendly punch-list page, offline later if needed.

---

## 9. Testing approach

- **Pure unit tests**, no services, `-m "not integration"`:
  - Port `tests/cx_fixtures.py` as-is (renamed imports). Its building
    and history generator are exactly what rungs 1–3 need.
  - Port `test_commissioning_ladder.py` first (8 tests) against the
    new `checks.py`, keeping its assertions: flat sensor → rung 2,
    `fell_to_rung`, inconclusive on no data.
  - Port `test_commissioning_points.py` / `_vocabulary.py` *as
    expectations* for Brick-class classification of field names. Where
    a #5 expectation conflicts with `mapping.py`, record the difference
    instead of forking the vocabulary.
  - Phase 2: port `_alignment` (16), `_reconcile` (12), `_punchlist`
    (11) and `_spec_model` (11) with their fixtures, re-keyed on
    `urn:equip:` URIs.
  - New: issue lifecycle (open → retest → closed only on pass;
    inconclusive doesn't close; risk covers then reopens after
    `review_by`), and shared stale/stuck predicates (the same cases as
    `test_fault_detector`).
- **Contract test**: `test_commissioning_openapi.py` in the shape of
  the other `test_*_openapi.py` files. It runs the real handler with
  an in-memory repo, as #5's did.
- **Scoping tests** (the #5 gap): two orgs, two sites with prefixes.
  Org B can't list or read org A's projects, runs, issues or results.
  Mirrored faults are visible only to the owning org. Inventory SPARQL
  never returns out-of-scope equip. Model these on
  `test_scope_in_query.py` / `test_site_scoping_integration.py`.
- **Integration** (`@pytest.mark.integration`, needs `docker compose
  up -d`): seed the mock-hospital data, create a project through the
  gateway, run it, and assert a known flat sensor yields a rung-2 issue.
- **Gateway**: new rows in `node gateway/njs/test_policy.mjs`, and
  `nginx -t` in CI's integration job (already exercised by compose
  bring-up).

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| **False assurance.** "Pass" on thin data (COV-only points, short windows, unoccupied periods) is worse than no report. | Inconclusive is a first-class status. Minimum samples per check. The freshness statement is on every report. The report shows its window and what wasn't exercised (for example "no setpoint change observed, so rung 4 not run"). |
| **Cross-org leakage** (the #5 bug class) | Projects bound to site_id. `Scope.sparql`/`allows` on every read. Fault mirroring only with real URIs. Dedicated scoping tests. |
| **Brick model quality** decides check quality. Unclassified points mean no pair checks. | Report "unclassified points" as a coverage gap and link to the record editor / autotag. SHACL templates. |
| **Trend quality**: sampling, COV, and timestamps from different controllers | Pair checks use a tolerance window (#5's `pair_tolerance_minutes`). Document Windrow `his_collect_*` prerequisites. |
| **Active writes** (Phase 3) can damage equipment or comfort | Off by default (deployment switch), admin only, windowed, `expires_at`, guaranteed relinquish, dry-run listing. |
| **Scope creep** into a CxAlloy competitor | Non-goals in §4. Export instead of rebuild. |
| **Liability.** A Cx report is used contractually. | Language: evidence, not certification. Every result cites its data window. Signed acceptance stays human. |
| **LLM hallucination** (Phase 4) | Proposals only, human accept. The #5 capture rule: never complete a partially legible value. |
| **Performance.** A whole site's windows at once. | `read_range_many` with per-point limits and rollups for long windows. Runs are background work since #57 (worker threads, own pool without the gateway statement timeout, interrupted runs failed at startup). |
| **Global config files** (`rules.json`) are not org-scoped | Cx definitions live in Postgres with org_id. Don't add Cx checks to `rules.json`. |
| **Another stale mega-PR** | Ship the MVP in three PRs that each stand alone (§5). |

---

## 11. Open questions for Jacob (answered, see §0)

Each has a recommended answer. Implementation stays parked until these
are answered.

1. **Scope and priority:** build toward the agentic-commissioning
   product pivot (GCs, CxAs, MEP firms), or a smaller verification
   report on top of existing fault rules?
   *Recommend:* the smaller report first (§5 MVP). It is the
   foundation either way, is useful to existing operators, and tests
   the pivot cheaply. Decide on Phases 2–4 after a real CxA has used
   it on one site.
2. **Where it lives:** a new `commissioning_api` on port 8009 at
   `/cx/`, or routes added to an existing API?
   *Recommend:* new service on 8009, which is free in compose, the
   gateway and `src/`.
3. **Model:** treat Brick classes as the Cx vocabulary, or revive #5's
   plain-English canonical model with Brick as a projection?
   *Recommend:* Brick-native. Keep confidence, alternatives and
   provenance in `cx_*` tables keyed by equip/point URI. Drop
   `vocabulary.py`, and port its test expectations against
   `mapping.py`.
4. **Target lifecycle phase for the MVP:** new construction, warranty,
   or existing-building operations (MBCx/re-Cx)?
   *Recommend:* keep `phase` explicit from day one (#5 got this
   right), but tune and demo on warranty/operations data (the mock
   hospital and nash-srv), where history already exists. New
   construction needs Phase 2 spec import to shine.
5. **Active tests:** may Baler ever command points for Cx (via
   `point_write`)?
   *Recommend:* yes, but only in Phase 3, behind a deployment switch
   (`BALER_CX_ALLOW_WRITES`), admin only, windowed, with `expires_at`
   and guaranteed relinquish. Never in the MVP.
6. **Spec ingestion:** structured CSV/XLSX point lists and schedules
   first, or LLM extraction from PDF drawings and specs?
   *Recommend:* CSV/XLSX first (Phase 2). LLM extraction in Phase 4,
   always as reviewed proposals.
7. **Alarm console:** should failed Cx checks also open faults in the
   shared `faults` table, and so fire webhooks?
   *Recommend:* yes, opt-in per project, only for confirmed equip,
   with `rule_id = cx:<check>` and the real `urn:equip:`/`urn:point:`
   URI (never a `cx:` pseudo-URI). Accepted risks suppress mirroring.
8. **Re-runs:** on demand only, or scheduled?
   *Recommend:* on demand in the MVP. In Phase 2, a schedule per
   project run by a small thread in `commissioning_api` (or a
   `commissioning_runner` if it needs isolation), not by
   `source_runner`, which is for data sources.
9. **Expected-point templates:** as SHACL shapes in `ontology/` (shared
   with `validate_api`) or as Python data in the Cx package?
   *Recommend:* SHACL if the MVP set stays at about 6 equip classes,
   so there's one definition of "complete AHU". Otherwise Python first.
10. **GL36 sequence verification (Phase 3):** write our own, or adopt
    PNNL ConStrain (BSD-2-Clause, Python, GL36-based, has a Brick
    demo)?
    *Recommend:* spike ConStrain first. It is license-compatible and
    already covers economizer, resets and zone deadband.
11. **Integrations:** export issues to CxAlloy / Facility Grid /
    Bluerithm?
    *Recommend:* CSV/JSON export in Phase 2. Build API integrations
    only when a paying user names a platform.
12. **Porting #5's tests:** #32 says 151 tests, and I count 92 test
    functions (plus parametrized cases) in 11 files. Port the pure ones
    (ladder, points/vocabulary expectations) in the MVP and the rest
    with Phase 2?
    *Recommend:* yes. Keep `tests/cx_fixtures.py`'s building as the
    shared fixture.

---

## Sources

[s202-cse]: https://www.csemag.com/?p=12879
[s202-hpac]: https://www.hpac.com/archive/article/20927321/first-commissioning-focused-ashrae-standard-published
[s202-2024]: https://standards.nimonik.com/products/ashrae/202/
[g0-news]: https://www.ashrae.org/news/esociety/updated-commissioning-guideline
[g0-cxplanner]: https://cxplanner.com/commissioning-101/ashrae-guideline-0
[g11-cse]: https://www.csemag.com/articles/new-hvacr-commissioning-guideline
[g11-draft]: https://osr.ashrae.org/Online-Comment-Database/ShowDoc2/Table/DocumentAttachments/FileName/4420-Guideline%201.1-2007R%20-%202ndPPRDraft.pdf/download/false
[g36-nist]: https://www.nist.gov/node/1543181
[nu-spec]: https://northwestern.edu/facilities/docs/construction/design_guidelines/25_integrated_automation/NU_25%200800%20-%20COMMISSIONING%20OF%20INTEGRATED%20AUTOMATION.pdf
[uh-spec]: https://www.uh.edu/facilities-planning-construction/vendor-resources/owners-design-criteria/master-specs/doc/25-08-00-bas-commissioning.doc
[everett]: https://lfportal.everettwa.gov/WebLink/0/doc/1762798/Page80.aspx
[eca-fpt]: https://www.energycodeace.com/site/custom/public/reference-ace-2019/Documents/127functionalperformancetesting.htm
[achr-unsampled]: https://achrnews.com/articles/157752-unsampled-systems
[lbnl-mbcx]: https://buildings.lbl.gov/sites/default/files/what-is-mbcx-faq-final.pdf
[dc-levels]: https://archdesk.com/blog/data-center-commissioning-levels
[cxalloy]: https://www.cxalloy.com/commissioning-project-management/
[cxalloy-ios]: https://apps.apple.com/app/id865607943
[bluerithm]: https://bluerithm.com/platform/
[bluerithm-p2p]: https://bluerithm.com/bas-point-point-checkout-commissioning/
[facilitygrid]: https://builtworlds.com/companies/facility-grid-commissioning-software/
[cxplanner-bcxa]: https://cxplanner.com/blog/bcxa-annual-conference-2025
[skyspark-cx]: https://skyfoundry.com/file/47/Case-Study-SkySpark-as-a-Tool-for-Commisioning.pdf
[skyspark-denver]: https://www.SkyFoundry.com/file/233/Case-Study-Broadway-Bldg-Denver-Group-14.pdf
[seac-y3]: https://buildings.lbl.gov/sites/default/files/Smart%20Energy%20Analytics%20Campaign%20Year%203%20Report_LBNL_11.1.19.pdf
[seac-doe]: https://www.energy.gov/cmei/buildings/articles/smart-energy-analytics-campaign-reveals-continued-energy-and-cost-savings
[nrel-aft]: https://docs.nrel.gov/docs/fy24osti/86647.pdf
[pingcx]: https://realcomm.com/filecabinet/ContactMedia/000000/pingCx-brochure.pdf
[constrain]: https://github.com/pnnl/ConStrain
[constrain-joss]: https://www.theoj.org/joss-papers/joss.08083/10.21105.joss.08083.pdf
[obc]: https://www.energy.gov/sites/default/files/2016/12/f34/Modeling%2C%20Controls%20and%20Grid-LBNL%20%28OpenBuildingControl%29.pdf

Vendor pages and case studies are self-reported. ASHRAE documents are
summarized from secondary sources. Clockworks, KGS and Cimetrics are
named as category examples only (no primary source found in this
pass).

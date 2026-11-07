# Messy data to ontology: cleaning, tagging, and specialist handoff

Status: research and design, 2026-11-07. Nothing here is built yet. The
tracking issues are listed at the end.

## The question

The data reaching Baler today looks clean, because almost all of it is
either already tagged (Haystack pulls, Haxall) or comes from our own mocks.
Real BACnet jobs aren't like that. Point names look like `AHU1-SAT`,
`ZN-T-3`, `CHWSupTmp` or `AI-17`, descriptions are missing or wrong, units
are `noUnits`, and sensors flatline or spike. This doc covers:

- how Baler turns that into a Brick/Haystack model;
- how it cleans the values;
- where each piece lives across Windrow, openhay and Baler;
- how the machine knows when to stop guessing and hand off to a person.

## What we have today

| Stage | Where | What it does | Gap |
|---|---|---|---|
| Capture | `windrow/discovery.py` | Reads `object-name`, `present-value`, `units`, `description`. | Object type is only in the identifier string, not used as a signal. Ignores writability, state text, `status-flags`/`reliability`, the device's vendor, model and name, and structured-view objects. That is free, high-signal evidence. |
| Pre-tag | `windrow/bacnet_tagger.py` + `rules/bacnet_to_haystack_tags.yaml` | Splits on `-_, ` and matches rules on subsets of the tokens. | 23 rules. No camelCase split, so `ZoneTemp` is one token and never matches. No abbreviation lexicon, so `SAT`, `ZN-T`, `sup` and `ret` miss. Units aren't used (its own `ponytail:` note says so). |
| Classify | `baler/mapping.py` | Haystack tags go to Brick classes by subset match. Partial overlaps become a `PROJ:` subclass; anything else is `TD:UnmappedPoint`. | `PROJ:` class names are built from leftover tags, so every odd point mints a new class. |
| Fallback | `baler/llm_classifier.py` via `python -m baler.autotag` | One Haiku call per point, given the tags and topic. Applied if the model's own confidence is 0.7 or more. | The model never sees the description, units, object type or values (`description` is passed as `None`). Its answer isn't checked against the Brick vocabulary, so it can assert a class that doesn't exist. A model's self-reported confidence is poorly calibrated. It calls Anthropic directly, unlike Cx's `BALER_LLM_PROVIDER`. |
| Learn | `llm_classifier.propose_rule` | Appends proposed rules as YAML comments to `rules/*.yaml`. | Writes into the bind-mounted checkout on the server (see CLAUDE.md on `/app` writes). There is no UI, so nobody sees the proposals and nobody promotes them. |
| Validate | `shacl_validate.py`, Cx expected points | SHACL shapes, plus Cx's "does this equip have the points it should". | Good. This is the right place to catch a wrong guess that is still valid Brick. |
| Value quality | `his_checks.py` (stale, stuck), fault rules | Stale and stuck. | No range, spike or unit sanity checks, and no per-sample quality flag. A bad value goes straight into faults and Cx. |
| Human review | Cx spec items and agent proposals (`proposed` → a person accepts) | The pattern we want, already proven in Cx. | Not used for mapping. |

We also already have a **labelled messy benchmark** that we haven't used as one.
`windrow/mock_hospital.py` generates about 500–1000 points in four naming
styles, with real BAS abbreviations and optional descriptions. It knows the
true type of every point. Nothing scores us against it yet.

## What the research says

- **Text-only learning works within a building, and struggles across buildings.**
  - [Scrabble](https://github.com/jbkoh/scrabble) ([BuildSys '18](https://cseweb.ucsd.edu/%7Ejmcauley/pdfs/buildsys18.pdf)) turns raw point names into an intermediate tag representation, then into Brick, using active learning. It reuses buildings already mapped, so a new building needs fewer human labels.
  - [Plaster](https://par.nsf.gov/servlets/purl/10106908) benchmarked several such methods on shared buildings, and sorted them by whether they use text, time series or both.
  - The lesson for us is the two-step design (name → tags → class), which Windrow and Baler already have. Learning from previous buildings is the part we lack.
- **Values identify point types surprisingly well, and they are naming-independent.**
  - [Hong et al., "Sensor-Type Classification in Buildings"](https://arxiv.org/pdf/1509.00498) used statistical features over short windows and a random forest. The authors report about 92–98% accuracy within a building and about 82% across buildings.
  - So time series is a weak classifier on its own, but a strong **veto**. A "temperature" that only reads 0/1, or a "%" reading 4000, is wrong whatever the name says.
- **LLMs with retrieved examples beat static prompts and fine-tuned BERTs.**
  - [Brick-DICL](https://arxiv.org/html/2606.17637) (AWS, KDD RelKD 2026) works in two stages: retrieve the 15 most similar points *already classified correctly* as few-shot examples, retrieve the 20 nearest Brick classes as candidates, then have the model pick the top 3.
  - It runs several models and sends a point to human review when they disagree. "Any-2 agree" gave the lowest disagreement.
  - The authors report it beats static and random few-shot prompting and the fine-tuned transformers on both of their buildings. They give no cost numbers.
  - Two things to adopt: **choose examples from our own confirmed points**, and **measure confidence by agreement, not by the model's own say-so**.
- **Parse names with deterministic grammar plus an abbreviation lexicon before anything learns.**
  - NREL's [BuildingMOTIF](https://github.com/NREL/BuildingMOTIF) has parser combinators for point labels (`label_parsing/combinators.py`, with `COMMON_POINT_ABBREVIATIONS` and `COMMON_EQUIP_ABBREVIATIONS_BRICK`). It also has ingress handlers for BACnet, CSV and naming conventions, and a visual parser builder in its app.
  - It shows that a site's naming convention can be written down once by a person and then applied to every point. That is the highest-leverage human input there is.
  - It's a heavy dependency (its own DB, templates, 223P). Borrow the approach and the lexicon idea, not the package.

## The design: a ladder, with a person at the top

Each point climbs the ladder until something is confident enough. Every step
records **what it concluded, from what evidence, and how sure it is**. A
person sees the point only when the machine runs out of ladder.

```
raw BACnet/Modbus/CSV metadata + sample values
  1. normalise      tokens, abbreviations, units, object type    (deterministic)
  2. site convention  "AHU{n}-{point}" parser a person wrote once (deterministic)
  3. rules          tags -> Brick (today's mapping.py)            (deterministic)
  4. memory         nearest confirmed points, this site then all  (cheap, learned)
  5. model          LLM, few-shot from 4, candidates = real Brick classes
  6. veto           value range, unit and type checks vs. class   (deterministic)
  7. validate       SHACL + Cx expected points                    (deterministic)
  -> auto-accept    if steps agree and nothing vetoes
  -> review queue   otherwise, grouped by pattern, ranked by impact
  -> specialist     when the reviewer can't tell either
```

### 1. Capture everything, decide nothing (Windrow)

Windrow should send Baler the **raw evidence**, not just its conclusion. Add
these to discovery and publish them in the point's metadata message:

- Object type as a field, not just inside the identifier. AI is a sensor, AO a command, AV/MSV often a setpoint, BI/BO a status or command.
- Whether it's commandable (a priority array means `cmd` or `sp`).
- `state-text` for multistate objects (`Off/Low/High` tells you a lot).
- `min-pres-value` and `max-pres-value`, and `cov-increment`.
- The device's `vendor-name`, `model-name`, `object-name` and `location`.
- Structured-view or subordinate lists, which give real equipment grouping when the integrator built them.

**Value quality comes free in BACnet:** `status-flags` (in-alarm, fault,
overridden, out-of-service) and `reliability`. Publish these with readings
so Baler can mark a sample as bad without guessing.

Windrow keeps its deterministic pre-tag, so the edge works offline on a Pi,
but it tags with the shared engine (below) and stamps each tag with its
rule id. It never calls a model.

### 2. Clean values: flag, never rewrite (Baler)

- Store a **quality flag per sample** next to the value: `ok`, `fault`, `overridden`, `out_of_range`, `spike`, `stuck`, `stale`, `unit_suspect`. The raw value is kept.
  - Faults, Cx and his queries skip or show flagged samples. They don't silently average them in.
  - This is the same "lossless, additive" rule `ingest._write_tags` already follows for tags.
- **Range by class:** a small table of plausible ranges per Brick class and unit. A zone temperature of −40 or 180 °F, humidity over 100%, a damper at 140%.
- **Spikes:** rolling median ± k·MAD. **Stuck and stale:** `his_checks` already does these.
- **Unit suspicion:** a value distribution that fits °C while the unit says °F. Offer the fix; don't apply it.

### 3. One deterministic tagging engine, shared (openhay)

Today the same subset-match, most-specific-wins algorithm is written twice:
in `windrow/bacnet_tagger.py` and in `baler/mapping.py`. Both repos already
depend on openhay, optionally, through its Python wheel. So the
standards-level, deterministic, ML-free parts belong there:

- **Tokeniser:** camelCase and digit splits, so `CHWSupTmp3` becomes `chw sup tmp 3`.
- **BAS abbreviation lexicon** to Haystack tags: `sat`, `rat`, `oat`, `znt`, `dat`, `stpt`, `sp`, `sts`, `fdbk`, `cmd`, `chw`, `hw`, `cw`. It is plain data, so it can be extended per site.
- **Units** to tags (°F/°C/K → temp, %RH → humidity, ppm → co2, cfm → flow). The BACnet→Haystack unit table already lives in Windrow; move it.
- **Rule matching**, and **Haystack protos/defs validation**: is this tag set a legal entity under Haystack 4's defs?

This is "what Haystack means", which is openhay's charter. Brick mapping,
learning, models and review stay in Baler, because those need the graph,
history and users. (Brick→Haystack is Baler's business too; openhay stays
Haystack-only.)

### 4. Site naming conventions: the best hour a person can spend

In a review UI, a person marks up one label (`AHU1-SAT`: `AHU` = equip type,
`1` = id, `SAT` = discharge air temp sensor). Baler turns that into a parser
for the site and runs it over every point. One annotation covers hundreds
of points. This is BuildingMOTIF's idea. It is also exactly what an
integrator does by hand in a spreadsheet today.

### 5. Memory: learn from previous tagging (Baler)

Every point a person confirms becomes a **labelled example**: raw label,
description, units, object type, value stats → final class and equip. Use
that store two ways:

1. **Nearest neighbour**, before any model. An embedding or TF-IDF over the
   normalised tokens. If the 5 nearest confirmed points (same site first,
   then all sites) agree, take it.
2. **Few-shot examples** for the model step (Brick-DICL's main finding).

Examples are org-scoped data: never shared across orgs without consent.
Baler is multi-tenant.

### 6. Model step, done properly (Baler)

- **Input:** label, description, units, object type, writability, state text, value stats, the equip's sibling points, and the k nearest confirmed examples.
- **Output:** constrained to **real Brick classes** (`brick_vocab.canonical_class`). Ask for the top 3, with reasons.
- **Confidence = agreement:**
  - the model's top-1 agrees with memory, or with a second model, or with itself across a paraphrased prompt;
  - and the veto (step 7) passes.
  - Self-reported confidence counts for little.
- **Provider:** go through the same `BALER_LLM_PROVIDER` abstraction as Cx, so it can be no model, a local model or Claude.
- **Batching:** one call per *pattern* (all `CHWSupTmp{n}` points), not per point.

### 7. Veto and validate

- **Value veto:** a range, unit or kind mismatch with the proposed class (binary values on an analog class, % values on a temperature class).
- **Structure:**
  - SHACL;
  - Cx expected points (an AHU proposed with no supply fan is suspicious);
  - an equip's type inferred from its point set (chw supply and return temps plus kW plus a status suggest a chiller).
- A veto never blocks storage. It only blocks **auto-accept**.

### 8. The handoff: a review queue a specialist can actually use

When the ladder ends without agreement, the point becomes a **mapping
proposal**. It is the same `proposed` → accepted pattern as Cx spec items,
with a `record_edits` audit row.

- **Grouped by pattern, not per point.** "41 points look like `VAV-{n}-DA-T`, proposed Discharge_Air_Temperature_Sensor" is approved with one click, and the decision becomes a site rule.
- **Ranked by impact.**
  1. Points a fault rule or a Cx check needs.
  2. Points on equipment in an active Cx phase.
  3. Everything else.
- **Evidence on screen:**
  - the raw label, description, units and object type;
  - a 24 h sparkline;
  - the 3 candidates with reasons;
  - the nearest confirmed examples;
  - which ladder step said what, and why auto-accept was refused.
- **Three answers:** accept a candidate, pick another class (vocab-checked), or **escalate**.
  - Escalate assigns the proposal to a *specialist*: the site's controls contractor or MSI, or our own expert.
  - It notifies through the existing webhook/email path, with a link.
  - The specialist answers in the same screen. "Can't tell, needs a site visit" is a valid answer. It becomes a Cx issue, not a guess.
- **Every answer feeds back:** memory gets an example, a pattern answer becomes a site rule, and a repeated cross-site answer becomes a candidate for the shared lexicon or rules. Promotion to shared rules is a reviewed PR, never automatic.

### 9. Measure it

- An **eval harness** over `mock_hospital`, which knows the truth, runs in CI. It reports:
  - accuracy per ladder step;
  - % auto-accepted;
  - % auto-accepted *but wrong*. This is the number that must stay near zero.
  - % sent to review.
- Later, add the Pi edge nodes' real BACnet points, hand-labelled once, and a public labelled set from the Scrabble/Plaster work if its licence allows.
- **Ops panel:** points by state (confirmed by rule / memory / model / person, proposed, escalated, unmapped). This is the number a customer asks about: "how much of my building does Baler understand?"

## Order of work

1. Eval harness on `mock_hospital`. Without it, every later step is a guess about a guess.
2. Fix the current model step: richer input, Brick-vocab check, provider abstraction, no writes into the checkout. These are small changes that help immediately.
3. Shared tokeniser, lexicon and units in openhay. Windrow and Baler adopt it.
4. Windrow publishes the richer raw metadata and BACnet quality.
5. Per-sample quality flags and range, spike and unit checks in Baler.
6. Mapping proposals and the review queue, with pattern grouping and escalation.
7. Memory (nearest confirmed) and few-shot retrieval.
8. Site naming-convention parser UI.
9. Equipment and relationship inference (equip type from its point set; AHU→VAV feeds from correlation).

## What we're not doing

- **No auto-promotion of learned rules to shared rule files.** A person reviews every shared rule.
- **No rewriting of raw values.** We flag them and keep them.
- **No training of our own deep model yet.** Retrieval plus a hosted or local model, with the eval harness, tells us whether we ever need one.

## Tracking issues

- Epic: Baler#188
- Baler:
  - #180 eval harness
  - #181 the LLM can assert a non-existent Brick class (bug)
  - #182 fix the model step
  - #183 per-sample quality flags
  - #184 review queue and specialist escalation
  - #185 memory
  - #186 site naming-convention parser
  - #187 equipment and relationship inference
- openhay:
  - openhay#7 `openhay-tag` (tokenizer, lexicon, units, rules)
  - openhay#8 Haystack 4 defs/protos validation
- Windrow:
  - Windrow#19 richer BACnet metadata
  - Windrow#20 status-flags/reliability as quality
  - Windrow#21 adopt `openhay-tag`

# Baler as a building's lifelong record (UBIS, cradle to grave)

Status: research and design, 2026-11-07. Nothing here is built unless it says so.
The tracking issues are listed at the end.

Baler started as data-driven commissioning, and Cx 1–10 are built
(`todo/commissioning-plan.md`). This doc covers where that goes next: one record
for a building's whole life. That record holds:

- schedules, O&M documents, prints, start-up guides;
- tests, assets, maintenance and efficiency;
- a handover and sale package;
- AI that helps the people who use it.

Two examples set the bar:

> Ten years after the building is built, a building engineer can pin-point a hunting motor.

> When the building is sold, show when the pumps were changed, that they were
> replaced with a different brand, what the chiller has lost in efficiency since
> it was installed, and that replacing it would save $X a year.

The record has to run on one computer for 15+ years, hold all of that data, and
migrate easily. Each organization picks its own topology, and the GC's IT team
usually sets it up, so every topology is documented and sized (§10).

---

## 1. Who uses it, and what they ask

| Person | When | The question | The evidence Baler shows |
|---|---|---|---|
| **GC / project manager** | construction | "Are we on schedule, by the data, not by the meeting?" | Schedule activities matched to evidence: points online, start-ups passed, FPTs passed, checklists signed (§9) |
| **Commissioning agent (CxA)** | start-up → acceptance → warranty | "Prove every piece of equipment works." | Cx runs, tests, issues and risks, readiness per space (§4, §7). *Built today, except readiness.* |
| **Controls / TAB / mechanical contractor** | start-up, punch | "What exactly do I fix, and did my fix work?" | Issues closed on evidence (Cx 4), active tests (Cx 9), TAB report linked to the spaces it balanced |
| **Clinical / infection control** (healthcare) | acceptance, then daily | "Is OR 3 ready for patients? Is it still?" | Readiness report: every applicable requirement for that space type, passing over a window (§3, §4) |
| **Building engineer / operator** | years 1–50 | "Why is this motor hunting? When did it start? What changed?" | Oscillation detection vs the start-up baseline, asset history, the O&M manual page (§6, §8) |
| **Energy / facility manager** | operations | "Where are the savings? What's my deferred maintenance? When do filters get changed?" | Performance baselines, PM by run hours, deferred-maintenance register (§8) |
| **Owner / buyer / due diligence** | sale, refinance | "What's the real condition? What was replaced, with what? What should be replaced?" | The building passport: asset history, efficiency trends, a capital plan with savings cases (§11) |

**One record, read differently by role.** Nobody re-enters data: the
commissioning evidence *becomes* the operations baseline, and the operations
history *becomes* the sale package.

## 2. The lifecycle spine

The existing `cx_phases` list (design → construction → start-up → functional
testing → acceptance → warranty → operations) is the spine. It extends at the end:

```
design ─ construction ─ start-up ─ FPT ─ acceptance ─ warranty ─ operations (yr 1…50) ─ transfer/sale ─ … ─ decommission
  OPR/BoD   schedule,     bench/power-on   active +     readiness   end-of-warranty   continuous compliance,   passport
  specs     submittals    start-up guides  passive      report      Cx report         analytics, PM, assets    export
```

**The point that makes "year 10" possible:** every start-up and FPT result is
stored with the asset and position it ran on, and stays there. In 2036, "is this
valve hunting?" is answered *against its own 2026 start-up trend*, not against a
generic rule. That requires the raw history to survive (§10), and requires a
replaced asset to be a new asset (§6), so the baseline isn't silently compared
against different hardware.

## 3. Standards as data: how we find requirements and make them expandable

Hard-coding "an OR needs 20 ACH" is wrong, for three reasons:

- the value depends on the standard **edition** the building was designed to;
- it depends on the **jurisdiction**;
- owners add their own standards on top.

So requirements are **data**, in packs, checked by engines we already have.

### 3.1 Where requirements come from

| Source | What it governs | How we get it | Licence |
|---|---|---|---|
| **ASHRAE 170** (ventilation of health care facilities) | Per-space-type pressure relationship, total and outdoor ACH, temperature, RH, filtration | Purchased by the owner/designer | Copyrighted: ship citations and structure, not text or tables |
| **FGI Guidelines** | Healthcare design. Adopts ASHRAE 170 by reference in most editions | Purchased | Same |
| **Joint Commission EC.02.05.01, EP 15** | Critical areas (ORs, isolation, sterile supply) must hold temperature, humidity and pressure "based on the edition of the FGI guidelines used at the time of design" | [Joint Commission materials](https://digitalassets.jointcommission.org/api/public/content/de68e9ebbf104a998fee16d03f4d88b5) | It doesn't set a check frequency: the facility defines and documents it ([Healthcare Facilities Today](https://healthcarefacilitiestoday.com/posts/Regulations-Codes-Standards-QA-Temperature-and-humidity-logs--17292)) |
| **NFPA 99, CMS CoPs, state DOH** | Healthcare operations and inspection | Public or purchased | Varies |
| **ASHRAE 90.1 / IECC / local energy code** | Controls requirements (setbacks, resets, economizers) | Purchased or adopted into public law | Varies |
| **ASHRAE Guideline 36** | High-performance sequences | Purchased. Verification items via PNNL ConStrain (BSD-2) | *Built: Cx 3* |
| **ASHRAE Std 202 / Guideline 0** | The Cx process: OPR, BoD, Cx plan, issues log, systems manual, training, final and end-of-warranty reports ([NEBB summary](https://www.nebb.org/wp-content/uploads/2021/09/NEBBinar_ASHRAE_202.pdf)) | Purchased | A process standard: it gives our *deliverables* list (§11) |
| **Owner OPR and design standards** | Anything: "all AHUs have DSP reset", "MERV 14 minimum" | The owner's own document | The owner's |
| **Project specs, BoD, sequences** | This building's intent | Already ingested as Cx spec items (Cx 7) | The project's |

**Editions are not optional metadata.** Public summaries of ASHRAE 170 disagree:

- one gives OR humidity as 20–60% RH, another as 30–60% (20% with a risk assessment);
- one gives 20 ACH, another "15 or 20 depending on year of construction";
- one gives the final filter as MERV 16, another as HEPA.

They differ because they cite different editions, 2008 against 2021 ([ACHC](https://achc.org/obs-patient-comfort-and-safety-can-be-found-in-the-air/),
plus vendor pages we don't rely on). A requirement without an edition is a
guess.

### 3.2 The requirement pack

Each requirement is one row:

```yaml
pack: ashrae-170-or            # id, version
source: {standard: "ASHRAE 170", edition: "2021", ref: "Table 7-1, Operating room"}
jurisdiction: [US]             # or a state, or an owner
applies_to: {space_type: operating_room, phase: [acceptance, operations]}
criterion: {quantity: pressure_to_adjacent, op: ">=", value: 0.01, unit: inH2O}   # entered/confirmed by the user
evidence: {points: [Differential_Pressure_Sensor @ room], window: 24h, coverage: 0.95}
check: range                   # an existing Cx check / procedure / SHACL template / GL36 item
verified_by: {user, at}        # nothing is active until a person confirms the numbers
```

How packs work:

- **Engines are reused, not new.** `check:` names something Baler already runs:
  - a Cx check (`commissioning/checks.py`);
  - a procedure criterion (`commissioning/procedures.py`, value ranges included);
  - a SHACL template (`ontology/cx-expected-points.ttl`);
  - a GL36 item.

  A pack adds the *citation, applicability and edition* around them.
- **People add packs from documents they hold.** They upload the standard, or the
  owner's design guide, and an LLM extracts requirement rows as **proposals**. This
  is exactly how Cx 7 handles spec items: cite page and verbatim text, a person
  confirms every number.
- **What we ship:**
  - **The schema and the structure.** We include structure-only packs: an ASHRAE 170 OR pack that names the quantities and cites the table, with blank values.
  - **The worked examples.** These can be filled in only when the user confirms them for their edition, and are marked "verify against your edition".

  We never ship standard text.
- **A pack is versioned.** A building keeps the pack version it was accepted
  under. A new edition is a new pack version, applied by choice: a building
  designed to FGI 2018 stays judged by FGI 2018.

### 3.3 Continuous compliance

The pack that proved OR 3 at acceptance keeps running in operations, as a
scheduled passive run (Cx 6's scheduler). That gives the daily temperature,
humidity and pressure log Joint Commission surveyors ask for, with alerts on
exceedances. Common survey findings are "no logs" and "no alerts when out of
range" ([Joint Commission materials](https://digitalassets.jointcommission.org/api/public/content/9235bfa7ca88433c9898f7f9f7ca432f)).

## 4. Spaces and readiness

Today Baler models equipment and points, not rooms. Requirements like pressure
and ACH belong to **spaces**, so we need:

- **Spaces and space types.** Locations follow RealEstateCore, equipment follows
  Brick. Under their published alignment, REC leads location classes and Brick
  leads equipment ([Memoori](https://memoori.com/?p=14288); Brick ships a
  [Brick–REC alignment](https://github.com/RealEstateCore/rec/releases)). A
  space type (`operating_room`) is what a pack's `applies_to` matches.
- **Adjacency:** room A is next to corridor B, so "positive to adjacent" can be
  evaluated.
- **Serving equipment:** space → VAV/terminal → AHU (Brick `feeds`). Then a
  failing OR traces to its equipment and back.

**Readiness report** (per space, zone or suite):

- every applicable requirement passing over its window;
- the required active tests passed;
- the TAB report attached and linked to the space;
- every open issue resolved or explicitly accepted as a risk (Cx 4).

"Is the surgery suite ready for patients?" is this report for the suite's
spaces. It is the same mechanism for a lab, a cleanroom, a data hall or an
office floor.

## 5. Document hub

A building's documents are part of its record:

- O&M manuals, prints, submittals, start-up guides;
- TAB reports, warranties, schedules, photos.

- **Store:** content-addressed by sha256, which is already the pattern in
  `cx_spec_documents`, so duplicates are free and integrity is checkable for
  decades. Each document is linked to positions, assets, spaces, systems and
  phases. It is searchable, and every AI answer cites document and page.
- **Extraction as reviewed proposals** (the Cx 7 pattern):
  - warranty terms and dates;
  - PM tasks and intervals, including filter sizes and change intervals;
  - make, model and serial from submittals and nameplates;
  - start-up guide checklists, which become procedures (Cx 6);
  - TAB measurements per space.
- **Drawings:** extract one schedule at a time (the equipment schedule, the VAV
  schedule), not "the whole sheet".
  - The evidence favours staged pipelines: detect regions, then a vision model
    reads each one. Task-specific pipelines beat one-shot general VLMs on
    engineering documents ([arXiv 2602.13345](https://arxiv.org/html/2602.13345)).
  - No public MEP benchmark exists, so we build an eval set before trusting
    any number.
- **COBie** import and export: Type, Component, Warranty, Spare, Job ([NIBS COBie Annex A](https://www.nibs.org/files/pdfs/nbimsv3/NBIMS-US_V3_4.2_COBie_Annex_A.pdf)).
  - It is the US public-owner handover interchange.
  - At least one Cx tool, CxAlloy, says it ignores the Spare, Resource and Job sheets ([CxAlloy](https://support.cxalloy.com/article/586-formatting-cobie-data)). Those sheets are exactly the maintenance half an operator needs, so that's an opening.
  - Sources disagree on whether warranty data sits on Type or Component. Follow the Annex A schema.

## 6. Positions and assets: the key data-model decision

"AHU-1 supply fan motor" is a **position**: it has points, a Brick class, a
sequence and requirements. The motor bolted there is an **asset**: make, model,
serial, install date, warranty, cost, and bench and start-up results. Positions
are permanent. Assets come and go.

```
position AHU-1/SF-motor ──installed── asset #A102 (Baldor, 2026-03-01 → 2031-07-14, removed: bearing failure)
                         └─installed── asset #A517 (WEG, 2031-07-14 → now)
```

- **A replacement appends an install/remove event.** It never overwrites. "When
  were the pumps changed, and to what brand" is then a query, and so is "did the
  hunting start after the WEG motor went in".
- **Baselines attach to the asset.** A new asset gets a new start-up baseline,
  so a comparison is never made against different hardware.
- **Service life:** expected life comes from ASHRAE's public
  [service-life database](https://costdatabase.ashrae.org/service_life.asp), as
  a reference with a date, not a copied table. Secondary reproductions disagree
  (base-mounted pumps: 20 or 25 years).
- **Warranties:** attached to the asset, with start, end, terms, and the
  document page they came from. An expiring warranty is an alert, and so is a
  failure still under warranty.

## 7. The testing continuum

| Test | When | How | Status |
|---|---|---|---|
| **Bench test** | Before install | Record a controller or device's test results against the *asset*, as its first baseline | New |
| **Start-up / power-on** | Start-up | Procedures as data (12 seeds), passive criteria | *Built: Cx 6* |
| **Functional / active** | FPT | Override, observe, auto-release, inside the safety envelope (window, admin, `expires_at`, guaranteed relinquish) | *Built: Cx 9* |
| **Passive override test** | Any time after start-up | When a person overrides a setpoint or command, check the equipment reaches it within X minutes, *given the schedule in force* | *Built: #212* (`commissioning/overrides.py`; priority arrays wait on Windrow#25) |
| **Sequence (GL36)** | FPT, seasonal | ConStrain items over trends | *Built: Cx 3* |
| **Continuous compliance** | Operations | Packs on a schedule (§3.3) | New (uses the Cx 6 scheduler) |

**The passive override test** turns every human override into a free functional
test:

- **Detecting the override:**
  - Windrow reads the controller's priority array and reports a level held by
    someone other than Baler (Baler only knows its own levels, in
    `point_write_levels`);
  - or a setpoint changes outside its schedule.
- **Judging the response:** Baler watches the controlled variable against the
  override, using the active weekly schedule and exceptions that
  `windrow/bacnet_schedules.py` already reads. That way "it didn't hit the
  setpoint" isn't confused with "the schedule had it unoccupied".

**Writing to BACnet** already works end to end. `point_write` sends an MQTT
command; Windrow's `connection_manager.write_point` performs a BACnet
WriteProperty at a priority, refusing commands past `expires_at`. It is used by
Haystack `pointWrite` and Cx 9. What's next:

- **audited operator writes** from the UI (who, why, auto-relinquish time); *built: #213*;
- **schedule and calendar writes**, which Windrow reads today but doesn't write.
  *Baler's side built (#213) against the Windrow#26 contract*, see
  docs-site `operator-writes.mdx`.

## 8. Operations analytics

### 8.1 Hunting and oscillation

Hunting means sustained or intermittent oscillation. It costs efficiency,
actuator wear and setpoint tracking:

- a survey of AHU data from 10 buildings suggests it's common (Chintala et al., ASHRAE 2015, via [ORNL](https://impact.ornl.gov/en/publications/identification-and-elimination-of-hunting-behavior-in-hvac-system/));
- NIST's BAS-data method flags "unstable, excessively oscillatory regulation" ([NIST](https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=912260));
- field data show cascaded control fixing it ([Price et al., *Buildings* 2022](https://doaj.org/article/6057004cd94f4af487f46cc44362abb1)).

The detector works per loop. A loop is a command, position or feedback paired
with its controlled variable and setpoint; the Cx ladder already finds these
pairs (`commissioning/ladder.py`).

- **Measure:** reversals of the command, and zero crossings of the error, per
  hour; dominant period; amplitude; travel per hour (actuator wear).
- **Compare** with the asset's start-up baseline (§6), not with a global
  threshold.
- **Report** "AHU-1 CHW valve: 38 reversals/h, 4-minute period, ±22%, since
  2036-02-11. At start-up: 2/h. Asset replaced 2036-02-10 (§6). Likely tuning.
  O&M manual p. 47 covers PID setup."

**Sampling matters.** A 4-minute oscillation is invisible in 15-minute
averages. The detector needs raw samples (COV, or 1 minute or faster) for
loop points, which is a Windrow sampling profile and a reason the raw tier
must be kept compressed rather than downsampled (§10).

### 8.2 Performance baselines and degradation

- **Chiller:**
  - kW/ton regressed on load and condenser-water temperature, so weather
    isn't mistaken for degradation;
  - fitted on the first season after acceptance, then tracked;
  - "the chiller has lost X% since install" is the gap between predicted and
    actual, at matched conditions.
- **The same pattern** applies to boilers (efficiency vs load and return-water
  temperature), fans (kW vs flow and static) and pumps (kW vs flow and head).
- **Model quality** uses ASHRAE Guideline 14's calibration statistics, CV(RMSE)
  and NMBE. Verify the thresholds against the current edition before quoting
  them.
- **Industry rules of thumb** (for example "inspect at 10% kW/ton drift")
  are vendor claims. They are shown as configurable thresholds, not as standards.

### 8.3 Savings, maintenance, deferred maintenance

- **Replace-vs-keep case:**
  - inputs: measured degradation, a utility tariff, the replacement unit's
    rated performance, cost, and remaining service life (§6);
  - outputs: annual savings and simple payback;
  - every assumption is visible and editable. "Replacing CH-2 saves ≈ $10k/yr"
    is only as good as its stated tariff and run hours.
- **PM by condition, not just calendar:**
  - filter change by fan run hours from the status trend;
  - or by filter ΔP when the point exists;
  - intervals from the O&M extraction (§5).
- **Deferred-maintenance register:**
  - deferred work with cost, risk and the asset it affects;
  - roll-ups such as FCI (deferred cost ÷ replacement value);
  - it feeds the capital plan in the passport.
- **Not a CMMS:** Baler generates the work and exports it (CSV, COBie Job, an
  API to the owner's CMMS). It doesn't become a work-order system.

## 9. GC schedule from data

- **Import:**
  - Primavera P6 XER, via [PyP6Xer](https://pypi.org/project/PyP6Xer/), which is LGPL-2.1, so it's an optional extra or a small clean-room parser of the table format;
  - MS Project XML (MSPDI), via stdlib XML.
- **Link** activities to evidence milestones:
  - "AHU-1 start-up" ← its start-up procedure passed;
  - "Level 3 VAVs online" ← points reporting;
  - "TAB complete" ← TAB report linked to the floor's spaces;
  - checklists from forms.
- **Report "earned evidence":**
  - activities the data says are done;
  - activities the schedule says should be done and the data says aren't;
  - activities marked done with no evidence.

  This works the same whether evidence comes from BACnet or from a form.

## 10. Lasting 15+ years: storage, migrations, deployment

### 10.1 The storage problem, in numbers

`point_history` today is one row per sample, with
`point_uri TEXT, ts, value, value_text, value_bool, unit, label` and
`PRIMARY KEY (point_uri, ts)` (`baler/timeseries.py`). It's a hypertable with
5-minute and 1-hour continuous aggregates, but **no compression policy and no
archive**. Estimating the cost per sample:

| Item | Bytes |
|---|---|
| Tuple header and alignment | ~24 |
| `point_uri` text, repeated every row | ~40–60 |
| ts + value + null bitmap | ~20 |
| PK btree entry (repeats the URI) | ~60 |
| **≈ total** | **~150** |

| Site | Samples/yr | Uncompressed/yr | 15 years |
|---|---|---|---|
| 1,000 points at 1/min (a hospital wing) | 525.6 M | ~79 GB | ~1.2 TB |
| 10,000 points at 1/min (a hospital) | 5.3 B | ~790 GB | ~12 TB |

With TimescaleDB columnar compression, segmenting by `point_uri` and ordering
by `ts`, the URI is stored once per segment and timestamps and values are
delta-encoded. Vendor documentation claims 90–95%+ reduction for sensor data
([Tiger Data](https://www.tigerdata.com/docs/learn/capabilities-and-comparison/understand-capabilities)),
and one production report cites 150 GB → 15 GB
([dev.to](https://dev.to/polliog/timescaledb-compression-from-150gb-to-15gb-90-reduction-real-production-data-bnj)).
At an assumed 3–5 bytes per sample, the 1,000-point wing is ~25–40 GB for 15
years, which fits on one disk. **We measure our own number** with
`hypertable_compression_stats` on real data before promising anything.

Also check:

- that idempotent re-inserts (retained-message replays) and backfill (trend
  logs) still work against compressed chunks on the Timescale version we pin;
- that the continuous aggregates refresh correctly over compressed chunks.

### 10.2 Exit and archive: the data must outlive the software

- **Building export:**
  - history as **Parquet**, one file per point-month, with a schema file;
  - the graph as **Turtle** (Brick/Haystack/REC);
  - documents by sha256 with a manifest;
  - packs, Cx records, asset history and issues as JSON/CSV;
  - a README that explains it without Baler.

  This is both the migration path and the sale-package backbone. *Built:
  #193, `python -m baler.export` (docs-site `building-export.mdx`); assets,
  packs and document files join it when #197, #205 and #201 land.*
- **Licence note:** TimescaleDB's compression is under the Timescale License,
  not Apache-2.0. It's free to self-host, which is what we do, but document
  it. The Parquet export is the exit if that ever changes.
- **Migrations:**
  - each service's `CREATE TABLE IF NOT EXISTS` (`baler/ddl.py`) is fine for adding tables, but not for changing them over 15 years;
  - add numbered migrations, a recorded schema version, and a CI test that upgrades a database from every tagged release to `main`;
  - Postgres and Timescale major upgrades get a documented, rehearsed procedure.

### 10.3 Deployment profiles (for the GC's IT team)

| Profile | Shape | Who | Notes |
|---|---|---|---|
| **Building box** | One small server or mini-PC per building, all services | One owner, one building | Data stays with the building and moves with it on sale (export) |
| **Portfolio server** | One install, many sites (multi-site scoping exists) | Owner, Cx firm, GC | Sale of one building = a per-site export |
| **Edge + central** | Windrow (or a Baler box) per building, a central Baler | Large owners | Edge spools while offline (Windrow already spools) |

Each profile gets:

- sizing from §10.1, as a calculator: points × rate × years → disk;
- backup and restore, extending `docs-site/pages/backups.mdx`;
- a **restore drill** that is actually run, not just documented.

## 11. The handover and sale package (building passport)

Std 202 already defines the handover deliverables: systems manual, issues log,
final and end-of-warranty Cx reports, training records. The passport adds
everything since. Per position and asset:

- install and remove history (brands, dates, reasons);
- warranties;
- start-up baseline vs today;
- efficiency trend and the replace-vs-keep case;
- open deferred maintenance;
- compliance history per space;
- document links.

Exported as a readable report plus the §10.2 export.

## 12. AI across the record

The Cx agent (Cx 10) is the pattern: tool use over the same API a person has,
proposals only, every call audited. Extend its tools to:

- documents (search, cite);
- assets and history;
- packs and readiness;
- schedules;
- analytics (hunting, baselines, savings).

Questions it can then answer:

- "Which ORs failed humidity last month?"
- "What does the O&M say about this pump's seal, and is it under warranty?"
- "Draft a test for this sequence."

It runs on the same `BALER_LLM_PROVIDER` choice: none, local, or Claude.

## 13. Where each piece lives

- **Baler:**
  - the record: spaces, packs, positions and assets, documents, schedules;
  - analytics, readiness, the passport, the UI and the assistant.
- **Windrow:**
  - device inventory for the asset registry: vendor, model, firmware, serial where exposed (overlaps Windrow#19);
  - priority-array reads (overrides);
  - schedule and calendar writes;
  - a fast or COV sampling profile for loop points;
  - Modbus parity where it applies.
- **openhay:** nothing new. Haystack wire formats and the tagging work in openhay#7/#8.

## 14. Order of work

1. **Durability first.** A compression policy, measured bytes per sample, the
   export, versioned migrations, and documented deployment profiles. Every year
   without these is data at risk or disk wasted.
2. **Positions and assets, and the document hub.** The skeleton everything else
   hangs on.
3. **Spaces and requirement packs**, with readiness. This is the "is it ready"
   answer, built on Cx.
4. **Testing continuum:** passive override tests, bench tests, operator and
   schedule writes.
5. **Operations analytics:** hunting first, then baselines and degradation, then
   savings.
6. **GC schedule import** and earned evidence.
7. **The passport and the assistant's new tools.**

## 15. What we're not doing

- **Shipping copyrighted standard text or tables.** Packs carry citations;
  numbers are confirmed by the user for their edition.
- **Building a CMMS.** Baler generates and exports work.
- **Downsampling raw history to save disk.** We compress it instead, because
  hunting needs the raw samples.
- **Auto-applying any extracted requirement, warranty or asset fact.** A person
  confirms every one, as in Cx 7.

## Tracking issues

- Epic: Baler#221
- Durability:
  - #192 compression
  - #193 building export
  - #194 versioned migrations
  - #195 deployment profiles
  - #196 restore drill
- Positions, assets, maintenance:
  - #197 position vs asset
  - #198 warranties
  - #199 PM schedules
  - #200 deferred maintenance
- Document hub:
  - #201 store
  - #202 O&M/warranty extraction
  - #203 drawing schedules
  - #204 COBie
- Requirement packs:
  - #205 schema
  - #206 extraction
  - #207 continuous compliance
  - #208 ASHRAE 170 OR example
- Spaces and readiness:
  - #209 space model
  - #210 readiness report
- Testing:
  - #211 bench tests
  - #212 passive override tests
  - #213 operator and schedule writes
- Analytics:
  - #214 hunting
  - #215 baselines and degradation
  - #216 savings case
- GC progress:
  - #217 schedule import
  - #218 earned evidence
- Passport: #219
- Assistant: #220
- Windrow:
  - Windrow#24 device inventory
  - Windrow#25 priority arrays
  - Windrow#26 schedule writes
  - Windrow#27 fast/COV sampling

# Cx 3 spike: PNNL ConStrain for GL36 / 90.1 sequence verification

Status: **spike done** (Baler#39, slice Cx 3 of #32). Code:
`src/baler/commissioning/gl36.py` (adapter),
`src/baler/commissioning/vendor_constrain/` (vendored subset), the
`cx-gl36` extra, and `tests/test_commissioning_gl36.py` over
`tests/cx_hospital.py`. Everything below was checked against the real
package (`constrain==0.8.0` from PyPI, GitHub `main` at `3717cd3`,
2026-06-30), installed in a throwaway venv. Nothing is assumed from docs.

## Recommendation

**Vendor a subset. Don't depend on `constrain`.** It can't be a dependency
of Baler at all (next section). The subset that runs here is about 250
lines: the rule-check base class and four item classes, with upstream's
`verify()` bodies unchanged. Its only dependency is pandas, which is the
whole `cx-gl36` extra. On the hospital fixture, the vendored items and
real ConStrain 0.8.0 give **identical per-sample verdicts** in all four
cases tried: HW reset (passing and failing), zone deadband (failing) and
CHW reset (passing). Upstream ran on pandas 2.3.3 and the vendored copy
on pandas 3.0.5.

Keep the adapter (Baler-side mapping, dataframes and results) as ours.
Re-vendor an item when we need one, and diff it against upstream when we
bump the version.

## Licence

BSD-2-Clause, Copyright (c) 2021 Battelle Memorial Institute. This is the
LICENSE.md in both the wheel and the repo. It is compatible with AGPL-3.0:
we can redistribute it if we keep the copyright notice and conditions. The
full text is in `vendor_constrain/__init__.py`, and each vendored file
names its source.

## Footprint and how it runs

- **Python**: `>=3.10,<4.0`. It imports and runs on 3.13.
- **Dependency conflict (the deciding fact)**: `constrain` pins
  `brickschema==0.7.1`, which pulls `rdflib 6.3.2` and `pyshacl 0.22.2`.
  Baler needs `rdflib>=7.0.0` and `pyshacl>=0.30.0`.
  `pip install constrain==0.8.0 "rdflib>=7.0.0"` fails with
  `ResolutionImpossible`. GitHub `main` has the same pin, so there is no
  newer release to wait for.
- **Size**: the wheel is 14 MB and unpacks to 174 MB, 172 MB of which is
  `constrain/demo`. A fresh venv with it is **1.2 GB** and 116
  distributions, including PyQt6 (273 MB), scipy (109 MB), pandas
  (75 MB), scikit-learn (48 MB), matplotlib and seaborn, eppy, fastapi
  and uvicorn, and `setuptools<70` (a downgrade). The vendored subset
  needs only pandas.
- **Import side effects**: `import constrain` takes about 1.4 s. It
  imports every submodule: EnergyPlus I/O, a FastAPI app, the AI workflow
  and plotting. It also calls `logging.basicConfig` and sets the **root
  logger to INFO** in any process that imports it. That alone rules it
  out for `commissioning_api`. The vendored import takes about 0.24 s,
  mostly pandas.
- **Two ways to run it**:
  1. *Library API.* Each item is a class:
     `Item(df, params=..., tolerances=...)` runs `verify()` in its
     constructor and fills `result`, a per-sample series of
     `True`/`False`/`"Untested"`. `check_bool()` returns False if any
     sample fails, True if any passes, otherwise `"Untested"`.
     `check_detail()` returns the counts. **This is the part we use.** It
     is plain pandas, and the dataframe is the only interface.
  2. *JSON workflow.* `api/workflow.py` runs a state machine over
     `verification_cases` JSON. Each case names an EnergyPlus
     `simulation_IO` (idf/idd/weather/output CSV), a
     `datapoints_source.dev_settings` (map from point to CSV column) and
     a `verification_class`. There is also a `BrickCompliance` path that
     runs SPARQL from `resources/brick/query_statement.yml` over a Brick
     TTL loaded by brickschema. This path is file- and simulation-centric
     (CSV in, markdown and PNG reports out). It doesn't fit a service
     reading `point_history`, and its Brick queries assume `HVAC_Zone`
     hasPoint setpoints and AHU→Fan→hasPoint shapes that our inventory
     doesn't use. We take nothing from it except, as a reference, the
     queries' choice of Brick classes.
- **Units**: items assume SI (deg C, m3/s). Tolerances come from
  `constrain/tolerances.json`. The library falls back to 0 when it isn't
  passed, but the workflow does pass it, so we pass the same values
  (`vendor_constrain.TOLERANCES`).

## Item-by-item mapping (Brick via the Cx inventory)

How the adapter works: a "role" is a ConStrain data point, meaning a
dataframe column. Each role resolves to a Baler point on the equipment
through the Cx inventory (`commissioning.inventory.build`, already scoped
to the caller and site). It matches on Brick class first, exact and then
by descendants (`brick_vocab`), and on Haystack markers second (the same
rule `templates.evaluate` uses). Outdoor air temperature may come from
anywhere in the scoped inventory, because the hospital's OAT sits on the
AHUs. Design values are constant **parameters** from project settings
(`settings.gl36.<item>`, in deg C, or in F with `"unit": "F"`).

The dataframe comes from `History.window` for the bound points over the
run window. Each point is resampled to a 5-minute grid (last value),
held forward up to 3 steps, and rows with any gap are dropped. Points in
F (by unit: `°F`, `degreesFahrenheit`, …) are converted to C, and binary
states (`true`/`active`/`on` and so on) become 1/0.

| Issue's item | ConStrain 0.8.0 class (requirement) | Equipment | Roles → Brick classes | Params | Mock hospital |
|---|---|---|---|---|---|
| Zone deadband | `ZoneTempControl` (90.1-2019 6.4.3.1.2: ≥ 5 F / 2.77 C between cooling and heating setpoints) | `Terminal_Unit` family | `Zone_Air_Cooling_Temperature_Setpoint`, `Zone_Air_Heating_Temperature_Setpoint` | – | **Can't run.** Hospital VAVs have occupied and unoccupied setpoints (`zone temp occ/unocc sp`), not a cooling/heating split. → `inconclusive`, missing points listed |
| Zone setback | *no item in 0.8.0* (closest: G36 terminal-box setpoint items, which need `mode_operation`) | – | – | – | – |
| SAT control | `SupplyAirTempReset` (90.1-2016 6.5.3.5: SAT setpoint range ≥ 25 % of design zone cooling SP − min SAT SP) | `Air_Handling_Unit` | `Supply_Air_Temperature_Setpoint` / `Discharge_Air_Temperature_Setpoint` | `temperature_air_zone_design_cool_setpoint` | **Can't run.** The hospital AHU has only a discharge air temperature *sensor*. → `inconclusive` |
| SAT control (G36 proper) | `G36SupplyAirTemperatureSetpoint` (G36-2021 5.16.2.2) | AHU | + `mode_operation`, SAT max | several | Not adapted: needs the AHU operating mode (cooldown/warmup/setback/occupied), which no source maps yet |
| Economizer high limit | *missing upstream.* `resources/brick/query_statement.yml` has queries for `EconomizerHighLimitA–D`, but no class exists in the 0.8.0 library or on `main`. Upstream branch `50-the-economizerhighlimit-verification-logic-are-missing` tracks it | – | (`Outside_Air_Temperature_Sensor`, `Outside_Air_Flow_Sensor`, `Outside_Air_Enthalpy_Sensor`) | – | No OA flow or enthalpy on the hospital |
| Integrated economizer | *missing upstream* as an item. `checklib.EconomizerIntegrationCompliance` exists but reads EnergyPlus outputs (`OA_timestep`, `OA_min_sys`, `Cool_sys_out`) | – | (`Outside_Air_Flow_Sensor`, cooling coil `Thermal_Power_Meter`) | – | No OA flow or coil power on the hospital |
| CHW reset | `CHWReset` (90.1-2016 6.5.4.4) | `Chiller` | `Outside_Air_Temperature_Sensor` (equip or site), `Chilled_Water_Supply_Temperature_Sensor`, `Chilled_Water_Flow_Sensor` → *proxy*: own `Run_Status` | OAT min/max, CHW SP min/max | Runs with the proxy, **but see the caveat below** |
| HW reset | `HWReset` (90.1-2016 6.5.4.4) | `Boiler` | `Outside_Air_Temperature_Sensor` (equip or site), `Hot_Water_Supply_Temperature_Sensor`, `Hot_Water_Flow_Sensor` → *proxy*: own `Run_Status` | OAT min/max, HW SP max/min | **Runs.** This is the spike's demonstration item |

The proxy: the hospital has no flow sensors. A boiler or chiller's own
run status stands in for flow, so "off" means no flow and the sample is
exempt, as ConStrain's `flow <= tolerance` branch intends. When the
proxy is used, the result's `evidence.proxies` says so.

Upstream's Brick queries bind `temperature_water_hot/chilled` to the
**return** water temperature sensor. 6.5.4.4 resets **supply**
temperature, so we bind supply. This is a deliberate deviation.

## What a result looks like

One result per enabled item per matching equipment: check id
`gl36:<item>`, subject the equipment, severity null.

- `pass`: ConStrain's `check_bool()` is True.
- `fail`: any sample failed. The symptom gives "n of N time steps break
  <requirement>", and evidence carries `first_failures` (up to 5
  timestamps), `fail_fraction`, the bound `points`, `proxies`, `params_c`
  and `samples`.
- `inconclusive`: missing points or parameters, no aligned samples, all
  samples untested, or the item raised an exception. It is never a pass.

Items run inside the normal (now asynchronous, #57) run when the project
enables them with `settings.gl36`, for example
`{"hw_reset": {"unit": "F", "temperature_air_outdoor_min": 20,
"temperature_air_outdoor_max": 60, "temperature_water_hot_setpoint_max": 180,
"temperature_water_hot_setpoint_min": 140}}`. Without the extra installed,
`PATCH /cx/projects/{id}` with `gl36` answers 400. The Docker image does
**not** install the extra yet.

On `tests/cx_hospital.py` (24 h, OAT 15–65 F):

- `hw_reset` passes 289 of 289 steps when the boiler follows the reset.
- `hw_reset` fails 39 of 289 steps (the warm afternoon) when HW sits at
  180 F.
- `zone_deadband` fails 24 of 289 steps (06–08 UTC, 2 F band) on the VAV
  that has both setpoints, and is inconclusive on the hospital-style VAV.
- `sat_reset` is inconclusive.

## Findings to carry forward

1. **ConStrain's verdict is all-or-nothing.** One bad 5-minute step fails
   the item. That is right for a code-compliance audit, but on real trends
   (sensor noise, transitions, overrides) most items will fail. We keep
   upstream's verdict, so a result still means what it means in
   ConStrain. `fail_fraction` and `first_failures` are there for a later
   threshold or "persistent for N steps" policy. That policy is ours to
   decide (Cx 6/Cx 8), not upstream's.
2. **`CHWReset` doesn't verify a reset.** At low outdoor temperature it
   checks only CHW ≥ min SP, and at high outdoor temperature only
   CHW ≤ max SP. A chiller held at a constant temperature inside the band
   passes (`test_chw_reset_passes_a_constant_chw_temperature`). `HWReset`
   checks both ends properly. Worth an upstream issue. We shouldn't
   "fix" the vendored copy, so it stays comparable with upstream.
3. **"GL36" is a loose label here.** The four items run so far check
   ASHRAE 90.1 code requirements. The real G36 items (31 `G36*` classes:
   SAT setpoint, OA/RA/relief damper positions, freeze protection,
   terminal-box airflow setpoints) need operating mode, damper positions
   and design airflows that the hospital doesn't carry. They are the next
   candidates once a site has those points.
4. **The economizer items in the issue don't exist upstream.** Writing
   our own (high-limit by fixed dry-bulb is a few lines) would be Baler
   code, not ConStrain's.
5. **Point coverage, not algorithms, is the bottleneck.** On the hospital
   only `hw_reset`, and `chw_reset` via the proxy, can run.
   Heating/cooling zone setpoints, SAT setpoint and OA flow and damper
   positions are what a real site needs before more items can run. This
   feeds the Cx 1 expected-point templates (add the setpoints the items
   need as `expected`).

## Decisions for Jacob

- Ship vendored (as built) or not at all. A dependency is impossible
  while upstream pins brickschema 0.7.1.
- Put `cx-gl36` (pandas) into the Docker image so projects can enable
  items live? It costs about 75 MB. The image doesn't have it today.
- Pass/fail policy beyond upstream's any-sample-fails rule (finding 1).
- Write our own economizer high-limit and integrated-economizer items,
  since upstream has none (finding 4).

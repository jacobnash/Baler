# Control plane & scripting — design for a building engineer

**Status:** Slice A partially implemented (Ops status API + UI + install script). Remaining slices B–D still design-only.
**Audience:** product / architecture decision
**Date:** 2026-09-09
**Updated:** 2026-09-09 — Slice A landed: `ops_api`, `/ui/ops.html`, `scripts/install.sh`
This document answers four questions in order:

1. What does SkySpark's first-run feel like to a building engineer?
2. What is Baler's actual container topology and health surface
   today?
3. What control-plane UX should Baler put in front of that stack?
4. What replaces SkySpark's saved-Axon-script model without letting the
   server rot?

---

## 0. Design principles (for the audience)

The user is a building engineer / commissioning agent / analyst. They
belong to a stationary-engineers or building-engineers local. They are
sharp about HVAC, controls, and point naming. They are not a software
engineer and it should not become their job to be one.

Assumptions we design around:

- They can install Docker from a script. That is the ceiling of
  "sysadmin" we may require.
- They will not read `docker compose ps`, `journalctl`, or OpenAPI.
- "Healthy" must mean *useful to the building*, not *container process
  exists*.
- Power users still need a path to advanced analysis (cycling harmonics,
  custom KPIs, site-specific rules). That path must not be "paste
  untested scripts onto a shared server."

---

## 1. SkySpark first-run (live walkthrough)

### 1.1 How we got it running

There is **no official SkyFoundry Docker image**. Community images exist
on Docker Hub; we used `phillipbirch/skyspark-latest` (SkySpark 3.1.18,
~3 years old). The image's default command is `/bin/bash` — it does not
start SkySpark by itself. A building engineer following "just Docker"
still needs to know to run:

```bash
docker run -d -p 8088:8080 --name skyspark \
  -w /app/skyspark-latest phillipbirch/skyspark-latest \
  ./bin/skyspark
```

That is already a step past "one script installs Docker and you're
running."

### 1.2 Boot log, as experienced

Within ~3 seconds of start:

```
<lic> [err] No license installed
<lic> [err] Fatal licensing err; shutting down exts
<web> [info] http started on port 8080
```

HTTP comes up anyway. `/` redirects to `/user/login`. Login with the
community-image default `su` / `su` works. After login the host home
shows five tiles — **Debug, Doc, Host, Settings, User** — and a
persistent red **"No license Installed"** chip.

Those five tiles are not decoration. They *are* SkySpark's instance
control plane. Clicking into them is how you manage the running host
without a terminal. We walked each app on this live instance; the
lessons below are from that pass, not from brochure copy.

What a building engineer sees immediately:

| Moment | Experience |
|---|---|
| After install | Login page with SkySpark branding. Looks finished. |
| After login | Host dashboard with five admin apps + license warning. |
| First useful action *on this screen* | Open **Host → Projects → New** or **Demogen** — create a project / generate demo data. |
| Building analytics work | Blocked or uncertain under the license wall (exts shut down at boot). |
| "Is the instance itself manageable?" | **Yes** — Host / Settings / Debug / User still operate as an admin surface. |

### 1.3 The host apps — how SkySpark manages the instance

This is the part to steal. One process, five apps, no `systemctl`.

#### Host — runtime objects

Tabs: **Projects, Cluster, Crypto, Install, Licenses, Mounts, Replicas,
Sessions**.

| Tab | What you can do (concrete UI) |
|---|---|
| **Projects** | Table of projects (`id`, `route`, `routeStatus`, `backup`, …). Buttons: **New**, **Demogen**, Rename, Delete, Details, Backup, Replicate. Empty on first run — **New** / **Demogen** is the obvious next step. Creating a project does **not** require a restart. |
| **Install** | Pod/extension inventory (BACnet, demogen, docker, …). Update / Install / Downgrade / Uninstall / StackHub Login. Module changes typically need a restart. |
| **Licenses** | Install / Update from Cloud / Uninstall; Effective License panel (`points`, `features`, `weather`); Host Info for licensing identity. |
| **Sessions** | Who is logged in (user, IP, browser, lease); Logout / Details. |
| **Cluster / Replicas / Crypto / Mounts** | Multi-node, DB replication, trust store, filesystem mounts — power-user / IT territory, but present in the same app. |

**First useful action on a fresh host:** Host → Projects → **New** (or
**Demogen**). That is clearer than Baler's current "curl an org,
then figure out Windrow."

#### Settings — what is enabled and how it listens

Tabs include **SysMods, API, Cluster, Email, HTTP, Host, Log, Session,
User, XQuery**.

| Tab | Why it matters |
|---|---|
| **SysMods** | ~40 modules with `libStatus` (Ok / Disabled), `enabled` (`boot`), docs, depends. **Enable / Disable** selected rows. This is "turn LDAP on / email off" without editing files. Changes need a restart; there is **no Apply & Restart** on the screen. |
| **HTTP** | `httpPort` (8080), `httpsEnabled`, `siteUri`, bind address — the "what port am I on" page. Restart required. |
| **Email** | SMTP URL / TLS / credentials with a **Test** button (hot, no restart). |
| **Log / Session / User / API** | Retention, timeouts, lockout, API hardening — all form fields + Submit. |

SysMods is the closest SkySpark analogue to "which of my services /
capabilities are on." Status is visible as a column (`libStatus: Ok`),
not buried in a process list.

#### Debug — health without SSH

Tabs: **Alerts, Cluster, Diagnostics, Host, Log, Pods, Threads,
Support**.

| Tab | Affordance |
|---|---|
| **Diagnostics** | Live cards: System (version, PID, uptime, paths), Projects count, Java, **CPU graph**, physical memory graph, Java heap, GC. This is the "is the box sick?" page. |
| **Log** | In-browser log stream with level filter / pause / search — no `docker logs`. |
| **Alerts** | System warnings (license among them) with links into docs. |
| **Pods / Threads** | Installed pod detail; thread dump with CPU — deeper than most building engineers need, available when support asks. |
| **Support** | One-click diagnostic bundle. |

#### User / Doc

- **User**: account table (su), role protos, user-DB backup/restore.
- **Doc**: in-product manuals (Axon, Haystack, Fresco, libs) — genuinely
  strong.

### 1.4 What SkySpark gets right (instance management)

1. **One browser surface for admin.** Projects, modules, HTTP port,
   sessions, logs, CPU — not five CLIs and a wiki.
2. **Projects as the unit of building work**, created from a button, with
   **Demogen** beside **New**.
3. **Module enable/disable with status columns** (SysMods `libStatus`),
   not "edit compose and hope."
4. **Debug Diagnostics + Log** answer "something's wrong" without leaving
   the product.
5. **Support bundle** — one click for when you escalate.

Baler's Ops page should be *this shape* — host apps mapped onto
a multi-container reality — not a Docker Desktop clone.

### 1.5 Where it is still clunky (for this audience)

1. **License wall after a successful login.** Exts shut down at boot;
   the admin apps still work, which makes it unclear what building work
   is actually possible. Ambiguity is worse than a hard fail.
2. **Jargon tax.** "SysMods", "pods", `libStatus`, `routeStatus` —
   fine for SkySpark natives, opaque for a stationary engineer on day
   one. The *structure* is right; the *labels* assume prior art.
3. **No restart from the UI.** SysMods and HTTP say "change this," then
   strand you at Docker/systemd/`fanlaunch`. The control plane stops one
   step short of the action it implies.
4. **Empty Projects table without a guided first run.** New/Demogen are
   there, but nothing says "click Demogen to see a building."
5. **Docker story is unofficial.** No official image; community image
   defaults to bash; Java is baked in, license is not.
6. **Single-app strength is real** once licensed and project-loaded —
   UI, historian, Axon, connectors share one skin. That emotional bar
   still stands.

### 1.6 Haxall as the open-source sibling (same session)

Official `ghcr.io/haxall/haxall` starts cleanly. Init prints a one-time
superuser password to container logs (easy to miss if you started via
Docker Desktop GUI). After login you land on **`/shell`** with the prompt
"Try out some Axon!" — `1 + 2`, `libs()`, `funcs()` all work immediately.

That is the best of the SkySpark family UX: **zero clicks to a live
query surface**. It is also the worst of the scripting problem in miniature:
a blank REPL, no unit tests, no packaged analysis library beyond what
ships in `funcs()`, and no guardrail against saving a slow recursive
query that will run forever on a shared host.

**Takeaways for Baler:**

- From **SkySpark Host apps**: ship one in-product control plane
  (projects/sites, enable/disable capabilities, diagnostics, logs,
  sessions, support bundle) — not a terminal, not Portainer.
- From **Haxall**: first useful action one screen away. Do **not** make
  the product *be* a REPL.
- Close SkySpark's gap: **Apply & Restart** (and start/stop of optional
  jobs) must live in that same UI.

---

## 2. Baler container topology

### 2.1 Counts

| Mode | Containers |
|---|---|
| `docker compose up -d` (default) | **14** |
| + all profiles | **18** |

Architecture docs still say "14 + haystack_puller"; compose actually has
four opt-in profiles (`haystack-pull`, `sql-pull`, `energystar-pull`,
`openmeteo-pull`).

### 2.2 Container map

```
                         ┌──────────────────────────────┐
   Browser / curl ──────►│ gateway (nginx :8080)        │
                         │  JWT + route + /ui/ static   │
                         └──────────────┬───────────────┘
              ┌───────────┬─────────────┼───────────┬───────────┐
              ▼           ▼             ▼           ▼           ▼
         ingest_api   fault_api   derivation_api  validate_api  auth_api
           :8000        :8002         :8003          :8005       :8006
              │           │             │              │           │
              └─────┬─────┴──────┬──────┴──────┬───────┴─────┬─────┘
                    ▼            ▼             ▼             ▼
               postgres     oxigraph      mosquitto      (jaeger OTLP)
              (Timescale)     RDF           MQTT
                    ▲            ▲             ▲
                    │            │             │
         mqtt_listener ──────────┼─────────────┤
         fault_detector ─────────┼─────────────┤
         derivation_engine ──────┴─────────────┘

   grafana (:3033) ──reads──► postgres
   jaeger UI (:16686) ◄── traces from daemons

   Profile-gated (off by default):
     haystack_puller | sql_puller | energystar_puller | openmeteo_puller
       └── each writes via ingest path; depends on postgres+oxigraph only
```

### 2.3 Per-service role, dependencies, health signals

#### Infrastructure (5)

| Service | Role | Depends on | Docker health | App-level status today |
|---|---|---|---|---|
| `postgres` | Timescale historian + faults + auth + health tables | — | `pg_isready` | SQL; Grafana datasource |
| `oxigraph` | RDF entity graph (SPARQL) | — | **none** | SPARQL `/query`; UI equipment tree |
| `mosquitto` | MQTT broker (`fbf/#`) | — | **none** | Daemon connect/disconnect logs |
| `jaeger` | Traces | — | **none** | UI `:16686` |
| `grafana` | Point-history dashboards | postgres (runtime) | **none** | Login `:3033` |

#### HTTP APIs (5) — all behind gateway

| Service | Role | Depends on | Docker health | App-level status today |
|---|---|---|---|---|
| `ingest_api` | `POST /ingest`, history, his, tags | pg + oxi + mqtt | `GET /openapi.yaml` | OpenAPI/docs only |
| `fault_api` | rules, webhooks, `GET /faults` | pg + oxi + mqtt | `GET /openapi.yaml` | webhook_health fields on `GET /webhooks` |
| `derivation_api` | derivations CRUD, test, dry-run | pg + oxi + mqtt | `GET /openapi.yaml` | `GET /derivations/{id}/targets` |
| `validate_api` | SHACL validate | pg + oxi + mqtt | `GET /openapi.yaml` | `POST /validate` report |
| `auth_api` | orgs, login, keys, sites | pg + oxi + mqtt | `GET /openapi.yaml` | `GET /me` |

There are **no** `/health`, `/ready`, or `/live` endpoints anywhere in
the repo. "Healthy" in Compose means "OpenAPI YAML returned 200."

#### Pure daemons (3)

| Service | Role | Restart | Health today |
|---|---|---|---|
| `mqtt_listener` | MQTT → stores | `unless-stopped` | Logs only (`connected…`) |
| `fault_detector` | Evaluate rules, fire webhooks | `unless-stopped` | Logs; open faults in Postgres |
| `derivation_engine` | Evaluate derivations | `unless-stopped` | Logs; per-target health table |

#### Gateway (1)

| Service | Role | Depends on | Health today |
|---|---|---|---|
| `gateway` | nginx + njs JWT; serves `/ui/` | all 5 APIs healthy | **none**; known stale-DNS bug after backend recreate |

#### Profile pullers (4)

| Service | Profile | Health today |
|---|---|---|
| `haystack_puller` | `haystack-pull` | Logs; silent fail if URL blank |
| `sql_puller` | `sql-pull` | same |
| `energystar_puller` | `energystar-pull` | same |
| `openmeteo_puller` | `openmeteo-pull` | same |

### 2.4 Dependency summary (what breaks what)

| If this dies… | User-visible failure |
|---|---|
| `postgres` | No history, faults, auth, Grafana; daemons crash-loop |
| `oxigraph` | Empty equipment tree; tags/validate/derivations blind |
| `mosquitto` | No live BACnet/Modbus ingest; cur-mode faults/derivations stall |
| any one HTTP API | Gateway may 502 that prefix; other APIs keep working |
| `gateway` | Entire product unreachable from browser (backends still up) |
| `mqtt_listener` | MQTT data stops landing; HTTP ingest still works |
| `fault_detector` / `derivation_engine` | Rules/derivations stop evaluating; CRUD APIs still look fine |
| a puller | That source goes quiet; stack otherwise green |

### 2.5 Status signals that already exist (and are unused by any UI)

Baler already has pieces of a control plane, just not assembled:

- Compose healthchecks on postgres + 5 APIs
- `derivation_target_health` (consecutive failures → auto-disable)
- webhook delivery health on `GET /fault/webhooks`
- open faults via `GET /fault/faults`
- Jaeger traces from every daemon
- Grafana for point history (not for service health)
- UI pages for equipment, his query, alarms, derivations, devices

Missing: one place that answers "is my building platform okay?" without
opening five tabs and a terminal.

---

## 3. Control-plane UX design

Not a Kubernetes dashboard. A **building control room for the software
that watches the building** — SkySpark's Host / Settings / Debug shape,
translated across Baler's multi-container reality, with the
restart button SkySpark forgot.

### 3.1 Product framing

Call it **Ops** (or **System**) — a first-class page in the existing
`/ui/` chrome, peer to Equipment / Query / Alarms / Derivations /
Devices. It is the page the install script opens when the stack first
comes up.

Steal SkySpark's five-app split, rename for the audience:

| SkySpark host app | Baler Ops analogue | Notes |
|---|---|---|
| **Host → Projects** (+ Demogen) | **Sites / data** + "Load demo" | Org/site already exists in `auth_api`; surface create + demo as the empty-state CTA |
| **Settings → SysMods** | **Jobs & sources** enable/disable | Map to compose services + profiles with plain names and Ok/Quiet/Down (SysMods `libStatus` without the jargon) |
| **Settings → HTTP / Email / …** | **Platform settings** | Gateway URL, MQTT, webhook allow-private — forms, not `.env` spelunking |
| **Debug → Diagnostics** | **Health** | Required: ingest freshness, open faults, disabled derivation targets, API probes; host CPU/disk optional later |
| **Debug → Log** | **Recent log** per job | Tail allowlisted container logs in-browser |
| **Debug → Support** | **Download diagnostics** | Bundle compose ps, last errors, versions |
| **Host → Sessions / User** | Account admin (existing auth UI) | Link from Ops; don't rebuild |
| **Host → Install / Licenses** | **Updates** (later); no license wall | Catalog packs / image update — not StackHub+EULA day one |

Mental model: three layers the engineer already understands.

| Layer | Analogy | Examples |
|---|---|---|
| **Building data** | "Are my points alive?" | last ingest age, open faults, derivation outputs |
| **Jobs** | "Are my analyses running?" (SysMods, plain language) | fault detector, derivation engine, each puller |
| **Platform** | "Is the machine okay?" (Diagnostics) | postgres, oxigraph, mqtt, gateway |

Never lead with container names. Lead with jobs and outcomes; drill to
containers only when diagnosing.

### 3.2 What the user sees immediately after install

**Install script contract** (the only terminal they should ever need):

```text
1. Install Docker (vendor script / Docker Desktop)
2. Run install-baler.sh
      - clones or unpacks release
      - generates JWT/gateway/MQTT secrets into .env
      - docker compose up -d --wait
      - opens browser to http://localhost:8080/ui/ops/
3. Create org (first-run form) → land on Ops
```

Mirror SkySpark's Host → Projects empty state, but label the intent
(SkySpark shows New/Demogen; we say what they're for):

**Ops first paint** (empty site, healthy platform):

```
┌─────────────────────────────────────────────────────────────┐
│  Baler                          System: All green    │
│  Equipment · Query · Alarms · Derivations · Devices · Ops   │
├─────────────────────────────────────────────────────────────┤
│  Overview · Jobs · Platform · Health · Logs                 │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  You're up. No building data yet.                           │
│                                                             │
│  First useful action                                        │
│  ┌──────────────────┐  ┌──────────────────┐                 │
│  │ Connect a source │  │ Load demo site   │  ← like Demogen │
│  │  MQTT / Windrow      │  │  (mock hospital) │                 │
│  └──────────────────┘  └──────────────────┘                 │
│                                                             │
│  Platform                                                   │
│  ● Historian  ● Model store  ● Live bus  ● Gateway  ● Auth  │
│                                                             │
│  Jobs (idle until data arrives)                             │
│  ○ Ingest listener   ○ Fault detector   ○ Derivations       │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

Copy rules:

- **One headline, one next step.** After install: get data in or try the
  demo (SkySpark: New / Demogen).
- No container grid on first paint.
- No Jaeger / Grafana / SPARQL links in the hero — **Advanced** only.
- One Ops chrome with tabs is enough; we do not need five separate tile
  apps at our scale.

### 3.3 First useful action

Priority order for an empty install:

1. **Connect live data (Windrow / MQTT)** — the real job.
2. **Load demo site** — SkySpark's Demogen equivalent; sample equipment +
   histories so Query / Alarms / Derivations are immediately meaningful.
3. **Pull from existing Haystack/SkySpark** — profile-gated
   `haystack_puller`, configured from a form (URL, user, password), not
   by editing compose.

SkySpark's path is: Host → Projects → New/Demogen → (licensed) query.
Ours should be: **see a point move**, then **open or silence a fault**,
then **save a tested derivation**. Ops exists so those three aren't
blocked by "which of 14 containers is wedged?" — and so enable/disable/
restart feel as direct as SysMods, with the Apply & Restart SkySpark
omits.
### 3.4 How they know something is broken

Status model (four states, plain language):

| State | Meaning | Color |
|---|---|---|
| **OK** | Doing its job | green |
| **Quiet** | Up, but no recent useful work (e.g. listener connected, zero messages for N minutes on a site that should be live) | amber |
| **Degraded** | Partial failure (one derivation target disabled; webhook failing; gateway serving but one API 502) | amber |
| **Down** | Process unhealthy / unreachable / crash-looping | red |

Top-bar chip: `System: All green` | `System: Needs attention (2)` | `System: Down`.

Clicking the chip always goes to Ops filtered to non-OK items.

**Broken → specific service** drill path:

```
Top chip "Needs attention (2)"
  → Ops lists:
      • Live bus — quiet since 14:02 (expected messages from site X)
      • Fault detector — down (restarting)
  → expand "Fault detector"
      plain explanation: "Watches rules and opens alarms. Without it,
      new alarms won't appear."
      last error line from logs (one paragraph, not a firehose)
      [Restart] [View recent log] [Open related: Alarms]
      advanced: container name `fault_detector`, compose service link
```

Mapping from engineer language → containers is owned by Ops, not by the
user.

### 3.5 Start / stop without a terminal

Ops actions (authenticated admin only):

| Action | Scope | Implementation sketch |
|---|---|---|
| Restart | one job or one platform component | `docker compose restart <service>` via a small **ops agent** |
| Stop / Start | optional jobs (pullers, demo) and non-critical tools (grafana, jaeger) | `compose stop/start` |
| Enable data source | turn on a profile + write env | `compose --profile … up -d` + `.env` update |
| Restart gateway | after backend changes | dedicated button; also auto-suggested when Ops detects 502 mismatch |

**Ops agent** (new, tiny): a privileged sidecar or host helper that can
only run an allowlisted set of compose operations. It is **not** Docker
socket exposed to the browser. The browser talks to `ops_api` over the
existing gateway JWT; `ops_api` talks to the agent.

Hard rules:

- Cannot delete volumes or run arbitrary compose from the UI.
- Core trio (postgres, oxigraph, mosquitto) stop requires an extra
  confirm typed phrase ("stop historian").
- Every action is audited (`who`, `what`, `when`) in Postgres.

### 3.6 Information architecture of Ops

Five tabs inside Ops (SkySpark Host/Settings/Debug collapsed into one
place a building engineer can find):

1. **Overview** — building outcomes + platform chips + attention list
   (SkySpark host home + Diagnostics summary)
2. **Jobs** — listener, detectors, engines, each configured puller;
   Enable/Disable/Restart like SysMods, with plain names
3. **Platform** — historian, model store, live bus, gateway, auth,
   optional tools; settings forms where safe (SkySpark Settings → HTTP
   etc.)
4. **Health** — Diagnostics equivalent: freshness, fault counts,
   disabled targets, probe results; optional host CPU/disk later
5. **Logs** — per-job recent log (SkySpark Debug → Log)

Each Jobs/Platform row shows: name (engineer language), state, "last
useful signal", primary action (**Restart** always present — the button
SkySpark's SysMods screen lacks).

**Advanced** (link, not a peer tab): raw container names, ports, Jaeger,
Grafana, OpenAPI, **Download diagnostics** (SkySpark Support bundle).

### 3.7 Health API we need to add

Compose OpenAPI checks are necessary but not sufficient. Proposal:

`GET /ops/status` (gateway-authenticated) returns a single document:

```json
{
  "summary": "degraded",
  "platform": [
    {"id": "historian", "service": "postgres", "state": "ok"},
    {"id": "model", "service": "oxigraph", "state": "ok"},
    {"id": "live_bus", "service": "mosquitto", "state": "quiet",
     "detail": "no messages on fbf/# in 12m"}
  ],
  "jobs": [
    {"id": "fault_detector", "state": "ok", "open_faults": 3},
    {"id": "derivation_engine", "state": "degraded",
     "disabled_targets": 2},
    {"id": "haystack_puller", "state": "down",
     "detail": "REMOTE_HAYSTACK_URL empty"}
  ],
  "building": {
    "points_seen_last_hour": 0,
    "open_faults": 3,
    "last_ingest_at": null
  }
}
```

Sources to aggregate (already mostly exist):

| Signal | Source |
|---|---|
| API up | existing OpenAPI healthchecks + active probe through gateway |
| Daemon up | Docker state via ops agent **or** heartbeat row written each tick |
| Ingest freshness | `max(ts)` from `point_history` / MQTT receive timestamps |
| Derivation pain | `derivation_target_health` |
| Webhook pain | webhook health columns |
| Puller misconfig | treat blank required env as **down**, not silent "up" |
| Gateway staleness | probe each API prefix; if backend healthy but gateway 502 → suggest restart gateway |

### 3.8 Tradeoffs — control plane

| Option | Pros | Cons | Recommendation |
|---|---|---|---|
| **A. Ops in `/ui/`, shaped like SkySpark Host/Settings/Debug** | Proven admin IA; one app; can add Restart SkySpark lacks | Must map multi-container jobs to SysMods-like rows | **Do this** |
| **B. Ship Portainer / Dozzle beside compose** | Fast; familiar to IT | Wrong audience; weaker than SkySpark Debug/Log for this user | Reject as primary; Advanced link only |
| **C. Collapse to one container** | Trivial status | Loses independent restart / isolation compose already gives | Reject |
| **D. Full k8s operator UX** | Powerful | User becomes a platform engineer | Reject |
| **E. Five separate mini-apps copying SkySpark tiles literally** | Familiar to migrants | Over-structures a smaller product | Reject — one Ops with tabs |

**Ops agent privilege model** tradeoff:

| Option | Pros | Cons | Recommendation |
|---|---|---|---|
| Host helper + named pipe / local socket | No Docker socket in containers | Needs install-script support on Windows/Mac/Linux | **Prefer** |
| Sidecar with mounted docker.sock, allowlisted commands | Simple on Linux servers | Socket mount is a footgun if allowlist slips | Acceptable for server installs if tightly locked |
| "Copy this command" only | Safest | Fails the "no terminal" requirement | Fallback for locked-down IT environments |

---

## 4. Scripting — keep the power, stop the rot

### 4.1 What SkySpark got right

- One language (Axon) against the live database.
- Save a function; call it from UI, jobs, reports, other functions.
- Immediate REPL feedback (Haxall makes this the landing page).

### 4.2 What rots

- Unoptimized scripts accumulate on a shared server.
- No unit tests, no CI, no package ecosystem, no Stack Overflow.
- Hard problems (e.g. cycling harmonics) aren't in the stdlib, so every
  site reinvents a worse version.
- Permissions are coarse; a bad `hisRead` can load the box.

### 4.3 What Baler already has (steal this, don't restart)

Derivations are already closer to the right model than Axon-on-a-server:

| Property | Derivations today | Axon saved funcs |
|---|---|---|
| Language | Sandboxed Python `def run(inputs, row)` | Axon |
| Required tests to save | **Yes** (`test_cases` non-empty + passing) | No |
| Dry-run against live data | **Yes** | Informal |
| Failure isolation | Per-target auto-disable after 5 failures | Usually whole-server pain |
| Lineage | `prov:wasDerivedFrom` into the graph | Varies |
| Discovery | JSON file + UI list | Folio funcs, uneven |

Fault rules are the declarative twin: range / stuck / stale with scope,
not free code.

**Gap:** derivations are *streaming writers of points*. They are not yet
a general "saved query / report / one-shot analysis / callable tool"
system. Engineers also need ad-hoc analysis that shouldn't write a point
every 5 seconds.

### 4.4 Proposal: three tiers, one ecosystem

Name the product surface **Analyses** (umbrella). Three tiers:

#### Tier 1 — Catalog (default path)

Versioned, reviewed, installable analysis packs:

- Examples: AHU cycling / short-cycle detector, simultaneous heat-cool,
  sensor stuck, meter residual, G36-ish rule packs where we can stand
  behind them.
- Shipped as data + sandboxed functions + tests + docs, not as "paste
  this Axon."
- Install from Ops / Analyses UI: pin version, enable per site.
- Update path: bump pack version; diffs visible; never silent overwrite
  of site-local edits.

This is how harmonics and other "isn't in Axon" gaps get filled —
**libraries with owners**, not folklore scripts.

#### Tier 2 — Site derivations & fault rules (already mostly built)

- Keep Python sandbox + mandatory tests + dry-run.
- Add: pack-import ("start from catalog template"), ownership
  (`created_by`, `updated_at`), and a **perf budget** (max runtime, max
  rows scanned) enforced by the engine.
- Fault rules stay declarative; escape hatch to a derivation that
  *writes a boolean/score point* the rule then watches.

#### Tier 3 — Notebooks / workbench (power users, ephemeral by default)

- REPL-like workbench for exploration (the Haxall joy): query his,
  plot, try a transform.
- **Default save = draft.** Drafts do not run on the server unattended.
- Promoting a draft to a live derivation/fault/report requires tests +
  dry-run + explicit "enable on site."
- Optional: export draft as a pack contribution PR template for people
  who can participate upstream.

### 4.5 Explicitly reject

| Idea | Why not |
|---|---|
| Embed Axon/Haxall as the scripting layer | Brings the rot model back; dual ontology tax; license/ops complexity |
| Unrestricted server-side Python | Security + one bad loop kills the box |
| "Just use Grafana transforms" | Fine for charts; terrible for reusable building logic with lineage |
| Site-local script folder mounted into the engine | Becomes SkySpark funcs with extra steps |

### 4.6 Callable from anywhere (Axon's real superpower)

SkySpark funcs can be invoked from UI, jobs, and HTTP. Match that with
boring, standard mechanisms:

| Call site | Mechanism |
|---|---|
| UI | Analyses page; point context menu "run analysis" |
| Schedule | Engine sweep / cron-like `schedule` field on a derivation or report |
| HTTP | `POST /derivation/derivations/{id}/run` and `POST /analyses/{id}/run` with JWT/API key (already the platform style) |
| Webhook / alarm | Fault open → optional analysis attachment |
| External tools | Same HTTP; OpenAPI-documented |

No proprietary bus. If it can't be curled, it doesn't count as "callable
from anywhere."

### 4.7 Governance that prevents rot

Minimum server-side rules (enforced, not documented wishfully):

1. **No unattended code without tests.** Already true for derivations;
   extend to any promoted analysis.
2. **Budgets.** Wall-clock and row-count limits per run; violations
   disable the target and surface on Ops.
3. **Ownership + staleness.** Analyses with no successful run in N days
   go Quiet on Ops; owners get a prompt to archive or fix.
4. **Pack vs site-local.** Site-local forks are visible ("modified from
   catalog v1.2"); encourages contributing back instead of forever-forks.
5. **Read-only workbench by default.** Writes only through the promotion
   path.

### 4.8 Tradeoffs — scripting

| Option | Pros | Cons | Recommendation |
|---|---|---|---|
| **Extend derivations + add catalog + workbench** | Builds on tested sandbox; Python has real libraries (numpy/scipy *inside a tighter sandbox or as pre-approved builtins*); matches stack | SPARQL+Python still a learning curve; must invest in catalog content | **Do this** |
| Reimplement Axon | Familiar to SkySpark migrants | Rebuilds the rot; tiny ecosystem; Fantom/Axon hiring pool | Reject |
| Celery/Airflow jobs in arbitrary containers | Infinite power | Infinite ops burden; wrong audience | Reject for site engineers; maybe later for OEM pipelines |
| Only declarative rules, no code | Safest | Caps out below what commissioning needs | Reject as sole answer; keep for Tier-2 faults |

**Python vs a custom DSL:** keep sandboxed Python for Tier 2–3. Building
engineers who can read Excel formulas can read `return sum(vals)/len(vals)`.
A custom DSL only pays off if we refuse libraries; we should not refuse
libraries — we should **vendor them as approved builtins** (stats,
FFT/harmonics helpers) behind the sandbox allowlist.

---

## 5. Recommended delivery sequence

Design-only here; suggested build order when implementation starts:

### Slice A — Make the empty install feel finished
1. Install script generates secrets + `compose up -d --wait` + opens Ops.
2. First-run org create embedded in UI (not curl).
3. Ops Overview with platform chips driven by Compose health + simple
   probes (even before full `/ops/status`).
4. Empty-state CTAs modeled on SkySpark Host → Projects: **Connect a
   source** and **Load demo** (Demogen equivalent).

### Slice B — Truthful status
1. Heartbeats for the three pure daemons.
2. Ingest freshness + puller misconfig as first-class states.
3. Gateway 502/stale detection with one-click restart suggestion.
4. Wire derivation_target_health + webhook health into Ops Jobs.

### Slice C — Safe buttons
1. Ops agent allowlist: restart / start / stop / enable-profile.
2. Audit log.
3. Kill silent-fail pullers (refuse to start, or mark Down, when required
   env blank).

### Slice D — Analyses platform
1. Catalog pack format + 2–3 high-value packs (incl. one cycling/
   harmonics analysis that Axon folklore usually gets wrong).
2. Workbench drafts that cannot run unattended.
3. Perf budgets + ownership fields on derivations.
4. `POST …/run` for on-demand callable analyses.

### Non-goals for v1
- Multi-host clustering UX
- Editing `docker-compose.yml` graphically
- Replacing Grafana/Jaeger (link from Advanced only)
- Full SkySpark connector parity

---

## 6. Success criteria

A building engineer, after the install script:

1. Sees a green Ops page without opening a terminal.
2. Completes one first useful action (demo data or live source) from that
   page.
3. When `fault_detector` is killed, sees **Jobs → Fault detector: Down**
   within a minute and can restart it from the UI.
4. Can install a catalog cycling analysis, run tests, dry-run, enable —
   without SSH or Axon.
5. Cannot save unattended code with zero tests.

If those five hold, Baler keeps Docker's install advantage *and*
matches the part of SkySpark that actually mattered — a single place to
run the building — without inheriting the scripting rot.

---

## Appendix A — Live evidence (2026-09-09)

| Artifact | What it shows |
|---|---|
| SkySpark boot log | `No license installed` / `Fatal licensing err; shutting down exts` then `http started on port 8080` |
| `/opt/cursor/artifacts/skyspark-home-with-license-warning.png` | Host home tiles + license chip |
| `/opt/cursor/artifacts/skyspark-host-app-projects.png` | Host → Projects (New / Demogen; empty table) |
| `/opt/cursor/artifacts/skyspark-host-app-new-project.png` | New Project dialog |
| `/opt/cursor/artifacts/skyspark-host-app-install.png` | Host → Install (pods/extensions) |
| `/opt/cursor/artifacts/skyspark-host-app-licenses.png` | Host → Licenses |
| `/opt/cursor/artifacts/skyspark-host-app-sessions.png` | Host → Sessions |
| `/opt/cursor/artifacts/skyspark-settings-sysmods-detail.png` | Settings → SysMods Enable/Disable + detail |
| `/opt/cursor/artifacts/skyspark-settings-http.png` | Settings → HTTP (`httpPort`) |
| `/opt/cursor/artifacts/skyspark-debug-diagnostics.png` | Debug → Diagnostics (CPU/mem/uptime) |
| `/opt/cursor/artifacts/skyspark-debug-log.png` | Debug → Log (in-browser) |
| `/opt/cursor/artifacts/skyspark-debug-support.png` | Debug → Support bundle |
| `/opt/cursor/artifacts/skyspark-user-users.png` | User → Users |
| `/opt/cursor/artifacts/haxall-shell.png` | Haxall REPL-first landing |
| `/opt/cursor/artifacts/haxall-libs-listing.png` | `libs()` introspection |
| Full writeup | `/opt/cursor/artifacts/skyspark-instance-management-report.md` |

Image used: `phillipbirch/skyspark-latest` (unofficial; UI reports 3.1.8).
Haxall: `ghcr.io/haxall/haxall`. Official SkySpark Docker does not exist;
licensed analytics beyond the license wall were not exercised — **host
admin apps above were**.

## Appendix B — Engineer language ↔ compose services

| Ops label | Compose service(s) |
|---|---|
| Historian | `postgres` |
| Model store | `oxigraph` |
| Live bus | `mosquitto` |
| Gateway | `gateway` |
| Sign-in | `auth_api` |
| Data API | `ingest_api` |
| Alarm config | `fault_api` |
| Alarm watcher | `fault_detector` |
| Analysis config | `derivation_api` |
| Analysis engine | `derivation_engine` |
| Model checker | `validate_api` |
| Live ingest | `mqtt_listener` |
| Haystack pull | `haystack_puller` |
| SQL pull | `sql_puller` |
| Energy Star pull | `energystar_puller` |
| Weather pull | `openmeteo_puller` |
| Charts (tool) | `grafana` |
| Traces (tool) | `jaeger` |

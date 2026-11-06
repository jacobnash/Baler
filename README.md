# Baler

> Formerly **Timberdoodle** (and its BACnet/Modbus bridge, formerly **FBF**, is now
> [Windrow](https://github.com/jacobnash/windrow)). Existing installs keep their data and
> `.env` - see [deploy/MIGRATING-TO-BALER.md](deploy/MIGRATING-TO-BALER.md).

Ontology-agnostic building data platform: API-first, load-time-pluggable
(Brick/Haystack/DBO), headless by design. Time-series lives in
Postgres/TimescaleDB (so Grafana, or anything else that speaks SQL, reads
it natively — no plugin required); entity/relationship data lives in
Oxigraph as RDF, queryable over SPARQL. Every API is documented with
OpenAPI and served live off the process that implements it.

Feeds from [Windrow](../windrow) (BACnet/Modbus → MQTT) over the same wire
envelope `mqtt_listener.py` and `POST /ingest` both consume.

## Quick start

```bash
cp .env.example .env                                                     # first time only, see below
echo "BALER_JWT_SECRET=$(openssl rand -hex 32)" >> .env           # first time only
echo "BALER_GATEWAY_SECRET=$(openssl rand -hex 32)" >> .env       # first time only
echo "MQTT_PASSWORD=$(openssl rand -hex 16)" >> .env                     # first time only
docker compose up -d --wait   # waits for the 7 HTTP APIs' healthchecks, not just "container started"
```

`docker compose up -d` refuses to start the gateway without both JWT/
gateway secrets set, and refuses to start mosquitto or any MQTT-connected
daemon without `MQTT_PASSWORD` — every route goes through real JWT
verification in nginx itself now (see "Gateway auth" below), and every
MQTT client authenticates now too (see `mosquitto/acl`), so there's no
working default the way there is for everything else in `.env.example`, which
lists every other environment variable any part of this repo reads,
each annotated with its default and which process needs it. Postgres's
password specifically comes from `POSTGRES_PASSWORD` in `.env` (defaults
to `baler` if unset — fine for local dev, change it for anything
else).

That's everything — 5 infra containers (mosquitto, oxigraph, postgres,
jaeger, grafana), all 8 processes below, and an nginx gateway in front of
the 7 HTTP APIs, all with one shared lifecycle now. Each process is still
its own container/image build target if you need to restart or update one
independently (`docker compose up -d --build ingest_api`), but nothing
requires manual per-process startup anymore. The gateway won't start
until all 7 HTTP APIs report healthy (`GET /openapi.yaml` on each), so
`docker compose up -d --wait` (or plain `up -d` followed by `docker
compose ps`) leaves you with a stack that's actually ready for the curl
examples below, not just "containers exist."

**If any `docker compose up -d` recreates a backend container while
`gateway` itself keeps running from before** (a rebuild, a `git pull`, a
targeted `--build ingest_api`, or just re-running `up -d` on a stack
that already had some containers up) — run `docker compose restart
gateway` afterward. The gateway resolves and caches each backend's
address at its own nginx startup, not per-request, so it can end up
routing to a since-recreated container's stale address and return
404/502 on every route even though every backend reports healthy. This
isn't limited to the "restart one service on purpose" case — it's
happened from a plain repeat `docker compose up -d` in this repo's own
CI/dev loop. `docker compose restart gateway` always fixes it.

| Process | What it does | Port (direct) |
|---|---|---|
| `mqtt_listener` | Subscribes to `fbf/#`, writes every reading to Oxigraph + Postgres. | — |
| `ingest_api` | `POST /ingest` — same write path as the listener, for push-style sources. | 8000 |
| `fault_api` | CRUD for fault-detection rules + webhook subscriptions, `GET /faults`. | 8002 |
| `fault_detector` | Evaluates rules against live readings (Cur) and history (His), fires webhooks on fault open/resolve. | — |
| `derivation_api` | CRUD for derivations (computed/synthetic histories) + per-target health, `POST /derivations/test`, `POST /derivations/dry-run`. | 8003 |
| `derivation_engine` | Evaluates derivations on a sweep and, for cur-mode ones, on matching MQTT messages; writes results back into Oxigraph + Postgres. | — |
| `validate_api` | `POST /validate` — checks the live entity graph against Brick's own SHACL shapes (`pip install -e ".[validate]"`). | 8005 |
| `auth_api` | Identity: `POST /login`, API-key issuance/revocation, org/site directory CRUD. See "Gateway auth" below. | 8006 |
| `haystack_api` | The Project Haystack HTTP API at `/api/{project}/{op}`, so SkySpark, Haxall, nhaystack and pyhaystack clients can point here unchanged. It handles HELLO/SCRAM login with your normal email and password. See [`docs-site/pages/haystack-api.mdx`](docs-site/pages/haystack-api.mdx). | 8008 |
| `commissioning_api` | Commissioning (Cx) projects with editable phases, a scoped Brick/Haystack inventory, SHACL expected-point templates (`ontology/cx-expected-points.ttl`) and verification runs. Plan: [`todo/commissioning-plan.md`](todo/commissioning-plan.md). | 8009 |

None of the 8 HTTP APIs are exposed on the host directly — go through
the gateway at `localhost:8080` instead, prefixed by API name
(`/ingest/*`, `/fault/*`, `/derivation/*`, `/validate/*`, `/auth/*`,
`/ops/*`, `/cx/*`, and `/api/*` for Haystack). The
demo browser UI (`ui/`) is served straight off the gateway at
[`localhost:8080/ui/`](http://localhost:8080/ui/) — no separate
`ui_server.py` needed for this path.

### Gateway auth

Every route goes through real JWT verification in nginx itself
(`gateway/njs/`, no per-API Python auth code) — role/route policy is
enforced by one central table, `gateway/njs/policy.js`. `POST
/auth/orgs` creates a new org plus its first admin user (public, no
credential needed — there's no other admin who could authorize a
brand-new org); `POST /auth/login` exchanges email/password for a JWT;
every other route on every API needs `Authorization: Bearer <token>`.

```bash
curl -X POST localhost:8080/auth/orgs -H 'Content-Type: application/json' \
  -d '{"name": "Acme", "admin_email": "admin@acme.example", "admin_password": "change-me"}'
curl -X POST localhost:8080/auth/login -H 'Content-Type: application/json' \
  -d '{"email": "admin@acme.example", "password": "change-me"}'
# -> {"token": "...", "user": {...}} - use the token as a Bearer credential from here on
curl localhost:8080/auth/me -H "Authorization: Bearer <token>"

curl localhost:8080/ingest/openapi.yaml   # public, no token needed
curl -X POST localhost:8080/ingest/ingest -H "Authorization: Bearer <token>" \
  -d '{"point": "fbf/mock-ahu-1/analogValue,1", "value": 71.0}'
```

The first `curl` should return the OpenAPI YAML with no auth at all; the
last returns `204 No Content` on success (add `-i` to `curl` to see the
status code) — a `401` means the token is missing/invalid/expired, a
`403` means the token's role doesn't satisfy that route (see
`gateway/njs/policy.js`), a `400` means the JSON body itself was
malformed or missing a required field.

Each API still serves its own OpenAPI spec at `GET /openapi.yaml` and a
Swagger UI at `GET /docs` (through the gateway). For the full
cross-referenced documentation site (all 5 API catalog entries plus
architecture and feature docs), see [`docs-site/`](docs-site) —
`cd docs-site && npm install && npm run dev`.

Everything from here on runs bare-metal (not inside a container), against
the docker-compose services above. It assumes `python`/`pip` resolve to
the environment you ran `pip install -e .` in (a venv is the normal way
to get that — `python3 -m venv .venv && source .venv/bin/activate &&
pip install -e .` if you haven't already); some commands below need an
optional extra on top of that base install (`.[dev]`, `.[llm]`,
`.[validate]`), called out where they're used.

### Mock hospital demo: auto-tagging, derivations/faults, UI

This demo needs live BACnet-over-MQTT readings, which come from
[Windrow](../windrow) — a separate sibling repo (`../windrow`, not part of this
checkout). Clone/set it up first, then generate and provision its mock
hospital site (~500 points, ~90 pieces of equipment):

```bash
# in ../windrow, after `pip install -e ".[dev]"` there:
python -m windrow.mock_hospital generate --out hospital_site_spec.json   # one-shot: writes the site spec

# each in its own long-running terminal:
python -m windrow.mock_device --address <your-iface>/24:47808 --instance 3456 --site-spec hospital_site_spec.json   # simulated BACnet device
MQTT_USERNAME=baler MQTT_PASSWORD=<your .env's MQTT_PASSWORD> \
  python -m windrow.api --address <your-iface>/24:47820 --mqtt-host localhost   # discovery/connections API, publishes to MQTT

# one-shot, once both of the above are up:
python -m windrow.mock_hospital provision --device-address <your-iface>:47808 --device-instance 3456   # provisions one connection per equipment
```

`<your-iface>` is your host's own network interface address (bacpypes3's
local-BACnet-address arg — see Windrow's `docs/bacnet-discovery-api.md`).
Once provisioned, Baler's `mqtt_listener.py` picks up the
readings/tags unchanged — no Windrow-specific code on this side. See
`../windrow/README.md` for anything not covered here (it's a separate,
independently-versioned project).

With `mqtt_listener.py` ingesting:

```bash
pip install -e ".[llm]"                    # anthropic + pydantic, only needed for autotag
ANTHROPIC_API_KEY=... python -m baler.autotag   # rule engine + LLM fallback for what it can't place
BALER_ADMIN_EMAIL=<admin-email> BALER_ADMIN_PASSWORD=<admin-password> \
  python scripts/seed_derivations_and_faults.py         # Brick derivations + fault rules, writes through the gateway
python -m baler.ui_server                         # browse equipment/points/charts at localhost:8004 (no-store, so edits show up on refresh)
```

`seed_derivations_and_faults.py` logs in through the gateway itself
(`POST /auth/login`) to get a Bearer token, so
`BALER_ADMIN_EMAIL`/`BALER_ADMIN_PASSWORD` must match an
existing admin account — create one first with `POST /auth/orgs` (see
"Gateway auth" above) if you don't have one yet.

### Query: `readAll(ahu and air).hisRead(thisMonth)`

Type one line, see every point on a piece of equipment charted — that's
`GET /ingest/his` plus `ui/his.html`
(localhost:8080/ui/his.html through the gateway, `Query` in the top nav).
The expression is Axon-shaped: a tag filter (`and`/`or`/`not`,
`tag == value`) whose words work the way people talk about Brick data
rather than the way Brick spells it - `zone and air and temperature`
matches by the words a class name is made of, `temperature_sensor` /
`AHU` / `Air_Handling_Unit` match a class in any case with all its
subclasses and aliases (including classes your own Brick extension adds
beneath them, which also resolve by their own names, aliases and tag
words), and Haystack markers (`ahu`, `temp`) still work
on tagged data - then `.hisRead(span)`
with Axon's span words (`today`, `yesterday`, `thisWeek`, `lastMonth`,
`2026-09`, `2026-09-01..2026-09-07`, ...) and an optional
`.hisRollup(avg, 1hr)` that aggregates server-side in SQL. Matched
equipment expands to all of its points, so `read(ahu and dis == "AHU-1")`
is enough - no `equipRef` hunting. Charts are grouped by unit, with a
per-point view, a grid, and CSV download; the URL carries the query, so a
result is a link.

```bash
curl -s -H "Authorization: Bearer $TOKEN" --get localhost:8080/ingest/his \
  --data-urlencode 'expr=readAll(ahu and air).hisRead(pastWeek).hisRollup(avg, 1hr)' \
  --data-urlencode 'tz=America/Chicago'
```

Set `BALER_TZ` in `.env` so `today` means the building's today for
clients that don't pass `tz`. Full grammar, span table, and what is
deliberately *not* supported (tag paths, arithmetic, other Axon
functions):
[`docs-site/pages/his-query.mdx`](docs-site/pages/his-query.mdx).

`ui/devices.html` is a dashboard for Windrow's periodic BACnet/Modbus device
discovery: review discovered devices, set per-device credentials, and
provision connections (see
[`docs-site/pages/windrow-periodic-discovery.mdx`](docs-site/pages/windrow-periodic-discovery.mdx)).
It calls `windrow.api` through the gateway's `/windrow/` route: login
required, every change admin-only, and site-restricted logins refused
(Windrow has no per-site filter yet). A deploy without a `windrow_api`
service answers `/windrow/` with 502. Pass `?windrow=<url>` to point the
page at another instance when serving `ui/` standalone.

`autotag.py` sweeps every point/equip with tags but no direct Brick match,
runs `mapping.classify_point_with_fallback` on each (rule engine first,
Claude Haiku fallback second), and appends high-confidence guesses that
reuse an existing Brick class to `rules/haystack_to_brick.yaml`/
`rules/haystack_equip_to_brick.yaml` as commented-out proposals for a
human to review and promote - it never edits the live rule set itself.

## Infrastructure ports

| Service | Port | Purpose |
|---|---|---|
| mosquitto | 1883 | MQTT broker |
| oxigraph | 7878 | RDF/SPARQL graph store |
| postgres | 5433 (→5432 in-container) | Timescale hypertable, `point_history` |
| jaeger | 16686 (UI), 4318 (OTLP HTTP) | Traces — every process exports here |
| grafana | 3033 (→3000 in-container) | Dashboards over Postgres — a "Baler Postgres" datasource and a starter "Baler: Point History" dashboard are auto-provisioned (`grafana/provisioning/`). Login is the image default `admin`/`admin` unless `GRAFANA_ANON_ENABLED=true` in `.env` (off by default — see `.env.example`; changing it needs `docker compose up -d grafana` to recreate the container, since compose only reads `.env` at container start, not while it's already running) |
| gateway | 8080 | JWT-auth-gated (nginx + njs) reverse proxy in front of every API — see "Gateway auth" above |

Postgres is remapped to host port 5433, not the default 5432, to avoid
colliding with a locally-running Postgres instance. `ingest_api`/`fault_api`/
`derivation_api`/`validate_api` don't publish host ports at all anymore —
reach them through the gateway on 8080.

## Haystack pull (optional)

A 13th, opt-in container pulls points/equipment/history from a remote
Project Haystack server (Haxall, SkySpark, ...) — not started by plain
`docker compose up -d`:

```bash
docker compose --profile haystack-pull up -d haystack_puller
```

Set `REMOTE_HAYSTACK_URL` and `REMOTE_HAYSTACK_USER` in `.env` first —
**the container starts fine without them** (docker compose always passes
these through, blank if unset, so nothing forces you to set them), it
just fails every pull cycle against an empty URL and retries silently
forever instead of erroring. See
[`docs-site/pages/haystack-puller.mdx`](docs-site/pages/haystack-puller.mdx)
for the full picture.

## Real building data (optional)

`scripts/load_bdg2_sample.py` loads a bounded slice of real hourly
electricity meter readings from the [Building Data Genome Project 2](https://github.com/buds-lab/building-data-genome-project-2)
(real energy meters from 1,636 real non-residential buildings, no mock
data) straight into a running stack — needs `postgres`/`oxigraph` up
(`docker compose up -d postgres oxigraph` is enough, the full stack isn't
required):

```bash
python scripts/load_bdg2_sample.py --site Panther --limit 5
```

Real Brick classification included — each building lands as an
`Electrical_Meter` equip with an `Electric_Energy_Sensor` point,
`brick:hasPoint`-linked, same as any other source. `--byte-limit`
(default 5MB) bounds how much of the ~166MB source file gets fetched;
that's roughly two weeks of hourly data. One-shot, not a daemon — see
`--site`/`--limit` in the script for other real sites/buildings (there
are 19 real sites; check `metadata.csv`'s `site_id` column for names).

## Backup and restore

`scripts/backup.py` dumps the two volumes that hold real state —
`postgres-data` (via `docker compose exec postgres pg_dump -U baler
baler` — that Postgres user/database name is fixed, not
configurable, same as `POSTGRES_USER`/`POSTGRES_DB` in docker-compose.yml)
and `oxigraph-data` (via the same bulk graph-store GET the SHACL
validator uses) — into timestamped, gzip'd files under `backups/`
(gitignored). `grafana-data` is regenerable dashboard config, out of
scope.

```bash
docker compose up -d postgres oxigraph   # only these two are needed
python scripts/backup.py                 # writes backups/postgres-<ts>.sql.gz, backups/oxigraph-<ts>.ttl.gz
python scripts/restore.py --postgres-file backups/postgres-<ts>.sql.gz --oxigraph-file backups/oxigraph-<ts>.ttl.gz
```

`restore.py` assumes a fresh/empty postgres+oxigraph (disaster recovery,
not a merge onto live data) — restoring onto tables that already exist
exits non-zero (`psql -v ON_ERROR_STOP=1`) rather than silently
succeeding. Both scripts default to this checkout's own docker-compose
project and to `$OXIGRAPH_URL`/`http://localhost:7878`; `--help` on
either documents `--compose-project-dir`/`--oxigraph-url`/`--out-dir` for
pointing at a different target (e.g. a throwaway container while testing
restore, rather than the live stack).

### Automating backups

Nothing runs `backup.py` on its own — pick one:

```bash
# systemd (recommended on a Linux host)
sudo cp scripts/systemd/baler-backup.{service,timer} /etc/systemd/system/
sudo $EDITOR /etc/systemd/system/baler-backup.service   # set WorkingDirectory=
sudo systemctl enable --now baler-backup.timer
```

```bash
# cron (non-systemd hosts)
0 3 * * * cd /path/to/baler && python3 scripts/backup.py >> backups/backup.log 2>&1
```

Both just call the same `backup.py` shown above on a schedule (default:
daily) — nothing about the script changes. Backups accumulate in
`backups/` indefinitely; there's no pruning/retention yet, so watch disk
usage or clean the directory out yourself if that becomes a problem. See
[`docs-site/pages/backups.mdx`](docs-site/pages/backups.mdx) for the full
picture.

## Tests

```bash
pip install -e ".[dev]"                                # first time only: pytest + the OpenAPI-spec-validation deps
set -a && source .env && set +a                        # mosquitto requires auth now - tests need the same MQTT_USERNAME/MQTT_PASSWORD as the containers
python -m pytest -m "integration or not integration"  # everything; requires `docker compose up -d` first
python -m pytest -m "not integration"                  # unit tests only, no services needed
```

The first command's marker expression is a tautology (every test either
has the `integration` marker or doesn't), so it runs the full suite,
including tests that hit live Postgres/Oxigraph/MQTT — bring the stack up
first. Use the second command on a fresh checkout with nothing running.
See `pyproject.toml` for the marker definition.

## License

AGPLv3 — see [LICENSE](LICENSE). You can self-host and charge for it;
if you modify and run it as a network service for others, you must make
your modified source available to them too.

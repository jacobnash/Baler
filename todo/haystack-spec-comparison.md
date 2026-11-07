# Baler vs Project Haystack: spec comparison run (nash-srv)

**Goal:** measure, on the real deployment, how close Baler's Haystack API is to the Project
Haystack spec (`docHaystack/Ops`, `Filters`, `Auth`, `Defs`). Run every check against
**Haxall 4.0.6**, the reference implementation, too. The output is a gap report: per item,
pass, partial or fail, with evidence. The fixes come afterwards, in the repo, and
nothing is changed on the box beyond what's listed here.

Who runs it: the Claude session on nash-srv. Where: nash-srv, a dev box. Writes go only to
throwaway test orgs and points, which get cleaned up.

## Ground rules

- **Don't touch:** the `baler` compose project's containers and volumes, the runner
  (`actions-runner-baler`, `_work/Baler/Baler`), `/usr/local/sbin/baler-deploy.sh`, or the
  other tenants on the host (featherai, flying-franc, inboxrulz, nash-srv, feather-mongo, ...).
- **Work in your own clone:** `git clone https://github.com/jacobnash/Baler` into your
  scratchpad, never the runner's checkout.
- **Postgres:** never let a script fall back to `localhost:5432`, because another tenant's Postgres
  may be listening there. Always set `BALER_PG_DSN` explicitly to the **baler** Postgres container
  (`docker inspect` its IP on the baler network, with the user and db `timberdoodle` from
  `/home/github-runner/.config/baler/nash-srv.env`). Never print the password.
- Don't print secrets. Test credentials you create yourself are fine.

## Phase 0: setup (read-only checks first)

1. `curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8080/auth/login -d '{}'`
   should return a 4xx, which means the gateway is up.
2. Is MQTT published on the host? Run `ss -ltnp | grep -E ':1883|:8080|:8280'`. The matrix's
   pointWrite cell plays a Windrow site over MQTT on port 1883. If 1883 isn't published, use the
   mosquitto container's IP and set `MQTT_HOST` to it. The MQTT credentials are in the baler env
   file.
3. Haxall reference, on a free port (8280 if it's free):
   `docker build -t haxall:spec -f windrow/docker/haxall.Dockerfile windrow/docker` from your
   clone, then
   `docker run -d --name haxall-spec -p 127.0.0.1:8280:8080 haxall:spec sh -c "bin/hx init -headless -suUser su -suPass <random> -httpPort 8080 /haxall/var/spec && bin/hx run /haxall/var/spec"`.
   Enable the `ph`, `hx.point` and `hx.conn` libs, as the CI job does (see `.github/workflows/ci.yml`
   haxall-matrix). The API is at `http://127.0.0.1:8280/api/sys`.
4. Python env with openhay. Prefer the already-built Baler image, which has openhay compiled in.
   Run the tests from it with `--network host`, mounting your clone, then
   `pip install -e ".[dev]"` inside it. If that's awkward, install rustup in your user account and
   run `pip install -e ".[dev,haystack]"` in a venv.

## Phase 1: the existing conformance matrix, live

Run `tests/test_haystack_matrix.py -m integration -s` with:
- `MATRIX_TARGETS=baler,haxall`
- `HAXALL_API=http://127.0.0.1:8280/api/sys`
- `HAXALL_SU_USERNAME` and `HAXALL_SU_PASSWORD`
- `BALER_PG_DSN`, `MQTT_HOST`, `MQTT_USERNAME`, `MQTT_PASSWORD`
- `MATRIX_OUT=<scratchpad>/matrix.md`

It creates and removes "Matrix Test Org". Note that the ingested `fbf/matrix-*` test points
stay in the graph/history.

**Expected:** the same table as CI. Any difference between CI and live is finding #1.

## Phase 2: spec probe, item by item

Write one script, `spec_probe.py`, in your scratchpad. It uses `openhay.Client.call` (and raw
HTTP where noted) against both targets. On Baler, it uses a fresh throwaway org, "Spec Probe
Org", created via `POST /auth/orgs`, with test points under `fbf/specprobe-<run>/...`. **Don't
give it topic prefixes**, or scoping turns on for every other org. Delete the org at the end
the way the matrix does.

Per item, record: the spec requirement, the Baler result, the Haxall result, and the verdict
(PASS / PARTIAL / FAIL / N/A), with the exact request and a trimmed response as evidence.

**Ops**
1. `about`: every spec column is present: haystackVersion, tz, serverName, serverTime,
   serverBootTime, productName, productUri, productVersion, vendorName, vendorUri, moduleName,
   moduleVersion.
2. `ops`: rows are **op defs**, with `def: ^op:about` etc., not just name and summary.
3. `formats` and `filetypes`: compare the advertised send/receive lists with what actually works.
   Try `Accept:` for each advertised mime, including `text/csv`, `text/turtle` and
   `application/ld+json`.
4. `defs`, `libs`, `filetypes`: filter and limit, and `def==^ahu`. Diff the lib/def sets
   against Haxall.
5. `read`: by filter with a limit; by id with a missing id (a null row, in request order);
   a filter matching nothing (empty grid, no error).
6. `nav`: walk from the root to a leaf, checking `navId` semantics and an `id` on each record row.
7. `watchSub`: watchDis on a new watch, adding to an existing watch, lease honored. Then `watchPoll`
   (changes only, `refresh` returns everything), and `watchUnsub` (by id, and with the `close` marker).
   Also check what happens after the lease expires.
8. `pointWrite`:
   - a read returns **17 rows** (level, levelDis, val, who);
   - write levels 1, 8 and 16;
   - **level 8 without `duration` must be rejected**, and with `duration` must expire;
   - level 17 (the default);
   - `val` null autos the level;
   - `who` defaults to the user's display name.
9. `hisRead` range formats:
   - `today` and `yesterday`;
   - `{date}`;
   - `{date},{date}`;
   - **`{dateTime},{dateTime}`**;
   - **`{dateTime}` alone**;
   - check the `hisStart`/`hisEnd` meta and the start-inclusive, end-exclusive edges.
   - **Batch hisRead:** meta `range`, several `id` rows, response columns `v0..vN` with `id` in
     the column meta.
10. `hisWrite`: single, and **batch** (`ts`, `v0`, `v1` columns with `id` in column meta).
    Also: units, a Bool, and a NaN value.
11. `invokeAction`: what each target returns.
12. `close`: **the bearer token must be invalid afterwards**. Re-use it and expect a 401.

**Filters** (Baler `read`, same records on both targets)

13. has / missing / and / or / parens.
14. Number units: `x == 5°F` against a value of 5 in °C must be false, and `!=` must be true. A
    unitless literal compared with a unit value: record it.
15. Literal types: str, number, bool, **date, time, uri (`` `...` ``), ref, symbol (`^x`)**.
16. Paths: `equipRef->dis`; chained paths; **a path through a Ref list**; **a path into a nested
    Dict**.
17. Xeto spec terms (`Meter`, `ph::Meter`). Note: Baler reads capitalized names as **Brick
    classes**. Record how Haxall answers and where the meanings collide.

**Auth**

18. HELLO → SCRAM → BEARER; a bad password; an unknown user, which must not reveal whether the
    user exists; a SCRAM handshake replay.
19. `Authorization: Bearer x` versus `BEARER authToken=x`.

**Encoding edge cases**

20. Round-trip one record holding every value type through Zinc, JSON v3 and Hayson. Include
    Coord, XStr, Span if supported, a nested grid, and NA/Remove.

## Phase 3: report

Write `<scratchpad>/haystack-spec-report.md` with:
- the Phase 1 table;
- the Phase 2 rows, grouped as above;
- a short **gap list**, ordered by how much each gap blocks a Haystack client;
- the commands to re-run everything.

Then post the gap list in your session for Jacob. Don't commit to the repo. The cloud session
turns the report into fixes and CI checks.

## Phase 4: cleanup

Delete "Spec Probe Org" and "Matrix Test Org" (via the Postgres DSN above). Run
`docker rm -f haxall-spec`, and remove your clone, venv and image if you built one. Leave the
baler stack exactly as you found it.

## The reference source as the oracle (cloud session, in parallel)

The reference sources are open, so expected behavior comes from the reference code and tests,
not just the prose spec:
[haxall/haxall](https://github.com/haxall/haxall),
[Project-Haystack/xeto](https://github.com/Project-Haystack/xeto),
[Project-Haystack/haystack-defs](https://github.com/Project-Haystack/haystack-defs) and
[fantom-lang/fantom](https://github.com/fantom-lang/fantom).
All are AFL-3.0.

- **Test vectors:** haxall's `src/test/testHaystack` has FilterTest, ZincTest, NumberTest,
  TrioTest, DateSpanTest, SpanTest, RefTest and CoordTest. Port their *cases*, inputs and expected
  results as data, into openhay's test suite, and run every codec and the filter evaluator
  against them. Do the same for Baler's filter compiler where the semantics overlap.
- **Op behavior:** `src/ext/hxApi/fan/PhApiFuncs.fan` (nav, hisRead single and batch, hisWrite
  single and batch, watches), `src/ext/hxPoint/fan/WriteMgr.fan` (priority array, level 8
  duration) and `ApiDispatchV4Ops.fan` (ops, close). Each Phase 2 item gets a "reference says"
  line citing the file and function.
- **Haystack 5:** haxall has an `ApiDispatchV5` (Xeto protocol). Add it to the comparison scope,
  next to the Xeto converter work, which reads its libs (`ph`, `ph.points`, `ph.equips.x`, ...)
  from the xeto repo.

**License hygiene:** AFL-3.0 is permissive, but the FSF considers it GPL-incompatible.
- Never copy Fantom code into Baler (AGPL) or openhay (BSD).
- Port test *cases* as data or independently written assertions. Keep them in openhay under
  `vectors/haxall/`, with the AFL notice and the source path and commit of each.
- Reimplement behavior from the spec and from what the reference shows it does.

## Gaps once known from the code (checked in Baler#134)

Each is now covered by a test in `tests/test_haystack_api.py`:

- `ops` returns the Haystack 4 op defs for the ops served. Fixed.
- `close` revokes the session token: `/api` refuses it at once, and the gateway refuses it on
  every API once its denylist refreshes. Fixed.
- `hisRead` takes `{dateTime},{dateTime}` and a single `{dateTime}`. Fixed.
- Batch `hisRead` and `hisWrite` work. Fixed.
- `pointWrite` applies a level-8 `duration` as a timed override. Levels run 1-17, with 17 as
  relinquish-default. Fixed.
- Filters compare units, and support `->` paths, date/time/uri literals and `^symbol` terms.
  Fixed. A Symbol as a comparison value isn't Haystack grammar and stays a parse error.
- `invokeAction` is deliberately unsupported: it returns an error grid, isn't listed by `ops`, and
  is documented in docs-site `haystack-api.mdx`.
- No Turtle or JSON-LD export.
- No Xeto (Haystack 5).

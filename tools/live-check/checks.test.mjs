// Unit tests for the #178 checks' pure halves, on fixtures: budgets, data
// freshness, expected edges, the viewport tag and the deploy window.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { evaluate as dataflow, evaluateMetrics, groupOf, parseProm, staleAfter } from "./dataflow.mjs";
import { evaluate as deploy } from "./deploywin.mjs";
import { evaluate as edges, hostOf, nodeId } from "./edges.mjs";
import { fingerprint, merge } from "./findings.mjs";
import { evaluateViewports, viewportProblem } from "./layout.mjs";
import { evaluate as perf, median, stats } from "./perf.mjs";

const fx = (name) => JSON.parse(readFileSync(new URL(`./fixtures/${name}`, import.meta.url), "utf8"));
const budgets = JSON.parse(readFileSync(new URL("./budgets.json", import.meta.url), "utf8"));
const expected = JSON.parse(readFileSync(new URL("./expected-edges.json", import.meta.url), "utf8"));
const route = (name) => budgets.routes.find((r) => r.name === name);
const samples = (secs, bytes = 1000) => secs.map((s) => ({ ms: s * 1000, bytes, status: 200 }));

// --- perf ------------------------------------------------------------------

test("stats: median, max and largest body over the runs; failed runs are errors", () => {
  assert.equal(median([3, 1, 2]), 2);
  assert.equal(median([4, 1, 3, 2]), 2.5);
  const st = stats([...samples([0.2, 0.4, 0.3]), { ms: 50, bytes: 0, status: 502 }]);
  assert.deepEqual(st, { runs: 4, median_s: 0.3, max_s: 0.4, max_bytes: 1000, errors: [502] });
});

test("perf: under budget passes; over the median or the size budget is one finding per route and phase", () => {
  const faults = route("GET /fault/faults");
  assert.equal(perf(faults, "warm", stats(samples([0.08, 0.1, 0.11], 188000))).check.status, "pass");
  const slow = perf(faults, "warm", stats(samples([0.6, 0.7, 0.62], 188000)));
  assert.equal(slow.check.status, "fail");
  assert.equal(slow.raw.kind, "perf-budget");
  assert.equal(slow.raw.scope, "perf:warm");
  assert.match(slow.raw.detail, /median 0\.62 s > 0\.5 s/);
  const big = perf(faults, "warm", stats(samples([0.1, 0.1, 0.1], 600000)));
  assert.match(big.check.detail, /600 kB > 500 kB/);
  // the size budget is warm-only; cold judges time against its own number
  assert.equal(perf(faults, "cold", stats(samples([0.6, 0.7, 0.9], 600000))).check.status, "pass");
  // same route, other phase: a different fingerprint, so cold and warm are separate issues
  const cold = perf(faults, "cold", stats(samples([1.2, 1.3, 1.4])));
  assert.notEqual(fingerprint(cold.raw.kind, cold.raw.key), fingerprint(slow.raw.kind, slow.raw.key));
  // and the fingerprint doesn't move with the numbers
  const slower = perf(faults, "warm", stats(samples([2, 3, 4])));
  assert.equal(fingerprint(slower.raw.kind, slower.raw.key), fingerprint(slow.raw.kind, slow.raw.key));
});

test("perf: today's live numbers (2026-11-07 baseline) pass every budget but the Cx report (#150)", () => {
  const live = { "GET /api/read?filter=equip": [[0.11, 0.29], 0.71], "GET /api/read?filter=point": [[0.91, 0.95], 2.99],
                 "GET /fault/faults": [[0.08, 0.11], 0.37], "GET /ops/status": [[0.27, 0.38], 0.83],
                 "his readAll(equip)": [[1.64, 1.65], 4.92], "Cx project report ({project})": [[3.3, 3.5], 10.2] };
  for (const [name, [warm, cold]] of Object.entries(live)) {
    const r = route(name);
    const w = perf(r, "warm", stats(samples([...warm, warm[1]], 188000))).check.status;
    const c = perf(r, "cold", stats(samples([cold, cold, cold], 188000))).check.status;
    const cx = name.startsWith("Cx");
    assert.equal(w, cx ? "fail" : "pass", `${name} warm`);
    assert.equal(c, cx ? "fail" : "pass", `${name} cold`);
  }
  assert.deepEqual(route("Cx project report ({project})").refs, [150]);
});

test("perf: loosening a budget past the observed value turns the breach into a pass (its issue then closes)", () => {
  const st = stats(samples([3.3, 3.4, 3.5]));
  const r = route("Cx project report ({project})");
  assert.equal(perf(r, "warm", st).check.status, "fail");
  assert.equal(perf({ ...r, warm: { median_s: 4 } }, "warm", st).check.status, "pass");
});

test("perf: a route that errors is an error check (incomplete), not a finding", () => {
  const out = perf(route("GET /ops/status"), "warm", stats([{ ms: 10, bytes: 0, status: 502 }]));
  assert.equal(out.check.status, "error");
  assert.equal(out.raw, undefined);
});

// --- dataflow ----------------------------------------------------------------

const cfg = budgets.dataflow;

test("freshness: groups points by the segment under the prefix", () => {
  assert.equal(groupOf("urn:point:fbf/home/modbus/reg0", "fbf/home"), "fbf/home/modbus");
  assert.equal(groupOf("urn:point:fbf/home/modbus/a/b", "fbf/home"), "fbf/home/modbus");
  assert.equal(groupOf("urn:point:fbf/home/weather", "fbf/home"), "fbf/home");
});

test("freshness: stale after stale_factor x poll, never less than min_stale_s, default poll when unknown", () => {
  assert.equal(staleAfter({ poll_s: 5 }, cfg), 30);       // 3 x 5 = 15 < 30
  assert.equal(staleAfter({ poll_s: 60 }, cfg), 180);
  assert.equal(staleAfter({ poll_s: null }, cfg), 180);   // default_poll_s 60
});

test("freshness: a healthy install passes, rate and every group", () => {
  const out = dataflow(cfg, { "fbf/home": fx("freshness-home.json") });
  assert.deepEqual(out.raw, []);
  assert.deepEqual(out.checks.map((c) => `${c.name}:${c.status}`),
    ["samples in the last 60 s:pass", "fbf/home/* fresh:pass", "fbf/home/bacnet/* fresh:pass", "fbf/home/modbus/* fresh:pass"]);
});

test("freshness: windrow-modbus stopped -> fbf/home/modbus/* stale (one finding), rate under the floor", () => {
  const out = dataflow(cfg, { "fbf/home": fx("freshness-modbus-stopped.json") });
  const stale = out.raw.filter((r) => r.kind === "stale");
  assert.equal(stale.length, 1);
  assert.equal(stale[0].key, "fbf/home/modbus");
  assert.match(stale[0].title, /fbf\/home\/modbus\/\* points are stale \(2 of 2\)/);
  assert.match(stale[0].detail, /fbf\/home\/modbus\/reg0: last 2026-11-07T22:41:00\+00:00, 540 s ago \(limit 30 s/);
  assert.ok(out.raw.some((r) => r.kind === "ingest-rate"));
  assert.equal(out.checks.find((c) => c.name === "fbf/home/bacnet/* fresh").status, "pass");
  // starting it again clears it: the healthy fixture has no stale finding with that fingerprint
  const again = merge(dataflow(cfg, { "fbf/home": fx("freshness-home.json") }).raw).map((f) => f.fp);
  assert.ok(!again.includes(merge(stale)[0].fp));
});

test("freshness: an expected group that sent nothing in a week, and an empty prefix, are findings", () => {
  const out = dataflow({ ...cfg, prefixes: [{ prefix: "fbf/home", expect_groups: ["fbf/home/solar"] }] }, { "fbf/home": fx("freshness-home.json") });
  assert.ok(out.raw.some((r) => r.key === "fbf/home/solar" && /stopped reporting/.test(r.title)));
  const empty = dataflow(cfg, { "fbf/home": { ...fx("freshness-home.json"), points: [] } });
  assert.ok(empty.raw.some((r) => r.key === "fbf/home" && /no recent data/.test(r.title)));
});

// --- edges -------------------------------------------------------------------

const NOW = Date.parse("2026-11-07T22:40:10Z");

test("edges: ids and BACnet hosts", () => {
  assert.equal(nodeId("local"), "local/local");
  assert.equal(nodeId({ site_id: "home-gateway", instance_id: "modbus" }), "home-gateway/modbus");
  assert.equal(hostOf("192.168.1.5"), "192.168.1.5");
  assert.equal(hostOf("192.168.1.5:47808"), "192.168.1.5");
  assert.equal(hostOf("192.168.1.5/24:47808"), "192.168.1.5");
});

test("edges: expected nodes online and recent, announced devices present, connections follow them", () => {
  const out = edges(expected, fx("edges-ok.json"), NOW, fx("announce-ok.json"));
  assert.deepEqual(out.raw, [], JSON.stringify(out.raw));
  // home-gateway/bacnet is offline but not expected, so it isn't checked
  assert.ok(!out.checks.some((c) => c.name.includes("home-gateway/bacnet")));
  // m1's device_name is matched case-insensitively; m2's device is offline, so not judged
  assert.ok(out.checks.some((c) => c.name === "modbus connection m1 follows Modbus" && c.status === "pass"));
  assert.ok(!out.checks.some((c) => c.name.includes("m2")));
});

test("edges: a missing, offline or stale node is a finding per node", () => {
  const nodes = fx("edges-ok.json").filter((n) => n.site_id !== "local");
  nodes[0] = { ...nodes[0], last_seen: "2026-11-07T22:30:00+00:00" };
  const out = edges(expected, nodes, NOW, null);
  assert.deepEqual(out.raw.map((r) => `${r.kind}:${r.key}`), ["edge-offline:local/local", "edge-offline:home-gateway/modbus"]);
  assert.match(out.raw[1].detail, /last seen 610 s ago \(limit 120 s\)/);
  const off = edges(expected, [{ ...fx("edges-ok.json")[0], online: false, error: "can't reach" }, fx("edges-ok.json")[1]], NOW, null);
  assert.match(off.raw[0].title, /local\/local is offline/);
});

test("edges: an unannounced device and a connection polling the wrong address are findings", () => {
  const ann = fx("announce-ok.json");
  ann.devices = ann.devices.filter((d) => d.name !== "bacnet");
  ann.connections.modbus[0].host = "192.168.128.99";
  const out = edges(expected, fx("edges-ok.json"), NOW, ann);
  assert.deepEqual(out.raw.map((r) => `${r.kind}:${r.key}`).sort(),
    ["announce-mismatch:local/local|modbus|m1", "announce-missing:local/local|bacnet"]);
  const skipped = edges(expected, fx("edges-ok.json"), NOW, { skip: "offline" });
  assert.equal(skipped.checks.find((c) => c.name.startsWith("announced devices")).status, "skip");
});

// --- layout ------------------------------------------------------------------

test("viewport tag: any attribute order and spacing; wrong or missing content is a problem", () => {
  assert.equal(viewportProblem('<meta name="viewport" content="width=device-width, initial-scale=1" />'), null);
  assert.equal(viewportProblem("<META content='initial-scale=1.0,width=device-width' NAME='viewport'>"), null);
  assert.match(viewportProblem("<meta charset=utf-8><title>x</title>"), /no <meta name="viewport">/);
  assert.match(viewportProblem('<meta name="viewport" content="width=1024">'), /width=1024/);
  assert.match(viewportProblem('<meta name="viewport" content="width=device-width">'), /initial-scale/);
});

test("viewport tag: every page missing it is one finding (like #177), listing the pages", () => {
  const out = evaluateViewports([{ file: "index.html", problem: "no tag" }, { file: "ops.html", problem: "no tag" }, { file: "connectors.html", problem: null }]);
  assert.equal(out.raw.length, 1);
  assert.deepEqual(out.raw[0].refs, [177]);
  assert.match(out.raw[0].title, /^2 UI page\(s\)/);
  assert.equal(evaluateViewports([{ file: "a.html", problem: null }]).raw.length, 0);
});

// --- deploy window -------------------------------------------------------------

test("deploy window: zero 5xx passes; any 5xx is one finding; no log is an error, not a pass", () => {
  const win = { start: "2026-11-07T22:00:00Z", end: "2026-11-07T22:03:00Z" };
  assert.equal(deploy({ max_5xx: 0 }, { available: true, count: 0, by_status: {}, by_route: [] }, win).checks[0].status, "pass");
  const burst = deploy({ max_5xx: 0 }, { available: true, count: 7, by_status: { 502: 7 }, first: "a", last: "b",
                                         by_route: [{ route: "GET /api/read", status: 502, count: 7 }] }, win);
  assert.equal(burst.raw[0].kind, "deploy-5xx");
  assert.match(burst.raw[0].detail, /7× 502 GET \/api\/read/);
  assert.equal(deploy({}, { available: false, detail: "no log" }, win).checks[0].status, "error");
});

// --- overload from /ops/metrics (#235) --------------------------------------

test("overload: Baler's own lag, backlog and drops from /ops/metrics, against budgets.json", () => {
  const text = "# HELP x y\n# TYPE baler_ingest_newest_reading_age_seconds gauge\nbaler_ingest_newest_reading_age_seconds 2.5\n"
    + "baler_listener_queue_oldest_age_seconds 45.0\nbaler_broker_dropped_since_listener_start 0\n"
    + 'baler_reading_feed_lag_seconds{consumer="fault_detector"} 1.5\n';
  assert.equal(parseProm(text)['baler_reading_feed_lag_seconds{consumer="fault_detector"}'], 1.5);
  const out = evaluateMetrics(budgets.dataflow, text);
  assert.deepEqual(out.checks.map((c) => [c.name, c.status]), [
    ["ingest lag (/ops/metrics)", "pass"], ["listener backlog (/ops/metrics)", "fail"], ["broker drops (/ops/metrics)", "pass"]]);
  assert.equal(out.raw.length, 1);
  assert.equal(out.raw[0].kind, "overload");
  // an older Baler without the endpoint's metrics: nothing judged
  assert.deepEqual(evaluateMetrics(budgets.dataflow, '{"rows": []}'), { checks: [], raw: [] });
});

// node --test tools/live-check/
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { hostLoad, parseLoadavg } from "./perf.mjs";
import { fetchPrevious, previousPhase } from "./previous.mjs";
import { fingerprint, issueBody, issueFp, issueScope, merge, plan, route } from "./findings.mjs";

test("route hides ids and query values, so a re-created project keeps its fingerprint", () => {
  assert.equal(route("https://x/cx/projects/9ddf1d04-aa11-4b2c-9d3e-0123456789ab/report?format=html"), "/cx/projects/:id/report?format");
  assert.equal(route("https://x/api/read?filter=point&limit=5"), "/api/read?filter&limit");
  assert.equal(route("https://x/ops/status"), "/ops/status");
});

test("merge folds repeats into one finding with every page and viewport", () => {
  const r = { kind: "slow", key: "GET /ops/status", title: "t" };
  const [f, ...rest] = merge([{ ...r, page: "Ops", viewport: "desktop", ms: 8800, detail: "a" },
                              { ...r, page: "Equipment", viewport: "phone", ms: 3000, detail: "a" }]);
  assert.equal(rest.length, 0);
  assert.deepEqual(f.pages, ["Equipment", "Ops"]);
  assert.deepEqual(f.viewports, ["desktop", "phone"]);
  assert.equal(f.ms, 8800);
  assert.equal(f.label, "optimization");
  assert.deepEqual(f.details, ["a"]);
});

test("plan: create new, update open, reopen closed, close unseen only on a complete run", () => {
  const run = { at: "2026-11-07T15:00:00Z", base: "https://x", role: "operator", slowMs: 2000 };
  const [a, b, c] = merge([{ kind: "a11y", key: "1", title: "a" }, { kind: "a11y", key: "2", title: "b" }, { kind: "a11y", key: "3", title: "c" }]);
  const gone = { fp: fingerprint("a11y", "4"), pages: [], viewports: [], details: [], kind: "a11y" };
  const issues = [{ number: 2, state: "OPEN", body: issueBody(b, run) }, { number: 3, state: "CLOSED", body: issueBody(c, run) },
                  { number: 4, state: "OPEN", body: issueBody(gone, run) }, { number: 5, state: "OPEN", body: "not ours" }];
  const ops = (complete) => plan([a, b, c], issues, { complete }).map((x) => `${x.op}:${x.number ?? x.f.title}`);
  assert.deepEqual(ops(true), ["create:a", "update:2", "reopen:3", "close:4"]);
  assert.deepEqual(ops(false), ["create:a", "update:2", "reopen:3"]);
});

test("plan: with a closed duplicate sharing the fingerprint, the open issue is updated", () => {
  const run = { at: "T", base: "b", role: "operator", slowMs: 1 };
  const [f] = merge([{ kind: "a11y", key: "x", title: "x" }]);
  const body = issueBody(f, run);
  for (const order of [[1, 2], [2, 1]]) {
    const issues = order.map((n) => ({ number: n === 1 ? 142 : 151, state: n === 1 ? "OPEN" : "CLOSED", body }));
    assert.deepEqual(plan([f], issues, { complete: true }).map((a) => `${a.op}:${a.number}`), ["update:142"]);
  }
  const closed = [151, 142].map((number) => ({ number, state: "CLOSED", body }));
  assert.deepEqual(plan([f], closed, { complete: true }).map((a) => `${a.op}:${a.number}`), ["reopen:142"]);
});

test("issueBody keeps First seen across updates and carries the fingerprint", () => {
  const [f] = merge([{ kind: "overflow", key: "Ops", title: "t", page: "Ops", viewport: "phone", detail: "d" }]);
  const first = issueBody(f, { at: "T1", base: "b", role: "operator", slowMs: 1 });
  const next = issueBody(f, { at: "T2", base: "b", role: "operator", slowMs: 1 }, first);
  assert.match(next, /\| First seen \| T1 \|/);
  assert.match(next, /\| Last seen \| T2 \|/);
  assert.equal(issueFp(next), f.fp);
});

test("plan: an issue closes only when the scope that owns it ran completely (#178)", () => {
  const run = { at: "T", base: "b", role: "operator", slowMs: 1 };
  const mk = (kind, key, scope) => merge([{ kind, key, title: key, scope }])[0];
  const warm = mk("perf-budget", "warm|GET /fault/faults", "perf:warm");
  const stale = mk("stale", "fbf/home/modbus", "dataflow");
  const legacy = { fp: fingerprint("overflow", "Ops"), pages: [], viewports: [], details: [], kind: "overflow" };
  const legacyBody = issueBody(legacy, run).replace(/ scope:\S+ -->/, " -->");  // filed before scopes existed
  assert.equal(issueScope(legacyBody), null);
  const issues = [{ number: 1, state: "OPEN", body: issueBody(warm, run) }, { number: 2, state: "OPEN", body: issueBody(stale, run) },
                  { number: 3, state: "OPEN", body: legacyBody }];
  const closes = (ran) => plan([], issues, { ran: new Set(ran) }).map((a) => a.number);
  assert.deepEqual(closes(["perf:cold", "browser", "deploy"]), []);          // a cold run says nothing about warm or dataflow
  assert.deepEqual(closes(["perf:warm", "dataflow", "edges"]), [1, 2]);
  assert.deepEqual(closes(["browser"]), []);                                  // legacy needs both browser passes
  assert.deepEqual(closes(["browser", "layout"]), [3]);
  assert.equal(issueScope(issueBody(warm, run)), "perf:warm");
});

test("plan: closed as not planned is never reopened; live-check:ignore is never touched (#178 R9)", () => {
  const run = { at: "T", base: "b", role: "operator", slowMs: 1 };
  const [f, g] = merge([{ kind: "a11y", key: "x", title: "x" }, { kind: "a11y", key: "y", title: "y" }]);
  const issues = [{ number: 10, state: "CLOSED", state_reason: "not_planned", labels: [], body: issueBody(f, run) },
                  { number: 11, state: "OPEN", labels: ["live-check", "live-check:ignore"], body: issueBody(g, run) }];
  assert.deepEqual(plan([f, g], issues, { complete: true }).map((a) => `${a.op}:${a.number}:${a.reason}`),
    ["skip:10:closed as not planned", "skip:11:live-check:ignore"]);
  // an ignored issue isn't closed when its finding goes away either
  assert.deepEqual(plan([], issues, { complete: true }), []);
  // closed as completed (what live-check itself does) still reopens
  assert.equal(plan([f], [{ ...issues[0], state_reason: "completed" }], { complete: true })[0].op, "reopen");
});

test("issueBody carries the latest numbers, related issues and the reproduce command for its module", () => {
  const [f] = merge([{ kind: "perf-budget", key: "cold|Cx", title: "t", scope: "perf:cold", refs: [150], rows: [["Median", "10.2 s"]] }]);
  const body = issueBody(f, { at: "T", base: "b", role: "operator", slowMs: 1, sha: "abc123" });
  assert.match(body, /\| Median \| 10\.2 s \|/);
  assert.match(body, /\| Related \| #150 \|/);
  assert.match(body, /\| Deployed commit \| abc123 \|/);
  assert.match(body, /--only perf --phase cold/);
  assert.match(body, /<!-- live-check fp:[0-9a-f]{12} scope:perf:cold -->/);
});

// Perf damping (#227, #251): fixtures shaped like the real warm phase.
const perfRun = { at: "2026-11-08T05:00:00Z", base: "b", role: "operator", slowMs: 1 };
const equip = () => merge([{ kind: "perf-budget", key: "warm|GET /api/read?filter=equip", scope: "perf:warm",
  title: "GET /api/read?filter=equip is over its warm budget", detail: "median 0.7 s > 0.5 s", rows: [["Median", "0.7 s"]] }])[0];
const ranWarm = new Set(["login", "perf:warm", "dataflow", "edges"]);
const ops = (actions) => actions.map((a) => `${a.op}:${a.number ?? a.f.title}`);

test("damping: one breach on a complete run opens nothing", () => {
  const f = equip();
  const [a] = plan([f], [], { ran: ranWarm, prev: new Map() });
  assert.equal(a.op, "hold");
  assert.match(a.reason, /first breach/);
  // the previous complete run breached something else: still a first breach for this route
  assert.equal(plan([f], [], { ran: ranWarm, prev: new Map([["perf:warm", new Set(["000000000000"])]]) })[0].op, "hold");
  // a cold breach doesn't confirm a warm one
  assert.equal(plan([f], [], { ran: ranWarm, prev: new Map([["perf:cold", new Set([f.fp])]]) })[0].op, "hold");
});

test("damping: a breach on two consecutive complete runs opens, or reopens a closed issue", () => {
  const f = equip();
  const prev = new Map([["perf:warm", new Set([f.fp])]]);
  assert.deepEqual(ops(plan([f], [], { ran: ranWarm, prev })), [`create:${f.title}`]);
  const closed = [{ number: 227, state: "CLOSED", state_reason: "completed", labels: [], body: issueBody(f, perfRun) }];
  assert.deepEqual(ops(plan([f], closed, { ran: ranWarm, prev })), ["reopen:227"]);
  // ...but not when this run's perf phase was incomplete (a route errored)
  const [h] = plan([f], closed, { ran: new Set(["login", "dataflow"]), prev });
  assert.equal(h.op, "hold");
  assert.equal(h.number, 227);
  assert.match(h.reason, /didn't run completely/);
  // a closed issue with a single breach stays closed
  assert.deepEqual(ops(plan([f], closed, { ran: ranWarm })), ["hold:227"]);
});

test("damping: an open perf issue is still updated on a breach and closed by one complete passing run", () => {
  const f = equip();
  const open = [{ number: 251, state: "OPEN", labels: [], body: issueBody(f, perfRun) }];
  assert.deepEqual(ops(plan([f], open, { ran: ranWarm })), ["update:251"]);
  assert.deepEqual(ops(plan([], open, { ran: ranWarm })), ["close:251"]);
  assert.deepEqual(ops(plan([], open, { ran: new Set(["perf:cold"]) })), []);
  // won't-fix and ignore still win over damping
  const wontfix = [{ number: 9, state: "CLOSED", state_reason: "not_planned", labels: [], body: issueBody(f, perfRun) }];
  assert.deepEqual(ops(plan([f], wontfix, { ran: ranWarm, prev: new Map([["perf:warm", new Set([f.fp])]]) })), ["skip:9"]);
});

test("damping: non-perf findings open and reopen on the first sighting, as before", () => {
  const raw = [{ kind: "page-error", key: "Ops|x", title: "pe", scope: "browser" }, { kind: "a11y", key: "y", title: "ax", scope: "browser" },
               { kind: "stale", key: "fbf/home/modbus", title: "st", scope: "dataflow" }, { kind: "edge-offline", key: "home/modbus", title: "eo", scope: "edges" },
               { kind: "deploy-5xx", key: "502", title: "5x", scope: "deploy" }];
  const fs = merge(raw);
  assert.deepEqual(ops(plan(fs, [], { ran: new Set() })), fs.map((f) => `create:${f.title}`));
  const closed = fs.map((f, n) => ({ number: n + 1, state: "CLOSED", state_reason: "completed", labels: [], body: issueBody(f, perfRun) }));
  assert.deepEqual(ops(plan(fs, closed, { ran: new Set() })), ["reopen:1", "reopen:2", "reopen:3", "reopen:4", "reopen:5"]);
});

test("previousPhase reads the last complete run's breaches and per-route numbers from an artifact", () => {
  const f = equip();
  const doc = { sha: "abc", phases: {
    cold: { run: { at: "T0", sha: "abc" }, ran: ["login", "browser"], checks: [], findings: [] },  // perf:cold incomplete
    warm: { run: { at: "T1", sha: "abc" }, ran: ["login", "perf:warm"], findings: [f, merge([{ kind: "stale", key: "k", title: "s", scope: "dataflow" }])[0]],
            checks: [{ module: "perf", phase: "warm", name: "GET /api/read?filter=equip", status: "fail", median_s: 0.7, max_s: 0.9 },
                     { module: "perf", phase: "warm", name: "GET /ops/status", status: "pass", median_s: 0.3, max_s: 0.4 },
                     { module: "dataflow", name: "rate", status: "pass" }],
            perf_host: { phase: "warm", before: { load_1m: 15.2, nproc: 1 } } } } };
  const p = previousPhase(doc, "warm");
  assert.equal(p.scope, "perf:warm");
  assert.deepEqual([...p.fps], [f.fp]);
  assert.deepEqual(p.routes["GET /api/read?filter=equip"], { status: "fail", median_s: 0.7, max_s: 0.9 });
  assert.equal(Object.keys(p.routes).length, 2);
  assert.equal(p.host.before.load_1m, 15.2);
  assert.equal(previousPhase(doc, "cold"), null, "an incomplete phase is skipped, not read as a pass");
  assert.equal(previousPhase({ phases: {} }, "warm"), null);
  // an artifact file can stand in for GitHub (LIVE_CHECK_PREVIOUS)
  const dir = mkdtempSync(path.join(os.tmpdir(), "live-check-prev-test-"));
  writeFileSync(path.join(dir, "prev.json"), JSON.stringify(doc));
  assert.deepEqual([...fetchPrevious("warm", { file: path.join(dir, "prev.json") }).fps], [f.fp]);
});

test("parseLoadavg reads /proc/loadavg; hostLoad records 1/5/15 min load and nproc", () => {
  assert.deepEqual(parseLoadavg("15.32 14.80 13.07 9/412 88123\n"), { load: [15.32, 14.8, 13.07], running: 9, total: 412 });
  assert.equal(parseLoadavg("garbage"), null);
  const h = hostLoad();
  for (const k of ["load_1m", "load_5m", "load_15m", "nproc"]) assert.equal(typeof h[k], "number");
  assert.ok(h.nproc >= 1);
});

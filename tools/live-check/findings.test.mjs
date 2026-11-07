// node --test tools/live-check/
import assert from "node:assert/strict";
import { test } from "node:test";
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

// node --test tools/live-check/
import assert from "node:assert/strict";
import { test } from "node:test";
import { fingerprint, issueBody, issueFp, merge, plan, route } from "./findings.mjs";

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

// node --test tools/live-check/  (supersede.mjs, #260)
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { check, decide, readMarker, report } from "./supersede.mjs";

const SHA = "a".repeat(40);
const NEW = "b".repeat(40);
const run = (n, status, conclusion = null) => ({ run_number: n, head_sha: NEW, deploy: status ? { status, conclusion } : null });

test("decide: the marker naming another commit supersedes", () => {
  const d = decide({ expectSha: SHA, marker: NEW, ownRunNumber: 5 });
  assert.equal(d.superseded, true);
  assert.equal(d.tentative, false);
  assert.match(d.reason, /now runs bbbbbbb, not aaaaaaa/);
  assert.equal(decide({ expectSha: SHA, marker: SHA, ownRunNumber: 5 }).superseded, false);
  assert.equal(decide({ expectSha: SHA, marker: null, ownRunNumber: 5 }).superseded, false, "no marker: nothing to compare");
  assert.equal(decide({ expectSha: "", marker: NEW, ownRunNumber: 5 }).superseded, false, "nothing expected: nothing to compare");
});

test("decide: a newer run deploying or deployed supersedes; a skipped, failed or cancelled one doesn't", () => {
  assert.match(decide({ expectSha: SHA, marker: SHA, ownRunNumber: 5, runs: [run(6, "in_progress")] }).reason, /#6 .* is deploying/);
  assert.match(decide({ expectSha: SHA, marker: SHA, ownRunNumber: 5, runs: [run(6, "completed", "success")] }).reason, /#6 .* has deployed/);
  for (const c of ["skipped", "failure", "cancelled"]) {
    assert.equal(decide({ expectSha: SHA, marker: SHA, ownRunNumber: 5, runs: [run(6, "completed", c)] }).superseded, false, c);
  }
  assert.equal(decide({ expectSha: SHA, marker: SHA, ownRunNumber: 5, runs: [run(6, null)] }).superseded, false, "no deploy job listed yet");
});

test("decide: older runs and this run itself never supersede", () => {
  const runs = [run(4, "in_progress"), run(5, "in_progress"), run(3, "completed", "success")];
  assert.equal(decide({ expectSha: SHA, marker: SHA, ownRunNumber: 5, runs }).superseded, false);
});

test("decide: a queued newer deploy is tentative; a running one wins over it", () => {
  const q = decide({ expectSha: SHA, marker: SHA, ownRunNumber: 5, runs: [run(6, "queued")] });
  assert.deepEqual([q.superseded, q.tentative], [true, true]);
  assert.match(q.reason, /waiting to deploy/);
  const r = decide({ expectSha: SHA, marker: SHA, ownRunNumber: 5, runs: [run(7, "queued"), run(6, "in_progress")] });
  assert.deepEqual([r.superseded, r.tentative], [true, false]);
});

test("check: a queued deploy must still be queued on a second look (a skipped job flickers as queued)", async () => {
  const seq = (...h) => { let i = 0; return () => h[Math.min(i++, h.length - 1)]; };
  const base = { expectSha: SHA, repo: "o/r", runId: "1", confirmMs: 0, marker: () => SHA };
  const flicker = await check({ ...base, history: seq({ ownRunNumber: 5, runs: [run(6, "queued")] }, { ownRunNumber: 5, runs: [run(6, "completed", "skipped")] }) });
  assert.equal(flicker.superseded, false);
  const real = await check({ ...base, history: seq({ ownRunNumber: 5, runs: [run(6, "queued")] }) });
  assert.deepEqual([real.superseded, real.tentative], [true, false]);
});

test("check: a GitHub error falls back to the marker (fail-open), and is reported", async () => {
  const boom = () => { throw new Error("HTTP 502"); };
  const ok = await check({ expectSha: SHA, repo: "o/r", runId: "1", history: boom, marker: () => SHA });
  assert.deepEqual([ok.superseded, ok.error], [false, "HTTP 502"]);
  const moved = await check({ expectSha: SHA, repo: "o/r", runId: "1", history: boom, marker: () => NEW });
  assert.equal(moved.superseded, true);
  const local = await check({ expectSha: SHA, repo: "", runId: "", history: boom, marker: () => null });
  assert.deepEqual([local.superseded, local.error], [false, null], "outside Actions: no run history asked");
});

test("readMarker and report: step output, notice and summary", () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), "live-check-sup-"));
  writeFileSync(path.join(dir, "m"), `${SHA}\n`);
  assert.equal(readMarker(path.join(dir, "m")), SHA);
  assert.equal(readMarker(path.join(dir, "missing")), null);
  const output = path.join(dir, "out");
  const summary = path.join(dir, "sum");
  report({ superseded: false, reason: null }, { output, summary });
  report({ superseded: true, reason: "Deploy run #6 (bbbbbbb) is deploying" }, { output, summary });
  assert.match(readFileSync(output, "utf8"), /superseded=false\nreason=\nsuperseded=true\nreason=Deploy run #6/);
  assert.match(readFileSync(summary, "utf8"), /\*\*Superseded:\*\* Deploy run #6 .*no issue was filed or closed/);
});

#!/usr/bin/env node
// Supersede detection (#260). The Deploy workflow's verify job no longer
// holds the deploy queue, so a newer deploy can start while an older verify
// is still running (or sleeping before its warm phase). Such a verify is
// "superseded": what it would measure is no longer the commit it was started
// for, or is a deploy in flight. It stops cleanly, files and closes no
// issues, keeps whatever phase it already finished in the artifact, and its
// phases don't count for perf damping (previous.mjs skips them).
//
// A verify is superseded when either
//   - the deployed-commit marker the deploy wrapper writes at the end of each
//     deploy (/var/lib/baler/deployed-commit, root-owned 644) names another
//     commit than the one this run verifies, or
//   - a newer run of the same workflow has a `deploy` job that is queued,
//     running, or finished successfully. A queued one counts only if it is
//     still queued or running a few seconds later (confirmMs): a run whose
//     deploy job will be skipped (CI failed) can show as queued for a moment.
//
// decide() is pure (supersede.test.mjs); check() does the I/O.
//
//   node supersede.mjs                     # check once (step output superseded=true|false)
//   node supersede.mjs --wait 300          # sleep up to 300 s, polling, stop early if superseded
//
// Env: LIVE_CHECK_EXPECT_SHA (falls back to LIVE_CHECK_SHA), LIVE_CHECK_MARKER
// (default /var/lib/baler/deployed-commit), LIVE_CHECK_REPO and GITHUB_RUN_ID
// (the run-history check; skipped without them), GITHUB_OUTPUT,
// GITHUB_STEP_SUMMARY.

import { execFileSync } from "node:child_process";
import { appendFileSync, readFileSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";

export const MARKER = "/var/lib/baler/deployed-commit";
const ACTIVE = new Set(["queued", "in_progress", "waiting", "pending", "requested"]);
const short = (s) => String(s || "").slice(0, 7);

// expectSha: the commit this verify is for. marker: the marker's contents, or
// null when it can't be read. ownRunNumber: this run's run_number. runs: newer
// or older runs of the workflow as [{run_number, head_sha, html_url, deploy:
// {status, conclusion} | null}] (deploy null: no deploy job listed yet).
// -> {superseded, reason, tentative}. tentative: only a queued deploy says so.
export function decide({ expectSha, marker, ownRunNumber, runs = [] }) {
  if (expectSha && marker && marker !== expectSha) {
    return { superseded: true, tentative: false, reason: `nash-srv now runs ${short(marker)}, not ${short(expectSha)} (a newer deploy finished)` };
  }
  let queued = null;
  for (const r of [...runs].sort((a, b) => b.run_number - a.run_number)) {
    if (!(r.run_number > ownRunNumber) || !r.deploy) continue;
    const { status, conclusion } = r.deploy;
    const what = `Deploy run #${r.run_number} (${short(r.head_sha)})`;
    if (status === "in_progress") return { superseded: true, tentative: false, reason: `${what} is deploying` };
    if (status === "completed" && conclusion === "success") return { superseded: true, tentative: false, reason: `${what} has deployed` };
    if (ACTIVE.has(status) && !queued) queued = { superseded: true, tentative: true, reason: `${what} is waiting to deploy` };
  }
  return queued || { superseded: false, tentative: false, reason: null };
}

export function readMarker(file = process.env.LIVE_CHECK_MARKER || MARKER) {
  try { return readFileSync(file, "utf8").trim() || null; } catch { return null; }
}

const ghJson = (p) => JSON.parse(execFileSync("gh", ["api", p], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }));

// This run's number, and the newer runs of the same workflow with their
// deploy job. Throws when GitHub can't be asked.
export function runHistory({ repo, runId }) {
  const own = ghJson(`repos/${repo}/actions/runs/${runId}`);
  const list = ghJson(`repos/${repo}/actions/workflows/${own.workflow_id}/runs?per_page=30`).workflow_runs || [];
  const runs = [];
  for (const r of list.filter((x) => x.run_number > own.run_number)) {
    const jobs = ghJson(`repos/${repo}/actions/runs/${r.id}/jobs?filter=latest&per_page=100`).jobs || [];
    const d = jobs.find((j) => j.name === "deploy");
    runs.push({ run_number: r.run_number, head_sha: r.head_sha, html_url: r.html_url, deploy: d ? { status: d.status, conclusion: d.conclusion } : null });
  }
  return { ownRunNumber: own.run_number, runs };
}

// One check. Never throws: a GitHub error is returned as `error` and the
// check falls back to the marker alone (fail-open, so an API hiccup can't
// suppress the last deploy's verification).
export async function check({
  expectSha = process.env.LIVE_CHECK_EXPECT_SHA || process.env.LIVE_CHECK_SHA,
  repo = process.env.LIVE_CHECK_REPO, runId = process.env.GITHUB_RUN_ID,
  confirmMs = 15000, history = runHistory, marker = readMarker,
} = {}) {
  let error = null;
  const once = () => {
    let hist = { ownRunNumber: Infinity, runs: [] };
    if (repo && runId) {
      try { hist = history({ repo, runId }); } catch (e) { error = (e.stderr && String(e.stderr).trim().split("\n").pop()) || String(e.message).split("\n")[0]; }
    }
    return decide({ expectSha, marker: marker(), ...hist });
  };
  let d = once();
  if (d.superseded && d.tentative) {
    await sleep(confirmMs);
    const again = once();
    d = again.superseded ? { ...again, tentative: false } : again;
  }
  return { ...d, error };
}

export function report(d, { output = process.env.GITHUB_OUTPUT, summary = process.env.GITHUB_STEP_SUMMARY } = {}) {
  if (output) appendFileSync(output, `superseded=${d.superseded}\nreason=${(d.reason || "").replace(/\n/g, " ")}\n`);
  if (d.superseded) {
    console.log(`::notice title=verify superseded::${d.reason}. This verification stops here: no issue is filed or closed, and it doesn't count for perf damping.`);
    if (summary) appendFileSync(summary, `\n**Superseded:** ${d.reason}. This verification stopped here: no issue was filed or closed, and it doesn't count for perf damping. The newer deploy gets its own complete verification.\n`);
  }
}

async function main() {
  const { values: opt } = parseArgs({ options: { wait: { type: "string", default: "0" }, interval: { type: "string", default: "15" } } });
  const waitMs = Number(opt.wait) * 1000;
  const intervalMs = Math.max(1, Number(opt.interval)) * 1000;
  const until = Date.now() + waitMs;
  let warned = false;
  let d;
  for (;;) {
    d = await check();
    if (d.error && !warned) {
      console.log(`::warning title=supersede check::couldn't read the run history (${d.error}); checking the deployed-commit marker only`);
      warned = true;
    }
    if (d.superseded || Date.now() >= until) break;
    await sleep(Math.min(intervalMs, until - Date.now()));
  }
  if (!d.superseded) console.log(waitMs ? `waited ${opt.wait} s; still the newest deploy` : "still the newest deploy");
  report(d);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) await main();

// Perf damping: a perf finding opens (or reopens) its issue only when the
// previous complete run of the same phase breached the same budget too.
// nash-srv's load swings make single warm timings flap (#227, #251).
//
// "The previous run" is the newest earlier `live-verify-<sha>` artifact
// (uploaded by the Deploy workflow's verify job, kept 30 days) whose phase
// ran its perf module completely. The artifact already holds every route's
// numbers and the run's findings, it lives on GitHub rather than on the
// runner, and reading it is read-only: no extra state to write, keep in sync
// or lose. previousPhase() is pure (findings.test.mjs); fetchPrevious() is
// the gh I/O.

import { execFileSync } from "node:child_process";
import { mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";

const ARTIFACT_PREFIX = "live-verify-";
const MAX_LOOKBACK = 10;

// doc: a live-verify-<sha>.json. -> null when that deploy has no complete
// run of this phase's perf module (an incomplete run is skipped, not taken
// as a pass) or that phase was superseded by a newer deploy, else {at, sha, scope, fps, routes}: fps are the perf-budget
// fingerprints it breached, routes each route's numbers.
export function previousPhase(doc, phase) {
  const p = doc && doc.phases && doc.phases[phase];
  const scope = `perf:${phase}`;
  if (!p || !Array.isArray(p.ran) || !p.ran.includes(scope)) return null;
  // A superseded phase (#260) counts neither for nor against damping.
  if (p.run?.superseded) return null;
  const fps = new Set((p.findings || []).filter((f) => f.kind === "perf-budget" && (f.scope || scope) === scope).map((f) => f.fp));
  const routes = {};
  for (const c of p.checks || []) {
    if (c.module === "perf" && c.phase === phase) routes[c.name] = { status: c.status, median_s: c.median_s ?? null, max_s: c.max_s ?? null };
  }
  return { at: p.run?.at || null, sha: p.run?.sha || doc.sha || null, scope, fps, routes, host: p.perf_host || null };
}

// The first artifact (newest first) with a complete run of `phase`.
// Throws when GitHub can't be asked (no token, no actions: read, API down):
// the caller reports that as an error check rather than guessing.
export function fetchPrevious(phase, { repo = process.env.LIVE_CHECK_REPO, file = process.env.LIVE_CHECK_PREVIOUS } = {}) {
  if (file) {
    const prev = previousPhase(JSON.parse(readFileSync(file, "utf8")), phase);
    return prev ? { ...prev, source: file } : null;
  }
  const R = repo ? ["-R", repo] : [];
  const list = execFileSync("gh", ["api", `repos/${repo || "{owner}/{repo}"}/actions/artifacts?per_page=100`, "-q",
    `.artifacts[] | select(.name | startswith("${ARTIFACT_PREFIX}")) | select(.expired | not) | {id, name, created_at, run: .workflow_run.id}`],
  { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] })
    .split("\n").filter(Boolean).map((l) => JSON.parse(l))
    .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))
    .slice(0, MAX_LOOKBACK);
  for (const a of list) {
    const dir = mkdtempSync(path.join(os.tmpdir(), "live-check-prev-"));
    try {
      execFileSync("gh", ["run", "download", String(a.run), "-n", a.name, "-D", dir, ...R], { stdio: ["ignore", "ignore", "pipe"] });
      const json = readdirSync(dir).find((f) => f.startsWith(ARTIFACT_PREFIX) && f.endsWith(".json"));
      if (!json) continue;
      let doc;
      try { doc = JSON.parse(readFileSync(path.join(dir, json), "utf8")); } catch { continue; }  // a damaged artifact says nothing
      const prev = previousPhase(doc, phase);
      if (prev) return { ...prev, source: `artifact ${a.name} (run ${a.run}, ${a.created_at})` };
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }
  return null;
}

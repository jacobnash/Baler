#!/usr/bin/env node
// Read-only check of a live Baler install: the browser pass over every page
// (R3) plus, since #178, performance budgets (perf), data flow (dataflow),
// edge nodes (edges), phone layout (layout) and gateway 5xx during the deploy
// (deploy). What it finds goes into the GitHub issue log (label live-check),
// deduplicated by fingerprint. See docs-site/pages/post-deploy-verify.mdx
// (and live-check.mdx).
//
//   node tools/live-check/live-check.mjs --dry-run                 # everything, report only
//   node tools/live-check/live-check.mjs --phase cold              # right after a deploy
//   node tools/live-check/live-check.mjs --phase warm              # five minutes later
//   node tools/live-check/live-check.mjs --dry-run --only perf,edges
//
// Options: --dry-run, --phase cold|warm, --only <modules>,
// --deploy-start <iso> --deploy-end <iso> (the deploy module's window).

import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync, appendFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { parseArgs } from "node:util";
import { LABEL, issueBody, merge, plan } from "./findings.mjs";
import { Guard } from "./guard.mjs";
import { fetchPrevious } from "./previous.mjs";
import { loadText } from "./perf.mjs";
import { summaryMarkdown, writeArtifact } from "./report.mjs";

const MODULES = ["perf", "browser", "layout", "dataflow", "edges", "deploy"];
const PHASE_MODULES = { cold: ["perf", "browser", "layout", "deploy"], warm: ["perf", "dataflow", "edges"], all: MODULES };

const { values: opt } = parseArgs({ options: {
  "dry-run": { type: "boolean", default: false }, phase: { type: "string" }, only: { type: "string" },
  "deploy-start": { type: "string" }, "deploy-end": { type: "string" }, help: { type: "boolean", default: false },
} });
if (opt.help) {
  console.log(readFileSync(new URL(import.meta.url), "utf8").split("\n").filter((l) => l.startsWith("//")).slice(0, 17).map((l) => l.slice(3)).join("\n"));
  process.exit(0);
}
const DRY = opt["dry-run"];
if (opt.phase && !["cold", "warm"].includes(opt.phase)) { console.error("--phase is cold or warm"); process.exit(2); }
const only = opt.only ? opt.only.split(",").map((s) => s.trim()).filter(Boolean) : null;
const bad = (only || []).filter((m) => !MODULES.includes(m));
if (bad.length) { console.error(`unknown module(s) ${bad.join(", ")}; one of ${MODULES.join(", ")}`); process.exit(2); }
// The order modules run in: perf first, before the browser warms anything up.
const modules = ["perf", "browser", "layout", "deploy", "dataflow", "edges"].filter((m) => (only || PHASE_MODULES[opt.phase || "all"]).includes(m));
// A run without --phase is judged against the warm budgets: it isn't right after a deploy.
const PERF_PHASE = opt.phase || "warm";

const ENV_FILE = process.env.BALER_TEST_ENV || path.join(os.homedir(), ".config/baler-tests/baler.env");
try {
  for (const line of readFileSync(ENV_FILE, "utf8").split("\n")) {
    const m = /^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$/.exec(line);
    if (m && !(m[1] in process.env)) process.env[m[1]] = m[2].replace(/^["']|["']$/g, "");
  }
} catch { /* env vars may come from the environment instead */ }

const BASE = (process.env.BALER_TEST_URL || "").replace(/\/$/, "");
const HERE = import.meta.dirname;
const OUT = process.env.LIVE_CHECK_OUT || path.join(HERE, "out");
if (!BASE || !process.env.BALER_TEST_EMAIL || !process.env.BALER_TEST_PASSWORD) {
  console.error(`Set BALER_TEST_URL, BALER_TEST_EMAIL and BALER_TEST_PASSWORD, or put them in ${ENV_FILE}.`);
  process.exit(2);
}
const loadJson = (envVar, file) => JSON.parse(readFileSync(process.env[envVar] || path.join(HERE, file), "utf8"));
const budgets = loadJson("LIVE_CHECK_BUDGETS", "budgets.json");
const expected = loadJson("LIVE_CHECK_EXPECTED_EDGES", "expected-edges.json");

function gitSha() {
  if (process.env.LIVE_CHECK_SHA) return process.env.LIVE_CHECK_SHA;
  try { return execFileSync("git", ["rev-parse", "HEAD"], { cwd: HERE, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim(); } catch { return "local"; }
}

const guard = new Guard(BASE);
guard.installFetch();
const raw = [];

async function login() {
  const res = await fetch(`${BASE}/auth/login`, { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ email: process.env.BALER_TEST_EMAIL, password: process.env.BALER_TEST_PASSWORD }) });
  if (!res.ok) return { error: res.status };
  const { token } = await res.json();
  const claims = JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString());
  return { token, role: claims.role || "unknown" };
}

// The API surface the modules get: GETs and the named edge reads only (the
// guard refuses anything else anyway).
function makeApi(token) {
  const h = { Authorization: `Bearer ${token}`, Accept: "application/json" };
  const get = (p) => fetch(`${BASE}${p}`, { headers: h });
  return {
    get,
    async json(p) {
      const res = await get(p);
      if (!res.ok) throw new Error(`GET ${p.split("?")[0]} answered ${res.status}: ${(await res.text()).slice(0, 160)}`);
      return res.json();
    },
    async time(p) {
      const t0 = performance.now();
      try {
        const res = await fetch(`${BASE}${p}`, { headers: { Authorization: h.Authorization } });
        const body = await res.arrayBuffer();
        return { ms: performance.now() - t0, bytes: body.byteLength, status: res.status };
      } catch (e) {
        return { ms: performance.now() - t0, bytes: 0, status: 0, error: e.message };
      }
    },
    async action(site, inst, name) {
      const p = `/ops/edges/${encodeURIComponent(site)}/${encodeURIComponent(inst)}/actions/${name}`;
      const res = await fetch(`${BASE}${p}`, { method: "POST", headers: { ...h, "content-type": "application/json" }, body: "{}" });
      if (!res.ok) throw new Error(`${name} on ${site}/${inst} answered ${res.status}: ${(await res.text()).slice(0, 160)}`);
      return (await res.json()).result;
    },
  };
}

function gh(args, input) {
  const repo = process.env.LIVE_CHECK_REPO ? ["-R", process.env.LIVE_CHECK_REPO] : [];
  return execFileSync("gh", [...args, ...repo], { input, encoding: "utf8", stdio: ["pipe", "pipe", "inherit"] });
}

// The only writes the tool makes: GitHub issues (never the install).
function sync(findings, ran, run, prev) {
  execFileSync("gh", ["label", "create", LABEL, "--color", "5319e7", "--force",
    "--description", "Found by tools/live-check against the live install",
    ...(process.env.LIVE_CHECK_REPO ? ["-R", process.env.LIVE_CHECK_REPO] : [])], { stdio: "ignore" });
  // REST, not `gh issue list --label`: that goes through the search index,
  // which lags minutes behind, so a quick second run filed duplicates.
  const repo = process.env.LIVE_CHECK_REPO || "{owner}/{repo}";
  const issues = execFileSync("gh", ["api", "--paginate", `repos/${repo}/issues?labels=${LABEL}&state=all&per_page=100`,
    "-q", ".[] | select(.pull_request | not) | {number, state: (.state | ascii_upcase), state_reason, labels: [.labels[].name], body}"], { encoding: "utf8" })
    .split("\n").filter(Boolean).map((l) => JSON.parse(l));
  const actions = plan(findings, issues, { ran, prev });
  for (const a of actions) {
    if (a.op === "create") {
      a.url = gh(["issue", "create", "--title", `[live-check] ${a.f.title}`, "--label", a.f.label, "--label", LABEL, "--body-file", "-"],
        issueBody(a.f, run)).trim();
      console.log(`created ${a.url}`);
    } else if (a.op === "close") {
      gh(["issue", "close", String(a.number), "--reason", "completed", "--comment", `Not seen by live-check at ${run.at} (a complete run of its check, as a ${run.role}). Closing; it reopens if it comes back.`]);
      console.log(`closed #${a.number}`);
    } else if (a.op === "skip") {
      console.log(`left #${a.number} alone (${a.reason})`);
    } else if (a.op === "hold") {
      console.log(`held ${a.f.title}${a.number ? ` (#${a.number} stays closed)` : ""}: ${a.reason}`);
    } else {
      if (a.op === "reopen") gh(["issue", "reopen", String(a.number), "--comment", `Seen again by live-check at ${run.at}.`]);
      gh(["issue", "edit", String(a.number), "--body-file", "-"], issueBody(a.f, run, a.body));
      console.log(`${a.op === "reopen" ? "reopened" : "updated"} #${a.number}`);
    }
  }
  return actions;
}

mkdirSync(OUT, { recursive: true });
const run = { at: new Date().toISOString().replace(/\.\d+Z$/, "Z"), base: BASE, role: "?", sha: gitSha(), phase: opt.phase || null,
              modules, slowMs: Number(process.env.LIVE_CHECK_SLOW_MS || 2000) };
const checks = [];
const ran = new Set();
let perfHost = null;
const auth = await login().catch((e) => ({ error: e.message }));
let browser = null;
if (auth.error) {
  raw.push({ kind: "login", key: String(auth.error), title: `can't sign in: POST /auth/login returns ${auth.error}`, page: "login", scope: "login",
             detail: `POST ${BASE}/auth/login -> ${auth.error}` });
  checks.push({ module: "login", name: "POST /auth/login", status: "error", detail: String(auth.error) });
} else {
  run.role = auth.role;
  ran.add("login");
  const ctx = {
    base: BASE, token: auth.token, guard, raw, budgets, expected, out: OUT, api: makeApi(auth.token),
    settleMs: Number(process.env.LIVE_CHECK_SETTLE_MS || 5000), slowMs: run.slowMs,
    project: process.env.LIVE_CHECK_PROJECT || "Home Edge Pilot", phase: PERF_PHASE,
    window: opt["deploy-start"] || process.env.LIVE_CHECK_DEPLOY_START
      ? { start: opt["deploy-start"] || process.env.LIVE_CHECK_DEPLOY_START, end: opt["deploy-end"] || process.env.LIVE_CHECK_DEPLOY_END } : null,
    async browser() {
      if (!browser) browser = await (await import("./browser.mjs")).launch();
      return browser;
    },
  };
  const impl = { perf: "./perf.mjs", browser: "./browser.mjs", layout: "./layout.mjs", dataflow: "./dataflow.mjs", edges: "./edges.mjs", deploy: "./deploywin.mjs" };
  for (const m of modules) {
    const t0 = Date.now();
    let res;
    try {
      res = await (await import(impl[m])).run(ctx);
    } catch (e) {
      res = { scope: m, complete: false, raw: [], checks: [{ module: m, name: m, status: "error", detail: String(e.message).split("\n")[0] }] };
    }
    checks.push(...res.checks);
    raw.push(...res.raw);
    if (res.complete) ran.add(res.scope);
    if (res.host) perfHost = { phase: PERF_PHASE, ...res.host };
    console.log(`${m}: ${res.checks.length} check(s), ${res.complete ? "complete" : "INCOMPLETE"} (${Math.round((Date.now() - t0) / 1000)} s)`);
  }
  if (browser) await browser.close();
}

// Perf damping (previous.mjs): the previous complete run of this perf phase,
// read back from its artifact. Not asked in a dry run (no issue changes)
// unless LIVE_CHECK_PREVIOUS names a file.
let perfPrevious = null;
const prev = new Map();
if (modules.includes("perf") && (!DRY || process.env.LIVE_CHECK_PREVIOUS)) {
  try {
    perfPrevious = fetchPrevious(PERF_PHASE);
    if (perfPrevious) prev.set(perfPrevious.scope, perfPrevious.fps);
    console.log(perfPrevious ? `perf: previous complete ${PERF_PHASE} run ${perfPrevious.at} from ${perfPrevious.source}, ${perfPrevious.fps.size} breach(es)`
      : `perf: no earlier complete ${PERF_PHASE} run found; a breach now is held`);
  } catch (e) {
    // Without the previous run no perf issue can open: say so loudly.
    checks.push({ module: "perf", phase: PERF_PHASE, name: "previous run (damping)", status: "error",
                  detail: `couldn't read the previous verification artifact, so no perf issue can open: ${(e.stderr && String(e.stderr).trim().split("\n").pop()) || String(e.message).split("\n")[0]}` });
  }
}
const findings = merge(raw);
for (const f of findings) {
  const r = perfPrevious && f.kind === "perf-budget" && f.scope === perfPrevious.scope && perfPrevious.routes[String(f.key).split("|").slice(1).join("|")];
  if (r) f.rows = [...f.rows, ["Previous complete run", `${r.status}, median ${r.median_s} s, max ${r.max_s} s (${perfPrevious.at})`]];
}
let actions = [];
if (DRY) console.log("\n--dry-run: no issues changed.");
else actions = sync(findings, ran, run, prev);

const active = findings.filter((f) => !actions.some((a) => a.op === "skip" && a.f?.fp === f.fp));
const errors = checks.filter((c) => c.status === "error");
const previousOut = perfPrevious && { at: perfPrevious.at, sha: perfPrevious.sha, scope: perfPrevious.scope, source: perfPrevious.source,
                                      breached: [...perfPrevious.fps], routes: perfPrevious.routes };
const data = { run, ran: [...ran].sort(), checks, findings, perf_host: perfHost, perf_previous: previousOut,
               actions: actions.map(({ f, body, ...a }) => ({ ...a, fp: f?.fp })),
               requests: guard.summary(), only_reads: guard.onlyReads(), blocked: [...guard.blocked] };
const artifact = path.join(OUT, `live-verify-${run.sha.slice(0, 12)}.json`);
writeArtifact(artifact, opt.phase || "manual", data);
writeFileSync(path.join(OUT, "findings.json"), JSON.stringify({ run, complete: ran.size === modules.length, blocked: data.blocked, findings }, null, 2));
const md = summaryMarkdown({ ...data, findings, actions, ran, dry: DRY });
writeFileSync(path.join(OUT, `summary-${opt.phase || "manual"}.md`), md);
if (process.env.GITHUB_STEP_SUMMARY) appendFileSync(process.env.GITHUB_STEP_SUMMARY, `${md}\n`);

console.log(`live-check ${run.at} against ${BASE} as ${run.role} (${modules.join(", ")}): ${findings.length} finding(s), ${errors.length} check error(s)`);
for (const c of checks.filter((x) => x.status !== "pass")) console.log(`  ${c.status.toUpperCase()} ${c.module}${c.phase ? `/${c.phase}` : ""}: ${c.name} - ${c.detail || ""}`);
for (const f of findings) console.log(`  [${f.label}] ${f.title}`);
if (perfHost) console.log(`perf (${perfHost.phase}) runner host ${perfHost.before.host}: ${loadText(perfHost.before)} before, ${loadText(perfHost.after)} after`);
if (guard.blocked.size) console.log(`blocked writes (read-only guard): ${[...guard.blocked].join(", ")}`);
console.log(`artifact: ${artifact}`);
process.exitCode = active.length || errors.length ? 1 : 0;

#!/usr/bin/env node
// Read-only check of a live Baler install in a real browser. It hands what it
// finds to the GitHub issue log (label live-check), deduplicated by
// fingerprint. See docs-site/pages/live-check.mdx.
//
//   node tools/live-check/live-check.mjs --dry-run   # report only
//   node tools/live-check/live-check.mjs             # report and sync issues

import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { chromium } from "playwright-core";
import { LABEL, issueBody, merge, plan, route } from "./findings.mjs";

const require = createRequire(import.meta.url);
const DRY = process.argv.includes("--dry-run");
const ENV_FILE = process.env.BALER_TEST_ENV || path.join(os.homedir(), ".config/baler-tests/baler.env");
try {
  for (const line of readFileSync(ENV_FILE, "utf8").split("\n")) {
    const m = /^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$/.exec(line);
    if (m && !(m[1] in process.env)) process.env[m[1]] = m[2].replace(/^["']|["']$/g, "");
  }
} catch { /* env vars may come from the environment instead */ }

const BASE = (process.env.BALER_TEST_URL || "").replace(/\/$/, "");
const SLOW_MS = Number(process.env.LIVE_CHECK_SLOW_MS || 2000);
const SETTLE_MS = Number(process.env.LIVE_CHECK_SETTLE_MS || 5000);
const PROJECT = process.env.LIVE_CHECK_PROJECT || "Home Edge Pilot";
const OUT = path.join(import.meta.dirname, "out");
const PAGES = { index: "Equipment", his: "Query", alarms: "Alarms", derivations: "Derivations",
                cx: "Commissioning", connectors: "Connectors", ops: "Ops" };
const VIEWPORTS = { desktop: { width: 1440, height: 900 }, phone: { width: 390, height: 844 } };
const A11Y_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
if (!BASE || !process.env.BALER_TEST_EMAIL) {
  console.error(`Set BALER_TEST_URL, BALER_TEST_EMAIL and BALER_TEST_PASSWORD, or put them in ${ENV_FILE}.`);
  process.exit(2);
}

const raw = [];
const blocked = new Set();
const firstLine = (s) => String(s).split("\n")[0].slice(0, 160);

async function login() {
  const res = await fetch(`${BASE}/auth/login`, { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ email: process.env.BALER_TEST_EMAIL, password: process.env.BALER_TEST_PASSWORD }) });
  if (!res.ok) return { error: res.status };
  const { token } = await res.json();
  const claims = JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString());
  return { token, role: claims.role || "unknown" };
}

// Every non-GET request is aborted: the operator account this uses can write,
// and a check must never change the install it checks. The one exception is
// the Connectors page's edge-node reads, which are POSTs by design (they
// travel the same action channel as writes); only those named reads pass.
const READ_POSTS = /^\/ops\/edges\/[^/]+\/[^/]+\/actions\/(list_devices|list_connections|modbus_list_connections)$/;
async function readOnly(ctx) {
  await ctx.route("**/*", (r) => {
    const req = r.request();
    if (["GET", "HEAD", "OPTIONS"].includes(req.method())) return r.continue();
    if (req.method() === "POST" && req.url().startsWith(BASE) && READ_POSTS.test(new URL(req.url()).pathname)) return r.continue();
    blocked.add(`${req.method()} ${req.url().startsWith(BASE) ? route(req.url()) : req.url()}`);
    return r.abort("blockedbyclient");
  });
}

async function checkPage(ctx, url, page, viewport, { a11y }) {
  const p = await ctx.newPage();
  const pending = [];
  const add = (r) => raw.push({ page, viewport, ...r });
  p.on("pageerror", (e) => add({ kind: "page-error", key: `${page}|${firstLine(e.message)}`,
    title: `${page}: uncaught error: ${firstLine(e.message)}`, detail: (e.stack || e.message).split("\n").slice(0, 5).join("\n") }));
  p.on("console", (m) => {
    const t = m.text();
    if (m.type() !== "error" || t.startsWith("Failed to load resource")) return;  // HTTP errors are reported with their URL below
    add({ kind: "console-error", key: `${page}|${t.slice(0, 120)}`, title: `${page}: console error: ${t.slice(0, 90)}`, detail: t.slice(0, 600) });
  });
  p.on("requestfailed", (req) => {
    const err = req.failure()?.errorText || "";
    if (!req.url().startsWith(BASE) || err.includes("BLOCKED_BY_CLIENT") || err.includes("ERR_ABORTED")) return;
    const r = `${req.method()} ${route(req.url())}`;
    add({ kind: "request-failed", key: r, title: `${r} fails: ${err}`, detail: `${r} -> ${err}` });
  });
  p.on("requestfinished", (req) => pending.push((async () => {
    if (!req.url().startsWith(BASE)) return;
    const res = await req.response();
    const status = res ? res.status() : 0;
    const ms = Math.round(req.timing().responseEnd);
    const r = `${req.method()} ${route(req.url())}`;
    if (status === 403) {
      add({ kind: "forbidden-call", key: r, title: `Pages call ${r} and get 403 for this role`,
            detail: `${r} -> 403 on ${page}. The page should not call it for this role, or should say why the data is missing.` });
    } else if (status >= 400 && !r.endsWith("/favicon.ico")) {
      add({ kind: "http-error", key: `${status}|${r}`, title: `${r} returns ${status}`, detail: `${r} -> ${status} on ${page}` });
    }
    if (ms > SLOW_MS && !route(req.url()).startsWith("/ui/")) {
      add({ kind: "slow", key: r, ms, title: `${r} takes over ${SLOW_MS} ms (${ms} ms seen)`, detail: `${r} -> ${status} in ${ms} ms on ${page}` });
    }
  })()));

  let loaded = true;
  try {
    await p.goto(url, { waitUntil: "load", timeout: 45000 });
    await p.waitForTimeout(SETTLE_MS);
  } catch (e) {
    loaded = false;
    add({ kind: "request-failed", key: `navigate|${page}`, title: `${page}: page doesn't load`, detail: firstLine(e.message) });
  }
  if (loaded) {
    if (await p.evaluate(() => { const g = document.getElementById("login-gate"); return !!g && getComputedStyle(g).display !== "none"; })) {
      add({ kind: "signed-out", key: page, title: `${page}: shows the sign-in screen with a valid token`, detail: url });
    }
    if (viewport === "phone" && await p.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)) {
      const w = await p.evaluate(() => document.documentElement.scrollWidth);
      add({ kind: "overflow", key: page, title: `${page}: scrolls sideways at phone width (390 px)`, detail: `content is ${w} px wide at a 390 px viewport` });
    }
    if (a11y) {
      try {
        await p.addScriptTag({ path: require.resolve("axe-core/axe.min.js") });
        const v = await p.evaluate((tags) => window.axe.run(document, { runOnly: { type: "tag", values: tags }, resultTypes: ["violations"] }), A11Y_TAGS);
        for (const x of v.violations.filter((x) => ["serious", "critical"].includes(x.impact))) {
          add({ kind: "a11y", key: `${page}|${x.id}`, title: `${page}: ${x.help} (${x.id}, ${x.impact})`,
                detail: `${x.help} - ${x.helpUrl}\n${x.nodes.length} element(s), e.g.:\n${x.nodes.slice(0, 3).map((n) => n.html.slice(0, 200)).join("\n")}` });
        }
      } catch (e) {
        loaded = false;  // the run didn't check everything, so it mustn't close anything
        console.error(`axe failed on ${page}: ${firstLine(e.message)}`);
      }
    }
    await p.screenshot({ path: path.join(OUT, `${page.replace(/\W+/g, "_")}-${viewport}.png`), fullPage: true });
  }
  await Promise.all(pending);
  await p.close();
  return loaded;
}

// The Cx reports are served HTML, not ui/ pages: open them with the token
// as a header, in a context of their own.
async function cxReports(browser, token) {
  const h = { Authorization: `Bearer ${token}` };
  const list = await (await fetch(`${BASE}/cx/projects`, { headers: h })).json();
  const projects = (Array.isArray(list) ? list : list.projects || []).filter((p) => !p.archived_at && p.status !== "archived");
  const proj = projects.find((p) => p.name === PROJECT) || projects[0];
  if (!proj) return [];
  const urls = [[`Cx project report (${proj.name})`, `${BASE}/cx/projects/${proj.id}/report?format=html`]];
  const rep = await (await fetch(`${BASE}/cx/projects/${proj.id}/report?format=json`, { headers: h })).json().catch(() => ({}));
  const rid = rep?.latest_run?.run?.id;
  if (rid) urls.push([`Cx run report (${proj.name})`, `${BASE}/cx/runs/${rid}/report?format=html`]);
  // bypassCSP: the reports send default-src 'none' (good), which would also block axe.
  const ctx = await browser.newContext({ viewport: VIEWPORTS.desktop, extraHTTPHeaders: h, bypassCSP: true });
  await readOnly(ctx);
  const ok = [];
  for (const [name, url] of urls) ok.push(await checkPage(ctx, url, name, "desktop", { a11y: true }));
  await ctx.close();
  return ok;
}

function gh(args, input) {
  const repo = process.env.LIVE_CHECK_REPO ? ["-R", process.env.LIVE_CHECK_REPO] : [];
  return execFileSync("gh", [...args, ...repo], { input, encoding: "utf8", stdio: ["pipe", "pipe", "inherit"] });
}

function sync(findings, complete, run) {
  if (DRY) return console.log("\n--dry-run: no issues changed.");
  execFileSync("gh", ["label", "create", LABEL, "--color", "5319e7", "--force",
    "--description", "Found by tools/live-check against the live install",
    ...(process.env.LIVE_CHECK_REPO ? ["-R", process.env.LIVE_CHECK_REPO] : [])], { stdio: "ignore" });
  // REST, not `gh issue list --label`: that goes through the search index,
  // which lags minutes behind, so a quick second run filed duplicates.
  const repo = process.env.LIVE_CHECK_REPO || "{owner}/{repo}";
  const issues = execFileSync("gh", ["api", "--paginate", `repos/${repo}/issues?labels=${LABEL}&state=all&per_page=100`,
    "-q", ".[] | {number, state: (.state | ascii_upcase), body}"], { encoding: "utf8" })
    .split("\n").filter(Boolean).map((l) => JSON.parse(l));
  for (const a of plan(findings, issues, { complete })) {
    if (a.op === "create") {
      const url = gh(["issue", "create", "--title", `[live-check] ${a.f.title}`, "--label", a.f.label, "--label", LABEL, "--body-file", "-"],
        issueBody(a.f, run)).trim();
      console.log(`created ${url}`);
    } else if (a.op === "close") {
      gh(["issue", "close", String(a.number), "--comment", `Not seen by live-check at ${run.at} (a complete run as a ${run.role}). Closing; it reopens if it comes back.`]);
      console.log(`closed #${a.number}`);
    } else {
      if (a.op === "reopen") gh(["issue", "reopen", String(a.number), "--comment", `Seen again by live-check at ${run.at}.`]);
      gh(["issue", "edit", String(a.number), "--body-file", "-"], issueBody(a.f, run, a.body));
      console.log(`${a.op === "reopen" ? "reopened" : "updated"} #${a.number}`);
    }
  }
}

mkdirSync(OUT, { recursive: true });
const run = { at: new Date().toISOString().replace(/\.\d+Z$/, "Z"), base: BASE, role: "?", slowMs: SLOW_MS };
const auth = await login();
let complete = false;
if (auth.error) {
  raw.push({ kind: "login", key: String(auth.error), title: `can't sign in: POST /auth/login returns ${auth.error}`, page: "login", detail: `POST ${BASE}/auth/login -> ${auth.error}` });
} else {
  run.role = auth.role;
  const browser = await chromium.launch(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: "chrome" });
  const ok = [];
  for (const [vp, size] of Object.entries(VIEWPORTS)) {
    const ctx = await browser.newContext({ viewport: size });
    await ctx.addInitScript((t) => localStorage.setItem("td_token", t), auth.token);
    await readOnly(ctx);
    for (const [file, name] of Object.entries(PAGES)) {
      ok.push(await checkPage(ctx, `${BASE}/ui/${file}.html`, `${name} (${file}.html)`, vp, { a11y: vp === "desktop" }));
    }
    await ctx.close();
  }
  ok.push(...await cxReports(browser, auth.token));
  await browser.close();
  complete = ok.length > 0 && ok.every(Boolean);
}

const findings = merge(raw);
writeFileSync(path.join(OUT, "findings.json"), JSON.stringify({ run, complete, blocked: [...blocked], findings }, null, 2));
console.log(`live-check ${run.at} against ${BASE} as ${run.role}: ${findings.length} finding(s)${complete ? "" : " (incomplete run: nothing will be closed)"}`);
for (const f of findings) console.log(`  [${f.label}] ${f.title}  (${f.pages.join(", ")})`);
if (blocked.size) console.log(`blocked writes (read-only guard): ${[...blocked].join(", ")}`);
sync(findings, complete, run);
process.exitCode = findings.length ? 1 : 0;

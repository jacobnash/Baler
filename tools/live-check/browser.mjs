// R3: the browser pass (the original live-check): every UI page and the Cx
// reports in a real browser, reporting uncaught errors, console errors,
// failed and 4xx/5xx requests, 403s the role shouldn't trigger, a sign-in
// screen despite a valid token, slow requests and axe WCAG 2.1 A/AA
// violations. layout.mjs reuses checkPage for its phone pass.

import { createRequire } from "node:module";
import path from "node:path";
import { route } from "./findings.mjs";

const require = createRequire(import.meta.url);
export const PAGES = { index: "Equipment", his: "Query", alarms: "Alarms", derivations: "Derivations",
                       cx: "Commissioning", connectors: "Connectors", ops: "Ops" };
export const DESKTOP = { width: 1440, height: 900 };
const A11Y_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
const firstLine = (s) => String(s).split("\n")[0].slice(0, 160);

// Playwright is loaded only when a browser module runs, so the API-only
// modules (and their tests) need no npm install.
export async function launch() {
  const { chromium } = await import("playwright-core");
  if (process.env.CHROME_PATH) return chromium.launch({ executablePath: process.env.CHROME_PATH });
  try {
    return await chromium.launch({ channel: "chrome" });
  } catch (e) {
    // No system Chrome: Playwright's own Chromium, if
    // `npx playwright-core install chromium` has fetched it.
    try {
      return await chromium.launch();
    } catch {
      throw e;
    }
  }
}

export async function devices() {
  return (await import("playwright-core")).devices;
}

// ctx: {base, out, settleMs, slowMs, raw}. Returns whether the page loaded
// and was fully checked (an incomplete check must not close issues).
// extra(p, add): more checks on the loaded page (layout.mjs).
export async function checkPage(ctx, browserCtx, url, page, viewport, { a11y, scope, extra } = {}) {
  const { base, out, settleMs, slowMs, raw } = ctx;
  const p = await browserCtx.newPage();
  const pending = [];
  const add = (r) => raw.push({ page, viewport, scope, ...r });
  p.on("pageerror", (e) => add({ kind: "page-error", key: `${page}|${firstLine(e.message)}`,
    title: `${page}: uncaught error: ${firstLine(e.message)}`, detail: (e.stack || e.message).split("\n").slice(0, 5).join("\n") }));
  p.on("console", (m) => {
    const t = m.text();
    if (m.type() !== "error" || t.startsWith("Failed to load resource")) return;  // HTTP errors are reported with their URL below
    add({ kind: "console-error", key: `${page}|${t.slice(0, 120)}`, title: `${page}: console error: ${t.slice(0, 90)}`, detail: t.slice(0, 600) });
  });
  p.on("requestfailed", (req) => {
    const err = req.failure()?.errorText || "";
    if (!req.url().startsWith(base) || err.includes("BLOCKED_BY_CLIENT") || err.includes("ERR_ABORTED")) return;
    const r = `${req.method()} ${route(req.url())}`;
    add({ kind: "request-failed", key: r, title: `${r} fails: ${err}`, detail: `${r} -> ${err}` });
  });
  p.on("requestfinished", (req) => pending.push((async () => {
    if (!req.url().startsWith(base)) return;
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
    if (ms > slowMs && !route(req.url()).startsWith("/ui/")) {
      add({ kind: "slow", key: r, ms, title: `${r} takes over ${slowMs} ms (${ms} ms seen)`, detail: `${r} -> ${status} in ${ms} ms on ${page}` });
    }
  })()));

  let loaded = true;
  try {
    await p.goto(url, { waitUntil: "load", timeout: 45000 });
    await p.waitForTimeout(settleMs);
  } catch (e) {
    loaded = false;
    add({ kind: "request-failed", key: `navigate|${page}`, title: `${page}: page doesn't load`, detail: firstLine(e.message) });
  }
  if (loaded) {
    if (await p.evaluate(() => { const g = document.getElementById("login-gate"); return !!g && getComputedStyle(g).display !== "none"; })) {
      add({ kind: "signed-out", key: page, title: `${page}: shows the sign-in screen with a valid token`, detail: url });
    }
    if (viewport === "phone" && await p.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)) {
      const w = await p.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
      add({ kind: "overflow", key: page, title: `${page}: scrolls sideways at phone width (390 px)`, detail: `content is ${w[0]} px wide at a ${w[1]} px viewport` });
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
    if (extra) loaded = (await extra(p, add)) !== false && loaded;
    await p.screenshot({ path: path.join(out, `${page.replace(/\W+/g, "_")}-${viewport}.png`), fullPage: true });
  }
  await Promise.all(pending);
  await p.close();
  return loaded;
}

// The Cx reports are served HTML, not ui/ pages: open them with the token
// as a header, in a context of their own.
async function cxReports(ctx, browser, token) {
  const { base, guard } = ctx;
  const h = { Authorization: `Bearer ${token}` };
  const list = await (await fetch(`${base}/cx/projects`, { headers: h })).json();
  const projects = (Array.isArray(list) ? list : list.projects || []).filter((p) => !p.archived_at && p.status !== "archived");
  const proj = projects.find((p) => p.name === ctx.project) || projects[0];
  if (!proj) return [];
  const urls = [[`Cx project report (${proj.name})`, `${base}/cx/projects/${proj.id}/report?format=html`]];
  const rep = await (await fetch(`${base}/cx/projects/${proj.id}/report?format=json`, { headers: h })).json().catch(() => ({}));
  const rid = rep?.latest_run?.run?.id;
  if (rid) urls.push([`Cx run report (${proj.name})`, `${base}/cx/runs/${rid}/report?format=html`]);
  // bypassCSP: the reports send default-src 'none' (good), which would also block axe.
  const bctx = await browser.newContext({ viewport: DESKTOP, extraHTTPHeaders: h, bypassCSP: true });
  await guard.installBrowser(bctx);
  const ok = [];
  for (const [name, url] of urls) ok.push(await checkPage(ctx, bctx, url, name, "desktop", { a11y: true, scope: "browser" }));
  await bctx.close();
  return ok;
}

export async function run(ctx) {
  const { base, token, guard } = ctx;
  const browser = await ctx.browser();
  const bctx = await browser.newContext({ viewport: DESKTOP });
  await bctx.addInitScript((t) => localStorage.setItem("td_token", t), token);
  await guard.installBrowser(bctx);
  const ok = [];
  for (const [file, name] of Object.entries(PAGES)) {
    ok.push(await checkPage(ctx, bctx, `${base}/ui/${file}.html`, `${name} (${file}.html)`, "desktop", { a11y: true, scope: "browser" }));
  }
  await bctx.close();
  ok.push(...await cxReports(ctx, browser, token));
  const complete = ok.length > 0 && ok.every(Boolean);
  const mine = ctx.raw.filter((r) => r.scope === "browser");
  return {
    scope: "browser", complete, raw: [],  // checkPage pushed straight into ctx.raw
    checks: [{ module: "browser", name: `${ok.length} pages and reports (desktop, axe)`, status: !complete ? "error" : mine.length ? "fail" : "pass",
               value: `${mine.length} observation(s)`, detail: complete ? "" : "a page or axe didn't finish; nothing in this scope is closed" }],
  };
}

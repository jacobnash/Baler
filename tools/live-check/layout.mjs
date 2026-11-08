// R7: phone layout. Every ui/*.html carries the viewport meta tag (#177;
// without it a phone lays the page out 980 px wide and shrinks it, which no
// overflow check can see), and on a phone profile (iPhone 13, 390 px) no
// page scrolls sideways and the Equipment detail pane is at least
// min_detail_px wide once a point is selected (#160).

import { readdirSync } from "node:fs";
import path from "node:path";
import { PAGES, checkPage, devices } from "./browser.mjs";

export const UI_DIR = path.join(import.meta.dirname, "..", "..", "ui");
const ATTR = /([a-zA-Z-]+)\s*=\s*("([^"]*)"|'([^']*)')/g;

// null when html has <meta name="viewport" content="width=device-width,
// initial-scale=1"> (any attribute order, spacing, extra keys), else why not.
export function viewportProblem(html) {
  const metas = String(html).match(/<meta\b[^>]*>/gi) || [];
  for (const m of metas) {
    const a = {};
    for (const x of m.matchAll(ATTR)) a[x[1].toLowerCase()] = x[3] ?? x[4];
    if ((a.name || "").toLowerCase() !== "viewport") continue;
    const kv = Object.fromEntries((a.content || "").split(/[,;]/).map((s) => s.split("=").map((t) => t.trim().toLowerCase())).filter((p) => p[0]));
    if (kv.width !== "device-width") return `viewport meta has width=${kv.width ?? "(none)"}, not device-width`;
    if (kv["initial-scale"] !== "1" && kv["initial-scale"] !== "1.0") return `viewport meta has initial-scale=${kv["initial-scale"] ?? "(none)"}, not 1`;
    return null;
  }
  return "no <meta name=\"viewport\"> tag";
}

export function evaluateViewports(results) {
  // results: [{file, problem}] -> one finding for all pages, like #177.
  const bad = results.filter((r) => r.problem);
  const check = { module: "layout", name: "viewport meta on every ui/*.html", status: bad.length ? "fail" : "pass",
                  value: `${results.length - bad.length}/${results.length}`, detail: bad.map((b) => b.file).join(", ") || "all pages" };
  if (!bad.length) return { checks: [check], raw: [] };
  return {
    checks: [check],
    raw: [{ kind: "viewport-meta", key: "ui", scope: "layout", refs: [177], viewport: "phone",
            title: `${bad.length} UI page(s) lack the viewport meta tag`,
            detail: bad.map((b) => `${b.file}: ${b.problem}`).join("\n"),
            rows: [["Pages", bad.map((b) => b.file).join(", ")],
                   ["Expected", '`<meta name="viewport" content="width=device-width, initial-scale=1">`']] }],
  };
}

export async function run(ctx) {
  const { base, token, guard, budgets } = ctx;
  const minDetail = budgets.layout?.min_detail_px ?? 300;
  const checks = [];
  const raw = [];
  let complete = true;

  // 1. the tag, on what the install actually serves
  const results = [];
  for (const file of readdirSync(UI_DIR).filter((f) => f.endsWith(".html")).sort()) {
    try {
      const res = await fetch(`${base}/ui/${file}`);
      results.push({ file, problem: res.ok ? viewportProblem(await res.text()) : null, status: res.status });
      if (!res.ok) { complete = false; checks.push({ module: "layout", name: `GET /ui/${file}`, status: "error", detail: `answered ${res.status}` }); }
    } catch (e) {
      complete = false;
      checks.push({ module: "layout", name: `GET /ui/${file}`, status: "error", detail: e.message });
    }
  }
  const vp = evaluateViewports(results.filter((r) => r.status < 400));
  checks.push(...vp.checks);
  raw.push(...vp.raw);

  // 2. a phone, page by page (also catches errors only the phone layout hits)
  const browser = await ctx.browser();
  const phone = (await devices())["iPhone 13"];
  const bctx = await browser.newContext({ ...phone });
  await bctx.addInitScript((t) => localStorage.setItem("td_token", t), token);
  await guard.installBrowser(bctx);
  let pane = null;
  for (const [file, name] of Object.entries(PAGES)) {
    const extra = file !== "index" ? undefined : async (p, add) => {
      pane = await detailPaneWidth(p);
      if (pane.error) return false;
      if (pane.skip) return true;
      if (pane.width < minDetail) {
        add({ kind: "narrow-pane", key: "index|#detail", refs: [160],
              title: `Equipment (index.html): the detail pane is ${Math.round(pane.width)} px wide on a phone`,
              detail: `after selecting "${pane.point}", #detail is ${Math.round(pane.width)} px wide at ${phone.viewport.width} px (minimum ${minDetail} px)` });
      }
    };
    complete = (await checkPage(ctx, bctx, `${base}/ui/${file}.html`, `${name} (${file}.html)`, "phone", { scope: "layout", extra })) && complete;
  }
  await bctx.close();
  const mine = ctx.raw.filter((r) => r.scope === "layout");
  const overflow = mine.filter((r) => r.kind === "overflow");
  checks.push({ module: "layout", name: `no sideways scroll at ${phone.viewport.width} px (iPhone 13)`, status: overflow.length ? "fail" : "pass",
                value: `${Object.keys(PAGES).length - overflow.length}/${Object.keys(PAGES).length}`, detail: overflow.map((o) => o.page).join(", ") || "all pages" });
  if (pane?.error || pane?.skip) {
    checks.push({ module: "layout", name: "Equipment detail pane width", status: pane.error ? "error" : "skip", detail: pane.error || pane.skip });
  } else if (pane) {
    checks.push({ module: "layout", name: "Equipment detail pane width", status: pane.width < minDetail ? "fail" : "pass",
                  value: `${Math.round(pane.width)} px`, budget: `≥ ${minDetail} px`, detail: `after selecting ${pane.point}` });
  }
  const other = mine.filter((r) => !["overflow", "narrow-pane"].includes(r.kind));
  checks.push({ module: "layout", name: "phone pass: errors and failed requests", status: other.length ? "fail" : "pass", value: `${other.length} observation(s)` });
  return { scope: "layout", complete, checks, raw };
}

// Open the first equipment, select its first point (a read: points and
// history are GETs), and measure #detail.
async function detailPaneWidth(p) {
  try {
    const opened = await p.evaluate(() => {
      const d = document.querySelector("#tree details[data-equip]");
      if (!d) return false;
      // the equipment sits inside closed group <details>: open the way down to it
      for (let e = d; e; e = e.parentElement) if (e.tagName === "DETAILS") e.open = true;
      return true;
    });
    if (!opened) return { skip: "no equipment in the tree to select a point from" };
    const btn = p.locator("#tree details[data-equip] ul li button").first();
    await btn.waitFor({ timeout: 15000 });
    const point = (await btn.textContent())?.trim().slice(0, 80);
    await btn.click();
    await p.waitForTimeout(2000);
    const width = await p.evaluate(() => document.getElementById("detail")?.getBoundingClientRect().width ?? 0);
    return { width, point };
  } catch (e) {
    return { error: `couldn't select a point: ${String(e.message).split("\n")[0]}` };
  }
}

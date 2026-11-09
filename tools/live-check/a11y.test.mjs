// The browser pass's axe check (browser.mjs checkPage: WCAG 2.1 A/AA,
// serious and critical) on the Connectors and Ops pages, served straight
// from ui/ against a stub API, so what the post-deploy cold pass would file
// fails CI first. Regression for the cold pass on 46f92c1:
// label-content-name-mismatch on the Connectors node buttons (and the
// admin-only "Add connector…"/"Recheck login" buttons), color-contrast on
// the Ops "off" job cards.
//
// Needs the npm install (playwright-core, axe-core) and Chrome or
// CHROME_PATH. Without them it skips, unless LIVE_CHECK_REQUIRE_BROWSER=1
// (CI sets it), which turns a missing browser into a failure.
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync } from "node:fs";
import http from "node:http";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

const UI = path.join(import.meta.dirname, "..", "..", "ui");
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };
const b64 = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
const NOW = new Date().toISOString();

// Every state the pages draw: an online and an offline node; pending,
// ignored and connected devices, one with a working factory login; an OK
// and a failing connector; every ops card state, including "off" (a job
// this install doesn't run - the card that failed contrast).
const card = (id, state, layer = "building") => ({ id, label: id, state, detail: `${id} detail`, layer, service: id });
const status = (empty) => ({
  summary: "degraded",
  building: { last_ingest_at: empty ? null : NOW, points_seen_last_hour: empty ? 0 : 400,
              cards: ["ok", "quiet", "degraded", "down"].map((s) => card(`building-${s}`, s)) },
  attention: [card("faults", "degraded")],
  platform: [card("historian", "ok", "platform"), card("live_bus", "down", "platform")],
  jobs: [card("backups", "off", "jobs"), card("compression", "ok", "jobs"), card("restore_drill", "off", "jobs")],
  tools: [card("docs", "ok", "tools")],
  alerts: [{ title: "Ingest stalled", detail: "no readings", opened_at: NOW, severity: "critical" }],
  processes: [{ service: "ingest", errors: 2, last_error_at: NOW, last_error: "boom", starts: 3 }],
});
const NODES = [
  { site_id: "s1", instance_id: "local", label: "nash-srv", kind: "local", online: true, last_seen: NOW, pending_devices: 1, announced: 1, announced_online: 1 },
  { site_id: "s2", instance_id: "pi1", label: "pi-1", kind: "remote", site_name: "Home", online: false, last_seen: NOW },
];
const ACTIONS = {
  list_devices: [
    { id: "d1", protocol: "bacnet", address: "10.0.0.5", device_instance: 5, status: "pending", default_credential_flag: true, last_seen: NOW },
    { id: "d2", protocol: "modbus", host: "10.0.0.6", port: 502, status: "ignored", last_seen: NOW },
    { id: "d3", protocol: "bacnet", address: "10.0.0.7", device_instance: 7, status: "provisioned", last_seen: NOW },
  ],
  list_connections: [
    { id: "c1", device_address: "10.0.0.7", device_instance: 7, topic_prefix: "fbf/home/ahu-1", poll_interval: 5, points: [{ label: "zat" }],
      poll_status: { state: "ok", last_poll_at: NOW, last_value_at: NOW } },
    { id: "c2", device_address: "10.0.0.8", device_instance: 8, topic_prefix: "fbf/home/vav-2", poll_interval: 5, points: [{ label: "zat" }],
      poll_status: { state: "error", last_error: "timeout", last_poll_at: NOW }, credential: { username: "u" } },
  ],
  modbus_list_connections: [],
  list_announced: [{ name: "ahu", address: "10.0.0.7", online: true, last_seen: NOW, services: [{ kind: "bacnet", instance: 7 }] }],
};

function stub({ role, empty }) {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://x");
    const json = (o) => { res.writeHead(200, { "content-type": "application/json" }); res.end(JSON.stringify(o)); };
    if (url.pathname.startsWith("/ui/")) {
      const file = path.join(UI, path.basename(url.pathname));
      if (!existsSync(file)) { res.writeHead(404); return res.end(); }
      res.writeHead(200, { "content-type": TYPES[path.extname(file)] || "text/plain" });
      return res.end(readFileSync(file));
    }
    if (url.pathname === "/auth/me") return json({ email: `${role}@example.test`, role, kind: "session" });
    if (url.pathname === "/auth/password-reset/config") return json({ self_service: false });
    if (url.pathname === "/ops/status") return json(status(empty));
    if (url.pathname === "/ops/edges") return json(empty ? [] : NODES);
    const action = /\/actions\/(\w+)$/.exec(url.pathname);
    if (action) return json({ result: ACTIONS[action[1]] ?? [] });
    return json([]);
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({ server, base: `http://127.0.0.1:${server.address().port}` })));
}

async function loadBrowser() {
  try {
    createRequire(import.meta.url).resolve("axe-core/axe.min.js");
    const mod = await import("./browser.mjs");
    return { mod, browser: await mod.launch() };
  } catch (e) {
    if (process.env.LIVE_CHECK_REQUIRE_BROWSER === "1") throw e;
    return { skip: `needs npm install and Chrome (${String(e.message).split("\n")[0]})` };
  }
}

const { mod, browser, skip } = await loadBrowser();

// The live pass signs in as an operator in the browser's default (light)
// scheme; admins see more buttons, and dark mode redefines the tokens.
const RUNS = [
  { role: "operator", colorScheme: "light", empty: false },
  { role: "operator", colorScheme: "light", empty: true },
  { role: "operator", colorScheme: "dark", empty: false },
  { role: "admin", colorScheme: "light", empty: false },
];

for (const run of RUNS) {
  test(`axe on Connectors and Ops: ${run.role}, ${run.colorScheme}${run.empty ? ", no data yet" : ""}`, { skip }, async () => {
    const { server, base } = await stub(run);
    const bctx = await browser.newContext({ viewport: mod.DESKTOP, colorScheme: run.colorScheme });
    await bctx.addInitScript((t) => localStorage.setItem("td_token", t), `${b64({ alg: "HS256" })}.${b64({ role: run.role })}.sig`);
    const ctx = { base, out: mkdtempSync(path.join(os.tmpdir(), "live-check-a11y-")), settleMs: 1000, slowMs: 60000, raw: [] };
    try {
      for (const file of ["connectors", "ops"]) {
        assert.ok(await mod.checkPage(ctx, bctx, `${base}/ui/${file}.html`, mod.PAGES[file], "desktop", { a11y: true, scope: "browser" }),
                  `${file}.html didn't load or axe didn't finish`);
      }
      const a11y = ctx.raw.filter((r) => r.kind === "a11y");
      assert.deepEqual(a11y.map((r) => r.title), [], a11y.map((r) => r.detail).join("\n\n"));
      assert.deepEqual(ctx.raw.filter((r) => r.kind === "page-error").map((r) => r.title), []);
    } finally {
      await bctx.close();
      server.close();
    }
  });
}

test.after(() => browser?.close());

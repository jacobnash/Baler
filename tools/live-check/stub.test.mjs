// End to end against a stub install (no browser, no npm install, no
// network): live-check.mjs --dry-run runs the API modules against a local
// HTTP server that answers like Baler, and the test checks the exit code,
// the summary, the artifact, and that the stub saw nothing but reads.
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { chmodSync, existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

const fx = (name) => readFileSync(new URL(`./fixtures/${name}`, import.meta.url), "utf8");
const b64 = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
const TOKEN = `${b64({ alg: "HS256" })}.${b64({ role: "operator" })}.sig`;

function stub({ freshness, slowFaults = 0 }) {
  const seen = [];
  const server = http.createServer((req, res) => {
    seen.push(`${req.method} ${req.url.split("?")[0]}`);
    const json = (o, status = 200) => { res.writeHead(status, { "content-type": "application/json", date: new Date("2026-11-07T22:40:10Z").toUTCString() }); res.end(JSON.stringify(o)); };
    const url = new URL(req.url, "http://x");
    if (req.method === "POST" && url.pathname === "/auth/login") return json({ token: TOKEN });
    if (req.method !== "GET" && !/\/actions\/(list_announced|list_connections|modbus_list_connections)$/.test(url.pathname)) return json({ error: "write!" }, 418);
    if (url.pathname === "/cx/projects") return json([{ id: "p1", name: "Home Edge Pilot" }]);
    if (url.pathname === "/fault/faults") return setTimeout(() => json([]), slowFaults);
    if (url.pathname === "/ops/freshness") return json(JSON.parse(fx(freshness)));
    if (url.pathname === "/ops/metrics") { res.writeHead(200, { "content-type": "text/plain; version=0.0.4" }); return res.end("baler_ingest_newest_reading_age_seconds 3\nbaler_listener_queue_oldest_age_seconds 0.2\n"); }
    if (url.pathname === "/ops/edges") return json(JSON.parse(fx("edges-ok.json")));
    if (url.pathname === "/ops/gateway-errors") return json({ available: true, count: 0, by_status: {}, by_route: [], first: null, last: null, samples: [] });
    const announce = JSON.parse(fx("announce-ok.json"));
    if (url.pathname.endsWith("/list_announced")) return json({ result: announce.devices });
    if (url.pathname.endsWith("/modbus_list_connections")) return json({ result: announce.connections.modbus });
    if (url.pathname.endsWith("/list_connections")) return json({ result: announce.connections.bacnet });
    return json({ rows: [] });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({ server, seen, base: `http://127.0.0.1:${server.address().port}` })));
}

function liveCheck(base, args, extraEnv = {}, { dry = true } = {}) {
  const dir = mkdtempSync(path.join(os.tmpdir(), "live-check-"));
  const envFile = path.join(dir, "test.env");
  writeFileSync(envFile, `BALER_TEST_URL=${base}\nBALER_TEST_EMAIL=op@example.test\nBALER_TEST_PASSWORD=not-a-real-secret\n`);
  const env = { ...process.env, BALER_TEST_ENV: envFile, LIVE_CHECK_OUT: dir, LIVE_CHECK_SHA: "0123456789abcdef", GITHUB_STEP_SUMMARY: path.join(dir, "summary.md"), ...extraEnv };
  for (const k of ["BALER_TEST_URL", "BALER_TEST_EMAIL", "BALER_TEST_PASSWORD"]) delete env[k];
  return new Promise((resolve) => execFile(process.execPath, [path.join(import.meta.dirname, "live-check.mjs"), ...(dry ? ["--dry-run"] : []), ...args], { env },
    (err, stdout, stderr) => resolve({ code: err ? err.code : 0, stdout, stderr, dir })));
}

test("dry run against a healthy stub: every API check passes, only reads are sent, artifact merges phases", async () => {
  const { server, seen, base } = await stub({ freshness: "freshness-home.json" });
  try {
    const args = ["--only", "perf,dataflow,edges,deploy", "--deploy-start", "2026-11-07T22:00:00Z", "--deploy-end", "2026-11-07T22:03:00Z"];
    const cold = await liveCheck(base, ["--phase", "cold", ...args]);
    assert.equal(cold.code, 0, cold.stdout + cold.stderr);
    const warm = await liveCheck(base, ["--phase", "warm", ...args], { LIVE_CHECK_OUT: cold.dir, GITHUB_STEP_SUMMARY: path.join(cold.dir, "summary.md") });
    assert.equal(warm.code, 0, warm.stdout + warm.stderr);
    assert.ok(seen.every((r) => r.startsWith("GET ") || r === "POST /auth/login" || /\/actions\/(list_announced|list_connections|modbus_list_connections)$/.test(r)), seen.join("\n"));
    const art = JSON.parse(readFileSync(path.join(cold.dir, "live-verify-0123456789ab.json"), "utf8"));
    assert.deepEqual(Object.keys(art.phases), ["cold", "warm"]);
    assert.equal(art.phases.cold.only_reads, true);
    assert.deepEqual(art.phases.warm.ran, ["dataflow", "deploy", "edges", "login", "perf:warm"]);
    assert.equal(art.phases.cold.checks.filter((c) => c.module === "perf").length, 6);
    for (const ph of ["cold", "warm"]) {
      const h = art.phases[ph].perf_host;
      assert.equal(h.phase, ph);
      for (const k of ["load_1m", "load_5m", "load_15m", "nproc"]) assert.equal(typeof h.before[k], "number", `${ph} ${k}`);
      assert.equal(typeof h.after.load_1m, "number");
    }
    const summary = readFileSync(path.join(cold.dir, "summary.md"), "utf8");
    assert.match(summary, /live-check \(cold phase\)/);
    assert.match(summary, /live-check \(warm phase\)/);
    assert.match(summary, /\| perf \(warm\) \| GET \/fault\/faults \| pass \|/);
    assert.match(summary, /Runner host `[^`]+` during perf \(cold\), load 1\/5\/15 min: [\d.]+ \/ [\d.]+ \/ [\d.]+, nproc \d+ before/);
    assert.doesNotMatch(summary + JSON.stringify(art), /not-a-real-secret|\.sig/, "no password or token in the outputs");
  } finally {
    server.close();
  }
});

test("dry run against a broken stub: stale modbus points and a slow route fail the run", async () => {
  const { server, base } = await stub({ freshness: "freshness-modbus-stopped.json", slowFaults: 650 });
  try {
    const r = await liveCheck(base, ["--phase", "warm", "--only", "perf,dataflow"], { LIVE_CHECK_BUDGETS: "" });
    assert.equal(r.code, 1);
    assert.match(r.stdout, /\[optimization\] GET \/fault\/faults is over its warm budget/);
    assert.match(r.stdout, /\[bug\] fbf\/home\/modbus\/\* points are stale \(2 of 2\)/);
    assert.match(r.stdout, /--dry-run: no issues changed/);
  } finally {
    server.close();
  }
});

test("missing credentials: exits 2 naming the env file", async () => {
  const r = await liveCheck("", [], {});
  assert.equal(r.code, 2);
  assert.match(r.stderr, /BALER_TEST_URL.*test\.env/s);
});

test("superseded (#260): a newer deploy's marker stops a real run cleanly, with no gh call and the phase kept in the artifact", async () => {
  const { server, base } = await stub({ freshness: "freshness-modbus-stopped.json", slowFaults: 650 });
  const tmp = mkdtempSync(path.join(os.tmpdir(), "live-check-sup-"));
  // Any gh call would leave this file behind: a superseded run makes none.
  const ghLog = path.join(tmp, "gh-called");
  writeFileSync(path.join(tmp, "gh"), `#!/bin/sh\necho "$@" >> ${ghLog}\nexit 1\n`);
  chmodSync(path.join(tmp, "gh"), 0o755);
  writeFileSync(path.join(tmp, "marker"), "fedcba9876543210\n");
  writeFileSync(path.join(tmp, "prev.json"), JSON.stringify({ phases: {} }));
  try {
    const env = { LIVE_CHECK_BUDGETS: "", LIVE_CHECK_MARKER: path.join(tmp, "marker"), LIVE_CHECK_PREVIOUS: path.join(tmp, "prev.json"),
                  PATH: `${tmp}:${process.env.PATH}`, GITHUB_RUN_ID: "", LIVE_CHECK_REPO: "", GITHUB_OUTPUT: path.join(tmp, "out") };
    const r = await liveCheck(base, ["--phase", "warm", "--only", "perf,dataflow"], env, { dry: false });
    assert.equal(r.code, 0, r.stdout + r.stderr);
    assert.match(r.stdout, /superseded: nash-srv now runs fedcba9, not 0123456 .*No issues changed/);
    assert.ok(!existsSync(ghLog), existsSync(ghLog) ? readFileSync(ghLog, "utf8") : "");
    assert.match(readFileSync(path.join(tmp, "out"), "utf8"), /^superseded=true$/m);
    const art = JSON.parse(readFileSync(path.join(r.dir, "live-verify-0123456789ab.json"), "utf8"));
    assert.match(art.phases.warm.run.superseded, /fedcba9/);
    assert.ok(art.phases.warm.findings.length > 0, "the findings are kept, just not filed");
    assert.deepEqual(art.phases.warm.actions, []);
    assert.match(readFileSync(path.join(r.dir, "summary.md"), "utf8"), /\*\*Superseded:\*\*.*superseded, no issue changed/s);
  } finally {
    server.close();
  }
});

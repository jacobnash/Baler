import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { EDGES_PY, Guard, READ_ACTIONS, allowed, pyTuple } from "./guard.mjs";

const BASE = "https://baler.example";
const WRITE_ACTIONS = pyTuple(readFileSync(EDGES_PY, "utf8"), "WRITE_ACTIONS");

test("reads go through: GET, the login (Node only), and every edge READ_ACTION", () => {
  assert.ok(allowed("GET", `${BASE}/ops/status`, BASE));
  assert.ok(allowed("POST", `${BASE}/auth/login`, BASE, { login: true }));
  assert.ok(!allowed("POST", `${BASE}/auth/login`, BASE), "the browser never signs in");
  assert.ok(READ_ACTIONS.includes("list_announced"), "the Connectors page calls it since #176");
  for (const a of READ_ACTIONS) {
    assert.ok(allowed("POST", `${BASE}/ops/edges/local/local/actions/${a}`, BASE), a);
    assert.ok(allowed("POST", `${BASE}/ops/edges/home-gateway/modbus/actions/${a}`, BASE), a);
  }
});

test("every edge write action, and every other write, is refused", () => {
  assert.ok(WRITE_ACTIONS.length > 10 && WRITE_ACTIONS.includes("create_connection"));
  for (const a of WRITE_ACTIONS) assert.ok(!allowed("POST", `${BASE}/ops/edges/local/local/actions/${a}`, BASE), a);
  assert.ok(!allowed("POST", `${BASE}/ops/edges/local/local/actions/list_announced_and_more`, BASE));
  assert.ok(!allowed("PATCH", `${BASE}/ops/edges/local/local/actions/list_devices`, BASE));
  assert.ok(!allowed("POST", `https://elsewhere.example/ops/edges/local/local/actions/list_devices`, BASE));
  assert.ok(!allowed("POST", `${BASE}.evil/ops/edges/local/local/actions/list_devices`, BASE));
  for (const m of ["POST", "PUT", "PATCH", "DELETE"]) assert.ok(!allowed(m, `${BASE}/fault/faults/x/ack`, BASE), m);
});

test("the gateway's viewer row for edge actions names the same reads as edges.py", () => {
  const policy = readFileSync(path.join(import.meta.dirname, "..", "..", "gateway", "njs", "policy.js"), "utf8");
  const row = /actions\\\/\(([a-z_|]+)\)\$\/, "devices", "viewer"/.exec(policy);
  assert.ok(row, "policy.js viewer row for edge actions");
  assert.deepEqual(row[1].split("|").sort(), [...READ_ACTIONS].sort());
});

test("the fetch guard refuses a write before sending it, and logs what it sent", async () => {
  const sent = [];
  const real = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => { sent.push(`${init.method || "GET"} ${url}`); return new Response("{}"); };
  try {
    const g = new Guard(BASE);
    g.installFetch();
    await fetch(`${BASE}/ops/status`);
    await fetch(`${BASE}/ops/edges/local/local/actions/list_announced`, { method: "POST" });
    await assert.rejects(fetch(`${BASE}/ops/edges/local/local/actions/delete_connection`, { method: "POST" }), /write guard/);
    await assert.rejects(fetch(`${BASE}/ops/sources/abc`, { method: "DELETE" }), /write guard/);
    assert.deepEqual(sent, [`GET ${BASE}/ops/status`, `POST ${BASE}/ops/edges/local/local/actions/list_announced`]);
    assert.deepEqual([...g.blocked], ["POST /ops/edges/local/local/actions/delete_connection", "DELETE /ops/sources/abc"]);
    assert.ok(g.onlyReads());
  } finally {
    globalThis.fetch = real;
  }
});

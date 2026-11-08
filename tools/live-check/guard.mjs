// The write guard. live-check signs in with an account that can write, and a
// check must never change the install it checks, so every request it makes,
// from Node or from the browser, goes through allowed(): GET/HEAD/OPTIONS,
// the login, and the Connectors page's named edge-node reads (POSTs by
// design: they travel the same action channel as writes). Everything else is
// refused before it leaves the machine, and every request to the install is
// logged, so a run's artifact shows exactly what it sent.

import { readFileSync } from "node:fs";
import path from "node:path";
import { route } from "./findings.mjs";

// Which edge actions are reads is decided once, in src/baler/edges.py
// (READ_ACTIONS; ops_api refuses the rest to non-admins, and policy.js's
// viewer row lists the same names). The guard reads that tuple from the
// checkout instead of keeping its own copy: a hand-kept copy missed
// list_announced when the Connectors page started calling it (#176), and the
// guard blocked a read. No list found is an error, never "allow everything".
export const EDGES_PY = path.join(import.meta.dirname, "..", "..", "src", "baler", "edges.py");

export function pyTuple(source, name) {
  const m = new RegExp(`^${name}\\s*=\\s*\\(([^)]*)\\)`, "m").exec(source);
  if (!m) throw new Error(`live-check: no ${name} = (...) in edges.py`);
  const names = [...m[1].matchAll(/"([a-z_]+)"/g)].map((x) => x[1]);
  if (!names.length) throw new Error(`live-check: ${name} in edges.py is empty`);
  return names;
}

export const READ_ACTIONS = pyTuple(readFileSync(EDGES_PY, "utf8"), "READ_ACTIONS");
export const READ_POSTS = new RegExp(`^/ops/edges/[^/]+/[^/]+/actions/(${READ_ACTIONS.join("|")})$`);
const SAFE = ["GET", "HEAD", "OPTIONS"];

// login: whether POST /auth/login counts (Node only; the browser never signs in).
export function allowed(method, url, base, { login = false } = {}) {
  const m = String(method || "GET").toUpperCase();
  if (SAFE.includes(m)) return true;
  if (m !== "POST" || !base || !(url === base || url.startsWith(`${base}/`))) return false;
  const path = new URL(url).pathname;
  return (login && path === "/auth/login") || READ_POSTS.test(path);
}

export class Guard {
  constructor(base) {
    this.base = base;
    this.log = [];         // [{method, route, status, ms, via}]
    this.blocked = new Set();
  }

  record(method, url, status, ms, via) {
    if (url.startsWith(this.base)) this.log.push({ method, route: route(url), status, ms, via });
  }

  block(method, url) {
    this.blocked.add(`${method} ${url.startsWith(this.base) ? route(url) : url}`);
  }

  // Replaces globalThis.fetch, so no module can send a write by accident.
  installFetch() {
    const real = globalThis.fetch;
    const guard = this;
    globalThis.fetch = async function guardedFetch(input, init = {}) {
      const url = typeof input === "string" ? input : input.url;
      const method = (init.method || (typeof input === "object" && input.method) || "GET").toUpperCase();
      if (!allowed(method, url, guard.base, { login: true })) {
        guard.block(method, url);
        throw new Error(`live-check write guard: refused ${method} ${url}`);
      }
      const t0 = performance.now();
      try {
        const res = await real(input, init);
        guard.record(method, url, res.status, Math.round(performance.now() - t0), "node");
        return res;
      } catch (e) {
        guard.record(method, url, 0, Math.round(performance.now() - t0), "node");
        throw e;
      }
    };
  }

  // The same rule for a Playwright browser context.
  async installBrowser(ctx) {
    await ctx.route("**/*", (r) => {
      const req = r.request();
      if (allowed(req.method(), req.url(), this.base)) return r.continue();
      this.block(req.method(), req.url());
      return r.abort("blockedbyclient");
    });
    ctx.on("requestfinished", async (req) => {
      const res = await req.response().catch(() => null);
      this.record(req.method(), req.url(), res ? res.status() : 0, Math.round(req.timing().responseEnd), "browser");
    });
    ctx.on("requestfailed", (req) => this.record(req.method(), req.url(), 0, 0, "browser"));
  }

  // {"GET /ops/status": 12, ...}: what the artifact shows. No query values,
  // no ids, no headers.
  summary() {
    const out = {};
    for (const r of this.log) out[`${r.method} ${r.route}`] = (out[`${r.method} ${r.route}`] || 0) + 1;
    return Object.fromEntries(Object.entries(out).sort());
  }

  onlyReads() {
    return this.log.every((r) => allowed(r.method, `${this.base}${r.route.split("?")[0]}`, this.base, { login: true }));
  }
}

// live-check's pure half: route normalising, fingerprints, merging raw
// observations into findings, and deciding what to do with GitHub issues.
// No I/O, so findings.test.mjs covers all of it.

import { createHash } from "node:crypto";

export const LABEL = "live-check";
const FP_RE = /<!-- live-check fp:([0-9a-f]{12}) -->/;
const FIRST_RE = /^\| First seen \| (.+) \|$/m;

export const KIND_LABEL = {
  "page-error": "bug", "console-error": "bug", "http-error": "bug", "request-failed": "bug",
  "forbidden-call": "bug", "signed-out": "bug", "login": "bug", overflow: "bug",
  slow: "optimization", a11y: "accessibility",
};

export const fingerprint = (kind, key) => createHash("sha1").update(`${kind}|${key}`).digest("hex").slice(0, 12);

// Ids change every demo run, so they must not change the fingerprint:
// /cx/projects/9ddf1d04-…/report?format=html -> /cx/projects/:id/report?format
export function route(url) {
  const u = new URL(url);
  const path = u.pathname.split("/").map((s) => (/^[0-9a-f-]{8,}$/i.test(s) || /^\d+$/.test(s) ? ":id" : s)).join("/");
  const keys = [...new Set(u.searchParams.keys())].sort();
  return keys.length ? `${path}?${keys.join("&")}` : path;
}

// raw: [{kind, key, title, page, viewport, detail, ms?}] -> one finding per fingerprint.
export function merge(raw) {
  const by = new Map();
  for (const r of raw) {
    const fp = fingerprint(r.kind, r.key);
    const f = by.get(fp) || { fp, kind: r.kind, label: KIND_LABEL[r.kind] || "bug", title: r.title,
                              pages: new Set(), viewports: new Set(), details: [], ms: 0, count: 0 };
    f.count += 1;
    if (r.page) f.pages.add(r.page);
    if (r.viewport) f.viewports.add(r.viewport);
    if (r.detail && !f.details.includes(r.detail) && f.details.length < 8) f.details.push(r.detail);
    if (r.ms > f.ms) f.ms = r.ms;
    by.set(fp, f);
  }
  return [...by.values()].map((f) => ({ ...f, pages: [...f.pages].sort(), viewports: [...f.viewports].sort() }));
}

export const issueFp = (body) => (FP_RE.exec(body || "") || [])[1] || null;

// complete: the run signed in and loaded every page, so an open issue it
// didn't see is not reproduced. Anything less closes nothing.
export function plan(findings, issues, { complete }) {
  // Several issues can share a fingerprint (a closed duplicate); use the open
  // one, else the oldest.
  const byFp = new Map();
  for (const i of [...issues].sort((a, b) => (a.state === "OPEN") - (b.state === "OPEN") || b.number - a.number)) {
    const k = issueFp(i.body);
    if (k) byFp.set(k, i);
  }
  const seen = new Set(findings.map((f) => f.fp));
  const actions = findings.map((f) => {
    const i = byFp.get(f.fp);
    if (!i) return { op: "create", f };
    return { op: i.state === "OPEN" ? "update" : "reopen", number: i.number, f, body: i.body };
  });
  if (complete) {
    for (const i of issues) {
      const k = issueFp(i.body);
      if (i.state === "OPEN" && k && !seen.has(k)) actions.push({ op: "close", number: i.number });
    }
  }
  return actions;
}

export function issueBody(f, run, oldBody = "") {
  const first = (FIRST_RE.exec(oldBody) || [])[1] || run.at;
  const rows = [["First seen", first], ["Last seen", run.at], ["Where", run.base], ["Signed in as", `${run.role} account`],
    ["Pages", f.pages.join(", ") || "-"], ["Viewports", f.viewports.join(", ") || "-"], ["Times seen this run", String(f.count)]];
  if (f.kind === "slow") rows.push(["Slowest", `${f.ms} ms (threshold ${run.slowMs} ms)`]);
  return [
    `Found by \`tools/live-check\`, a read-only browser pass over the live install (every non-GET request is blocked).`,
    "",
    "| | |", "|---|---|", ...rows.map(([k, v]) => `| ${k} | ${v} |`),
    "",
    "**What it saw**",
    "",
    "```", ...f.details, "```",
    "",
    `**Reproduce:** sign in as a ${run.role} and open ${f.pages.map((p) => `\`${p}\``).join(", ") || "the page"} with the browser's console and network tabs open, or run \`node tools/live-check/live-check.mjs --dry-run\`.`,
    "",
    "live-check updates this issue on every run, closes it when a complete run no longer sees it, and reopens it if it comes back.",
    "",
    `<!-- live-check fp:${f.fp} -->`,
  ].join("\n");
}

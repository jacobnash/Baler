// live-check's pure half: route normalising, fingerprints, merging raw
// observations into findings, and deciding what to do with GitHub issues.
// No I/O, so findings.test.mjs covers all of it.

import { createHash } from "node:crypto";

export const LABEL = "live-check";
export const IGNORE_LABEL = "live-check:ignore";
// fp, and since #178 the scope (module, and phase for perf) that owns it.
const FP_RE = /<!-- live-check fp:([0-9a-f]{12})(?: scope:(\S+))? -->/;
// Issues filed before scopes existed came from the desktop and phone browser
// passes, which are now the browser and layout modules.
const LEGACY_SCOPES = ["browser", "layout"];
const FIRST_RE = /^\| First seen \| (.+) \|$/m;

export const KIND_LABEL = {
  "page-error": "bug", "console-error": "bug", "http-error": "bug", "request-failed": "bug",
  "forbidden-call": "bug", "signed-out": "bug", "login": "bug", overflow: "bug",
  slow: "optimization", a11y: "accessibility",
  // post-deploy verification (#178)
  "perf-budget": "optimization", "ingest-rate": "bug", stale: "bug", "edge-offline": "bug",
  "announce-missing": "bug", "announce-mismatch": "bug", "viewport-meta": "bug", "narrow-pane": "bug",
  "deploy-5xx": "bug",
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

// raw: [{kind, key, title, page, viewport, detail, ms?, scope?, rows?, refs?}]
// -> one finding per fingerprint. rows: extra [label, value] lines for the
// issue table (the latest numbers); refs: related issue numbers.
export function merge(raw) {
  const by = new Map();
  for (const r of raw) {
    const fp = fingerprint(r.kind, r.key);
    const f = by.get(fp) || { fp, kind: r.kind, key: r.key, label: KIND_LABEL[r.kind] || "bug", title: r.title, scope: r.scope || "browser",
                              pages: new Set(), viewports: new Set(), details: [], ms: 0, count: 0, rows: [], refs: [] };
    f.count += 1;
    if (r.page) f.pages.add(r.page);
    if (r.viewport) f.viewports.add(r.viewport);
    if (r.detail && !f.details.includes(r.detail) && f.details.length < 8) f.details.push(r.detail);
    if (r.ms > f.ms) f.ms = r.ms;
    if (r.rows) f.rows = r.rows;
    for (const n of r.refs || []) if (!f.refs.includes(n)) f.refs.push(n);
    by.set(fp, f);
  }
  return [...by.values()].map((f) => ({ ...f, pages: [...f.pages].sort(), viewports: [...f.viewports].sort() }));
}

export const issueFp = (body) => (FP_RE.exec(body || "") || [])[1] || null;
export const issueScope = (body) => (FP_RE.exec(body || "") || [])[2] || null;

const notPlanned = (i) => String(i.state_reason || i.stateReason || "").toLowerCase() === "not_planned";
const ignored = (i) => (i.labels || []).some((l) => (typeof l === "string" ? l : l.name) === IGNORE_LABEL);

// ran: the scopes whose module ran completely this time (signed in, every
// page, route or endpoint answered). Only an issue owned by one of those can
// be closed for not being seen: a cold run says nothing about a warm-phase
// issue, and an incomplete run closes nothing. { complete: true } is the old
// all-or-nothing form.
//
// A person has the last word: an issue closed as "not planned" (won't fix)
// is never reopened or edited, and one labelled live-check:ignore is never
// touched at all. Those findings come back as op "skip".
//
// Perf damping (#227, #251): a perf-budget finding opens or reopens its
// issue only when its scope ran completely this time AND the previous
// complete run of that phase breached the same budget. prev maps a scope
// (perf:cold, perf:warm) to the fingerprints that run breached
// (previous.mjs). Otherwise it comes back as op "hold" and no issue changes.
// An issue that is already open is still updated, and closing is unchanged:
// one complete passing run closes it. Every other kind is not damped.
export const DAMPED_KINDS = new Set(["perf-budget"]);

export function plan(findings, issues, { complete = false, ran = null, prev = new Map() } = {}) {
  // Several issues can share a fingerprint (a closed duplicate); use the open
  // one, else the newest.
  const byFp = new Map();
  for (const i of [...issues].sort((a, b) => (a.state === "OPEN") - (b.state === "OPEN") || b.number - a.number)) {
    const k = issueFp(i.body);
    if (k) byFp.set(k, i);
  }
  const closes = (scope) => complete === true || (!!ran && (scope ? ran.has(scope) : LEGACY_SCOPES.every((s) => ran.has(s))));
  const seen = new Set(findings.map((f) => f.fp));
  const confirmed = (f) => !DAMPED_KINDS.has(f.kind) || (closes(f.scope) && !!prev.get(f.scope)?.has(f.fp));
  const hold = (f, i) => ({ op: "hold", number: i?.number, f,
    reason: closes(f.scope) ? "first breach; opens if the next complete run of this phase breaches too" : "its check didn't run completely" });
  const actions = findings.map((f) => {
    const i = byFp.get(f.fp);
    if (!i) return confirmed(f) ? { op: "create", f } : hold(f);
    if (ignored(i)) return { op: "skip", number: i.number, f, reason: IGNORE_LABEL };
    if (i.state !== "OPEN" && notPlanned(i)) return { op: "skip", number: i.number, f, reason: "closed as not planned" };
    if (i.state !== "OPEN" && !confirmed(f)) return hold(f, i);
    return { op: i.state === "OPEN" ? "update" : "reopen", number: i.number, f, body: i.body };
  });
  for (const i of issues) {
    const k = issueFp(i.body);
    if (i.state === "OPEN" && k && !seen.has(k) && !ignored(i) && closes(issueScope(i.body))) actions.push({ op: "close", number: i.number });
  }
  return actions;
}

function reproduce(f, run) {
  const scope = f.scope || "browser";
  if (scope === "browser" || scope === "layout") {
    return `**Reproduce:** sign in as a ${run.role} and open ${f.pages.map((p) => `\`${p}\``).join(", ") || "the page"} with the browser's console and network tabs open, or run \`node tools/live-check/live-check.mjs --dry-run --only ${scope}\`.`;
  }
  const [mod, phase] = scope.split(":");
  return `**Reproduce:** \`node tools/live-check/live-check.mjs --dry-run --only ${mod}${phase ? ` --phase ${phase}` : ""}\`.`;
}

export function issueBody(f, run, oldBody = "") {
  const first = (FIRST_RE.exec(oldBody) || [])[1] || run.at;
  const rows = [["First seen", first], ["Last seen", run.at], ["Where", run.base], ["Signed in as", `${run.role} account`],
    ["Pages", f.pages.join(", ") || "-"], ["Viewports", f.viewports.join(", ") || "-"], ["Times seen this run", String(f.count)]];
  if (f.kind === "slow") rows.push(["Slowest", `${f.ms} ms (threshold ${run.slowMs} ms)`]);
  if (run.sha) rows.push(["Deployed commit", run.sha]);
  rows.push(...(f.rows || []));
  if (f.refs && f.refs.length) rows.push(["Related", f.refs.map((n) => `#${n}`).join(", ")]);
  return [
    `Found by \`tools/live-check\` (${f.scope || "browser"} check), a read-only pass over the live install (every request but GETs, the login and named edge-node reads is blocked). See docs-site/pages/post-deploy-verify.mdx for what it means and what to do.`,
    "",
    "| | |", "|---|---|", ...rows.map(([k, v]) => `| ${k} | ${v} |`),
    "",
    "**What it saw**",
    "",
    "```", ...f.details, "```",
    "",
    reproduce(f, run),
    "",
    f.kind === "perf-budget"
      ? "live-check updates this issue on every run and closes it when a complete run of its phase is back under budget. It opens or reopens only after two consecutive complete runs of the phase breach, so one slow run on a loaded host doesn't."
      : "live-check updates this issue on every run, closes it when a complete run no longer sees it, and reopens it if it comes back.",
    "",
    `<!-- live-check fp:${f.fp} scope:${f.scope || "browser"} -->`,
  ].join("\n");
}

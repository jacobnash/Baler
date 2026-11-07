// Output: a Markdown summary (the GitHub job summary) and the JSON artifact
// live-verify-<sha>.json, which collects the cold and warm phases of one
// deploy side by side for trend comparison. Neither holds a token, a
// password or a query value.

import { existsSync, readFileSync, writeFileSync } from "node:fs";

const ICON = { pass: "pass", fail: "**FAIL**", error: "**ERROR**", skip: "skipped" };
const cell = (v) => String(v ?? "").replace(/\|/g, "\\|").replace(/\n/g, " ");

export function counts(checks) {
  const c = { pass: 0, fail: 0, error: 0, skip: 0 };
  for (const x of checks) c[x.status] = (c[x.status] || 0) + 1;
  return c;
}

export function summaryMarkdown({ run, checks, findings, actions, requests, blocked, ran, dry }) {
  const c = counts(checks);
  const lines = [
    `## live-check${run.phase ? ` (${run.phase} phase)` : ""} against ${run.base}`,
    "",
    `${run.at}, commit \`${run.sha}\`, signed in as ${run.role}. ${checks.length} checks: ${c.pass} pass, ${c.fail} fail, ${c.error} error, ${c.skip} skipped. ${findings.length} finding(s).`,
    `Complete scopes (may close issues): ${[...ran].sort().join(", ") || "none"}.`,
    "",
    "| Module | Check | Result | Numbers | Budget | Detail |",
    "|---|---|---|---|---|---|",
    ...checks.map((x) => {
      const nums = x.module === "perf" && x.median_s != null ? `median ${x.median_s} s, max ${x.max_s} s, ${Math.round((x.max_bytes || 0) / 1000)} kB` : x.value;
      return `| ${cell(x.module + (x.phase ? ` (${x.phase})` : ""))} | ${cell(x.name)} | ${ICON[x.status] || x.status} | ${cell(nums)} | ${cell(x.budget)} | ${cell(x.detail)} |`;
    }),
    "",
  ];
  if (findings.length) {
    const byFp = new Map((actions || []).filter((a) => a.f).map((a) => [a.f.fp, a]));
    lines.push("### Findings", "");
    for (const f of findings) {
      const a = byFp.get(f.fp);
      const what = dry ? "dry run, no issue changed" : !a ? "" : a.op === "skip" ? `#${a.number} left alone (${a.reason})` : a.url || `${a.op} #${a.number}`;
      lines.push(`- [${f.label}] ${f.title}${what ? ` - ${what}` : ""}`);
    }
    lines.push("");
  }
  const closed = (actions || []).filter((a) => a.op === "close");
  if (closed.length) lines.push(`Closed (no longer seen): ${closed.map((a) => `#${a.number}`).join(", ")}`, "");
  lines.push("<details><summary>Requests sent to the install</summary>", "", "| Request | Count |", "|---|---|",
             ...Object.entries(requests).map(([k, n]) => `| ${cell(k)} | ${n} |`), "",
             `Blocked by the write guard: ${blocked.length ? blocked.map((b) => `\`${b}\``).join(", ") : "none"}.`, "", "</details>", "");
  return lines.join("\n");
}

// Adds this phase to the deploy's artifact (cold first, then warm).
export function writeArtifact(file, phaseKey, data) {
  let doc = { sha: data.run.sha, base: data.run.base, phases: {} };
  if (existsSync(file)) {
    try { doc = JSON.parse(readFileSync(file, "utf8")); } catch { /* start over */ }
  }
  doc.phases[phaseKey] = data;
  writeFileSync(file, JSON.stringify(doc, null, 2));
  return doc;
}

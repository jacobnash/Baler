// R4: performance budgets, cold (right after a deploy) and warm (five minutes
// later). Each route in budgets.json is fetched `runs` times through the
// public gateway; the median and max time and the largest response are
// judged against that phase's budget. evaluate() is pure (perf.test.mjs).

export const median = (xs) => {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};

// samples: [{ms, bytes, status}] -> {runs, median_s, max_s, max_bytes, errors}
export function stats(samples) {
  const ok = samples.filter((s) => s.status >= 200 && s.status < 400);
  const secs = ok.map((s) => s.ms / 1000);
  return {
    runs: samples.length,
    median_s: secs.length ? Math.round(median(secs) * 1000) / 1000 : null,
    max_s: secs.length ? Math.round(Math.max(...secs) * 1000) / 1000 : null,
    max_bytes: ok.length ? Math.max(...ok.map((s) => s.bytes)) : null,
    errors: samples.filter((s) => !(s.status >= 200 && s.status < 400)).map((s) => s.status || s.error || "no answer"),
  };
}

const kb = (b) => `${Math.round(b / 1000)} kB`;

// One route's numbers against its budget for this phase.
// -> {check, raw?} where check.status is pass, fail or error.
export function evaluate(route, phase, st) {
  const budget = (route[phase] || {});
  const name = route.name;
  const limits = [];
  if (budget.median_s != null) limits.push(`median ≤ ${budget.median_s} s`);
  if (budget.max_s != null) limits.push(`max ≤ ${budget.max_s} s`);
  if (budget.max_bytes != null) limits.push(`≤ ${kb(budget.max_bytes)}`);
  const check = { module: "perf", phase, name, budget: limits.join(", ") || "none", ...st };
  if (st.errors.length || st.median_s == null) {
    return { check: { ...check, status: "error", detail: `answered ${st.errors.join(", ")}` } };
  }
  const breaches = [];
  if (budget.median_s != null && st.median_s > budget.median_s) breaches.push(`median ${st.median_s} s > ${budget.median_s} s`);
  if (budget.max_s != null && st.max_s > budget.max_s) breaches.push(`max ${st.max_s} s > ${budget.max_s} s`);
  if (budget.max_bytes != null && st.max_bytes > budget.max_bytes) breaches.push(`${kb(st.max_bytes)} > ${kb(budget.max_bytes)}`);
  check.status = breaches.length ? "fail" : "pass";
  check.detail = breaches.join("; ") || `median ${st.median_s} s, max ${st.max_s} s, ${kb(st.max_bytes)}`;
  if (!breaches.length) return { check };
  return {
    check,
    raw: {
      kind: "perf-budget", key: `${phase}|${name}`, scope: `perf:${phase}`, refs: route.refs || [],
      title: `${name} is over its ${phase} budget`,
      detail: `${name}: ${breaches.join("; ")} over ${st.runs} runs through the gateway (${phase} phase)`,
      rows: [["Phase", phase], ["Budget", check.budget], ["Median", `${st.median_s} s`], ["Max", `${st.max_s} s`],
             ["Largest response", kb(st.max_bytes)], ["Runs", String(st.runs)],
             ["Change the budget", "`tools/live-check/budgets.json`"]],
    },
  };
}

// Resolve a route's path: plain `path`, or `cx_report` (a Cx project's HTML
// report, found by project name; LIVE_CHECK_PROJECT overrides the name).
async function pathFor(route, api) {
  if (route.path) return route.path;
  if (route.cx_report) {
    const name = process.env.LIVE_CHECK_PROJECT || route.cx_report;
    const list = await api.json("/cx/projects");
    const projects = (Array.isArray(list) ? list : list.projects || []).filter((p) => !p.archived_at && p.status !== "archived");
    const proj = projects.find((p) => p.name === name);
    if (!proj) throw new Error(`no Cx project named "${name}"`);
    return `/cx/projects/${proj.id}/report?format=html`;
  }
  throw new Error("route has neither path nor cx_report");
}

export async function run({ api, budgets, phase }) {
  const checks = [];
  const raw = [];
  let complete = true;
  const runs = budgets.runs || 5;
  for (const route of budgets.routes || []) {
    const name = route.name.replace("{project}", process.env.LIVE_CHECK_PROJECT || route.cx_report || "");
    const r = { ...route, name };
    let path;
    try {
      path = await pathFor(r, api);
    } catch (e) {
      complete = false;
      checks.push({ module: "perf", phase, name, status: "error", detail: e.message });
      continue;
    }
    const samples = [];
    for (let i = 0; i < runs; i++) samples.push(await api.time(path));
    const { check, raw: f } = evaluate(r, phase, stats(samples));
    if (check.status === "error") complete = false;
    checks.push(check);
    if (f) raw.push(f);
  }
  return { scope: `perf:${phase}`, complete, checks, raw };
}

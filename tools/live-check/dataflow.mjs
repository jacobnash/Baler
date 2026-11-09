// R5: data flow. Samples are arriving (rows in point_history over the last
// 60 s above a floor), and every point under each watched prefix reported
// within stale_factor x its poll interval. Read from GET /ops/freshness
// (ops_api), never from the database. evaluate() is pure (dataflow.test.mjs).

// The group a point is reported under: the first segment below the watched
// prefix (fbf/home/modbus/reg1 -> fbf/home/modbus), so a quiet Pi or bridge
// is one finding, not one per register.
export function groupOf(uri, prefix) {
  const key = uri.replace(/^urn:point:/, "");
  const rest = key.slice(prefix.length + 1);
  const first = rest.split("/")[0];
  return rest.includes("/") ? `${prefix}/${first}` : prefix;
}

// How old a point may get before it counts as stale.
export function staleAfter(point, cfg) {
  const poll = point.poll_s != null ? point.poll_s : cfg.default_poll_s ?? 60;
  return Math.max((cfg.stale_factor ?? 3) * poll, cfg.min_stale_s ?? 30);
}

// cfg: budgets.json "dataflow"; payloads: {prefix: GET /ops/freshness answer}.
export function evaluate(cfg, payloads) {
  const checks = [];
  const raw = [];
  const any = Object.values(payloads)[0];
  if (any && cfg.rows_last_60s_min != null) {
    const n = any.rows_last_60s;
    const ok = n >= cfg.rows_last_60s_min;
    checks.push({ module: "dataflow", name: "samples in the last 60 s", status: ok ? "pass" : "fail",
                  value: n, budget: `≥ ${cfg.rows_last_60s_min}`, detail: `${n} rows in point_history over the last 60 s` });
    if (!ok) {
      raw.push({ kind: "ingest-rate", key: "rows_last_60s", scope: "dataflow",
                 title: `ingest is below its floor: ${n} rows in the last 60 s`,
                 detail: `point_history got ${n} rows in the last 60 s; the floor is ${cfg.rows_last_60s_min} (budgets.json dataflow.rows_last_60s_min)`,
                 rows: [["Rows in the last 60 s", String(n)], ["Floor", String(cfg.rows_last_60s_min)]] });
    }
  }
  for (const w of cfg.prefixes || []) {
    const p = payloads[w.prefix];
    if (!p) continue;
    const opts = { ...cfg, ...w };
    const groups = new Map();
    for (const pt of p.points) {
      const g = groupOf(pt.uri, w.prefix);
      if (!groups.has(g)) groups.set(g, { total: 0, stale: [] });
      const e = groups.get(g);
      e.total += 1;
      const limit = staleAfter(pt, opts);
      if (pt.age_s > limit) e.stale.push({ ...pt, limit });
    }
    for (const g of w.expect_groups || []) if (!groups.has(g)) groups.set(g, { total: 0, stale: [] });
    if (p.points.length < (w.min_points ?? 1)) {
      checks.push({ module: "dataflow", name: `${w.prefix}/* points`, status: "fail", value: p.points.length,
                    budget: `≥ ${w.min_points ?? 1}`, detail: `only ${p.points.length} point(s) under ${w.prefix} reported in the last week` });
      raw.push({ kind: "stale", key: w.prefix, scope: "dataflow", title: `no recent data under ${w.prefix}/`,
                 detail: `${p.points.length} point(s) under ${w.prefix}/ reported in the last week; expected at least ${w.min_points ?? 1}` });
    }
    for (const [g, e] of [...groups.entries()].sort()) {
      const name = `${g}/* fresh`;
      if (!e.total) {
        checks.push({ module: "dataflow", name, status: "fail", value: "0 points", detail: `nothing under ${g}/ in the last week` });
        raw.push({ kind: "stale", key: g, scope: "dataflow", title: `${g}/* has stopped reporting`,
                   detail: `no point under ${g}/ reported in the last week` });
        continue;
      }
      const worst = e.stale.sort((a, b) => b.age_s - a.age_s);
      checks.push({ module: "dataflow", name, status: e.stale.length ? "fail" : "pass", value: `${e.total - e.stale.length}/${e.total} fresh`,
                    budget: `age ≤ ${opts.stale_factor ?? 3}× poll`,
                    detail: e.stale.length ? `${e.stale.length} stale, oldest ${Math.round(worst[0].age_s)} s` : `all ${e.total} reported within ${opts.stale_factor ?? 3}× their poll interval` });
      if (e.stale.length) {
        raw.push({
          kind: "stale", key: g, scope: "dataflow",
          title: `${g}/* points are stale (${e.stale.length} of ${e.total})`,
          detail: worst.slice(0, 8).map((s) => `${s.uri.replace(/^urn:point:/, "")}: last ${s.last_ts}, ${Math.round(s.age_s)} s ago (limit ${Math.round(s.limit)} s, poll ~${s.poll_s ?? "?"} s)`).join("\n"),
          rows: [["Stale points", `${e.stale.length} of ${e.total}`], ["Oldest", `${Math.round(worst[0].age_s)} s ago`],
                 ["Rule", `stale after max(${opts.stale_factor ?? 3} × poll interval, ${opts.min_stale_s ?? 30} s)`]],
        });
      }
    }
  }
  return { checks, raw };
}

// Overload (#235): Baler's own numbers from GET /ops/metrics, the endpoint
// the scale ladder checks against its canaries - end-to-end ingest lag, the
// listener's oldest uncommitted reading, broker drops since the listener
// started. A metric the install doesn't export (an older Baler) is skipped.
export function parseProm(text) {
  const out = {};
  for (const line of String(text || "").split("\n")) {
    if (!line || line.startsWith("#")) continue;
    const i = line.lastIndexOf(" ");
    const v = Number(line.slice(i + 1));
    if (i > 0 && !Number.isNaN(v)) out[line.slice(0, i)] = v;
  }
  return out;
}

const OVERLOAD = [
  ["baler_ingest_newest_reading_age_seconds", "max_ingest_lag_s", "ingest lag", "now minus the newest stored reading"],
  ["baler_listener_queue_oldest_age_seconds", "max_queue_age_s", "listener backlog", "the listener's oldest uncommitted reading"],
  ["baler_broker_dropped_since_listener_start", "max_broker_drops", "broker drops", "messages the broker dropped since the listener started"],
];

export function evaluateMetrics(cfg, text) {
  const m = parseProm(text);
  const checks = [];
  const raw = [];
  for (const [name, key, label, what] of OVERLOAD) {
    if (cfg[key] == null || m[name] === undefined) continue;
    const ok = m[name] <= cfg[key];
    checks.push({ module: "dataflow", name: `${label} (/ops/metrics)`, status: ok ? "pass" : "fail", value: m[name],
                  budget: `≤ ${cfg[key]}`, detail: `${what}: ${m[name]}` });
    if (!ok) {
      raw.push({ kind: "overload", key: name, scope: "dataflow", title: `${label} over budget: ${m[name]} (≤ ${cfg[key]})`,
                 detail: `${name} = ${m[name]} from GET /ops/metrics; the budget is ${cfg[key]} (budgets.json dataflow.${key})` });
    }
  }
  return { checks, raw };
}

export async function run({ api, budgets }) {
  const cfg = budgets.dataflow || {};
  const payloads = {};
  try {
    for (const w of cfg.prefixes || []) payloads[w.prefix] = await api.json(`/ops/freshness?prefix=${encodeURIComponent(w.prefix)}`);
    if (!(cfg.prefixes || []).length) payloads[""] = await api.json("/ops/freshness?prefix=fbf");
  } catch (e) {
    return { scope: "dataflow", complete: false, raw: [],
             checks: [{ module: "dataflow", name: "GET /ops/freshness", status: "error", detail: e.message }] };
  }
  const out = evaluate(cfg, payloads);
  try {
    const res = await api.get("/ops/metrics");
    if (res.ok) {
      const extra = evaluateMetrics(cfg, await res.text());
      out.checks.push(...extra.checks);
      out.raw.push(...extra.raw);
    }
  } catch {
    // an install without /ops/metrics: freshness above still judges data flow
  }
  return { scope: "dataflow", complete: true, ...out };
}

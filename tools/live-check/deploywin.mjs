// R8: the gateway answered no 5xx between the deploy's start and end.
// GET /ops/gateway-errors reads the gateway's own 5xx log (gateway/nginx.conf);
// the window comes from the Deploy workflow (--deploy-start/--deploy-end).
// evaluate() is pure (deploywin.test.mjs).

export function evaluate(cfg, payload, window) {
  const max = cfg.max_5xx ?? 0;
  const name = "gateway 5xx during the deploy";
  if (!payload.available) {
    return { checks: [{ module: "deploy", name, status: "error", detail: payload.detail || "the gateway's 5xx log isn't available" }], raw: [] };
  }
  const n = payload.count;
  const ok = n <= max;
  const by = Object.entries(payload.by_status).map(([s, c]) => `${c}× ${s}`).join(", ");
  const check = { module: "deploy", name, status: ok ? "pass" : "fail", value: n, budget: `≤ ${max}`,
                  detail: ok ? `${window.start} - ${window.end}` : `${by} between ${payload.first} and ${payload.last}` };
  if (ok) return { checks: [check], raw: [] };
  return {
    checks: [check],
    raw: [{
      kind: "deploy-5xx", key: "deploy-window", scope: "deploy", title: "the gateway answered 5xx during a deploy",
      detail: payload.by_route.slice(0, 8).map((r) => `${r.count}× ${r.status} ${r.route}`).join("\n"),
      rows: [["Deploy window", `${window.start} - ${window.end}`], ["5xx answers", `${n} (${by})`],
             ["First / last", `${payload.first} / ${payload.last}`], ["Allowed", String(max)]],
    }],
  };
}

export async function run({ api, budgets, window }) {
  if (!window || !window.start || !window.end) {
    return { scope: "deploy", complete: false, raw: [],
             checks: [{ module: "deploy", name: "gateway 5xx during the deploy", status: "skip", detail: "no deploy window given (--deploy-start/--deploy-end)" }] };
  }
  try {
    const q = `since=${encodeURIComponent(window.start)}&until=${encodeURIComponent(window.end)}`;
    const payload = await api.json(`/ops/gateway-errors?${q}`);
    const out = evaluate(budgets.deploy || {}, payload, window);
    return { scope: "deploy", complete: payload.available, ...out };
  } catch (e) {
    return { scope: "deploy", complete: false, raw: [], checks: [{ module: "deploy", name: "GET /ops/gateway-errors", status: "error", detail: e.message }] };
  }
}

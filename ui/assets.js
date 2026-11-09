// Assets page (#197): the lifecycle record's assets and install/remove
// history, over ops_api's /ops/assets and /ops/positions routes. Plain
// globals, no build step, like every other page here.
const OPS_URL = apiBase("ops", "/ops");  // same-origin paths only (apibase.js)
const statusEl = document.getElementById("status");
let currentAsset = null;

async function api(path, opts = {}) {
  const res = await fetch(`${OPS_URL}${path}`, {
    ...opts,
    headers: { ...authHeaders(), ...(opts.body ? { "Content-Type": "application/json" } : {}) },
  });
  if (res.status === 401) { handleUnauthorized(); throw new Error("signed out"); }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `${res.status}`);
  return body;
}

function cell(tr, text) {
  const td = document.createElement("td");
  td.textContent = text == null ? "" : String(text);
  tr.appendChild(td);
  return td;
}

function fill(tbodySel, items, cols) {
  const tbody = document.querySelector(tbodySel);
  tbody.replaceChildren();
  for (const it of items) {
    const tr = document.createElement("tr");
    for (const c of cols) cell(tr, typeof c === "function" ? c(it) : it[c]);
    tbody.appendChild(tr);
  }
  return tbody;
}

function formJSON(form) {
  const out = {};
  for (const [k, v] of new FormData(form).entries()) {
    if (v === "") continue;
    out[k] = form.elements[k].type === "datetime-local" ? new Date(v).toISOString() : v;
  }
  return out;
}

function say(msg) { statusEl.textContent = msg; }

async function loadAssets() {
  const q = new URLSearchParams();
  const text = document.getElementById("filter-q").value;
  const st = document.getElementById("filter-status").value;
  if (text) q.set("q", text);
  if (st) q.set("status", st);
  const items = await api(`/assets?${q}`);
  const tbody = fill("#assets-table tbody", items,
    ["tag", "asset_type", "manufacturer", "model", "serial", "status", "position_uri", (a) => (a.installed_at || "").slice(0, 10)]);
  [...tbody.children].forEach((tr, i) => {
    tr.style.cursor = "pointer";
    tr.addEventListener("click", () => showAsset(items[i].id));
  });
  say(`${items.length} asset(s)`);
}

async function showAsset(id) {
  const a = await api(`/assets/${encodeURIComponent(id)}`);
  currentAsset = a;
  document.getElementById("detail").hidden = false;
  document.getElementById("detail-title").textContent = `${a.tag || a.model || a.serial || a.id} (${a.status})`;
  const { history, ...fields } = a;
  document.getElementById("detail-fields").textContent = JSON.stringify(fields, null, 2);
  fill("#history-table tbody", history, ["position_uri", (h) => h.installed_at, "install_reason", (h) => h.removed_at || "still installed", "remove_reason"]);
  const tests = await api(`/assets/${encodeURIComponent(id)}/bench-tests`);
  fill("#bench-table tbody", tests, [(t) => t.tested_at.slice(0, 16), (t) => t.procedure.name, "verdict",
    (t) => (t.before_install ? "yes" : "no"), (t) => (t.baseline ? "baseline" : ""), "technician"]);
  const ws = await api(`/assets/${encodeURIComponent(id)}/warranties`);
  fill("#warranty-table tbody", ws, ["kind", "provider", "starts_on", "ends_on", "status",
    (w) => (w.source ? `doc ${w.source.document_id || "?"} p. ${w.source.page || "?"}` : "")]);
  document.getElementById("install-form").hidden = a.status !== "in_stock";
  document.getElementById("remove-form").hidden = a.status !== "installed";
}

function onSubmit(id, fn) {
  document.getElementById(id).addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try { await fn(ev.target); } catch (err) { say(`error: ${err.message}`); }
  });
}

async function loadProcedures() {
  const procs = await api("/bench/procedures");
  const sel = document.getElementById("bench-procedure");
  sel.replaceChildren(...procs.filter((p) => p.status === "active").map((p) =>
    Object.assign(document.createElement("option"), { value: p.id, textContent: p.name })));
}

async function loadDeferred() {
  const [items, f, life] = await Promise.all([api("/deferred?status=open,planned"), api("/deferred/fci"), api("/service-life/assets")]);
  document.getElementById("fci-summary").textContent = f.fci == null
    ? `Open deferred cost ${f.deferred_cost}; no replacement values yet, so no FCI.`
    : `FCI ${(f.fci * 100).toFixed(1)}%: ${f.deferred_cost} deferred / ${f.replacement_value} replacement value. ${f.note}`;
  fill("#deferred-table tbody", items, ["title", "risk", "cost", "target_year", "status", (i) => i.asset_id || i.position_uri]);
  fill("#life-table tbody", life, [(r) => `${r.asset.tag || r.asset.serial || r.asset_id} (${r.asset.asset_type || ""})`, "installed_on",
    "age_years", "expected_life_years", "remaining_years", "replacement_value",
    (r) => (r.reference ? `${r.reference.source_title} (read ${r.reference.retrieved_on})` : "")]);
}

async function loadPM() {
  const items = await api("/pm/due");
  const tbody = document.querySelector("#pm-due-table tbody");
  fill("#pm-due-table tbody", items, ["status", "title", (i) => i.asset.tag || i.asset.serial || i.asset_id, "basis",
    (i) => `${i.since.slice(0, 10)} (${i.since_what})`,
    (i) => i.due_at ? i.due_at.slice(0, 10) : i.run_hours != null ? `${i.run_hours} h run` : i.value != null ? i.value : "", () => ""]);
  [...tbody.children].forEach((tr, n) => {
    const b = Object.assign(document.createElement("button"), { type: "button", textContent: "Done" });
    b.addEventListener("click", async () => {
      try {
        await api(`/pm/tasks/${encodeURIComponent(items[n].task_id)}/complete`, { method: "POST", body: JSON.stringify({ asset_id: items[n].asset_id }) });
        await loadPM();
      } catch (err) { say(`error: ${err.message}`); }
    });
    tr.lastChild.appendChild(b);
  });
}

async function downloadCSV(path, name) {
  const res = await fetch(`${OPS_URL}${path}`, { headers: authHeaders() });
  if (!res.ok) { say(`download failed: ${res.status}`); return; }
  const url = URL.createObjectURL(await res.blob());
  Object.assign(document.createElement("a"), { href: url, download: name }).click();
  URL.revokeObjectURL(url);
}

async function loadAlerts() {
  const al = await api("/warranties/alerts");
  const label = (a) => `${a.manufacturer || ""} ${a.model || ""} (${a.serial || a.tag || a.id})`;
  fill("#expiring-table tbody", al.expiring, [(w) => label(w.asset), "kind", "ends_on", "days_left"]);
  fill("#failures-table tbody", al.failures_under_warranty, [(f) => f.failure.opened_at.slice(0, 10),
    (f) => `${f.failure.kind}: ${f.failure.title}`, "position_uri", (f) => label(f.asset),
    (f) => f.warranties.map((w) => w.ends_on).join(", ")]);
}

initAuth(() => {
  loadAssets().catch((err) => say(`error: ${err.message}`));
  loadProcedures().catch((err) => say(`error: ${err.message}`));
  onSubmit("bench-form", async (f) => {
    const body = formJSON(f);
    body.measurements = body.measurements ? JSON.parse(body.measurements) : {};
    const t = await api(`/assets/${encodeURIComponent(currentAsset.id)}/bench-tests`, { method: "POST", body: JSON.stringify(body) });
    say(`bench test recorded: ${t.verdict}`);
    f.reset();
    await showAsset(currentAsset.id);
  });
  loadDeferred().catch((err) => say(`error: ${err.message}`));
  onSubmit("deferred-form", async (f) => {
    const body = formJSON(f);
    body.cost = Number(body.cost);
    if (body.target_year) body.target_year = parseInt(body.target_year, 10);
    await api("/deferred", { method: "POST", body: JSON.stringify(body) });
    f.reset();
    await loadDeferred();
  });
  onSubmit("life-form", async (f) => {
    const body = formJSON(f);
    body.median_years = Number(body.median_years);
    await api("/service-life", { method: "POST", body: JSON.stringify(body) });
    f.reset();
    await loadDeferred();
  });
  onSubmit("value-form", async (f) => {
    const body = formJSON(f);
    const id = body.asset_id;
    delete body.asset_id;
    body.replacement_value = Number(body.replacement_value);
    await api(`/assets/${encodeURIComponent(id)}/values`, { method: "POST", body: JSON.stringify(body) });
    f.reset();
    await loadDeferred();
  });
  loadPM().catch((err) => say(`error: ${err.message}`));
  document.getElementById("pm-csv").addEventListener("click", (ev) => { ev.preventDefault(); downloadCSV("/pm/due/export?format=csv", "pm-due.csv"); });
  document.getElementById("pm-cobie").addEventListener("click", (ev) => { ev.preventDefault(); downloadCSV("/pm/due/export?format=cobie-job", "cobie-job.csv"); });
  onSubmit("pm-form", async (f) => {
    const body = formJSON(f);
    for (const k of ["interval_days"]) if (body[k]) body[k] = parseInt(body[k], 10);
    for (const k of ["interval_hours", "condition_value"]) if (body[k]) body[k] = Number(body[k]);
    if (body.trigger !== "condition") { delete body.condition_op; delete body.condition_point; delete body.condition_value; }
    await api("/pm/tasks", { method: "POST", body: JSON.stringify(body) });
    f.reset();
    await loadPM();
  });
  loadAlerts().catch((err) => say(`error: ${err.message}`));
  onSubmit("warranty-form", async (f) => {
    const body = formJSON(f);
    if (body.duration_months) body.duration_months = Number(body.duration_months);
    await api(`/assets/${encodeURIComponent(currentAsset.id)}/warranties`, { method: "POST", body: JSON.stringify(body) });
    f.reset();
    await showAsset(currentAsset.id);
    await loadAlerts();
  });
  onSubmit("filter-form", loadAssets);
  onSubmit("create-form", async (f) => {
    const a = await api("/assets", { method: "POST", body: JSON.stringify(formJSON(f)) });
    f.reset();
    await loadAssets();
    await showAsset(a.id);
  });
  onSubmit("install-form", async (f) => {
    await api(`/assets/${encodeURIComponent(currentAsset.id)}/install`, { method: "POST", body: JSON.stringify(formJSON(f)) });
    await loadAssets();
    await showAsset(currentAsset.id);
  });
  onSubmit("remove-form", async (f) => {
    await api(`/assets/${encodeURIComponent(currentAsset.id)}/remove`, { method: "POST", body: JSON.stringify(formJSON(f)) });
    await loadAssets();
    await showAsset(currentAsset.id);
  });
  onSubmit("position-form", async (f) => {
    const h = await api(`/positions/history?uri=${encodeURIComponent(f.elements.uri.value)}`);
    fill("#position-table tbody", h.history, [(x) => `${x.asset.manufacturer || ""} ${x.asset.model || ""} (${x.asset.serial || x.asset.id})`,
      "installed_at", (x) => x.removed_at || "still installed", "remove_reason"]);
  });
  onSubmit("changes-form", async (f) => {
    const items = await api(`/assets/changes?${new URLSearchParams(formJSON(f))}`);
    fill("#changes-table tbody", items, [(c) => c.at.slice(0, 10), "position_uri",
      (c) => `${c.from.manufacturer || ""} ${c.from.model || ""}`, (c) => `${c.to.manufacturer || ""} ${c.to.model || ""}`, "removed_reason"]);
  });
});

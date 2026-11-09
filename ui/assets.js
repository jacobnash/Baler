// Assets page (#197): the lifecycle record's assets and install/remove
// history, over ops_api's /ops/assets and /ops/positions routes. Plain
// globals, no build step, like every other page here.
const OPS_URL = new URLSearchParams(location.search).get("ops") || "/ops";
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

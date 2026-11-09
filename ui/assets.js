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
  document.getElementById("install-form").hidden = a.status !== "in_stock";
  document.getElementById("remove-form").hidden = a.status !== "installed";
}

function onSubmit(id, fn) {
  document.getElementById(id).addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try { await fn(ev.target); } catch (err) { say(`error: ${err.message}`); }
  });
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

initAuth(() => {
  loadAssets().catch((err) => say(`error: ${err.message}`));
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

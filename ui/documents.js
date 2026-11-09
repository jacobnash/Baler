// Documents page (#201): the building document store over ops_api's
// /ops/documents routes. Plain globals, no build step.
const OPS_URL = new URLSearchParams(location.search).get("ops") || "/ops";
const statusEl = document.getElementById("status");
let current = null;

async function api(path, opts = {}) {
  const headers = { ...authHeaders(), ...(opts.headers || {}) };
  const res = await fetch(`${OPS_URL}${path}`, { ...opts, headers });
  if (res.status === 401) { handleUnauthorized(); throw new Error("signed out"); }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `${res.status}`);
  return body;
}

function say(msg) { statusEl.textContent = msg; }

function fill(sel, items, cols) {
  const tbody = document.querySelector(sel);
  tbody.replaceChildren();
  for (const it of items) {
    const tr = document.createElement("tr");
    for (const c of cols) {
      const td = document.createElement("td");
      const v = typeof c === "function" ? c(it) : it[c];
      if (v instanceof Node) td.appendChild(v); else td.textContent = v == null ? "" : String(v);
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
}

function button(label, fn) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = label;
  b.addEventListener("click", fn);
  return b;
}

async function loadDocs() {
  const docs = await api("/documents");
  fill("#docs-table tbody", docs, [(d) => d.title || d.filename, "kind", "page_count", "text_status",
    (d) => (d.created_at || "").slice(0, 10), (d) => d.sha256.slice(0, 12), (d) => button("Open", () => showDoc(d.id))]);
  say(`${docs.length} document(s)`);
}

async function showDoc(id) {
  current = await api(`/documents/${encodeURIComponent(id)}`);
  document.getElementById("detail").hidden = false;
  document.getElementById("detail-title").textContent = `${current.title || current.filename} (${current.kind})`;
  document.getElementById("verify-result").textContent = "";
  fill("#links-table tbody", current.links, ["target_kind", "target", "page", "note"]);
  await loadProposals();
}

async function decide(p, action) {
  let body = { action };
  if (action === "confirm") {
    const edited = prompt("Confirm this fact (edit the JSON to correct it):", JSON.stringify(p.payload));
    if (edited === null) return;
    body.payload = JSON.parse(edited);
  }
  await api(`/document-proposals/${encodeURIComponent(p.id)}/decide`, { method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json" } });
  await loadProposals();
}

async function loadProposals() {
  const props = await api(`/documents/${encodeURIComponent(current.id)}/proposals`);
  fill("#proposals-table tbody", props, ["kind", (p) => JSON.stringify(p.decided_payload || p.payload), "page", "quote",
    (p) => p.model || p.method, "status", (p) => {
      if (p.status !== "proposed") return "";
      const span = document.createElement("span");
      span.append(button("Confirm", () => decide(p, "confirm").catch((e) => say(e.message))), " ",
        button("Reject", () => decide(p, "reject").catch((e) => say(e.message))));
      return span;
    }]);
}

async function download() {
  const res = await fetch(`${OPS_URL}/documents/${encodeURIComponent(current.id)}/content`, { headers: authHeaders() });
  if (!res.ok) { say(`download failed: ${res.status}`); return; }
  const url = URL.createObjectURL(await res.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: current.filename });
  a.click();
  URL.revokeObjectURL(url);
}

function onSubmit(id, fn) {
  document.getElementById(id).addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try { await fn(ev.target); } catch (err) { say(`error: ${err.message}`); }
  });
}

initAuth(async () => {
  try {
    const k = await api("/documents/kinds");
    const sel = document.getElementById("kind-select");
    for (const kind of k.kinds) sel.appendChild(Object.assign(document.createElement("option"), { value: kind, textContent: kind }));
    await loadDocs();
  } catch (err) { say(`error: ${err.message}`); }
  onSubmit("search-form", async (f) => {
    const hits = await api(`/documents/search?q=${encodeURIComponent(f.elements.q.value)}`);
    fill("#hits-table tbody", hits, [(h) => button(h.citation, () => showDoc(h.document_id)), "kind", "snippet"]);
    say(`${hits.length} page(s)`);
  });
  onSubmit("upload-form", async (f) => {
    const file = f.elements.file.files[0];
    const q = new URLSearchParams({ filename: file.name, kind: f.elements.kind.value });
    if (f.elements.title.value) q.set("title", f.elements.title.value);
    say("uploading…");
    const d = await api(`/documents?${q}`, { method: "POST", body: file, headers: { "Content-Type": file.type || "application/octet-stream" } });
    say(d.duplicate ? "already stored: showing the existing document" : "stored");
    f.reset();
    await loadDocs();
    await showDoc(d.id);
  });
  onSubmit("link-form", async (f) => {
    const body = { target_kind: f.elements.target_kind.value, target: f.elements.target.value };
    if (f.elements.page.value) body.page = Number(f.elements.page.value);
    if (f.elements.note.value) body.note = f.elements.note.value;
    await api(`/documents/${encodeURIComponent(current.id)}/links`, { method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json" } });
    f.reset();
    await showDoc(current.id);
  });
  document.getElementById("extract").addEventListener("click", async () => {
    try {
      say("extracting…");
      const out = await api(`/documents/${encodeURIComponent(current.id)}/extract`, { method: "POST", body: "{}", headers: { "Content-Type": "application/json" } });
      const e = out.extraction;
      document.getElementById("extract-result").textContent =
        `${out.proposals.length} proposal(s) via ${e.provider}${e.model ? " (" + e.model + ")" : ""}. ${e.info.notes || ""}`;
      say("extracted");
      await loadProposals();
    } catch (err) { say(`error: ${err.message}`); }
  });
  document.getElementById("schedules").addEventListener("click", async () => {
    try {
      say("reading schedules…");
      const out = await api(`/documents/${encodeURIComponent(current.id)}/schedules`, { method: "POST", body: "{}", headers: { "Content-Type": "application/json" } });
      document.getElementById("extract-result").textContent = out.schedules.map((s) => `${s.title}: ${s.rows} rows (${s.reader})`).join("; ") || "no schedules found";
      say("done");
      await loadProposals();
    } catch (err) { say(`error: ${err.message}`); }
  });
  document.getElementById("download").addEventListener("click", () => download().catch((e) => say(e.message)));
  document.getElementById("verify").addEventListener("click", async () => {
    const v = await api(`/documents/${encodeURIComponent(current.id)}/verify`);
    document.getElementById("verify-result").textContent = v.ok ? "sha256 matches" : `MISMATCH: computed ${v.computed}`;
  });
});

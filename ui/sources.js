// Ops -> Sources (Baler#20): the source registry - what feeds Baler besides
// Windrow, its settings, and whether it's working. Admin-only, like Users;
// users.js calls initSources() for admins. Secrets are write-only: the API
// says only whether one is set, and a blank secret field keeps it.

const OPS_SOURCES_URL = (new URLSearchParams(location.search).get("ops") || "/ops") + "/sources";
const srcState = { kinds: {}, list: [], editing: null };
const $s = (id) => document.getElementById(id);

async function sourcesApi(method, path, body) {
  const headers = { ...authHeaders() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`${OPS_SOURCES_URL}${path}`, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
  if (res.status === 401) {
    handleUnauthorized();
    throw new Error("session ended - sign in again");
  }
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (res.status === 403) throw new Error("only an admin can do that");
  if (!res.ok) throw new Error((data && data.error) || text.trim() || `${method} failed (${res.status})`);
  return data;
}

function ago(iso) {
  if (!iso) return "never";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return `${s}s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleString();
}

function renderSources() {
  const body = $s("sources-body");
  body.innerHTML = "";
  for (const src of srcState.list) {
    const tr = document.createElement("tr");
    const cell = (text, cls) => Object.assign(document.createElement("td"), { textContent: text, className: cls || "" });
    const health = document.createElement("td");
    health.appendChild(Object.assign(document.createElement("span"), { className: `badge health-${src.health}`, textContent: src.health }));
    const problem = src.health === "misconfigured" ? `missing: ${src.missing.join(", ")}` : src.health === "failing" ? src.last_error : "";
    const actions = document.createElement("td");
    actions.className = "actions";
    const btn = (text, fn) => {
      const b = Object.assign(document.createElement("button"), { type: "button", textContent: text });
      b.addEventListener("click", fn);
      actions.appendChild(b);
    };
    btn("Edit", () => openSourceDialog(src));
    btn(src.enabled ? "Disable" : "Enable", () => saveSource(src.id, { enabled: !src.enabled }));
    btn("Delete", async () => {
      if (!confirm(`Delete source "${src.name}"? Data it already pulled stays.`)) return;
      await sourcesApi("DELETE", `/${encodeURIComponent(src.id)}`).catch((e) => ($s("sources-status").textContent = e.message));
      loadSources();
    });
    tr.append(cell(src.name), cell((srcState.kinds[src.kind] || {}).label || src.kind), health,
      cell(ago(src.last_run_at)), cell(problem, "problem"), actions);
    body.appendChild(tr);
  }
  const bad = srcState.list.filter((s) => s.health === "failing" || s.health === "misconfigured").length;
  $s("sources-status").textContent = srcState.list.length
    ? `${srcState.list.length} source${srcState.list.length === 1 ? "" : "s"}${bad ? `, ${bad} need${bad === 1 ? "s" : ""} attention` : ""}`
    : "No sources yet - Add source to pull from a Haystack server, SQL, weather or ENERGY STAR.";
}

async function loadSources() {
  try {
    srcState.list = await sourcesApi("GET", "");
    renderSources();
  } catch (err) {
    $s("sources-status").textContent = err.message;
  }
}

async function saveSource(id, patch) {
  try {
    await sourcesApi("PATCH", `/${encodeURIComponent(id)}`, patch);
  } catch (err) {
    $s("sources-status").textContent = err.message;
  }
  loadSources();
}

function renderSourceFields(kind, src) {
  const box = $s("source-fields");
  box.innerHTML = "";
  const spec = srcState.kinds[kind];
  $s("source-kind-help").textContent = spec ? spec.help : "";
  if (!spec) return;
  for (const f of spec.fields) {
    const label = document.createElement("label");
    label.append(`${f.label}${f.required ? " *" : ""} `);
    let input;
    if (f.type === "bool") {
      input = Object.assign(document.createElement("input"), { type: "checkbox" });
      input.checked = src ? Boolean(src.settings[f.name] ?? f.default) : Boolean(f.default);
      label.className = "inline";
      label.prepend(input);
    } else {
      input = Object.assign(document.createElement("input"), { type: f.secret ? "password" : "text", autocomplete: "off" });
      if (f.secret) {
        input.placeholder = src && src.secrets_set.includes(f.name) ? "set - leave blank to keep" : "";
      } else {
        const v = src ? src.settings[f.name] : undefined;
        input.value = v === undefined || v === null ? "" : String(v);
        if (f.default !== null && f.default !== undefined) input.placeholder = `default ${f.default}`;
      }
      label.appendChild(input);
    }
    input.dataset.field = f.name;
    input.dataset.type = f.type;
    input.dataset.secret = f.secret ? "1" : "";
    if (f.help) label.title = f.help;
    box.appendChild(label);
  }
}

function openSourceDialog(src) {
  srcState.editing = src || null;
  $s("source-form").reset();
  $s("source-error").hidden = true;
  $s("source-title").textContent = src ? `Edit ${src.name}` : "Add source";
  const kind = $s("source-kind");
  kind.innerHTML = "";
  for (const [k, spec] of Object.entries(srcState.kinds)) kind.appendChild(new Option(spec.label, k));
  kind.value = src ? src.kind : Object.keys(srcState.kinds)[0];
  kind.disabled = Boolean(src);
  $s("source-name").value = src ? src.name : "";
  $s("source-enabled").checked = src ? src.enabled : false;
  renderSourceFields(kind.value, src);
  $s("source-dialog").showModal();
}

function collectSettings() {
  const out = {};
  for (const input of $s("source-fields").querySelectorAll("[data-field]")) {
    const name = input.dataset.field;
    if (input.dataset.type === "bool") {
      out[name] = input.checked;
    } else if (input.dataset.secret) {
      if (input.value) out[name] = input.value; // blank keeps the stored secret
    } else {
      out[name] = input.value.trim() === "" ? null : input.value.trim();
    }
  }
  return out;
}

function wireSources() {
  $s("new-source-btn").addEventListener("click", () => openSourceDialog(null));
  $s("source-kind").addEventListener("change", () => renderSourceFields($s("source-kind").value, null));
  $s("source-cancel").addEventListener("click", () => $s("source-dialog").close());
  $s("source-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const body = { name: $s("source-name").value.trim(), settings: collectSettings(), enabled: $s("source-enabled").checked };
    try {
      if (srcState.editing) {
        await sourcesApi("PATCH", `/${encodeURIComponent(srcState.editing.id)}`, body);
      } else {
        // a new source has nothing to clear: send only what was filled in
        body.settings = Object.fromEntries(Object.entries(body.settings).filter(([, v]) => v !== null));
        await sourcesApi("POST", "", { kind: $s("source-kind").value, ...body });
      }
      $s("source-dialog").close();
      loadSources();
    } catch (err) {
      $s("source-error").textContent = err.message;
      $s("source-error").hidden = false;
    }
  });
}

let sourcesWired = false;

async function initSources() {
  $s("sources-section").hidden = false;
  if (!sourcesWired) {
    sourcesWired = true;
    wireSources();
    setInterval(loadSources, 30000); // health changes as the runner works
  }
  try {
    srcState.kinds = await sourcesApi("GET", "/kinds");
  } catch (err) {
    $s("sources-status").textContent = err.message;
    return;
  }
  loadSources();
}

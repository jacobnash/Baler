// Hand edits to an equip/point record (Baler#19): display name, Brick
// class, tags, delete. PATCH/DELETE /ingest/records through the gateway
// (operator to edit, admin to delete) - every edit is logged server-side
// and shown here as the record's edit history. Loaded before app.js.

const RECORD_API_URL = new URLSearchParams(location.search).get("ingest") || "/ingest";
// Shown read-only in the editor: they come from graph links or live history.
const RECORD_RESERVED = new Set(["id", "equipRef", "siteRef", "brickClass", "brickHasPart", "brickFeeds", "navId",
  "curVal", "curStatus", "his", "hisStart", "hisEnd", "mod", "writeVal", "point", "equip", "site", "tz", "kind"]);

let recordRole = null; // the caller's role, from /auth/me (null until known)
let recordEditing = null;

async function recordRoleKnown() {
  if (recordRole !== null) return recordRole;
  try {
    const res = await fetch(`${AUTH_API_URL}/me`, { headers: authHeaders() });
    recordRole = res.ok ? (await res.json()).role : "";
  } catch {
    recordRole = "";
  }
  return recordRole;
}

const canEditRecords = () => recordRole === "operator" || recordRole === "admin";

async function recordApi(method, path, body) {
  const headers = { ...authHeaders() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`${RECORD_API_URL}${path}`, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
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
  if (res.status === 403) throw new Error("your role can't do that");
  if (!res.ok) throw new Error((data && data.error) || text.trim() || `${method} failed (${res.status})`);
  return data;
}

// "72°F" -> {val: 72, unit: "°F"}; "72" -> 72; "" -> marker; else text.
function parseTagValue(text) {
  const t = text.trim();
  if (!t) return true;
  const m = /^(-?\d+(?:\.\d+)?)\s*([^\d\s].*)?$/.exec(t);
  if (m) return m[2] ? { val: Number(m[1]), unit: m[2].trim() } : Number(m[1]);
  return t;
}

function recordEditButton(entity, onSaved) {
  const btn = Object.assign(document.createElement("button"), { type: "button", className: "edit-btn", textContent: "✎ Edit" });
  btn.title = "Edit name, Brick class and tags";
  btn.addEventListener("click", (ev) => {
    ev.stopPropagation(); // inside a <summary>, don't toggle the tree node
    ev.preventDefault();
    openRecordEditor(entity, onSaved);
  });
  return btn;
}

async function openRecordEditor(entity, onSaved) {
  recordEditing = { entity, onSaved };
  const $r = (id) => document.getElementById(id);
  $r("rec-form").reset();
  $r("rec-error").hidden = true;
  $r("rec-title").textContent = `Edit ${entity.dis}`;
  $r("rec-dis").value = entity.dis;
  $r("rec-class").value = entity.brickClass || "";
  $r("rec-delete").hidden = recordRole !== "admin";
  const list = $r("rec-tag-list");
  list.innerHTML = "";
  for (const name of Object.keys(entity.rec).sort()) {
    if (name === "dis") continue;
    const label = document.createElement("label");
    const box = Object.assign(document.createElement("input"), { type: "checkbox", value: name, disabled: RECORD_RESERVED.has(name) });
    label.append(box, tagText(name, entity.rec[name]));
    if (RECORD_RESERVED.has(name)) label.title = "Comes from the graph's links or live history";
    list.appendChild(label);
  }
  const history = $r("rec-history-list");
  history.textContent = "loading…";
  $r("rec-dialog").showModal();
  try {
    const edits = await recordApi("GET", `/records/edits?id=${encodeURIComponent(entity.uri)}`);
    history.innerHTML = "";
    if (!edits.length) history.textContent = "No hand edits yet.";
    for (const e of edits) {
      const li = document.createElement("li");
      li.textContent = `${new Date(e.ts).toLocaleString()} - ${e.who || "?"} - ${e.action} ${e.action === "edit" ? JSON.stringify(e.detail) : ""}`;
      history.appendChild(li);
    }
  } catch (err) {
    history.textContent = err.message;
  }
}

function wireRecordEditor() {
  const $r = (id) => document.getElementById(id);
  if (!$r("rec-dialog")) return;
  $r("rec-cancel").addEventListener("click", () => $r("rec-dialog").close());
  $r("rec-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const { entity, onSaved } = recordEditing;
    const body = { id: entity.uri };
    const dis = $r("rec-dis").value.trim();
    if (dis && dis !== entity.dis) body.dis = dis;
    const cls = $r("rec-class").value.trim();
    if (cls && cls !== (entity.brickClass || "")) body.brickClass = cls;
    const remove = [...$r("rec-tag-list").querySelectorAll("input:checked")].map((b) => b.value);
    if (remove.length) body.remove = remove;
    const addName = $r("rec-add-name").value.trim();
    if (addName) body.set = { [addName]: parseTagValue($r("rec-add-value").value) };
    if (Object.keys(body).length === 1) {
      $r("rec-dialog").close();
      return;
    }
    try {
      await recordApi("PATCH", "/records", body);
      $r("rec-dialog").close();
      if (onSaved) onSaved();
    } catch (err) {
      $r("rec-error").textContent = err.message;
      $r("rec-error").hidden = false;
    }
  });
  $r("rec-delete").addEventListener("click", async () => {
    const { entity, onSaved } = recordEditing;
    const typed = prompt(`Delete ${entity.dis} from the model? Its history stays. Type its name to confirm:`);
    if (typed === null) return;
    if (typed.trim() !== entity.dis) {
      $r("rec-error").textContent = "that doesn't match - nothing deleted";
      $r("rec-error").hidden = false;
      return;
    }
    try {
      await recordApi("DELETE", `/records?id=${encodeURIComponent(entity.uri)}`);
      $r("rec-dialog").close();
      if (onSaved) onSaved();
    } catch (err) {
      $r("rec-error").textContent = err.message;
      $r("rec-error").hidden = false;
    }
  });
}

// --- creating records (Baler#24) -------------------------------------------

let recordCreating = null;

function recordNewButton(text, kind, link, parents, onSaved) {
  const btn = Object.assign(document.createElement("button"), { type: "button", className: "edit-btn", textContent: text });
  btn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    ev.preventDefault();
    openCreateDialog(kind, link, parents, onSaved);
  });
  return btn;
}

// kind "equip": `parents` are locations/equips it may be part of; kind
// "point": `link` is its equip (required) and the key starts under it.
function openCreateDialog(kind, link, parents, onSaved) {
  recordCreating = { kind, link, onSaved };
  const $r = (id) => document.getElementById(id);
  $r("new-form").reset();
  $r("new-error").hidden = true;
  $r("new-title").textContent = kind === "point" ? `New point on ${link.dis}` : "New equipment";
  const keyOf = (uri) => uri.replace(/^urn:(equip|point):/, "");
  $r("new-key").value = link ? `${keyOf(link.uri)}/` : "";
  // inline style, not `hidden`: `dialog label { display: block }` overrides the attribute
  $r("new-parent-label").style.display = kind === "point" ? "none" : "";
  const sel = $r("new-parent");
  sel.length = 1;
  for (const p of parents || []) sel.appendChild(new Option(`${p.dis}${p.brickClass ? ` (${p.brickClass})` : ""}`, p.uri));
  $r("new-dialog").showModal();
}

function wireCreateDialog() {
  const $r = (id) => document.getElementById(id);
  if (!$r("new-dialog")) return;
  $r("new-cancel").addEventListener("click", () => $r("new-dialog").close());
  $r("new-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const { kind, link, onSaved } = recordCreating;
    const tags = {};
    for (const t of $r("new-tags").value.split(/\s+/).filter(Boolean)) tags[t] = true;
    const body = { kind, key: $r("new-key").value.trim(), dis: $r("new-dis").value.trim(), tags };
    if ($r("new-class").value.trim()) body.brickClass = $r("new-class").value.trim();
    if (kind === "point") body.equip = link.uri;
    else if ($r("new-parent").value) body.parent = $r("new-parent").value;
    try {
      await recordApi("POST", "/records", body);
      $r("new-dialog").close();
      if (onSaved) onSaved();
    } catch (err) {
      $r("new-error").textContent = err.message;
      $r("new-error").hidden = false;
    }
  });
}

wireRecordEditor();
wireCreateDialog();

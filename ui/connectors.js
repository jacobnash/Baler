// Connectors page: every Windrow edge node, the controllers it found, and
// the connectors that poll them - add, edit, delete, scan. All through
// ops_api's /ops/edges (baler.edges), which reaches the Windrow beside
// Baler over HTTP and remote ones over the MQTT command channel; the page
// doesn't care which. No build step, same as the rest of ui/.
const OPS_URL = new URLSearchParams(location.search).get("ops") || "/ops";
const REFRESH_MS = 15000;
const $ = (id) => document.getElementById(id);
const statusEl = $("status");

const state = { nodes: [], node: null, devices: [], bacnet: [], modbus: [], sitePrefixes: {}, editing: null, learned: [] };

function el(tag, props, children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k.includes("-")) node.setAttribute(k, v); else node[k] = v;
  }
  for (const c of children || []) node.append(typeof c === "string" ? document.createTextNode(c) : c);
  return node;
}

async function ops(method, path, body) {
  const headers = { ...authHeaders() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`${OPS_URL}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (res.status === 401) {
    handleUnauthorized();
    throw new Error("session ended - sign in again");
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (res.status === 403) throw new Error((data && data.error) || "your role can't do that");
  if (!res.ok) throw new Error((data && data.error) || `${method} ${path} failed (${res.status})`);
  return data;
}

function act(action, body) {
  const n = state.node;
  return ops("POST", `/edges/${encodeURIComponent(n.site_id)}/${encodeURIComponent(n.instance_id)}/actions/${action}`, body || {})
    .then((r) => r.result);
}

const isAdmin = () => roleAtLeast("admin");
const when = (v) => {
  if (v === null || v === undefined || v === "") return "—";
  const d = typeof v === "number" ? new Date(v * 1000) : new Date(v);
  return isNaN(d) ? String(v) : d.toLocaleString();
};

// ------------------------------------------------------------- nodes

function renderNodes() {
  const box = $("nodes");
  box.innerHTML = "";
  $("nodes-empty").hidden = state.nodes.length > 0;
  for (const n of state.nodes) {
    const key = `${n.site_id}/${n.instance_id}`;
    const selected = state.node && key === `${state.node.site_id}/${state.node.instance_id}`;
    const dot = el("span", { className: `dot ${n.online ? "online" : "offline"}`, title: n.online ? "online" : "offline" });
    const meta = n.online
      ? `${n.kind === "local" ? "beside Baler" : "remote"} · ${n.pending_devices ? `${n.pending_devices} new device(s) · ` : ""}seen ${when(n.last_seen)}`
      : (n.error ? `offline - ${n.error}` : `offline · last seen ${when(n.last_seen)}`);
    const btn = el("button", { type: "button", className: "node", role: "radio" }, [
      el("span", { className: "node-name" }, [dot, n.label]),
      el("span", { className: "node-meta", textContent: meta }),
    ]);
    btn.setAttribute("aria-checked", selected ? "true" : "false");
    btn.addEventListener("click", () => selectNode(n));
    box.append(btn);
  }
}

async function loadNodes() {
  try {
    state.nodes = await ops("GET", "/edges");
  } catch (err) {
    state.nodes = [];
    statusEl.textContent = err.message;
  }
  if (state.node) {
    state.node = state.nodes.find((n) => n.site_id === state.node.site_id && n.instance_id === state.node.instance_id) || null;
  }
  if (!state.node && state.nodes.length) {
    const wanted = location.hash.slice(1);
    state.node = state.nodes.find((n) => `${n.site_id}/${n.instance_id}` === wanted) || state.nodes.find((n) => n.online) || state.nodes[0];
  }
  renderNodes();
  $("node-body").hidden = !state.node;
  if (!state.nodes.length && !statusEl.textContent.startsWith("can")) statusEl.textContent = "no edge nodes yet";
}

async function selectNode(n) {
  state.node = n;
  history.replaceState(null, "", `#${n.site_id}/${n.instance_id}`);
  renderNodes();
  $("node-body").hidden = false;
  await loadNode();
}

// --------------------------------------------------- devices/connectors

let loading = false;
async function loadNode() {
  if (!state.node || loading || $("connector-dialog").open || $("credential-dialog").open) return;
  loading = true;
  statusEl.textContent = "loading…";
  try {
    const [d, b, m] = await Promise.allSettled([act("list_devices"), act("list_connections"), act("modbus_list_connections")]);
    if (d.status === "fulfilled") state.devices = d.value || [];
    if (b.status === "fulfilled") state.bacnet = b.value || [];
    if (m.status === "fulfilled") state.modbus = m.value || [];
    renderConnections();
    renderDevices();
    const failed = [d, b, m].filter((r) => r.status === "rejected");
    statusEl.textContent = failed.length
      ? `couldn't load from ${state.node.label}: ${failed[0].reason.message}`
      : `${state.bacnet.length + state.modbus.length} connector(s), ${state.devices.length} device(s) on ${state.node.label}`;
    $("node-note").textContent = state.node.online ? "" : "This node is offline: changes will fail until it reconnects.";
  } finally {
    loading = false;
  }
}

function loginCell(record, target) {
  const cred = record.credential;
  const td = el("td", {}, [el("span", { textContent: cred ? cred.username || "set" : "—" })]);
  if (isAdmin()) {
    const b = el("button", { type: "button", textContent: cred ? "Change" : "Set" });
    b.addEventListener("click", () => openCredential(target, record.id));
    td.append(" ", b);
  }
  return td;
}

function renderConnections() {
  const tbody = $("connections-table").querySelector("tbody");
  tbody.innerHTML = "";
  const rows = [
    ...state.bacnet.map((c) => ({ kind: "bacnet", c, device: `${c.device_address} · instance ${c.device_instance}`,
                                  points: (c.points || []).map((p) => p.label) })),
    ...state.modbus.map((c) => ({ kind: "modbus", c, device: `${c.host}:${c.port} · unit ${c.device_id ?? 1}`,
                                  points: Object.keys(c.points || {}) })),
  ];
  for (const r of rows) {
    const actions = el("td", { className: "actions" });
    if (isAdmin()) {
      const edit = el("button", { type: "button", textContent: "Edit" });
      edit.addEventListener("click", () => openConnector({ kind: r.kind, record: r.c }));
      const del = el("button", { type: "button", textContent: "Delete", className: "danger" });
      del.addEventListener("click", () => deleteConnector(r.kind, r.c));
      actions.append(edit, del);
    }
    const pts = el("td", { textContent: `${r.points.length}`, title: r.points.join(", ") });
    tbody.append(el("tr", {}, [
      el("td", { textContent: r.kind === "bacnet" ? "BACnet" : "Modbus" }),
      el("td", { textContent: r.device }),
      el("td", { className: "mono", textContent: r.c.topic_prefix }),
      pts,
      el("td", { textContent: `${r.c.poll_interval ?? "—"} s` }),
      loginCell(r.c, r.kind === "bacnet" ? "connection" : "modbus_connection"),
      actions,
    ]));
  }
  if (!rows.length) {
    tbody.append(el("tr", {}, [el("td", { colSpan: 7, className: "hint",
      textContent: isAdmin() ? "No connectors yet. Add one, or scan and connect a device found below." : "No connectors yet." })]));
  }
}

function deviceLabel(d) {
  return d.protocol === "bacnet" ? `${d.address} · instance ${d.device_instance}` : `${d.host}:${d.port}`;
}

function renderDevices() {
  const tbody = $("devices-table").querySelector("tbody");
  tbody.innerHTML = "";
  const order = { pending: 0, provisioned: 1, ignored: 2 };
  const devices = [...state.devices].sort((a, b) => (order[a.status] ?? 3) - (order[b.status] ?? 3));
  for (const d of devices) {
    const actions = el("td", { className: "actions" });
    if (isAdmin()) {
      if (d.status === "pending") {
        const c = el("button", { type: "button", textContent: "Connect…", className: "primary" });
        c.addEventListener("click", () => openConnector({ kind: d.protocol, device: d }));
        actions.append(c);
      }
      if (d.status !== "ignored") {
        const ig = el("button", { type: "button", textContent: "Ignore" });
        ig.addEventListener("click", async () => {
          const ok = await confirmDanger({ title: "Ignore this device?", body: `${deviceLabel(d)} drops out of the list of new devices. You can bring it back later.`, action: "Ignore", danger: false });
          if (ok) run(() => act("set_device_status", { id: d.id, status: "ignored" }), "Device ignored");
        });
        actions.append(ig);
      } else {
        const un = el("button", { type: "button", textContent: "Restore" });
        un.addEventListener("click", () => run(() => act("set_device_status", { id: d.id, status: "pending" }), "Device restored"));
        actions.append(un);
      }
      if (d.default_credential_flag) {
        const re = el("button", { type: "button", textContent: "Recheck login" });
        re.addEventListener("click", () => run(() => act("recheck_credentials", { id: d.id }), "Rechecked"));
        actions.append(re);
      }
    }
    const vendor = d.protocol === "bacnet" ? (d.vendor_id !== undefined ? `vendor ${d.vendor_id}` : "—")
      : [d.vendor, d.product_code].filter(Boolean).join(" / ") || "—";
    const status = el("td", {}, [el("span", { className: `badge status-${d.status}`, textContent: d.status === "provisioned" ? "connected" : d.status })]);
    if (d.default_credential_flag) {
      // Never print the working password - only that the factory default works.
      status.append(" ", el("span", { className: "badge default-cred-flag", textContent: "factory login works",
        title: "The device accepts a well-known default login. Change it on the device." }));
    }
    tbody.append(el("tr", {}, [
      el("td", { textContent: d.protocol === "bacnet" ? "BACnet" : "Modbus" }),
      el("td", { textContent: deviceLabel(d) }),
      el("td", { textContent: vendor }),
      status,
      el("td", { textContent: when(d.last_seen_at) }),
      loginCell(d, "device"),
      actions,
    ]));
  }
  if (!devices.length) {
    tbody.append(el("tr", {}, [el("td", { colSpan: 7, className: "hint",
      textContent: "No devices found yet. Scan, or add a connector by address." })]));
  }
}

async function run(fn, okMessage) {
  try {
    await fn();
    if (okMessage) toast(okMessage);
  } catch (err) {
    toast(err.message, "error");
  }
  await loadNode();
}

async function deleteConnector(kind, c) {
  const ok = await confirmDanger({
    title: "Delete this connector?",
    body: `Polling stops and its points stop updating in Baler. History already recorded is kept.`,
    action: "Delete connector",
    typeToConfirm: c.topic_prefix,
  });
  if (!ok) return;
  await run(() => act(kind === "bacnet" ? "delete_connection" : "modbus_delete_connection", { id: c.id }), "Connector deleted");
}

// ------------------------------------------------------------- scans

async function scanBacnet() {
  statusEl.textContent = `scanning ${state.node.label} for BACnet devices…`;
  await run(async () => {
    const found = await act("discover", {});
    toast(`Scan finished: ${Array.isArray(found) ? found.length : "some"} device(s) answered`);
  });
}

async function scanModbus() {
  const cidr = prompt("Subnet to sweep for Modbus TCP devices (port 502), e.g. 192.168.1.0/24:", "");
  if (!cidr) return;
  if (!/^\d{1,3}(\.\d{1,3}){3}\/\d{1,2}$/.test(cidr.trim())) {
    toast("That isn't a subnet like 192.168.1.0/24", "error");
    return;
  }
  statusEl.textContent = `sweeping ${cidr} from ${state.node.label}…`;
  await run(async () => {
    const found = await act("modbus_discover", { cidr: cidr.trim() });
    toast(`Sweep finished: ${Array.isArray(found) ? found.length : "some"} device(s) answered`);
  });
}

// ---------------------------------------------------- connector dialog

const dlg = $("connector-dialog");

function slug(s) {
  return String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40);
}

function defaultPrefix(label) {
  const site = state.node.kind === "remote" ? state.sitePrefixes[state.node.site_id] : null;
  return `${site || "fbf/" + slug(state.node.site_id === "local" ? "site" : state.node.site_id)}/${slug(label) || "equipment"}`;
}

function setProtocol(kind) {
  for (const r of document.querySelectorAll('input[name="protocol"]')) r.checked = r.value === kind;
  $("bacnet-fields").hidden = kind !== "bacnet";
  $("modbus-fields").hidden = kind !== "modbus";
}

function protocol() {
  return document.querySelector('input[name="protocol"]:checked').value;
}

// A Modbus point is a bare holding-register number (the original form) or
// {register, function, type, word_order, byte_order, scale, offset, unit,
// writable} (Baler#128). modbusSpec sends only what differs from the
// defaults, so a plain holding/uint16 row stays a bare number and old
// connectors round-trip unchanged; a unit on a bare row goes in the
// connection's `units` map instead, which older Windrows understand too.
const MB_FUNCTIONS = [["holding", "Holding reg"], ["input", "Input reg"], ["coil", "Coil"], ["discrete", "Discrete in"]];
const MB_TYPES = ["uint16", "int16", "uint32", "int32", "float32"];
const MB_BITS = ["coil", "discrete"];

function modbusSpec(f) {
  const bits = MB_BITS.includes(f.function);
  const spec = { register: f.register };
  if (f.function !== "holding") spec.function = f.function;
  if (!bits) {
    if (f.type !== "uint16") spec.type = f.type;
    if (f.type.endsWith("32") && f.word_order !== "big") spec.word_order = f.word_order;
    if (f.byte_order && f.byte_order !== "big") spec.byte_order = f.byte_order;
    if (f.scale !== 1) spec.scale = f.scale;
    if (f.offset !== 0) spec.offset = f.offset;
  }
  if (f.writable && (f.function === "holding" || f.function === "coil")) spec.writable = true;
  if (Object.keys(spec).length === 1) return { spec: f.register, unit: f.unit || null };
  if (f.unit) spec.unit = f.unit;
  return { spec, unit: null };
}

function addRegisterRow(label = "", spec = "", unit = "") {
  const p = typeof spec === "object" && spec ? spec : { register: spec };
  const tbody = $("modbus-points").querySelector("tbody");
  const del = el("button", { type: "button", textContent: "Remove", className: "danger" });
  const select = (cls, options, value, aria) => {
    const s = el("select", { className: cls, "aria-label": aria },
      options.map(([v, t]) => el("option", { value: v, textContent: t })));
    s.value = value;
    return s;
  };
  const fn = select("reg-fn", MB_FUNCTIONS, p.function || "holding", "Function");
  const type = select("reg-type", MB_TYPES.map((t) => [t, t]), p.type || "uint16", "Data type");
  const word = select("reg-word", [["big", "high first"], ["little", "low first"]], p.word_order || "big", "Word order");
  const scale = el("input", { type: "number", className: "reg-scale", value: p.scale ?? 1, step: "any", "aria-label": "Scale" });
  const offset = el("input", { type: "number", className: "reg-offset", value: p.offset ?? 0, step: "any", "aria-label": "Offset" });
  const write = el("input", { type: "checkbox", className: "reg-write", checked: !!p.writable, "aria-label": "Writable" });
  const sync = () => {
    const bits = MB_BITS.includes(fn.value);
    type.disabled = scale.disabled = offset.disabled = bits;
    word.disabled = bits || !type.value.endsWith("32");
    write.disabled = fn.value === "input" || fn.value === "discrete";
  };
  fn.addEventListener("change", sync);
  type.addEventListener("change", sync);
  const tr = el("tr", {}, [
    el("td", {}, [el("input", { type: "text", className: "reg-label", value: label, placeholder: "zone_temp", "aria-label": "Point name" })]),
    el("td", {}, [fn]),
    el("td", {}, [el("input", { type: "number", className: "reg-num", value: p.register, min: 0, max: 65535, "aria-label": "Register" })]),
    el("td", {}, [type]),
    el("td", {}, [word]),
    el("td", {}, [scale]),
    el("td", {}, [offset]),
    el("td", {}, [el("input", { type: "text", className: "reg-unit", value: p.unit || unit || "", placeholder: "°F", "aria-label": "Unit" })]),
    el("td", {}, [write]),
    el("td", {}, [del]),
  ]);
  tr.dataset.byteOrder = p.byte_order || "";  // no column for it; kept as loaded
  sync();
  del.addEventListener("click", () => tr.remove());
  tbody.append(tr);
}

function renderLearned(points, selected) {
  const tbody = $("bacnet-points").querySelector("tbody");
  tbody.innerHTML = "";
  state.learned = points;
  points.forEach((p, i) => {
    const cb = el("input", { type: "checkbox", className: "pt-on", checked: selected ? selected.has(p.object_identifier) : true });
    cb.dataset.i = i;
    tbody.append(el("tr", {}, [
      el("td", {}, [cb]),
      el("td", {}, [el("input", { type: "text", className: "pt-label", value: p.label || "", "aria-label": "Name in Baler" })]),
      el("td", { className: "mono", textContent: p.object_identifier }),
      el("td", { textContent: p.units || "" }),
    ]));
  });
  $("bacnet-points-wrap").hidden = !points.length;
}

function openConnector({ kind, record = null, device = null }) {
  state.editing = record ? { kind, id: record.id } : null;
  $("connector-error").hidden = true;
  $("connector-title").textContent = record ? `Edit connector · ${record.topic_prefix}` : device ? `Connect ${deviceLabel(device)}` : "Add connector";
  $("connector-save").textContent = record ? "Save changes" : "Create connector";
  $("protocol-pick").disabled = !!(record || device);
  setProtocol(kind || "bacnet");
  $("learn-status").textContent = "";
  $("modbus-points").querySelector("tbody").innerHTML = "";
  renderLearned([]);
  $("c-poll").value = record?.poll_interval ?? 5;
  for (const id of ["c-address", "c-instance", "c-host"]) $(id).disabled = !!(record || device);
  $("c-port").disabled = $("c-unit").disabled = !!(record || device);
  if (kind === "modbus") {
    $("c-host").value = record?.host || device?.host || "";
    $("c-port").value = record?.port || device?.port || 502;
    $("c-unit").value = record?.device_id ?? device?.device_id ?? 1;
    const pts = Object.entries(record?.points || {});
    if (pts.length) pts.forEach(([l, r]) => addRegisterRow(l, r, record?.units?.[l])); else addRegisterRow();
  } else {
    $("c-address").value = record?.device_address || device?.address || "";
    $("c-instance").value = record?.device_instance ?? device?.device_instance ?? "";
    if (record) renderLearned(record.points || [], new Set((record.points || []).map((p) => p.object_identifier)));
  }
  $("c-prefix").value = record?.topic_prefix || (device ? defaultPrefix(deviceLabel(device)) : "");
  $("c-prefix").disabled = false;
  $("prefix-help").innerHTML = state.node.kind === "remote" && state.sitePrefixes[state.node.site_id]
    ? `Must start with this site's prefix <code>${state.sitePrefixes[state.node.site_id]}/</code>, or the broker drops the data. One connector = one piece of equipment.`
    : "Becomes the equipment's id in Baler, e.g. <code>fbf/site/ahu-1</code>. One connector = one piece of equipment.";
  dlg.showModal();
}

async function learn() {
  const address = $("c-address").value.trim();
  const instance = Number($("c-instance").value);
  if (!address || !Number.isInteger(instance)) {
    showError("Enter the device address and instance first.");
    return;
  }
  $("learn-status").textContent = "reading the device's object list - large controllers take a minute…";
  $("learn-btn").disabled = true;
  try {
    const keep = new Set([...document.querySelectorAll(".pt-on:checked")].map((cb) => state.learned[cb.dataset.i].object_identifier));
    const points = await act("learn", { device_address: address, device_instance: instance });
    renderLearned(points, keep.size ? keep : null);
    $("learn-status").textContent = `${points.length} point(s) found`;
    if (!$("c-prefix").value) $("c-prefix").value = defaultPrefix(`device-${instance}`);
  } catch (err) {
    $("learn-status").textContent = "";
    showError(`Couldn't read the device: ${err.message}`);
  } finally {
    $("learn-btn").disabled = false;
  }
}

function showError(msg) {
  const e = $("connector-error");
  e.textContent = msg;
  e.hidden = false;
}

function validPrefix(prefix) {
  if (!prefix || /[\s#+]/.test(prefix) || prefix.startsWith("/") || prefix.endsWith("/")) {
    return "Topic prefix is like fbf/site/ahu-1: no spaces, no # or +, no leading or trailing /.";
  }
  const site = state.node.kind === "remote" ? state.sitePrefixes[state.node.site_id] : null;
  if (site && !(prefix === site || prefix.startsWith(site + "/"))) return `Topic prefix must start with ${site}/ for this site.`;
  return null;
}

async function saveConnector(ev) {
  ev.preventDefault();
  $("connector-error").hidden = true;
  const kind = protocol();
  const prefix = $("c-prefix").value.trim();
  const poll = Number($("c-poll").value);
  const bad = validPrefix(prefix);
  if (bad) return showError(bad);
  if (!(poll >= 1)) return showError("Poll interval is at least 1 second.");
  let body;
  if (kind === "bacnet") {
    const points = [...document.querySelectorAll(".pt-on:checked")].map((cb) => {
      const p = state.learned[cb.dataset.i];
      const label = cb.closest("tr").querySelector(".pt-label").value.trim() || p.label;
      return { ...p, label };
    });
    if (!points.length) return showError("Pick at least one point (read the point list first).");
    const labels = points.map((p) => p.label);
    if (new Set(labels).size !== labels.length) return showError("Two points have the same name - names must be unique.");
    body = { topic_prefix: prefix, poll_interval: poll, points };
    if (!state.editing) Object.assign(body, { device_address: $("c-address").value.trim(), device_instance: Number($("c-instance").value) });
  } else {
    const points = {};
    const units = {};
    for (const tr of $("modbus-points").querySelectorAll("tbody tr")) {
      const q = (cls) => tr.querySelector(cls);
      const label = q(".reg-label").value.trim();
      const reg = q(".reg-num").value;
      if (!label && reg === "") continue;
      if (!label || reg === "" || !Number.isInteger(Number(reg)) || Number(reg) < 0 || Number(reg) > 65535) {
        return showError("Each register row needs a name and a register number 0-65535.");
      }
      if (label in points) return showError(`"${label}" is used twice - names must be unique.`);
      const scale = Number(q(".reg-scale").value || 1);
      const offset = Number(q(".reg-offset").value || 0);
      if (!Number.isFinite(scale) || scale === 0 || !Number.isFinite(offset)) return showError(`"${label}": scale must be a non-zero number, offset a number.`);
      const out = modbusSpec({
        register: Number(reg), function: q(".reg-fn").value, type: q(".reg-type").value,
        word_order: q(".reg-word").value, byte_order: tr.dataset.byteOrder, scale, offset,
        unit: q(".reg-unit").value.trim(), writable: q(".reg-write").checked,
      });
      points[label] = out.spec;
      if (out.unit) units[label] = out.unit;
    }
    if (!Object.keys(points).length) return showError("Add at least one register.");
    body = { topic_prefix: prefix, poll_interval: poll, points, units };
    if (!state.editing) {
      const host = $("c-host").value.trim();
      if (!host) return showError("Enter the device's host or IP.");
      Object.assign(body, { host, port: Number($("c-port").value) || 502, device_id: Number($("c-unit").value) || 1 });
    }
  }
  $("connector-save").disabled = true;
  try {
    if (state.editing) {
      await act(kind === "bacnet" ? "update_connection" : "modbus_update_connection", { id: state.editing.id, ...body });
      toast("Connector updated");
    } else {
      await act(kind === "bacnet" ? "create_connection" : "modbus_create_connection", body);
      toast(`Connector created - points appear under ${prefix} as they report`);
    }
    dlg.close();
    await loadNode();
  } catch (err) {
    showError(err.message);
  } finally {
    $("connector-save").disabled = false;
  }
}

// ---------------------------------------------------- credential dialog

let credTarget = null;
function openCredential(target, id) {
  credTarget = { target, id };
  $("credential-form").reset();
  $("credential-dialog").showModal();
}

async function saveCredential(ev) {
  ev.preventDefault();
  const credential = { username: $("credential-username").value, password: $("credential-password").value };
  $("credential-dialog").close();
  await run(() => act("set_credential", { ...credTarget, credential }), "Login saved");
}

// ------------------------------------------------------------- wiring

async function loadSitePrefixes() {
  try {
    const sites = await fetch("/auth/sites", { headers: authHeaders() }).then((r) => (r.ok ? r.json() : []));
    for (const s of sites || []) if (s.topic_prefixes && s.topic_prefixes.length) state.sitePrefixes[s.id] = s.topic_prefixes[0];
  } catch {
    // only used for defaults and a friendlier prefix check
  }
}

async function init() {
  for (const b of document.querySelectorAll(".admin-only")) b.hidden = !isAdmin();
  await loadSitePrefixes();
  await loadNodes();
  await loadNode();
  setInterval(async () => { await loadNodes(); await loadNode(); }, REFRESH_MS);
}

$("add-connector").addEventListener("click", () => openConnector({ kind: "bacnet" }));
$("scan-bacnet").addEventListener("click", scanBacnet);
$("scan-modbus").addEventListener("click", scanModbus);
$("refresh").addEventListener("click", async () => { await loadNodes(); await loadNode(); });
for (const r of document.querySelectorAll('input[name="protocol"]')) r.addEventListener("change", () => {
  setProtocol(protocol());
  if (protocol() === "modbus" && !$("modbus-points").querySelector("tbody tr")) addRegisterRow();
});
$("learn-btn").addEventListener("click", learn);
$("add-register").addEventListener("click", () => addRegisterRow());
$("all-points").addEventListener("change", (e) => { for (const cb of document.querySelectorAll(".pt-on")) cb.checked = e.target.checked; });
$("connector-cancel").addEventListener("click", () => dlg.close());
$("connector-form").addEventListener("submit", saveConnector);
$("credential-cancel").addEventListener("click", () => $("credential-dialog").close());
$("credential-form").addEventListener("submit", saveCredential);

initAuth(init);

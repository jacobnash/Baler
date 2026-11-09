// Connectors page: every Windrow edge node, the controllers it found, and
// the connectors that poll them - add, edit, delete, scan. All through
// ops_api's /ops/edges (baler.edges), which reaches the Windrow beside
// Baler over HTTP and remote ones over the MQTT command channel; the page
// doesn't care which. No build step, same as the rest of ui/.
// How-to: docs-site/pages/connectors.mdx (linked from the page).
const OPS_URL = apiBase("ops", "/ops");
const REFRESH_MS = 15000;
// Bigger than this asks first; bigger than MIN_PREFIX_BITS is refused.
const SCAN_CONFIRM_BITS = 22;
const MIN_PREFIX_BITS = 16;
// A first read selects every point only on a small device; on a big one
// the integrator picks (search/filter) instead of unticking hundreds.
const SELECT_ALL_UP_TO = 50;
const $ = (id) => document.getElementById(id);
const statusEl = $("status");

const state = {
  nodes: [], node: null, devices: [], bacnet: [], modbus: [], loadedFor: null, loadedAt: null,
  announced: null, // the node's announced devices, or null when it can't list them
  editing: null, // {kind, id} while editing a connector
  // The BACnet point picker's model, one entry per object, keyed by
  // object_identifier so a re-read keeps what the integrator did (Baler#112).
  points: [], // [{oid, learned, stored, label, edited, on, tr}]
};

function el(tag, props, children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k.includes("-")) node.setAttribute(k, v); else node[k] = v;
  }
  for (const c of children || []) if (c !== null && c !== undefined) node.append(typeof c === "string" ? document.createTextNode(c) : c);
  return node;
}

const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;
const nodeKey = (n) => `${n.site_id}/${n.instance_id}`;

async function ops(method, path, body, signal) {
  const headers = { ...authHeaders() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`${OPS_URL}${path}`, { method, headers, signal, body: body === undefined ? undefined : JSON.stringify(body) });
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

function act(action, body, signal) {
  const n = state.node;
  return ops("POST", `/edges/${encodeURIComponent(n.site_id)}/${encodeURIComponent(n.instance_id)}/actions/${action}`, body || {}, signal)
    .then((r) => r.result);
}

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    if (signal) signal.addEventListener("abort", () => { clearTimeout(t); reject(new DOMException("stopped", "AbortError")); }, { once: true });
  });
}

// A scan or learn runs as a job on the node (Baler#139): start it, then ask
// how it's going until it's done - no single request has to outlast it.
// A Windrow from before jobs gets the old one-shot action instead.
const JOB_POLL_MS = 1500;
async function runJob(job, body, signal, onProgress) {
  let j;
  try {
    j = await act("start_job", { job, ...body }, signal);
  } catch (err) {
    if (/unknown action|answered 404/.test(err.message)) return act(job, body, signal);
    throw err;
  }
  const id = j.id;
  let misses = 0;
  while (j.state === "running") {
    if (onProgress) onProgress(j);
    await sleep(JOB_POLL_MS, signal);
    try {
      j = await act("get_job", { id }, signal);
      misses = 0;
    } catch (err) {
      // a remote node can miss one answer; give up after a few in a row
      if (err.name === "AbortError" || ++misses >= 4) throw err;
    }
  }
  if (j.state === "failed") throw new Error(j.error || `the ${job} job failed`);
  return j.result;
}

const isAdmin = () => roleAtLeast("admin");
// Admin, and a node that can answer: an offline node's actions only fail later.
const canChange = () => isAdmin() && !!state.node && !!state.node.online;

function toDate(v) {
  if (v === null || v === undefined || v === "") return null;
  const d = typeof v === "number" ? new Date(v * 1000) : new Date(v);
  return isNaN(d) ? null : d;
}
const when = (v) => {
  const d = toDate(v);
  return d ? d.toLocaleString() : (v ? String(v) : "—");
};
// Short form for table cells: a time today, a date and time otherwise.
const short = (v) => {
  const d = toDate(v);
  if (!d) return "—";
  return d.toDateString() === new Date().toDateString()
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
    : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
};

// The header status is a polite live region: only say something when it changes.
function setStatus(text) {
  if (statusEl.textContent !== text) statusEl.textContent = text;
}

// Re-rendering a table drops keyboard focus (WCAG 2.4.3); buttons carry a
// data-fk naming what they act on, and focus goes back to the same one.
function keepFocus(render) {
  const fk = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.fk : null;
  render();
  if (fk) {
    const again = document.querySelector(`[data-fk="${CSS.escape(fk)}"]`);
    if (again) again.focus();
  }
}

function button(text, opts) {
  const o = opts || {};
  const b = el("button", { type: "button", textContent: text, className: o.className || "" });
  if (o.label) b.setAttribute("aria-label", o.label);
  if (o.fk) b.dataset.fk = o.fk;
  b.disabled = !!o.disabled;
  if (o.onClick) b.addEventListener("click", o.onClick);
  return b;
}

// ------------------------------------------------------------- nodes

// Device announce (Windrow): devices on the LAN say "I am here", and
// connections with that device_name follow them to a new address.
function announceMeta(n) {
  if (typeof n.announced !== "number" || (!n.announced && !n.announce_rejected)) return "";
  const parts = [];
  if (n.announced) parts.push(`${n.announced_online ?? 0} of ${n.announced} announced online`);
  if (n.announce_rejected) parts.push(`${n.announce_rejected} announcement${n.announce_rejected === 1 ? "" : "s"} refused`);
  return `${parts.join(" · ")} · `;
}

function nodeMeta(n) {
  if (!n.online) return n.error ? `offline - ${n.error}` : `offline · last seen ${when(n.last_seen)}`;
  const where = n.kind === "local" ? "beside Baler" : (n.site_name ? `site ${n.site_name}` : "remote");
  const pending = n.pending_devices ? `${plural(n.pending_devices, "new device")} · ` : "";
  return `${where} · ${pending}${announceMeta(n)}seen ${short(n.last_seen)}`;
}

// Updates the buttons in place rather than rebuilding them, so the 15 s
// refresh never takes focus away from the one a keyboard user is on.
function renderNodes() {
  const box = $("nodes");
  const old = new Map([...box.children].map((b) => [b.dataset.key, b]));
  state.nodes.forEach((n, i) => {
    const key = nodeKey(n);
    let btn = old.get(key);
    if (btn) {
      old.delete(key);
    } else {
      btn = el("button", { type: "button", className: "node" }, [
        el("span", { className: "node-name" }, [el("span", { className: "dot" }), el("span", { className: "node-label" })]),
        el("span", { className: "node-meta" }),
      ]);
      btn.dataset.key = key;
      btn.addEventListener("click", () => selectNode(btn.dataset.key));
    }
    const dot = btn.querySelector(".dot");
    dot.className = `dot ${n.online ? "online" : "offline"}`;
    btn.querySelector(".node-label").textContent = n.label;
    btn.querySelector(".node-meta").textContent = nodeMeta(n);
    btn.setAttribute("aria-label", `${n.label}, ${n.online ? "online" : "offline"}: ${nodeMeta(n)}`);
    btn.setAttribute("aria-pressed", state.node && nodeKey(state.node) === key ? "true" : "false");
    if (box.children[i] !== btn) box.insertBefore(btn, box.children[i] || null);
  });
  for (const b of old.values()) b.remove();
  $("nodes-empty").hidden = state.nodes.length > 0;
}

async function loadNodes() {
  try {
    state.nodes = await ops("GET", "/edges");
  } catch (err) {
    state.nodes = [];
    setStatus(`can't list edge nodes: ${err.message}`);
  }
  if (state.node) state.node = state.nodes.find((n) => nodeKey(n) === nodeKey(state.node)) || null;
  if (!state.node && state.nodes.length) {
    const wanted = decodeURIComponent(location.hash.slice(1));
    state.node = state.nodes.find((n) => nodeKey(n) === wanted) || state.nodes.find((n) => n.online) || state.nodes[0];
  }
  renderNodes();
  $("node-body").hidden = !state.node;
  if (!state.nodes.length && !statusEl.textContent.startsWith("can")) setStatus("no edge nodes yet");
}

async function selectNode(key) {
  const n = state.nodes.find((x) => nodeKey(x) === key);
  if (!n) return;
  state.node = n;
  history.replaceState(null, "", `#${key}`);
  renderNodes();
  $("node-body").hidden = false;
  await loadNode();
}

// --------------------------------------------------- devices/connectors

let loading = false;
async function loadNode() {
  if (!state.node || loading || document.querySelector("dialog[open]")) return;
  const n = state.node;
  const key = nodeKey(n);
  if (state.loadedFor !== key) {
    state.devices = []; state.bacnet = []; state.modbus = []; state.loadedAt = null; state.announced = null; state.announcedError = null;
  }
  renderToolbar();
  if (!n.online) {
    // Known to fail: don't ask an offline node every 15 s.
    renderAll();
    setStatus(`${n.label} is offline`);
    return;
  }
  loading = true;
  try {
    const [d, b, m, a] = await Promise.allSettled([act("list_devices"), act("list_connections"), act("modbus_list_connections"),
                                                   act("list_announced")]);
    if (!state.node || nodeKey(state.node) !== key) return; // switched nodes meanwhile
    if (d.status === "fulfilled") state.devices = d.value || [];
    if (b.status === "fulfilled") state.bacnet = b.value || [];
    if (m.status === "fulfilled") state.modbus = m.value || [];
    // A Windrow without announce (or a remote one whose command channel
    // doesn't take list_announced) can't list them: say so, don't fail the page.
    state.announced = a.status === "fulfilled" ? (a.value || []) : null;
    state.announcedError = a.status === "rejected" ? a.reason.message : null;
    const failed = [d, b, m].filter((r) => r.status === "rejected");
    if (failed.length < 3) { state.loadedFor = key; state.loadedAt = Date.now(); }
    renderAll();
    setStatus(failed.length
      ? `couldn't load from ${n.label}: ${failed[0].reason.message}`
      : `${plural(state.bacnet.length + state.modbus.length, "connector")}, ${plural(state.devices.length, "device")} on ${n.label}`);
  } finally {
    loading = false;
  }
}

function renderAll() {
  keepFocus(() => { renderConnections(); renderDevices(); renderAnnounced(); });
}

function servicesText(services) {
  return (services || []).map((sv) => {
    const id = sv.kind === "bacnet" && sv.instance !== undefined ? ` instance ${sv.instance}`
      : sv.kind === "modbus" && sv.unit !== undefined ? ` unit ${sv.unit}` : "";
    return `${sv.kind === "bacnet" ? "BACnet" : sv.kind === "modbus" ? "Modbus" : sv.kind}${sv.port ? ` :${sv.port}` : ""}${id}`;
  }).join(", ") || "—";
}

// The node's announced devices (GET /announced): which connections follow
// each one (by device_name) and where it has moved.
function renderAnnounced() {
  const n = state.node;
  const section = $("announced-section");
  const counted = n && typeof n.announced === "number";
  const list = state.announced;
  section.hidden = !n || (!counted && !(list && list.length));
  if (section.hidden) return;
  const summary = $("announced-summary");
  summary.textContent = counted
    ? `${n.announced_online ?? 0} of ${plural(n.announced, "device")} online${n.announce_rejected ? ` · ${n.announce_rejected} announcement${n.announce_rejected === 1 ? "" : "s"} refused (outside the allowed subnet, unsigned or too old)` : ""}`
    : "";
  const tbody = $("announced-table").querySelector("tbody");
  tbody.innerHTML = "";
  const followers = {};
  for (const r of connectorRows()) {
    const name = (r.c.device_name || "").toLowerCase();
    if (name) (followers[name] = followers[name] || []).push(r.c.topic_prefix);
  }
  for (const dv of list || []) {
    const moved = (dv.history || []).slice(-1)[0];
    const addr = el("td", { className: "mono" }, [dv.address || "—"]);
    if (moved) addr.append(el("div", { className: "hint small", textContent: `moved from ${moved.from} ${short(moved.at)}` }));
    const follows = followers[(dv.name || "").toLowerCase()] || [];
    tbody.append(el("tr", {}, [
      el("td", { className: "mono", textContent: dv.name }),
      addr,
      el("td", { textContent: servicesText(dv.services) }),
      el("td", { textContent: when(dv.last_seen), title: dv.first_seen ? `first seen ${when(dv.first_seen)}` : "" }),
      el("td", {}, [el("span", { className: `badge ${dv.online ? "poll-ok" : "poll-error"}`, textContent: dv.online ? "online" : "offline" })]),
      el("td", { textContent: follows.length ? follows.join(", ") : "—" }),
    ]));
  }
  if (!list || !list.length) {
    const text = list === null
      ? `Can't list the announced devices: ${state.announcedError || "this node doesn't report them"}.`
      : "No device has announced itself to this node yet.";
    tbody.append(el("tr", {}, [el("td", { colSpan: 6, className: "hint", textContent: text })]));
  }
}

function renderToolbar() {
  const n = state.node;
  const banner = $("offline-banner");
  if (n && !n.online) {
    banner.hidden = false;
    banner.textContent = `${n.label} is offline${n.last_seen ? ` (last seen ${when(n.last_seen)})` : ""}${n.error ? `: ${n.error}` : ""}. `
      + (state.loadedAt ? `Showing what it reported at ${new Date(state.loadedAt).toLocaleTimeString()}. ` : "")
      + "Nothing can be changed until it reconnects.";
  } else {
    banner.hidden = true;
  }
  const off = !canChange();
  $("add-connector").disabled = off;
  $("scan-bacnet").disabled = off || !!busy;
  $("scan-modbus").disabled = off || !!busy;
}

function loginCell(record, target, what) {
  const cred = record.credential;
  const td = el("td", {}, [el("span", { textContent: cred ? cred.username || "set" : "—" })]);
  if (isAdmin()) {
    td.append(" ", button(cred ? "Change" : "Set", {
      label: `${cred ? "Change" : "Set"} the login for ${what}`, fk: `login:${target}:${record.id}`, disabled: !canChange(),
      onClick: () => openCredential(target, record.id, what),
    }));
  }
  return td;
}

const STATE_TEXT = { ok: "OK", partial: "Some failing", error: "Not reading", waiting: "Starting" };

// What a controls integrator asks first: is it polling? (Baler#110)
function pollCell(c, pointCount) {
  const st = c.poll_status;
  if (!st) {
    return el("td", { className: "hint", textContent: "—", title: "This edge node's Windrow doesn't report poll status yet (update Windrow)." });
  }
  const badge = el("span", { className: `badge poll-${st.state}`, textContent: STATE_TEXT[st.state] || st.state });
  const td = el("td", { className: "poll-cell" }, [badge]);
  if (st.state === "partial") badge.textContent = `${st.failing_count} of ${pointCount} failing`;
  if (st.last_value_at) td.append(" ", el("span", { textContent: `last value ${short(st.last_value_at)}` }));
  else if (st.state === "waiting") td.append(" ", el("span", { className: "hint", textContent: "no reads yet" }));
  else if (st.state === "error") td.append(" ", el("span", { className: "hint", textContent: "no value yet" }));
  const lines = [];
  if (st.last_poll_at) lines.push(`polled ${short(st.last_poll_at)}`);
  if (st.last_success_at && st.state !== "ok") lines.push(`last good read ${short(st.last_success_at)}`);
  if (lines.length) td.append(el("div", { className: "hint small", textContent: lines.join(" · ") }));
  if (st.last_error && st.state !== "ok") {
    const who = st.failing_points && st.failing_points.length && st.state === "partial" ? ` (${st.failing_points.slice(0, 3).join(", ")}${st.failing_count > 3 ? "…" : ""})` : "";
    td.append(el("div", { className: "poll-error", textContent: `${st.last_error}${who}` }));
  } else if (st.last_error && st.last_error_at) {
    td.title = `Last error ${when(st.last_error_at)}: ${st.last_error}`;
  }
  return td;
}

function equipLink(prefix) {
  const a = el("a", { className: "mono", href: `index.html?equip=${encodeURIComponent(`urn:equip:${prefix}`)}`, textContent: prefix });
  a.setAttribute("aria-label", `${prefix} - open this equipment in Equipment`);
  return a;
}

function connectorRows() {
  return [
    ...state.bacnet.map((c) => ({ kind: "bacnet", c, device: `${c.device_name ? c.device_name + " · " : ""}${c.device_address} · instance ${c.device_instance}`,
                                  points: (c.points || []).map((p) => p.label) })),
    ...state.modbus.map((c) => ({ kind: "modbus", c, device: `${c.device_name ? c.device_name + " · " : ""}${c.host}:${c.port} · unit ${c.device_id ?? 1}`,
                                  points: Object.keys(c.points || {}) })),
  ];
}

// Many connectors on one node (Baler#161): a filter over prefix and device,
// one group per controller (header with counts, collapsible), sorted by
// prefix with numbers in order (vav-2 before vav-10), and at most
// GROUP_PAGE rows per group until "Show more".
const GROUP_PAGE = 25;
const listView = { filter: "", failingOnly: false, collapsed: new Set(), shown: new Map() };
const byPrefix = (a, b) => a.c.topic_prefix.localeCompare(b.c.topic_prefix, undefined, { numeric: true });
const notOk = (r) => r.c.poll_status && r.c.poll_status.state !== "ok" && r.c.poll_status.state !== "waiting";

function connectorRow(r) {
  const actions = el("td", { className: "actions" });
  const prefix = r.c.topic_prefix;
  if (isAdmin()) {
    actions.append(
      button("Edit", { label: `Edit connector ${prefix}`, fk: `edit:${r.kind}:${r.c.id}`, disabled: !canChange(),
                       onClick: () => openConnector({ kind: r.kind, record: r.c }) }),
      button("Delete", { label: `Delete connector ${prefix}`, fk: `del:${r.kind}:${r.c.id}`, className: "danger", disabled: !canChange(),
                         onClick: () => deleteConnector(r.kind, r.c) }),
    );
  }
  return el("tr", {}, [
    el("td", { textContent: r.kind === "bacnet" ? "BACnet" : "Modbus" }),
    el("td", { textContent: r.device }),
    el("td", {}, [equipLink(prefix)]),
    el("td", { textContent: `${r.points.length}`, title: r.points.join(", ") }),
    el("td", { textContent: `${r.c.poll_interval ?? "—"} s` }),
    pollCell(r.c, r.points.length),
    loginCell(r.c, r.kind === "bacnet" ? "connection" : "modbus_connection", prefix),
    actions,
  ]);
}

function renderConnections() {
  const table = $("connections-table");
  for (const tb of table.querySelectorAll("tbody")) tb.remove();
  const all = connectorRows();
  const q = listView.filter.trim().toLowerCase();
  const rows = all.filter((r) => (!q || `${r.c.topic_prefix} ${r.device} ${r.kind}`.toLowerCase().includes(q))
    && (!listView.failingOnly || notOk(r))).sort(byPrefix);
  $("conn-tools").hidden = all.length < 2;
  $("conn-count").textContent = q || listView.failingOnly
    ? `${rows.length} of ${plural(all.length, "connector")}`
    : plural(all.length, "connector");

  const groups = new Map();
  for (const r of rows) {
    const key = `${r.kind}|${r.device}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  }
  // A header per controller only when there's something to group.
  const grouped = groups.size > 1 || rows.length > GROUP_PAGE;
  for (const [key, list] of groups) {
    const tbody = el("tbody");
    const open = !listView.collapsed.has(key) || !!q;
    if (grouped) {
      const failing = list.filter(notOk).length;
      const toggle = button(`${open ? "▾" : "▸"} ${list[0].kind === "bacnet" ? "BACnet" : "Modbus"} ${list[0].device}`, { className: "group-toggle", fk: `group:${key}` });
      toggle.setAttribute("aria-expanded", String(open));
      toggle.addEventListener("click", () => {
        if (listView.collapsed.has(key)) listView.collapsed.delete(key); else listView.collapsed.add(key);
        keepFocus(renderConnections);
      });
      const th = el("th", { colSpan: 8, scope: "rowgroup" }, [toggle, " ",
        el("span", { className: "hint", textContent: `${plural(list.length, "connector")}${failing ? ` · ${failing} not OK` : ""}` })]);
      tbody.append(el("tr", { className: "group-head" }, [th]));
    }
    if (open) {
      const limit = q ? list.length : (listView.shown.get(key) || GROUP_PAGE);
      for (const r of list.slice(0, limit)) tbody.append(connectorRow(r));
      if (list.length > limit) {
        const more = button(`Show ${Math.min(GROUP_PAGE, list.length - limit)} more (${list.length - limit} hidden)`, { fk: `more:${key}` });
        more.addEventListener("click", () => { listView.shown.set(key, limit + GROUP_PAGE); keepFocus(renderConnections); });
        tbody.append(el("tr", { className: "group-more" }, [el("td", { colSpan: 8 }, [more])]));
      }
    }
    table.append(tbody);
  }
  if (!rows.length) {
    const tbody = el("tbody");
    let td;
    if (all.length) {
      td = el("td", { colSpan: 8, className: "hint", textContent: "No connector matches the filter." });
    } else {
      let text = "No connectors yet.";
      if (state.node && !state.node.online && !state.loadedAt) text = "Can't list connectors while this node is offline.";
      else if (isAdmin()) text = "No connectors yet. Add one by address, or scan and add one for a device found below.";
      td = el("td", { colSpan: 8, className: "hint" }, [text, " "]);
      td.append(el("a", { href: "/docs/connectors", target: "_blank", rel: "noopener", textContent: "How to add a connector" }));
    }
    tbody.append(el("tr", {}, [td]));
    table.append(tbody);
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
  const off = !canChange();
  for (const d of devices) {
    const what = deviceLabel(d);
    const actions = el("td", { className: "actions" });
    if (isAdmin()) {
      if (d.status === "pending") {
        actions.append(button("Add connector…", { className: "primary", label: `Add a connector for ${what}`, fk: `add:${d.id}`, disabled: off,
                                                  onClick: () => openConnector({ kind: d.protocol, device: d }) }));
      }
      if (d.status !== "ignored") {
        actions.append(button("Ignore", { label: `Ignore ${what}`, fk: `ignore:${d.id}`, disabled: off, onClick: async () => {
          const ok = await confirmDanger({ title: "Ignore this device?", body: `${what} drops out of the list of new devices. You can bring it back later.`, action: "Ignore", danger: false });
          if (ok) run(() => act("set_device_status", { id: d.id, status: "ignored" }), "Device ignored");
        } }));
      } else {
        actions.append(button("Restore", { label: `Restore ${what}`, fk: `restore:${d.id}`, disabled: off,
                                           onClick: () => run(() => act("set_device_status", { id: d.id, status: "pending" }), "Device restored") }));
      }
      if (d.default_credential_flag) {
        actions.append(button("Recheck login", { label: `Recheck the factory login on ${what}`, fk: `recheck:${d.id}`, disabled: off,
                                                 onClick: () => run(() => act("recheck_credentials", { id: d.id }), "Rechecked") }));
      }
    }
    const vendor = d.protocol === "bacnet" ? (d.vendor_id !== undefined ? `vendor ${d.vendor_id}` : "—")
      : [d.vendor, d.product_code].filter(Boolean).join(" / ") || "—";
    const status = el("td", {}, [el("span", { className: `badge status-${d.status}`, textContent: d.status === "provisioned" ? "connected" : d.status })]);
    if (d.default_credential_flag) {
      // Never print the working password - only that the factory default works.
      status.append(" ", el("span", { className: "badge default-cred-flag", textContent: "factory login works" }),
        el("div", { className: "hint small", textContent: "It accepts a well-known default login. Change the password on the device." }));
    }
    tbody.append(el("tr", {}, [
      el("td", { textContent: d.protocol === "bacnet" ? "BACnet" : "Modbus" }),
      el("td", { textContent: what }),
      el("td", { textContent: vendor }),
      status,
      el("td", { textContent: when(d.last_seen_at) }),
      loginCell(d, "device", what),
      actions,
    ]));
  }
  if (!devices.length) {
    tbody.append(el("tr", {}, [el("td", { colSpan: 7, className: "hint",
      textContent: state.node && !state.node.online && !state.loadedAt ? "Can't list devices while this node is offline."
        : "No devices found yet. Scan, or add a connector by address." })]));
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
    body: "Polling stops and its points stop updating in Baler. History already recorded is kept.",
    action: "Delete connector",
    typeToConfirm: c.topic_prefix,
  });
  if (!ok) return;
  await run(() => act(kind === "bacnet" ? "delete_connection" : "modbus_delete_connection", { id: c.id }), "Connector deleted");
}

// ------------------------------------------------------------- scans

// One scan at a time, and the buttons say so. Stopping only stops the
// page waiting: the node may still finish and record what it found.
let busy = null;
async function runScan(label, fn) {
  if (busy) return;
  const ctrl = new AbortController();
  const t0 = Date.now();
  busy = ctrl;
  renderToolbar();
  const line = $("scan-progress");
  const text = $("scan-progress-text");
  let detail = "";
  const show = () => { text.textContent = `${label}… ${Math.round((Date.now() - t0) / 1000)} s${detail ? ` · ${detail}` : ""}`; };
  line.hidden = false;
  show();
  const tick = setInterval(show, 1000);
  const progress = (job) => {
    const p = job && job.progress;
    if (p && p.total) detail = `${p.done.toLocaleString()} of ${p.total.toLocaleString()} addresses probed`;
  };
  try {
    await fn(ctrl.signal, progress);
  } catch (err) {
    if (err.name === "AbortError") toast("Stopped waiting. The node may still finish the scan; anything it finds appears under Devices found.");
    else toast(err.message, "error");
  } finally {
    clearInterval(tick);
    busy = null;
    line.hidden = true;
    renderToolbar();
    await loadNode();
  }
}

function scanBacnet() {
  return runScan(`Scanning from ${state.node.label} for BACnet devices`, async (signal) => {
    const found = await runJob("discover", {}, signal);
    toast(`Scan finished: ${Array.isArray(found) ? plural(found.length, "device") + " answered" : "done"}`);
  });
}

function parseCidr(text) {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\/(\d{1,2})$/.exec(String(text).trim());
  if (!m || m.slice(1, 5).some((o) => Number(o) > 255) || Number(m[5]) > 32) return null;
  return { cidr: String(text).trim(), bits: Number(m[5]), size: 2 ** (32 - Number(m[5])) };
}

// The node's own subnet if its Windrow reports one, else the /24 of a
// device it already knows about.
function guessSubnet() {
  const n = state.node;
  const v4 = (n.networks || []).find((c) => parseCidr(c));
  if (v4) return v4;
  for (const d of state.devices) {
    const ip = d.host || String(d.address || "").split(":")[0];
    const m = /^(\d{1,3}\.\d{1,3}\.\d{1,3})\.\d{1,3}$/.exec(ip || "");
    if (m) return `${m[1]}.0/24`;
  }
  return "";
}

let scanConfirmFor = null;
function openScanDialog() {
  if (busy) return;
  scanConfirmFor = null;
  $("scan-node").textContent = state.node.label;
  $("scan-cidr").value = guessSubnet();
  $("scan-error").hidden = true;
  $("scan-go").textContent = "Sweep";
  $("scan-dialog").showModal();
  $("scan-cidr").focus();
}

function submitScan(ev) {
  ev.preventDefault();
  const err = $("scan-error");
  const c = parseCidr($("scan-cidr").value);
  const fail = (msg) => { err.textContent = msg; err.hidden = false; };
  if (!c) return fail("Enter a subnet like 192.168.1.0/24.");
  if (c.bits < MIN_PREFIX_BITS) return fail(`/${c.bits} is ${c.size.toLocaleString()} addresses - too many to sweep. Sweep /${MIN_PREFIX_BITS} or smaller pieces.`);
  if (c.bits < SCAN_CONFIRM_BITS && scanConfirmFor !== c.cidr) {
    scanConfirmFor = c.cidr;
    $("scan-go").textContent = `Sweep ${c.size.toLocaleString()} addresses`;
    return fail(`/${c.bits} is ${c.size.toLocaleString()} addresses and can take many minutes. Press the button again to sweep it anyway.`);
  }
  $("scan-dialog").close();
  runScan(`Sweeping ${c.cidr} (${c.size.toLocaleString()} addresses) from ${state.node.label}`, async (signal, progress) => {
    const found = await runJob("modbus_discover", { cidr: c.cidr }, signal, progress);
    toast(`Sweep finished: ${Array.isArray(found) ? plural(found.length, "device") + " answered" : "done"}`);
  });
}

// ---------------------------------------------------- connector dialog

const dlg = $("connector-dialog");

function slug(s) {
  return String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40);
}

const allowedPrefixes = () => (state.node && state.node.allowed_prefixes) || null;

function defaultPrefix(label) {
  const allowed = allowedPrefixes();
  const root = allowed && allowed.length ? allowed[0] : "fbf/site";
  return `${root}/${slug(label) || "equipment"}`;
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
const MODBUS_HELP = "Register numbers are 0-based protocol addresses: holding register 40001 is 0, input register 30010 is 9. "
  + "32-bit types use the register and the next one. Published value = raw × scale + offset. "
  + "Writable points (holding registers and coils) can be commanded from Baler.";

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

// One register row's fields, read back from the form.
function registerRow(tr) {
  const q = (cls) => tr.querySelector(cls);
  return {
    label: q(".reg-label").value.trim(), reg: q(".reg-num").value,
    function: q(".reg-fn").value, type: q(".reg-type").value, word_order: q(".reg-word").value,
    byte_order: tr.dataset.byteOrder, scale: Number(q(".reg-scale").value || 1), offset: Number(q(".reg-offset").value || 0),
    unit: q(".reg-unit").value.trim(), writable: q(".reg-write").checked,
  };
}

function addRegisterRow(label = "", spec = "", unit = "") {
  const p = typeof spec === "object" && spec ? spec : { register: spec };
  const tbody = $("modbus-points").querySelector("tbody");
  const n = tbody.children.length + 1;
  const del = el("button", { type: "button", textContent: "Remove", className: "danger" });
  del.setAttribute("aria-label", `Remove register row ${n}`);
  const select = (cls, options, value, aria) => {
    const s = el("select", { className: cls, "aria-label": `${aria}, row ${n}` },
      options.map(([v, t]) => el("option", { value: v, textContent: t })));
    s.value = value;
    return s;
  };
  const fn = select("reg-fn", MB_FUNCTIONS, p.function || "holding", "Function");
  const type = select("reg-type", MB_TYPES.map((t) => [t, t]), p.type || "uint16", "Data type");
  const word = select("reg-word", [["big", "high first"], ["little", "low first"]], p.word_order || "big", "Word order");
  const scale = el("input", { type: "number", className: "reg-scale", value: p.scale ?? 1, step: "any", "aria-label": `Scale, row ${n}` });
  const offset = el("input", { type: "number", className: "reg-offset", value: p.offset ?? 0, step: "any", "aria-label": `Offset, row ${n}` });
  const write = el("input", { type: "checkbox", className: "reg-write", checked: !!p.writable, "aria-label": `Writable, row ${n}` });
  const sync = () => {
    const bits = MB_BITS.includes(fn.value);
    type.disabled = scale.disabled = offset.disabled = bits;
    word.disabled = bits || !type.value.endsWith("32");
    write.disabled = fn.value === "input" || fn.value === "discrete";
  };
  fn.addEventListener("change", sync);
  type.addEventListener("change", sync);
  const tr = el("tr", {}, [
    el("td", {}, [el("input", { type: "text", className: "reg-label", value: label, placeholder: "zone_temp", "aria-label": `Point name, row ${n}` })]),
    el("td", {}, [fn]),
    el("td", {}, [el("input", { type: "number", className: "reg-num", value: p.register, min: 0, max: 65535, "aria-label": `Register, row ${n}` })]),
    el("td", {}, [type]),
    el("td", {}, [word]),
    el("td", {}, [scale]),
    el("td", {}, [offset]),
    el("td", {}, [el("input", { type: "text", className: "reg-unit", value: p.unit || unit || "", placeholder: "°F", "aria-label": `Unit, row ${n}` })]),
    el("td", {}, [write]),
    el("td", {}, [del]),
  ]);
  tr.dataset.byteOrder = p.byte_order || "";  // no column for it; kept as loaded
  sync();
  del.addEventListener("click", () => tr.remove());
  tbody.append(tr);
}

// ---- BACnet point picker (Baler#112, #113)

const TYPE_ABBR = {
  "analog-input": "AI", "analog-output": "AO", "analog-value": "AV",
  "binary-input": "BI", "binary-output": "BO", "binary-value": "BV",
  "multi-state-input": "MSI", "multi-state-output": "MSO", "multi-state-value": "MSV",
};
const objType = (oid) => String(oid).split(",")[0].trim();
const objShort = (oid) => {
  const [t, i] = String(oid).split(",");
  return `${TYPE_ABBR[t.trim()] || t.trim()} ${(i || "").trim()}`.trim();
};

// Merge a fresh read into the model: a point seen before keeps its tick
// and, if the integrator renamed it, its name.
function mergePoints(learned, { selectNew }) {
  const prior = new Map(state.points.map((p) => [p.oid, p]));
  const merged = learned.map((lp) => {
    const old = prior.get(lp.object_identifier);
    prior.delete(lp.object_identifier);
    if (old) return { ...old, learned: lp, label: old.edited ? old.label : (old.stored ? old.label : lp.label || old.label) };
    return { oid: lp.object_identifier, learned: lp, stored: null, label: lp.label || lp.object_identifier, edited: false, on: selectNew };
  });
  // A stored point the device no longer lists stays (ticked), at the end.
  for (const old of prior.values()) if (old.stored) merged.push(old);
  state.points = merged;
}

function renderPoints() {
  const tbody = $("bacnet-points").querySelector("tbody");
  tbody.innerHTML = "";
  for (const p of state.points) {
    const lp = p.learned || {};
    const stored = p.stored || {};
    const cb = el("input", { type: "checkbox", className: "pt-on", checked: p.on });
    cb.setAttribute("aria-label", `Poll ${p.label}`);
    cb.addEventListener("change", () => { p.on = cb.checked; updateCount(); });
    const name = el("input", { type: "text", className: "pt-label", value: p.label });
    name.setAttribute("aria-label", `Name in Baler for ${objShort(p.oid)}`);
    name.addEventListener("input", () => { p.label = name.value; p.edited = true; cb.setAttribute("aria-label", `Poll ${p.label}`); });
    const deviceName = lp.label && lp.label !== p.oid ? lp.label : null;
    const description = lp.description || stored.description;
    const units = lp.units || stored.units;
    const pv = typeof lp.present_value === "number" && !Number.isInteger(lp.present_value)
      ? Number(lp.present_value.toFixed(2)) : lp.present_value;
    const value = pv !== undefined && pv !== null ? `${pv}${units ? " " + units : ""}` : (units || "");
    const meta = [deviceName && deviceName !== p.label ? `device name: ${deviceName}` : null, description || null].filter(Boolean).join(" · ");
    const narrow = [objShort(p.oid), value].filter(Boolean).join(" · ");
    p.tr = el("tr", {}, [
      el("td", { className: "cb" }, [cb]),
      el("td", {}, [name,
        meta ? el("div", { className: "pt-meta", textContent: meta }) : null,
        el("div", { className: "pt-meta narrow-only", textContent: narrow })]),
      el("td", { className: "mono wide-only", textContent: objShort(p.oid), title: p.oid }),
      el("td", { className: "wide-only", textContent: value }),
    ]);
    p.search = [p.oid, objShort(p.oid), deviceName, description, units].filter(Boolean).join(" ").toLowerCase();
    tbody.append(p.tr);
  }
  // Type filter: only the types this device has, with counts.
  const sel = $("pt-type");
  const keep = sel.value;
  const counts = {};
  for (const p of state.points) counts[objType(p.oid)] = (counts[objType(p.oid)] || 0) + 1;
  sel.innerHTML = "";
  sel.append(el("option", { value: "", textContent: `All types (${state.points.length})` }));
  for (const t of Object.keys(counts).sort()) {
    sel.append(el("option", { value: t, textContent: `${TYPE_ABBR[t] ? TYPE_ABBR[t] + " · " : ""}${t.replace(/-/g, " ")} (${counts[t]})` }));
  }
  sel.value = counts[keep] ? keep : "";
  $("bacnet-points-box").hidden = !state.points.length;
  applyFilter();
}

function shownPoints() {
  return state.points.filter((p) => p.tr && !p.tr.hidden);
}

function applyFilter() {
  const q = $("pt-search").value.trim().toLowerCase();
  const t = $("pt-type").value;
  for (const p of state.points) {
    const text = `${p.label.toLowerCase()} ${p.search}`;
    p.tr.hidden = !!((q && !text.includes(q)) || (t && objType(p.oid) !== t));
  }
  updateCount();
}

function updateCount() {
  const on = state.points.filter((p) => p.on).length;
  const shown = shownPoints();
  const filtered = shown.length !== state.points.length;
  $("pt-count").textContent = `${on} of ${plural(state.points.length, "point")} selected${filtered ? ` · ${shown.length} shown` : ""}`;
  const all = $("all-points");
  const shownOn = shown.filter((p) => p.on).length;
  all.checked = shown.length > 0 && shownOn === shown.length;
  all.indeterminate = shownOn > 0 && shownOn < shown.length;
}

function setShown(on) {
  for (const p of shownPoints()) {
    p.on = on;
    p.tr.querySelector(".pt-on").checked = on;
  }
  updateCount();
}

function selectNone() {
  for (const p of state.points) {
    p.on = false;
    if (p.tr) p.tr.querySelector(".pt-on").checked = false;
  }
  updateCount();
}

function prefixHelp() {
  const allowed = allowedPrefixes();
  const help = $("prefix-help");
  help.textContent = "";
  if (allowed && allowed.length) {
    help.append("Must start with ");
    allowed.forEach((a, i) => {
      if (i) help.append(" or ");
      help.append(el("code", { textContent: `${a}/` }));
    });
    help.append(state.node.kind === "local"
      ? ": Baler only takes in topics under it. "
      : `: ${state.node.site_name ? `site ${state.node.site_name}'s` : "this site's"} prefix. The broker drops anything else. `);
  } else if (state.node.kind === "remote") {
    help.append("Couldn't look up this node's site prefix: make sure it starts with the site's prefix, or the broker drops the data. ");
  }
  help.append("It becomes the equipment's id in Baler, so one connector = one piece of equipment, and no two connectors share one.");
}

function setEditHints(kind, record, device) {
  const bh = $("bacnet-help");
  const mh = $("modbus-help");
  bh.textContent = record
    ? "The address can change (the device moved); the instance can't: to poll another device, add a new connector. Read the point list again to add points the device has gained; your names and picks are kept."
    : device
      ? "Address and instance come from the scan. Press Read to list the device's points."
      : "The controller's IP (and port, if not 47808) and its BACnet device instance number. Press Enter to read its point list.";
  mh.textContent = device && !record ? `Host and port come from the scan. ${MODBUS_HELP}` : MODBUS_HELP;
}

function openConnector({ kind, record = null, device = null }) {
  state.editing = record ? { kind, id: record.id, deviceName: record.device_name || "", address: record.device_address || "" } : null;
  $("connector-error").hidden = true;
  $("connector-title").textContent = record ? `Edit connector · ${record.topic_prefix}` : device ? `Add connector for ${deviceLabel(device)}` : "Add connector";
  $("connector-save").textContent = record ? "Save changes" : "Add connector";
  $("protocol-pick").disabled = !!(record || device);
  setProtocol(kind || "bacnet");
  setEditHints(kind, record, device);
  $("learn-status").textContent = "";
  $("test-read-status").textContent = "";
  $("modbus-points").querySelector("tbody").innerHTML = "";
  $("pt-search").value = "";
  $("pt-type").value = "";
  state.points = [];
  renderPoints();
  $("c-poll").value = record?.poll_interval ?? 5;
  // An existing BACnet connector can move to the device's new address, not
  // to another device (instance); Modbus can change all of it.
  $("c-address").disabled = !!(device && !record);
  $("c-instance").disabled = !!(record || device);
  $("c-device-name").value = record?.device_name || "";
  for (const id of ["c-host", "c-port", "c-unit"]) $(id).disabled = !!(device && !record);
  if (kind === "modbus") {
    $("c-host").value = record?.host || device?.host || "";
    $("c-port").value = record?.port || device?.port || 502;
    $("c-unit").value = record?.device_id ?? device?.device_id ?? 1;
    const pts = Object.entries(record?.points || {});
    if (pts.length) pts.forEach(([l, r]) => addRegisterRow(l, r, record?.units?.[l])); else addRegisterRow();
  } else {
    $("c-address").value = record?.device_address || device?.address || "";
    $("c-instance").value = record?.device_instance ?? device?.device_instance ?? "";
    if (record) {
      state.points = (record.points || []).map((sp) => ({ oid: sp.object_identifier, learned: null, stored: sp, label: sp.label, edited: true, on: true }));
      renderPoints();
    }
  }
  $("c-prefix").value = record?.topic_prefix || (device ? defaultPrefix(deviceLabel(device)) : "");
  prefixHelp();
  dlg.showModal();
  const first = record ? $("c-prefix") : kind === "modbus" ? (device ? $("modbus-points").querySelector(".reg-label") : $("c-host")) : (device ? $("learn-btn") : $("c-address"));
  if (first) first.focus();
}

let learnCtrl = null;
async function learn() {
  if (learnCtrl) return;
  const address = $("c-address").value.trim();
  const instance = Number($("c-instance").value);
  if (!address || $("c-instance").value === "" || !Number.isInteger(instance)) {
    showError("Enter the device address and instance first.");
    return;
  }
  $("connector-error").hidden = true;
  learnCtrl = new AbortController();
  const t0 = Date.now();
  const status = $("learn-status");
  status.textContent = "Reading the device's object list - large controllers take a minute…";
  const tick = setInterval(() => { status.textContent = `Reading the device's object list… ${Math.round((Date.now() - t0) / 1000)} s`; }, 1000);
  $("learn-btn").disabled = true;
  $("learn-cancel").hidden = false;
  try {
    const learned = await runJob("learn", { device_address: address, device_instance: instance }, learnCtrl.signal);
    const firstRead = !state.points.some((p) => p.learned);
    const selectNew = !state.editing && firstRead && learned.length <= SELECT_ALL_UP_TO && !state.points.length;
    mergePoints(learned, { selectNew });
    renderPoints();
    const fresh = state.editing ? state.points.filter((p) => p.learned && !p.stored).length : 0;
    status.textContent = `${plural(learned.length, "point")} found`
      + (state.editing ? (fresh ? `, ${fresh} not polled yet - tick them to add` : ", nothing new") : "")
      + (!state.editing && !selectNew && firstRead ? " - none selected yet: search or filter, then tick the ones to poll" : "");
    if (!$("c-prefix").value) $("c-prefix").value = defaultPrefix(`device-${instance}`);
  } catch (err) {
    status.textContent = "";
    if (err.name === "AbortError") status.textContent = "Stopped waiting for the device.";
    else showError(`Couldn't read the device: ${err.message}`);
  } finally {
    clearInterval(tick);
    learnCtrl = null;
    $("learn-btn").disabled = false;
    $("learn-cancel").hidden = true;
  }
}

async function testRead() {
  const status = $("test-read-status");
  const host = $("c-host").value.trim();
  const row = [...$("modbus-points").querySelectorAll("tbody tr")].find((tr) => tr.querySelector(".reg-num").value !== "");
  if (!host) return showError("Enter the device's host or IP first.");
  if (!row) return showError("Enter a register number to test.");
  const f = registerRow(row);
  const register = Number(f.reg);
  const label = f.label || `register ${register}`;
  const { spec } = modbusSpec({ ...f, register });
  $("connector-error").hidden = true;
  $("test-read").disabled = true;
  status.textContent = `Reading register ${register}…`;
  try {
    // The row's type, function and scale go along, so the value reads as it will publish.
    const r = await act("modbus_test_read", { host, port: Number($("c-port").value) || 502, device_id: Number($("c-unit").value) || 0, register,
                                              ...(typeof spec === "object" ? { point: spec } : {}) });
    status.textContent = `OK: ${label} (register ${register}) = ${r && r.value !== undefined ? r.value : "?"}`;
  } catch (err) {
    status.textContent = "";
    showError(`Test read failed: ${err.message}`);
  } finally {
    $("test-read").disabled = false;
  }
}

function showError(msg) {
  const e = $("connector-error");
  e.textContent = msg;
  e.hidden = false;
}

// Format, the node's allowed prefix (Baler#111), and no other connector on
// this node at, inside or above the same prefix.
function prefixProblem(prefix) {
  if (!prefix || /[\s#+]/.test(prefix) || prefix.startsWith("/") || prefix.endsWith("/") || prefix.includes("//")) {
    return "A topic prefix is like fbf/site/ahu-1: no spaces, no # or +, no leading, trailing or double /.";
  }
  const allowed = allowedPrefixes();
  if (allowed && allowed.length && !allowed.some((a) => prefix.startsWith(a + "/"))) {
    const list = allowed.map((a) => `${a}/`).join(" or ");
    return `This node's data is only taken in under ${list}, so ${prefix} would never reach Baler. Try ${allowed[0]}/${slug(prefix.split("/").pop()) || "equipment"}.`;
  }
  for (const r of connectorRows()) {
    if (state.editing && state.editing.id === r.c.id && state.editing.kind === r.kind) continue;
    const other = r.c.topic_prefix;
    if (other === prefix) return `${prefix} is already used by the ${r.kind === "bacnet" ? "BACnet" : "Modbus"} connector for ${r.device}. Each connector needs its own prefix.`;
    if (other.startsWith(prefix + "/") || prefix.startsWith(other + "/")) {
      return `${prefix} overlaps ${other} (the connector for ${r.device}): their points would mix. Pick a prefix beside it, not inside or above it.`;
    }
  }
  return null;
}

async function saveConnector(ev) {
  ev.preventDefault();
  $("connector-error").hidden = true;
  const kind = protocol();
  const prefix = $("c-prefix").value.trim();
  const poll = Number($("c-poll").value);
  const bad = prefixProblem(prefix);
  if (bad) { showError(bad); $("c-prefix").focus(); return; }
  if (!(poll >= 1)) return showError("Poll interval is at least 1 second.");
  let body;
  if (kind === "bacnet") {
    const points = state.points.filter((p) => p.on).map((p) => {
      const base = p.stored ? { ...p.stored } : {
        object_identifier: p.oid,
        ...(p.learned && p.learned.units ? { units: p.learned.units } : {}),
        ...(p.learned && p.learned.description ? { description: p.learned.description } : {}),
      };
      return { ...base, label: p.label.trim() || (p.learned && p.learned.label) || p.oid };
    });
    if (!points.length) return showError("Pick at least one point (read the point list first).");
    const labels = points.map((p) => p.label);
    const dup = labels.find((l, i) => labels.indexOf(l) !== i);
    if (dup) return showError(`Two points are named "${dup}" - names must be unique.`);
    const address = $("c-address").value.trim();
    if (!address) return showError("Enter the device address.");
    body = { topic_prefix: prefix, poll_interval: poll, points };
    // Only what changed goes in an edit: a Windrow from before announce
    // refuses device_address/device_name in a PATCH.
    if (!state.editing || address !== state.editing.address) body.device_address = address;
    if (!state.editing) body.device_instance = Number($("c-instance").value);
  } else {
    const points = {};
    const units = {};
    for (const tr of $("modbus-points").querySelectorAll("tbody tr")) {
      const f = registerRow(tr);
      if (!f.label && f.reg === "") continue;
      if (!f.label || f.reg === "" || !Number.isInteger(Number(f.reg)) || Number(f.reg) < 0 || Number(f.reg) > 65535) {
        return showError("Each register row needs a name and a register number 0-65535.");
      }
      if (f.label in points) return showError(`"${f.label}" is used twice - names must be unique.`);
      if (!Number.isFinite(f.scale) || f.scale === 0 || !Number.isFinite(f.offset)) return showError(`"${f.label}": scale must be a non-zero number, offset a number.`);
      const out = modbusSpec({ ...f, register: Number(f.reg) });
      points[f.label] = out.spec;
      if (out.unit) units[f.label] = out.unit;
    }
    if (!Object.keys(points).length) return showError("Add at least one register.");
    const host = $("c-host").value.trim();
    if (!host) return showError("Enter the device's host or IP.");
    body = { topic_prefix: prefix, poll_interval: poll, points, units, host, port: Number($("c-port").value) || 502, device_id: Number($("c-unit").value) || 0 };
  }
  // device_name (Windrow device announce): on edit, an empty field clears it.
  const deviceName = $("c-device-name").value.trim();
  if (/\s/.test(deviceName)) return showError("The device name can't contain spaces: it's the name the device announces, or a DNS name.");
  if (state.editing ? deviceName !== state.editing.deviceName.toLowerCase() && deviceName !== state.editing.deviceName : !!deviceName) {
    body.device_name = deviceName || null;
  }
  $("connector-save").disabled = true;
  try {
    if (state.editing) {
      await act(kind === "bacnet" ? "update_connection" : "modbus_update_connection", { id: state.editing.id, ...body });
      toast("Connector updated");
    } else {
      await act(kind === "bacnet" ? "create_connection" : "modbus_create_connection", body);
      toast(`Connector added. Its status shows here as soon as it polls; points appear under ${prefix} in Equipment.`);
    }
    dlg.close();
    await loadNode();
  } catch (err) {
    showError(err.message);
  } finally {
    $("connector-save").disabled = false;
  }
}

// Enter reads the point list from the address fields, does nothing in the
// picker's own fields, and only submits from the connector's settings.
function onConnectorKey(ev) {
  if (ev.key !== "Enter" || ev.isComposing) return;
  const t = ev.target;
  if (t.id === "c-address" || t.id === "c-instance") {
    ev.preventDefault();
    learn();
  } else if (t.matches(".pt-label, .pt-on, .reg-label, .reg-num, .reg-scale, .reg-offset, .reg-unit, .reg-write, #pt-search, #pt-type, #c-host, #c-port, #c-unit")) {
    ev.preventDefault();
  }
}

// ---------------------------------------------------- credential dialog

let credTarget = null;
function openCredential(target, id, what) {
  credTarget = { target, id };
  $("credential-form").reset();
  $("credential-error").hidden = true;
  $("credential-title").textContent = `Device login · ${what}`;
  $("credential-dialog").showModal();
  $("credential-username").focus();
}

// Closes only once the node has it, so a failed save keeps what was typed.
async function saveCredential(ev) {
  ev.preventDefault();
  const credential = { username: $("credential-username").value, password: $("credential-password").value };
  const err = $("credential-error");
  err.hidden = true;
  $("credential-save").disabled = true;
  try {
    await act("set_credential", { ...credTarget, credential });
    $("credential-dialog").close();
    toast("Login saved");
    await loadNode();
  } catch (e) {
    err.textContent = `Couldn't save the login: ${e.message}`;
    err.hidden = false;
  } finally {
    $("credential-save").disabled = false;
  }
}

// ------------------------------------------------------------- wiring

async function refresh() {
  await loadNodes();
  await loadNode();
}

async function init() {
  for (const b of document.querySelectorAll(".admin-only")) b.hidden = !isAdmin();
  $("role-hint").hidden = isAdmin();
  await refresh();
  setInterval(refresh, REFRESH_MS);
}

$("add-connector").addEventListener("click", () => openConnector({ kind: "bacnet" }));
$("scan-bacnet").addEventListener("click", scanBacnet);
$("scan-modbus").addEventListener("click", openScanDialog);
$("scan-cancel").addEventListener("click", () => busy && busy.abort());
$("scan-form").addEventListener("submit", submitScan);
$("scan-cidr").addEventListener("input", () => { scanConfirmFor = null; $("scan-go").textContent = "Sweep"; $("scan-error").hidden = true; });
$("scan-dialog-cancel").addEventListener("click", () => $("scan-dialog").close());
$("refresh").addEventListener("click", refresh);
$("conn-filter").addEventListener("input", (e) => { listView.filter = e.target.value; renderConnections(); });
$("conn-failing").addEventListener("change", (e) => { listView.failingOnly = e.target.checked; renderConnections(); });
for (const r of document.querySelectorAll('input[name="protocol"]')) r.addEventListener("change", () => {
  setProtocol(protocol());
  if (protocol() === "modbus" && !$("modbus-points").querySelector("tbody tr")) addRegisterRow();
});
$("learn-btn").addEventListener("click", learn);
$("learn-cancel").addEventListener("click", () => learnCtrl && learnCtrl.abort());
$("add-register").addEventListener("click", () => addRegisterRow());
$("test-read").addEventListener("click", testRead);
$("all-points").addEventListener("change", (e) => setShown(e.target.checked));
$("pt-none").addEventListener("click", selectNone);
$("pt-search").addEventListener("input", applyFilter);
$("pt-type").addEventListener("change", applyFilter);
$("connector-form").addEventListener("keydown", onConnectorKey);
$("connector-cancel").addEventListener("click", () => dlg.close());
dlg.addEventListener("close", () => { if (learnCtrl) learnCtrl.abort(); });
$("connector-form").addEventListener("submit", saveConnector);
$("credential-cancel").addEventListener("click", () => $("credential-dialog").close());
$("credential-form").addEventListener("submit", saveCredential);

initAuth(init);

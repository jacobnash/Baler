// No build step, no framework - same spirit as app.js. Talks to
// windrow.api (a separate process/repo) through the gateway's /windrow/
// route: login required, changes admin-only, and site-restricted logins
// are refused there (Windrow has no per-site filter - Baler#14).
// ?windrow=<url> still points it at another instance for local dev.
const params = new URLSearchParams(location.search);
const WINDROW_API_URL = params.get("windrow") || "/windrow";

const statusEl = document.getElementById("status");
const REFRESH_INTERVAL_MS = 5000;

async function windrow(method, path, body) {
  const headers = { ...authHeaders() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`${WINDROW_API_URL}${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) {
    handleUnauthorized();
    throw new Error("session expired - sign in again");
  }
  const text = await res.text();
  if (res.status === 403) throw new Error(text.trim() || "not allowed for your role");
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    throw new Error(`${method} ${path} failed: ${res.status}`);
  }
  if (!res.ok) throw new Error(data && data.error ? data.error : `${method} ${path} failed: ${res.status}`);
  return data;
}

function el(tag, props, children) {
  const node = Object.assign(document.createElement(tag), props);
  for (const child of children || []) node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  return node;
}

function credentialCell(record, kind) {
  const cred = record.credential;
  const summary = el("span", { textContent: cred ? `set (${cred.username})` : "—" });
  const btn = el("button", { type: "button", textContent: cred ? "Change" : "Set" });
  btn.addEventListener("click", () => openCredentialDialog(kind, record.id));
  return el("td", {}, [summary, document.createElement("br"), btn]);
}

function defaultFlagCell(device) {
  if (device.protocol === "bacnet") return el("td", { className: "hint", textContent: "n/a (no BACnet login surface)" });
  if (!device.default_credential_checked_at) return el("td", { className: "hint", textContent: "checking…" });
  if (!device.default_credential_flag) return el("td", { textContent: "clear" });
  const flag = device.default_credential_flag;
  const badge = el("span", { className: "badge default-cred-flag", textContent: `${flag.username}/${flag.password || "(blank)"} works` });
  const recheckBtn = el("button", { type: "button", textContent: "Recheck" });
  recheckBtn.addEventListener("click", async () => {
    await windrow("POST", `/devices/${device.id}/recheck-credentials`);
    await loadAll();
  });
  return el("td", {}, [badge, document.createElement("br"), recheckBtn]);
}

function deviceAddressLabel(device) {
  return device.protocol === "bacnet"
    ? `${device.address} (instance ${device.device_instance})`
    : `${device.host}:${device.port}`;
}

function renderDevices(devices) {
  const tbody = document.querySelector("#devices-table tbody");
  tbody.innerHTML = "";
  for (const device of devices) {
    const actions = el("td", {}, []);

    if (device.status !== "ignored") {
      const ignoreBtn = el("button", { type: "button", textContent: "Ignore" });
      ignoreBtn.addEventListener("click", async () => {
        await windrow("PATCH", `/devices/${device.id}`, { status: "ignored" });
        await loadAll();
      });
      actions.appendChild(ignoreBtn);
    }
    if (device.status === "pending") {
      const provisionBtn = el("button", { type: "button", textContent: "Create connection" });
      provisionBtn.addEventListener("click", () => openProvisionDialog(device));
      actions.appendChild(provisionBtn);
    }
    if (device.status === "ignored") {
      const restoreBtn = el("button", { type: "button", textContent: "Unignore" });
      restoreBtn.addEventListener("click", async () => {
        await windrow("PATCH", `/devices/${device.id}`, { status: "pending" });
        await loadAll();
      });
      actions.appendChild(restoreBtn);
    }

    const vendor = device.protocol === "bacnet" ? `vendor id ${device.vendor_id}` : [device.vendor, device.product_code].filter(Boolean).join(" / ") || "—";

    tbody.appendChild(
      el("tr", {}, [
        el("td", { textContent: device.protocol }),
        el("td", { textContent: deviceAddressLabel(device) }),
        el("td", { textContent: vendor }),
        el("td", {}, [el("span", { className: `badge status-${device.status}`, textContent: device.status })]),
        el("td", { textContent: new Date(device.last_seen_at * 1000).toLocaleString() }),
        credentialCell(device, "device"),
        defaultFlagCell(device),
        actions,
      ]),
    );
  }
  if (!devices.length) tbody.appendChild(el("tr", {}, [el("td", { colSpan: 8, className: "hint", textContent: "No devices seen yet." })]));
}

function renderConnections(connections) {
  const tbody = document.querySelector("#connections-table tbody");
  tbody.innerHTML = "";
  for (const conn of connections) {
    const deleteBtn = el("button", { type: "button", textContent: "Delete" });
    deleteBtn.addEventListener("click", async () => {
      await windrow("DELETE", `/connections/${conn.id}`);
      await loadAll();
    });
    tbody.appendChild(
      el("tr", {}, [
        el("td", { textContent: `${conn.device_address} (instance ${conn.device_instance})` }),
        el("td", { textContent: conn.topic_prefix }),
        el("td", { textContent: conn.points.map((p) => p.label).join(", ") }),
        credentialCell(conn, "connection"),
        el("td", {}, [deleteBtn]),
      ]),
    );
  }
  if (!connections.length) tbody.appendChild(el("tr", {}, [el("td", { colSpan: 5, className: "hint", textContent: "None yet." })]));
}

function renderModbusConnections(connections) {
  const tbody = document.querySelector("#modbus-connections-table tbody");
  tbody.innerHTML = "";
  for (const conn of connections) {
    const deleteBtn = el("button", { type: "button", textContent: "Delete" });
    deleteBtn.addEventListener("click", async () => {
      await windrow("DELETE", `/modbus/connections/${conn.id}`);
      await loadAll();
    });
    tbody.appendChild(
      el("tr", {}, [
        el("td", { textContent: `${conn.host}:${conn.port}` }),
        el("td", { textContent: conn.topic_prefix }),
        el("td", { textContent: Object.keys(conn.points).join(", ") }),
        credentialCell(conn, "modbus-connection"),
        el("td", {}, [deleteBtn]),
      ]),
    );
  }
  if (!connections.length) tbody.appendChild(el("tr", {}, [el("td", { colSpan: 5, className: "hint", textContent: "None yet." })]));
}

// --- credential dialog ---

const credentialDialog = document.getElementById("credential-dialog");
const credentialForm = document.getElementById("credential-form");
let credentialTarget = null; // { kind, id }

document.getElementById("credential-cancel").addEventListener("click", () => credentialDialog.close());

function credentialPath({ kind, id }) {
  if (kind === "device") return `/devices/${id}/credential`;
  if (kind === "connection") return `/connections/${id}/credential`;
  return `/modbus/connections/${id}/credential`;
}

function openCredentialDialog(kind, id) {
  credentialTarget = { kind, id };
  credentialForm.reset();
  credentialDialog.showModal();
}

credentialForm.addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value !== "save") return;
  ev.preventDefault();
  const username = document.getElementById("credential-username").value;
  const password = document.getElementById("credential-password").value;
  await windrow("PATCH", credentialPath(credentialTarget), { username, password });
  credentialDialog.close();
  await loadAll();
});

// --- provision (create connection) dialog ---

const provisionDialog = document.getElementById("provision-dialog");
const provisionForm = document.getElementById("provision-form");
const provisionFields = document.getElementById("provision-fields");
document.getElementById("provision-cancel").addEventListener("click", () => provisionDialog.close());

function openProvisionDialog(device) {
  provisionFields.innerHTML = "";
  document.getElementById("provision-title").textContent = `Create connection — ${deviceAddressLabel(device)}`;

  const topicInput = el("input", { type: "text", id: "provision-topic-prefix", placeholder: `fbf/${device.protocol}-device`, required: true });
  provisionFields.appendChild(el("label", {}, ["Topic prefix", topicInput]));

  if (device.protocol === "bacnet") {
    const learnBtn = el("button", { type: "button", textContent: "Learn points" });
    const learnList = el("ul", { className: "learn-list" });
    learnBtn.addEventListener("click", async () => {
      learnList.innerHTML = "<li class=\"hint\">loading…</li>";
      const points = await windrow("POST", "/learn", { device_address: device.address, device_instance: device.device_instance });
      learnList.innerHTML = "";
      for (const point of points) {
        const checkbox = el("input", { type: "checkbox", value: JSON.stringify({ object_identifier: point.object_identifier, label: point.label }) });
        checkbox.checked = true;
        learnList.appendChild(el("li", {}, [el("label", {}, [checkbox, `${point.label} (${point.object_identifier})`])]));
      }
    });
    provisionFields.appendChild(learnBtn);
    provisionFields.appendChild(learnList);
    provisionForm.dataset.mode = "bacnet";
    provisionForm.dataset.deviceAddress = device.address;
    provisionForm.dataset.deviceInstance = device.device_instance;
  } else {
    const pointsInput = el("textarea", { id: "provision-points", placeholder: '{"zone-temp": 0, "fan-status": 1}' });
    provisionFields.appendChild(el("label", {}, ["Points ({label: register_address})", pointsInput]));
    provisionForm.dataset.mode = "modbus";
    provisionForm.dataset.host = device.host;
    provisionForm.dataset.port = device.port;
  }

  provisionDialog.showModal();
}

provisionForm.addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value !== "create") return;
  ev.preventDefault();
  const topicPrefix = document.getElementById("provision-topic-prefix").value;

  if (provisionForm.dataset.mode === "bacnet") {
    const points = [...provisionFields.querySelectorAll('input[type="checkbox"]:checked')].map((cb) => JSON.parse(cb.value));
    await windrow("POST", "/connections", {
      device_address: provisionForm.dataset.deviceAddress,
      device_instance: Number(provisionForm.dataset.deviceInstance),
      topic_prefix: topicPrefix,
      points,
    });
  } else {
    const points = JSON.parse(document.getElementById("provision-points").value || "{}");
    await windrow("POST", "/modbus/connections", {
      host: provisionForm.dataset.host,
      port: Number(provisionForm.dataset.port),
      topic_prefix: topicPrefix,
      points,
    });
  }

  provisionDialog.close();
  await loadAll();
});

// --- polling ---

async function loadAll() {
  if (credentialDialog.open || provisionDialog.open) return; // don't clobber a form the human is mid-editing
  try {
    const [devices, connections, modbusConnections] = await Promise.all([
      windrow("GET", "/devices"),
      windrow("GET", "/connections"),
      windrow("GET", "/modbus/connections"),
    ]);
    renderDevices(devices);
    renderConnections(connections);
    renderModbusConnections(modbusConnections);
    statusEl.textContent = `${devices.length} known devices, ${connections.length + modbusConnections.length} connections`;
  } catch (err) {
    // A 502 here means the gateway has no Windrow behind it (a deploy that
    // doesn't run windrow_api); 403s carry the gateway's own reason.
    statusEl.textContent = `Can't load devices from ${WINDROW_API_URL}: ${err.message}`;
  }
}

function init() {
  loadAll();
  setInterval(loadAll, REFRESH_INTERVAL_MS);
}

initAuth(init);

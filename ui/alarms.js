// No build step, no framework - same spirit as app.js/devices.js. A thin
// table over fault_api.py's GET /faults plus the two operator actions
// (POST .../ack, POST .../snooze) - detection/webhook delivery are
// fault_detector.py's job, untouched by this page.
const params = new URLSearchParams(location.search);
const FAULT_API_URL = params.get("fault") || "/fault";

const statusEl = document.getElementById("status");
const statusFilterEl = document.getElementById("status-filter");
const tbodyEl = document.querySelector("#faults-table tbody");
const faultsCountEl = document.getElementById("faults-count");
const rulesCountEl = document.getElementById("rules-count");

const REFRESH_INTERVAL_MS = 5000;
let refreshTimer = null;
let dialogOpen = false; // suppress the poll while a snooze prompt is up, same reasoning as devices.js's dialog guard

async function faultApi(method, path, body) {
  const res = await fetch(`${FAULT_API_URL}${path}`, {
    method,
    headers: { ...authHeaders(), ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) {
    handleUnauthorized();
    throw new Error("session expired");
  }
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) throw new Error(data && data.error ? data.error : `${method} ${path} failed: ${res.status}`);
  return data;
}

function el(tag, props, children) {
  const node = Object.assign(document.createElement(tag), props);
  for (const child of children || []) node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  return node;
}

function isSnoozed(fault) {
  return fault.snoozed_until && new Date(fault.snoozed_until) > new Date();
}

// Full value still reachable via the native title="" tooltip on hover -
// truncation only affects the table layout, not the data shown.
function truncatedCell(text) {
  return el("td", {}, [el("span", { className: "truncate", title: text, textContent: text })]);
}

// A controller's own alarm, forwarded by Windrow (rule_id "bacnet:alarm",
// Baler#168): show what the controller said instead of a rule id and raw JSON.
function faultSource(fault) {
  return fault.rule_id === "bacnet:alarm" ? "BACnet alarm (controller)" : fault.rule_id;
}

function faultDetail(fault) {
  const d = fault.detail;
  if (!d) return "—";
  if (fault.rule_id === "bacnet:alarm") {
    const state = d.to_state || d.event_state || "?";
    const parts = [d.event_type ? `${d.event_type}: ${state}` : state];
    if (d.from_state) parts[0] = `${d.event_type || "event"}: ${d.from_state} → ${state}`;
    if (d.message) parts.push(d.message);
    if (d.object) parts.push(d.object);
    return parts.join(" · ");
  }
  return JSON.stringify(d);
}

function renderFaults(faults) {
  tbodyEl.innerHTML = "";
  for (const fault of faults) {
    const ackBtn = el("button", {
      type: "button",
      textContent: fault.acked_at ? `Acked (${fault.acked_by})` : "Ack",
      disabled: !!fault.acked_at,
    });
    ackBtn.addEventListener("click", async () => {
      await faultApi("POST", `/faults/${fault.id}/ack`);
      await loadFaults();
    });

    const snoozeBtn = el("button", {
      type: "button",
      textContent: isSnoozed(fault) ? `Snoozed until ${new Date(fault.snoozed_until).toLocaleTimeString()}` : "Snooze",
    });
    snoozeBtn.addEventListener("click", async () => {
      dialogOpen = true;
      const minutes = Number(prompt("Snooze for how many minutes?", "60"));
      dialogOpen = false;
      if (!minutes || minutes <= 0) return;
      await faultApi("POST", `/faults/${fault.id}/snooze`, { minutes });
      await loadFaults();
    });

    tbodyEl.appendChild(
      el("tr", { className: "fault-row" }, [
        el("td", {}, [el("span", { className: `badge severity-${fault.severity}`, textContent: fault.severity })]),
        truncatedCell(faultSource(fault)),
        truncatedCell(fault.point_uri),
        el("td", {}, [el("span", { className: `badge fault-status-${fault.status}`, textContent: fault.status })]),
        el("td", { textContent: new Date(fault.started_at).toLocaleString() }),
        el("td", { textContent: faultDetail(fault), title: fault.detail ? JSON.stringify(fault.detail) : "" }),
        el("td", {}, [ackBtn]),
        el("td", {}, [snoozeBtn]),
      ]),
    );
  }
  if (!faults.length) tbodyEl.appendChild(el("tr", {}, [el("td", { colSpan: 8, className: "hint", textContent: "No faults." })]));
  // The count is the rows just drawn, never a second tally (Baler#63) -
  // and it sits on this table's own heading, so it can't be read as a
  // count of every row on the page (the Rules table has rows too).
  const n = tbodyEl.querySelectorAll("tr.fault-row").length;
  const open = faults.filter((f) => f.status === "open").length;
  const which = statusFilterEl.value;
  const text = which ? `${n} ${which} fault${n === 1 ? "" : "s"}` : `${n} fault${n === 1 ? "" : "s"} (${open} open)`;
  faultsCountEl.textContent = which ? `(${n} ${which})` : `(${n}, ${open} open)`;
  statusEl.textContent = text;
}

async function loadFaults() {
  if (dialogOpen) return;
  try {
    const status = statusFilterEl.value;
    const faults = await faultApi("GET", status ? `/faults?status=${status}` : "/faults");
    renderFaults(faults);
  } catch (err) {
    statusEl.textContent = `failed to reach fault API: ${err.message}`;
  }
}

statusFilterEl.addEventListener("change", loadFaults);

// --- rule creation - same Form/Raw JSON editor pattern as derivations.js's
// config pane, adapted to this one textarea (no fn_source/test_cases here). ---

const ruleFormEl = document.getElementById("rule-form");
const ruleConfigEl = document.getElementById("rule-config");
const ruleConfigErrorEl = document.getElementById("rule-config-error");
const ruleResultEl = document.getElementById("rule-result");
const rulesTbodyEl = document.querySelector("#rules-table tbody");

const DEFAULT_RULE_CONFIG = {
  name: "zone-temp-out-of-range",
  mode: "cur",
  type: "range",
  applies_to: { topic_glob: "fbf/mock-ahu-1/*" },
  min: 60,
  max: 80,
  severity: "warning",
};

// --- Form/Raw JSON toggle - same pattern as derivations.js's editor: the
// form is a friendlier way to fill in the exact same config JSON
// hand-writing already produces, not a second source of truth. ---

const ruleModeFormBtn = document.getElementById("rule-mode-form");
const ruleModeRawBtn = document.getElementById("rule-mode-raw");
const ruleFormFieldsEl = document.getElementById("rule-form-fields");
const ruleConfigLabelEl = document.getElementById("rule-config-label");
const ruleFieldsetRangeEl = document.getElementById("rule-fieldset-range");
const ruleFieldsetStuckEl = document.getElementById("rule-fieldset-stuck");
const ruleFieldsetStaleEl = document.getElementById("rule-fieldset-stale");

const ruleFieldNameEl = document.getElementById("rule-field-name");
const ruleFieldModeEl = document.getElementById("rule-field-mode");
const ruleFieldSeverityEl = document.getElementById("rule-field-severity");
const ruleFieldTopicGlobEl = document.getElementById("rule-field-topic-glob");
const ruleFieldBrickClassEl = document.getElementById("rule-field-brick-class");
const ruleFieldTypeEl = document.getElementById("rule-field-type");
const ruleFieldMinEl = document.getElementById("rule-field-min");
const ruleFieldMaxEl = document.getElementById("rule-field-max");
const ruleFieldStuckSecondsEl = document.getElementById("rule-field-stuck-seconds");
const ruleFieldMaxAgeSecondsEl = document.getElementById("rule-field-max-age-seconds");

const RULE_FORM_FIELDS = [
  ruleFieldNameEl, ruleFieldModeEl, ruleFieldSeverityEl, ruleFieldTopicGlobEl, ruleFieldBrickClassEl,
  ruleFieldMinEl, ruleFieldMaxEl, ruleFieldStuckSecondsEl, ruleFieldMaxAgeSecondsEl,
];

// Assembles the exact object shape CreateRuleRequest expects (see
// fault-api-openapi.yaml) - nothing invented beyond what the raw JSON pane
// already accepted.
function collectRuleFormValues() {
  const type = ruleFieldTypeEl.value;
  const config = { name: ruleFieldNameEl.value, mode: ruleFieldModeEl.value, type };
  if (ruleFieldSeverityEl.value) config.severity = ruleFieldSeverityEl.value;

  const appliesTo = {};
  if (ruleFieldTopicGlobEl.value) appliesTo.topic_glob = ruleFieldTopicGlobEl.value;
  if (ruleFieldBrickClassEl.value) appliesTo.brick_class = ruleFieldBrickClassEl.value;
  config.applies_to = appliesTo;

  if (type === "range") {
    if (ruleFieldMinEl.value !== "") config.min = Number(ruleFieldMinEl.value);
    if (ruleFieldMaxEl.value !== "") config.max = Number(ruleFieldMaxEl.value);
  } else if (type === "stuck") {
    if (ruleFieldStuckSecondsEl.value !== "") config.stuck_seconds = Number(ruleFieldStuckSecondsEl.value);
  } else {
    if (ruleFieldMaxAgeSecondsEl.value !== "") config.max_age_seconds = Number(ruleFieldMaxAgeSecondsEl.value);
  }
  return config;
}

function populateRuleFormFromConfig(config) {
  ruleFieldNameEl.value = config.name || "";
  ruleFieldModeEl.value = config.mode || "cur";
  ruleFieldSeverityEl.value = config.severity || "";
  ruleFieldTopicGlobEl.value = config.applies_to?.topic_glob || "";
  ruleFieldBrickClassEl.value = config.applies_to?.brick_class || "";
  ruleFieldTypeEl.value = config.type || "range";
  ruleFieldMinEl.value = config.min ?? "";
  ruleFieldMaxEl.value = config.max ?? "";
  ruleFieldStuckSecondsEl.value = config.stuck_seconds ?? "";
  ruleFieldMaxAgeSecondsEl.value = config.max_age_seconds ?? "";
  updateRuleFieldsetVisibility();
}

function updateRuleFieldsetVisibility() {
  const type = ruleFieldTypeEl.value;
  ruleFieldsetRangeEl.hidden = type !== "range";
  ruleFieldsetStuckEl.hidden = type !== "stuck";
  ruleFieldsetStaleEl.hidden = type !== "stale";
}

function syncRuleConfigFromForm() {
  ruleConfigEl.value = JSON.stringify(collectRuleFormValues(), null, 2);
  validateRuleConfig();
}

function setRuleMode(mode) {
  const isForm = mode === "form";
  ruleFormFieldsEl.hidden = !isForm;
  // Inline style, not `hidden` - see derivations.js's setMode for why.
  ruleConfigLabelEl.style.display = isForm ? "none" : "";
  ruleModeFormBtn.setAttribute("aria-pressed", String(isForm));
  ruleModeRawBtn.setAttribute("aria-pressed", String(!isForm));
  if (isForm) {
    try {
      populateRuleFormFromConfig(JSON.parse(ruleConfigEl.value || "{}"));
    } catch {
      // Bad JSON in the raw pane - leave the form fields as they were.
    }
  } else {
    syncRuleConfigFromForm();
  }
}

ruleFieldTypeEl.addEventListener("change", () => {
  updateRuleFieldsetVisibility();
  syncRuleConfigFromForm();
});
RULE_FORM_FIELDS.forEach((field) => field.addEventListener("input", syncRuleConfigFromForm));
ruleModeFormBtn.addEventListener("click", () => setRuleMode("form"));
ruleModeRawBtn.addEventListener("click", () => setRuleMode("raw"));

function validateRuleConfig() {
  if (!ruleConfigEl.value.trim()) {
    ruleConfigEl.classList.remove("invalid");
    ruleConfigErrorEl.hidden = true;
    return true;
  }
  try {
    JSON.parse(ruleConfigEl.value);
    ruleConfigEl.classList.remove("invalid");
    ruleConfigErrorEl.hidden = true;
    return true;
  } catch (err) {
    ruleConfigEl.classList.add("invalid");
    ruleConfigErrorEl.textContent = err.message;
    ruleConfigErrorEl.hidden = false;
    return false;
  }
}

ruleConfigEl.addEventListener("input", validateRuleConfig);

function loadRuleEditorTemplate() {
  ruleConfigEl.value = JSON.stringify(DEFAULT_RULE_CONFIG, null, 2);
  setRuleMode("form");
  ruleResultEl.textContent = "";
  ruleResultEl.classList.remove("result-error");
  validateRuleConfig();
}

document.getElementById("rule-clear").addEventListener("click", loadRuleEditorTemplate);

function showRuleResult(value, isError) {
  ruleResultEl.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  ruleResultEl.classList.toggle("result-error", !!isError);
}

ruleFormEl.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const config = JSON.parse(ruleConfigEl.value || "{}");
    const created = await faultApi("POST", "/rules", config);
    showRuleResult(created, false);
    await loadRules();
  } catch (err) {
    // JSON.parse's own error carries a character position (a server-side
    // 400 doesn't) - jump to it the same way derivations.js's editor does.
    const positionMatch = err.message.match(/position (\d+)/);
    if (positionMatch) {
      ruleConfigEl.markErrorLine?.(ruleConfigEl.value.slice(0, Number(positionMatch[1])).split("\n").length);
    }
    showRuleResult(err.message, true);
  }
});

function renderRules(rules) {
  rulesTbodyEl.innerHTML = "";
  for (const rule of rules) {
    const deleteBtn = el("button", { type: "button", textContent: "Delete" });
    deleteBtn.addEventListener("click", async () => {
      // Same one-native-confirm cheap-guard as derivations.js's delete -
      // deleting a rule is irreversible and stops future fault detection
      // for it, but doesn't touch faults it already raised.
      if (!confirm(`Delete rule "${rule.name}"? This can't be undone.`)) return;
      await faultApi("DELETE", `/rules/${rule.id}`);
      await loadRules();
    });

    rulesTbodyEl.appendChild(
      el("tr", {}, [
        el("td", { textContent: rule.name }),
        el("td", { textContent: rule.mode }),
        el("td", { textContent: rule.type }),
        el("td", {}, [el("span", { className: `badge severity-${rule.severity}`, textContent: rule.severity })]),
        truncatedCell(JSON.stringify(rule.applies_to)),
        el("td", {}, [deleteBtn]),
      ]),
    );
  }
  rulesCountEl.textContent = `(${rules.length})`;
  if (!rules.length) rulesTbodyEl.appendChild(el("tr", {}, [el("td", { colSpan: 6, className: "hint", textContent: "No rules yet." })]));
}

async function loadRules() {
  try {
    const rules = await faultApi("GET", "/rules");
    renderRules(rules);
  } catch (err) {
    rulesTbodyEl.innerHTML = "";
    rulesTbodyEl.appendChild(el("tr", {}, [el("td", { colSpan: 6, className: "hint", textContent: `failed to load rules: ${err.message}` })]));
  }
}

// Replaces the blank flash between page load and the first fetch settling
// - same colSpan/className shape as the "No faults." empty state, just a
// different message, so there's exactly one render path a reader learns.
tbodyEl.appendChild(el("tr", {}, [el("td", { colSpan: 8, className: "hint", textContent: "Loading…" })]));

function init() {
  loadRuleEditorTemplate();
  loadRules();
  loadFaults();
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(loadFaults, REFRESH_INTERVAL_MS);
}

initAuth(init);

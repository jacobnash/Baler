// No build step, no framework - same spirit as app.js/devices.js. This is
// a browser surface over derivation_api.py's existing sandboxed endpoints
// (POST /derivations/test, /dry-run, and plain CRUD) - no new expression
// language, no client-side validation beyond "is this valid JSON", the
// sandbox and the API's own 400s are the real validation.
const DERIVATION_API_URL = apiBase("derivation", "/derivation");

const statusEl = document.getElementById("status");
const tbodyEl = document.querySelector("#derivations-table tbody");
const configEl = document.getElementById("editor-config");
const fnSourceEl = document.getElementById("editor-fn-source");
const testCasesEl = document.getElementById("editor-test-cases");
const configErrorEl = document.getElementById("config-error");
const testCasesErrorEl = document.getElementById("test-cases-error");
const resultEl = document.getElementById("editor-result");
const testResultsEl = document.getElementById("test-results");
const formEl = document.getElementById("editor-form");

// --- Form/Raw JSON toggle - the form is a friendlier way to fill in the
// exact same config JSON hand-writing already produces, not a second
// source of truth. #editor-config stays what's actually submitted. ---

const modeFormBtn = document.getElementById("editor-mode-form");
const modeRawBtn = document.getElementById("editor-mode-raw");
const formFieldsEl = document.getElementById("editor-form-fields");
const configLabelEl = document.getElementById("editor-config-label");
const fieldsetFormulaEl = document.getElementById("fieldset-formula");
const fieldsetRollupEl = document.getElementById("fieldset-rollup");

const fieldKindEl = document.getElementById("field-kind");
const fieldNameEl = document.getElementById("field-name");
const fieldWindowSecondsEl = document.getElementById("field-window-seconds");
const fieldIntervalSecondsEl = document.getElementById("field-interval-seconds");
const fieldOutputBrickClassEl = document.getElementById("field-output-brick-class");
const fieldOutputUnitEl = document.getElementById("field-output-unit");
const fieldOutputLabelEl = document.getElementById("field-output-label");
const fieldDependsOnEl = document.getElementById("field-depends-on");
const fieldSelectEl = document.getElementById("field-select");
const fieldTargetVarEl = document.getElementById("field-target-var");
const fieldInputVarsEl = document.getElementById("field-input-vars");
const fieldExtraVarsEl = document.getElementById("field-extra-vars");
const fieldAppliesToTopicGlobEl = document.getElementById("field-applies-to-topic-glob");
const fieldInputWindowsEl = document.getElementById("field-input-windows");
const fieldRootSelectEl = document.getElementById("field-root-select");
const fieldPartRelationshipEl = document.getElementById("field-part-relationship");
const fieldLeafPointClassEl = document.getElementById("field-leaf-point-class");

const FORM_FIELDS = [
  fieldNameEl, fieldWindowSecondsEl, fieldIntervalSecondsEl,
  fieldOutputBrickClassEl, fieldOutputUnitEl, fieldOutputLabelEl, fieldDependsOnEl,
  fieldSelectEl, fieldTargetVarEl, fieldInputVarsEl, fieldExtraVarsEl, fieldAppliesToTopicGlobEl, fieldInputWindowsEl,
  fieldRootSelectEl, fieldPartRelationshipEl, fieldLeafPointClassEl,
];

function splitList(value) {
  return value.split(",").map((s) => s.trim()).filter(Boolean);
}

// Assembles the exact object shape buildDraftBody() below expects from
// JSON.parse(configEl.value) - field names/shapes straight from
// CreateDerivationRequest in derivation-api-openapi.yaml, nothing invented.
function collectFormValues() {
  const kind = fieldKindEl.value;
  const config = {
    name: fieldNameEl.value,
    kind,
    window_seconds: Number(fieldWindowSecondsEl.value) || 3600,
  };
  if (fieldIntervalSecondsEl.value !== "") config.interval_seconds = Number(fieldIntervalSecondsEl.value);

  const output = {};
  if (fieldOutputBrickClassEl.value) output.brick_class = fieldOutputBrickClassEl.value;
  if (fieldOutputUnitEl.value) output.unit = fieldOutputUnitEl.value;
  if (fieldOutputLabelEl.value) output.label = fieldOutputLabelEl.value;
  config.output = output;

  const dependsOn = splitList(fieldDependsOnEl.value);
  if (dependsOn.length) config.depends_on = dependsOn;

  if (kind === "formula") {
    config.select = fieldSelectEl.value;
    config.target_var = fieldTargetVarEl.value;
    config.input_vars = splitList(fieldInputVarsEl.value);
    const extraVars = splitList(fieldExtraVarsEl.value);
    if (extraVars.length) config.extra_vars = extraVars;
    if (fieldAppliesToTopicGlobEl.value) config.applies_to_topic_glob = fieldAppliesToTopicGlobEl.value;
    if (fieldInputWindowsEl.value.trim()) {
      try {
        config.input_windows = JSON.parse(fieldInputWindowsEl.value);
        fieldInputWindowsEl.classList.remove("invalid");
      } catch {
        // Left out of the generated config rather than blocking the whole
        // form - switch to Raw JSON mode for anything more than a quick
        // fix here, same escape hatch as every other form-doesn't-cover-it case.
        fieldInputWindowsEl.classList.add("invalid");
      }
    } else {
      fieldInputWindowsEl.classList.remove("invalid");
    }
  } else {
    config.root_select = fieldRootSelectEl.value;
    config.part_relationship = fieldPartRelationshipEl.value;
    config.leaf_point_class = fieldLeafPointClassEl.value;
  }
  return config;
}

function populateFormFromConfig(config) {
  fieldKindEl.value = config.kind || "formula";
  fieldNameEl.value = config.name || "";
  fieldWindowSecondsEl.value = config.window_seconds ?? 3600;
  fieldIntervalSecondsEl.value = config.interval_seconds ?? "";
  fieldOutputBrickClassEl.value = config.output?.brick_class || "";
  fieldOutputUnitEl.value = config.output?.unit || "";
  fieldOutputLabelEl.value = config.output?.label || "";
  fieldDependsOnEl.value = (config.depends_on || []).join(", ");
  fieldSelectEl.value = config.select || "";
  fieldTargetVarEl.value = config.target_var || "";
  fieldInputVarsEl.value = (config.input_vars || []).join(", ");
  fieldExtraVarsEl.value = (config.extra_vars || []).join(", ");
  fieldAppliesToTopicGlobEl.value = config.applies_to_topic_glob || "";
  fieldInputWindowsEl.value = config.input_windows ? JSON.stringify(config.input_windows) : "";
  fieldRootSelectEl.value = config.root_select || "";
  fieldPartRelationshipEl.value = config.part_relationship || "";
  fieldLeafPointClassEl.value = config.leaf_point_class || "";
  updateFieldsetVisibility();
}

function updateFieldsetVisibility() {
  const isFormula = fieldKindEl.value === "formula";
  fieldsetFormulaEl.hidden = !isFormula;
  fieldsetRollupEl.hidden = isFormula;
}

function syncConfigFromForm() {
  configEl.value = JSON.stringify(collectFormValues(), null, 2);
  validateJsonField(configEl, configErrorEl);
}

function setMode(mode) {
  const isForm = mode === "form";
  formFieldsEl.hidden = !isForm;
  // Inline style, not the `hidden` attribute: #editor-form label's own
  // `display: block` (console.css) is an author rule and always beats the
  // UA stylesheet's `[hidden] { display: none }` regardless of specificity
  // (origin is compared before specificity in the cascade) - same trap
  // auth.js's showLoginGate/hideLoginGate already documents for #main.
  configLabelEl.style.display = isForm ? "none" : "";
  modeFormBtn.setAttribute("aria-pressed", String(isForm));
  modeRawBtn.setAttribute("aria-pressed", String(!isForm));
  if (isForm) {
    try {
      populateFormFromConfig(JSON.parse(configEl.value || "{}"));
    } catch {
      // Bad JSON in the raw pane - leave the form fields as they were,
      // config-error already flags the problem for the user to fix (or
      // switch back to Raw JSON to fix it directly).
    }
  } else {
    syncConfigFromForm();
  }
}

fieldKindEl.addEventListener("change", () => {
  updateFieldsetVisibility();
  syncConfigFromForm();
});
FORM_FIELDS.forEach((field) => field.addEventListener("input", syncConfigFromForm));
modeFormBtn.addEventListener("click", () => setMode("form"));
modeRawBtn.addEventListener("click", () => setMode("raw"));

const DEFAULT_CONFIG = {
  name: "effective-zone-temp",
  kind: "formula",
  select:
    "PREFIX brick: <https://brickschema.org/schema/Brick#> SELECT ?target ?zoneTemp1 ?zoneTemp2 WHERE { ?target brick:hasPoint ?zoneTemp1, ?zoneTemp2 . ?zoneTemp1 a brick:Zone_Air_Temperature_Sensor . ?zoneTemp2 a brick:Zone_Air_Temperature_Sensor . FILTER(?zoneTemp1 != ?zoneTemp2) }",
  target_var: "target",
  input_vars: ["zoneTemp1", "zoneTemp2"],
  window_seconds: 3600,
  output: { brick_class: "Effective_Zone_Air_Temperature_Sensor", unit: "°F" },
  depends_on: [],
};
const DEFAULT_FN_SOURCE = "def run(inputs, row):\n    vals = [v for s in inputs.values() for _, v in s[-1:]]\n    return sum(vals) / len(vals) if vals else None\n";
const DEFAULT_TEST_CASES = [
  { inputs: { zoneTemp1: [["2026-01-01T00:00:00Z", 70.0]], zoneTemp2: [["2026-01-01T00:00:00Z", 74.0]] }, row: {}, expected: 72.0 },
];

function loadEditorTemplate() {
  configEl.value = JSON.stringify(DEFAULT_CONFIG, null, 2);
  fnSourceEl.value = DEFAULT_FN_SOURCE;
  testCasesEl.value = JSON.stringify(DEFAULT_TEST_CASES, null, 2);
  setMode("form");
  setEditorUrlId(null);
  showResult("Results appear here.", false);
  clearTestResults();
  validateJsonField(configEl, configErrorEl);
  validateJsonField(testCasesEl, testCasesErrorEl);
}

// Live feedback on the two JSON textareas - surfaces a malformed-JSON typo
// where it was made, instead of only after clicking Test/Dry run/Save.
// Empty is treated as valid (buildDraftBody() below defaults it), same as
// the API's own optional-field handling.
function validateJsonField(textarea, errorEl) {
  if (!textarea.value.trim()) {
    textarea.classList.remove("invalid");
    errorEl.hidden = true;
    return true;
  }
  try {
    JSON.parse(textarea.value);
    textarea.classList.remove("invalid");
    errorEl.hidden = true;
    return true;
  } catch (err) {
    textarea.classList.add("invalid");
    errorEl.textContent = err.message;
    errorEl.hidden = false;
    return false;
  }
}

configEl.addEventListener("input", () => validateJsonField(configEl, configErrorEl));
testCasesEl.addEventListener("input", () => validateJsonField(testCasesEl, testCasesErrorEl));

function clearTestResults() {
  testResultsEl.innerHTML = "";
}

// Adapts a test case's [isoTimestamp, value] series into the {ts, value}
// (epoch seconds) shape chart.js's drawLineChart expects - the same shape
// app.js already feeds it from real point history, just parsed from a
// JSON string instead of coming straight off the wire.
function seriesToHistory(series) {
  return series.map(([iso, value]) => ({ ts: Date.parse(iso) / 1000, value }));
}

// A "rollup" derivation's test-case inputs are a bare list of series (no
// names - see derivation-api-openapi.yaml's rollup example); a "formula"
// derivation's are a {name: series} object. Normalizes both into
// [label, series] pairs so the chart rendering below doesn't care which.
function namedInputSeries(inputs) {
  if (Array.isArray(inputs)) return inputs.map((series, i) => [`input ${i + 1}`, series]);
  return Object.entries(inputs || {});
}

// Renders one card per test case: a small line chart per named input
// series (real data, not just the number it reduces to) plus a pass/fail
// badge and expected-vs-actual - replaces dumping the raw JSON array for
// this one action. Dry-run/Save results still render as JSON in
// #editor-result below - only "Run tests" has fixed-shape, chartable
// series data to work with.
function renderTestResults(testCases, results) {
  testResultsEl.innerHTML = "";
  results.forEach((result, i) => {
    const testCase = testCases[i] || {};
    const card = el("div", { className: "test-case-card" });

    const badge = el("span", {
      className: `badge result-${result.passed ? "pass" : "fail"}`,
      textContent: result.passed ? "pass" : "fail",
    });
    card.appendChild(el("h3", {}, [badge, ` test case #${i + 1}`]));

    const chartsRow = el("div", { className: "test-case-charts" });
    for (const [name, series] of namedInputSeries(testCase.inputs)) {
      const history = seriesToHistory(series);
      const figure = el("figure");
      if (history.length) {
        const canvas = el("canvas", { width: 220, height: 70 });
        figure.appendChild(canvas);
        drawLineChart(canvas, history, 14); // smaller pad than app.js's default - this canvas is a third the size
      } else {
        figure.appendChild(el("p", { className: "hint", textContent: "no data" }));
      }
      figure.appendChild(el("figcaption", { textContent: name }));
      chartsRow.appendChild(figure);
    }
    card.appendChild(chartsRow);

    const outcomeText = `expected: ${JSON.stringify(result.expected)}   actual: ${JSON.stringify(result.actual)}`;
    card.appendChild(el("p", { textContent: outcomeText }));
    if (result.error) card.appendChild(el("p", { className: "field-error", textContent: result.error }));

    testResultsEl.appendChild(card);
  });
}

async function derivationApi(method, path, body) {
  const res = await fetch(`${DERIVATION_API_URL}${path}`, {
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

// Every non-chart result (dry-run/save/errors) goes through here, and
// always clears any chart cards from a previous "Run tests" - the two
// results areas never show stale, unrelated content at the same time.
function showResult(value, isError) {
  clearTestResults();
  resultEl.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  resultEl.classList.toggle("result-error", !!isError);
}

// Splits a saved derivation back into the editor's three panes - the
// inverse of buildDraftBody() below.
function loadIntoEditor(derivation) {
  const { fn_source, test_cases, ...config } = derivation;
  configEl.value = JSON.stringify(config, null, 2);
  fnSourceEl.value = fn_source;
  testCasesEl.value = JSON.stringify(test_cases, null, 2);
  setMode("form");
  setEditorUrlId(derivation.id);
  validateJsonField(configEl, configErrorEl);
  validateJsonField(testCasesEl, testCasesErrorEl);
  showResult(`Loaded ${derivation.name} (${derivation.id}) into the editor.`, false);
}

function buildDraftBody() {
  let config;
  try {
    config = JSON.parse(configEl.value || "{}");
  } catch (err) {
    throw new Error(`config is not valid JSON: ${err.message}`);
  }
  let testCases;
  try {
    testCases = JSON.parse(testCasesEl.value || "[]");
  } catch (err) {
    throw new Error(`test_cases is not valid JSON: ${err.message}`);
  }
  return { ...config, fn_source: fnSourceEl.value, test_cases: testCases };
}

// Full value still reachable via the native title="" tooltip on hover -
// truncation only affects the table layout, not the data shown.
function truncatedCell(text) {
  return el("td", {}, [el("span", { className: "truncate", title: text, textContent: text })]);
}

function renderDerivations(derivations) {
  tbodyEl.innerHTML = "";
  for (const derivation of derivations) {
    const loadBtn = el("button", { type: "button", textContent: "Load into editor" });
    loadBtn.addEventListener("click", () => loadIntoEditor(derivation));
    const deleteBtn = el("button", { type: "button", textContent: "Delete" });
    deleteBtn.addEventListener("click", async () => {
      // Deleting a derivation is irreversible (no undo, no trash) - one
      // native confirm is the cheapest real error-prevention step short
      // of a full custom dialog, which this pass deliberately skips.
      if (!confirm(`Delete derivation "${derivation.name}"? This can't be undone.`)) return;
      await derivationApi("DELETE", `/derivations/${derivation.id}`);
      await loadDerivations();
    });

    tbodyEl.appendChild(
      el("tr", {}, [
        el("td", { textContent: derivation.name }),
        el("td", { textContent: derivation.kind }),
        el("td", { textContent: String(derivation.window_seconds) }),
        truncatedCell(JSON.stringify(derivation.output || {})),
        el("td", {}, [loadBtn, deleteBtn]),
      ]),
    );
  }
  if (!derivations.length) tbodyEl.appendChild(el("tr", {}, [el("td", { colSpan: 5, className: "hint", textContent: "None yet." })]));
}

// Replaces the blank flash between page load and the first fetch settling
// - same colSpan/className shape as the "None yet." empty state, just a
// different message, so there's exactly one render path a reader learns.
tbodyEl.appendChild(el("tr", {}, [el("td", { colSpan: 5, className: "hint", textContent: "Loading…" })]));

async function loadDerivations() {
  try {
    const derivations = await derivationApi("GET", "/derivations");
    renderDerivations(derivations);
    statusEl.textContent = `${derivations.length} derivations`;
    return derivations;
  } catch (err) {
    statusEl.textContent = `failed to reach derivation API: ${err.message}`;
    return [];
  }
}

// Keeps the currently-loaded derivation's id in the URL (?id=...) so the
// address bar is a shareable/bookmarkable link straight to it - "share the
// rule I'm looking at" only works if the URL says which one that is.
// replaceState, not pushState: switching what's loaded in the editor isn't
// a page navigation the back button should step through.
function setEditorUrlId(id) {
  const url = new URL(location.href);
  if (id) url.searchParams.set("id", id);
  else url.searchParams.delete("id");
  history.replaceState(null, "", url);
}

// Loads the derivation matching a ?id=... captured before
// loadEditorTemplate() (which runs first in init() and clears that same
// param) - the other half of setEditorUrlId, so a pasted link reproduces
// what the sender saw instead of just the template.
function loadFromUrlId(id, derivations) {
  if (!id) return;
  const derivation = derivations.find((d) => d.id === id);
  if (derivation) {
    loadIntoEditor(derivation);
  } else {
    showResult(`No derivation found for id "${id}" (link may be stale) - showing a fresh template instead.`, true);
  }
}

// Turns a flat error message into: (1) a jump to the offending line in
// whichever editor pane it refers to, when the message carries one, and
// (2) for a rejected Save's bundled test-case failures, the same pass/fail
// chart cards Run Tests already renders instead of a raw JSON dump.
function lineFromPosition(text, position) {
  return text.slice(0, position).split("\n").length;
}

async function handleEditorError(err) {
  const message = err.message;
  const sandboxLineMatch = message.match(/line (\d+)\)/);
  if (sandboxLineMatch) {
    fnSourceEl.markErrorLine?.(Number(sandboxLineMatch[1]));
  } else {
    const positionMatch = message.match(/position (\d+)/);
    if (positionMatch) {
      const target = message.startsWith("test_cases is not valid JSON") ? testCasesEl : configEl;
      target.markErrorLine?.(lineFromPosition(target.value, Number(positionMatch[1])));
    }
  }

  // POST /derivations rejects with "test_cases failed: {...}" - the
  // embedded JSON only lists the *failed* cases, losing index alignment
  // with the original list, so re-running the already-existing test
  // endpoint for a full, aligned result set is simpler than parsing it out.
  if (message.startsWith("test_cases failed:")) {
    try {
      const { fn_source, test_cases } = buildDraftBody();
      const results = await derivationApi("POST", "/derivations/test", { fn_source, test_cases });
      const failCount = results.filter((r) => !r.passed).length;
      showResult(`Save rejected - ${failCount}/${results.length} test case(s) failed. See below.`, true);
      renderTestResults(test_cases, results);
    } catch (rerunErr) {
      showResult(rerunErr.message, true);
    }
    return;
  }

  showResult(message, true);
}

document.getElementById("editor-test").addEventListener("click", async () => {
  try {
    const { fn_source, test_cases } = buildDraftBody();
    const results = await derivationApi("POST", "/derivations/test", { fn_source, test_cases });
    const failCount = results.filter((r) => !r.passed).length;
    showResult(failCount ? `${failCount}/${results.length} test case(s) failed - see below.` : `All ${results.length} test case(s) passed.`, !!failCount);
    renderTestResults(test_cases, results);
  } catch (err) {
    handleEditorError(err);
  }
});

document.getElementById("editor-dry-run").addEventListener("click", async () => {
  try {
    const draft = buildDraftBody();
    const trace = await derivationApi("POST", "/derivations/dry-run", draft);
    showResult(trace, false);
  } catch (err) {
    handleEditorError(err);
  }
});

document.getElementById("editor-clear").addEventListener("click", loadEditorTemplate);

formEl.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const draft = buildDraftBody();
    const created = await derivationApi("POST", "/derivations", draft);
    showResult(created, false);
    setEditorUrlId(created.id); // newly-saved derivation gets a shareable link too
    await loadDerivations();
  } catch (err) {
    handleEditorError(err);
  }
});

async function init() {
  const requestedId = new URLSearchParams(location.search).get("id");
  loadEditorTemplate();
  const derivations = await loadDerivations();
  loadFromUrlId(requestedId, derivations);
}

initAuth(init);

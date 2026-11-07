// Commissioning (Cx) page - Baler#41. Talks to commissioning_api through
// the gateway at /cx/ (override with ?cx=...). No build step, no
// framework: same plain-globals style as ops.js/users.js. auth.js (login
// gate, authHeaders) and chart.js (drawLineChart) load first.
//
// URL state: ?project=<id>&run=<id>&equip=<uri> - the equipment tree's
// Cx dot (app.js) links here with those, and selecting things updates it,
// so a view can be shared by copying the address.

const cxParams = new URLSearchParams(location.search);
const CX_URL = cxParams.get("cx") || "/cx";
const CX_INGEST_URL = cxParams.get("ingest") || "/ingest";
const ROLE_RANK = { viewer: 1, operator: 2, admin: 3 };
const STATUS_ORDER = { fail: 0, inconclusive: 1, pass: 2, not_applicable: 3 };
const FAMILY_ORDER = ["exists", "live", "pair", "ladder", "template", "gl36", "cx"];
const FAMILY_LABEL = {
  exists: "1 · reporting", live: "2 · live", pair: "3 · pairs", ladder: "ladder",
  template: "expected points", gl36: "GL36 / 90.1", cx: "economizer",
};

const cx = {
  me: null,
  projects: [],
  project: null,
  runs: [],
  run: null,
  results: [],
  pollTimer: null,
  sites: [],
};

const $ = (id) => document.getElementById(id);
const statusEl = $("status");

function esc(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function when(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString();
}

function el(tag, props = {}, children = []) {
  const e = Object.assign(document.createElement(tag), props);
  for (const c of children) e.append(c);
  return e;
}

async function cxApi(method, path, body, raw = false) {
  const headers = { ...authHeaders() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`${CX_URL}${path}`, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
  if (res.status === 401) {
    handleUnauthorized();
    throw new Error("session ended - sign in again");
  }
  if (raw && res.ok) return res;
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (res.status === 403) throw new Error("your role can't do that");
  if (!res.ok) {
    const err = new Error((data && data.error) || text.trim() || `${method} ${path} failed (${res.status})`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

const can = (role) => cx.me && (ROLE_RANK[cx.me.role] || 0) >= ROLE_RANK[role];

function applyRoles(root = document) {
  for (const b of root.querySelectorAll("[data-min-role]")) {
    b.hidden = !can(b.dataset.minRole);
  }
}

function setUrl() {
  const q = new URLSearchParams(location.search);
  for (const k of ["project", "run", "equip"]) q.delete(k);
  if (cx.project) q.set("project", cx.project.id);
  if (cx.run) q.set("run", cx.run.id);
  history.replaceState(null, "", `${location.pathname}${q.toString() ? `?${q}` : ""}`);
}

function flash(target, message, isError = false) {
  target.textContent = message;
  target.classList.toggle("st-fail", isError);
}

// --------------------------------------------------------------- projects

async function loadProjects() {
  const archived = $("show-archived").checked;
  cx.projects = await cxApi("GET", `/projects${archived ? "?include_archived=true" : ""}`);
  renderProjects();
}

function siteName(id) {
  if (!id) return "whole organization";
  const s = cx.sites.find((x) => x.id === id);
  return s ? s.name : id;
}

function renderProjects() {
  const body = $("projects-body");
  body.innerHTML = "";
  if (!cx.projects.length) {
    body.innerHTML = `<tr><td colspan="4" class="hint">No projects yet.${can("operator") ? " Create one to start verifying a site." : ""}</td></tr>`;
  }
  for (const p of cx.projects) {
    const tr = el("tr", { className: "clickable" });
    if (cx.project && cx.project.id === p.id) tr.classList.add("selected");
    const phase = (p.phases.find((x) => x.key === p.phase) || {}).name || p.phase || "-";
    tr.innerHTML = `<td>${esc(p.name)}${p.archived_at ? ' <span class="hint">(archived)</span>' : ""}</td>
      <td>${esc(scopeLabel(p))}</td><td>${esc(phase)}</td><td>${esc(when(p.created_at))}</td>`;
    tr.addEventListener("click", () => selectProject(p.id).catch((err) => flash($("projects-status"), err.message, true)));
    body.append(tr);
  }
}

async function selectProject(id, runId = null) {
  stopPolling();
  cx.project = await cxApi("GET", `/projects/${encodeURIComponent(id)}`);
  cx.run = null;
  cx.results = [];
  $("project-view").hidden = false;
  $("results-section").hidden = true;
  renderProjects();
  renderProject();
  setUrl();
  await Promise.all([loadRuns(runId), loadEvents(), loadGaps()]);
  if (typeof cxProjectHooks !== "undefined") for (const hook of cxProjectHooks) hook(cx.project);
}

function scopeLabel(p) {
  const prefixes = (p.settings && p.settings.prefixes) || [];
  return prefixes.length ? `${siteName(p.site_id)} · ${prefixes.join(", ")}` : siteName(p.site_id);
}

function renderProject() {
  const p = cx.project;
  $("project-title").textContent = p.name;
  $("project-meta").textContent = `${scopeLabel(p)} · created ${when(p.created_at)}${p.archived_at ? ` · archived ${when(p.archived_at)}` : ""}`;
  $("run-window").placeholder = String((p.settings && p.settings.window_hours) || 24);
  $("archive-project-btn").disabled = !!p.archived_at;
  $("run-btn").disabled = !!p.archived_at;
  renderPhaseBar();
}

function renderPhaseBar() {
  const bar = $("phase-bar");
  bar.innerHTML = "";
  for (const ph of cx.project.phases) {
    const btn = el("button", { type: "button", textContent: ph.name });
    btn.title = `${ph.status}${ph.started_at ? ` · started ${when(ph.started_at)}` : ""}${ph.completed_at ? ` · completed ${when(ph.completed_at)}` : ""}`;
    btn.disabled = !can("operator") || ph.status === "active" || !!cx.project.archived_at;
    btn.addEventListener("click", () => setPhase(ph));
    if (ph.status === "active") btn.setAttribute("aria-current", "step");
    bar.append(el("li", { className: ph.status }, [btn]));
  }
}

async function setPhase(ph) {
  const note = prompt(`Make "${ph.name}" the current phase?\nOptional note for the activity log:`, "");
  if (note === null) return;
  try {
    cx.project = await cxApi("POST", `/projects/${cx.project.id}/phase`, { key: ph.key, note: note || undefined });
    renderProject();
    await Promise.all([loadProjects(), loadEvents()]);
  } catch (err) {
    alert(err.message);
  }
}

// ---------------------------------------------------------- phase editor

function phaseRow(ph) {
  const tr = el("tr");
  const key = el("input", { value: ph.key, required: true, pattern: "[a-z][a-z0-9_\\-]{0,39}", size: 12 });
  key.disabled = !ph.isNew;
  key.className = "ph-key";
  const name = el("input", { value: ph.name, required: true, size: 24, className: "ph-name" });
  const status = el("select", { className: "ph-status" });
  for (const s of ["pending", "active", "complete", "skipped"]) status.append(el("option", { value: s, textContent: s, selected: s === ph.status }));
  const up = el("button", { type: "button", textContent: "↑", title: "Move up" });
  const down = el("button", { type: "button", textContent: "↓", title: "Move down" });
  const del = el("button", { type: "button", textContent: "Remove" });
  up.addEventListener("click", () => tr.previousElementSibling && tr.parentNode.insertBefore(tr, tr.previousElementSibling));
  down.addEventListener("click", () => tr.nextElementSibling && tr.parentNode.insertBefore(tr.nextElementSibling, tr));
  del.addEventListener("click", () => tr.remove());
  tr.append(el("td", {}, [key]), el("td", {}, [name]), el("td", {}, [status]), el("td", {}, [up, down, del]));
  return tr;
}

function openPhaseEditor() {
  const body = $("phases-edit-body");
  body.innerHTML = "";
  for (const ph of cx.project.phases) body.append(phaseRow(ph));
  $("phases-error").hidden = true;
  $("phases-dialog").showModal();
}

async function savePhases(ev) {
  ev.preventDefault();
  const phases = [...$("phases-edit-body").querySelectorAll("tr")].map((tr) => ({
    key: tr.querySelector(".ph-key").value.trim(),
    name: tr.querySelector(".ph-name").value.trim(),
    status: tr.querySelector(".ph-status").value,
  }));
  try {
    cx.project = await cxApi("PUT", `/projects/${cx.project.id}/phases`, { phases });
    $("phases-dialog").close();
    renderProject();
    await Promise.all([loadProjects(), loadEvents()]);
  } catch (err) {
    flash($("phases-error"), err.message, true);
    $("phases-error").hidden = false;
  }
}

// ------------------------------------------------------- project dialogs

async function loadSites() {
  if (can("admin")) {
    try {
      const res = await fetch(`${AUTH_API_URL}/sites`, { headers: authHeaders() });
      if (res.ok) cx.sites = await res.json();
    } catch {
      cx.sites = [];
    }
  }
  if (!cx.sites.length && cx.me && cx.me.site_ids) cx.sites = cx.me.site_ids.map((id) => ({ id, name: id }));
}

function openNewProject() {
  const site = $("project-site");
  site.innerHTML = "";
  if (!(cx.me.site_ids || []).length) site.append(el("option", { value: "", textContent: "Whole organization" }));
  for (const s of cx.sites) site.append(el("option", { value: s.id, textContent: s.name }));
  const phase = $("project-phase");
  phase.innerHTML = "";
  const defaults = [["design", "Design"], ["construction", "Construction / installation"], ["startup", "Start-up / power-on"],
    ["functional", "Functional testing"], ["acceptance", "Acceptance"], ["warranty", "Warranty"], ["operations", "Operations"]];
  for (const [k, n] of defaults) phase.append(el("option", { value: k, textContent: n, selected: k === "startup" }));
  $("project-prefixes").value = "";
  $("project-error").hidden = true;
  $("project-dialog").showModal();
}

async function createProject(ev) {
  ev.preventDefault();
  const body = { name: $("project-name").value.trim(), phase: $("project-phase").value };
  if ($("project-site").value) body.site_id = $("project-site").value;
  const prefixes = $("project-prefixes").value.split(",").map((s) => s.trim()).filter(Boolean);
  if (prefixes.length) body.settings = { prefixes };
  try {
    const p = await cxApi("POST", "/projects", body);
    $("project-dialog").close();
    await loadProjects();
    await selectProject(p.id);
  } catch (err) {
    flash($("project-error"), err.message, true);
    $("project-error").hidden = false;
  }
}

async function renameProject() {
  const name = prompt("Project name", cx.project.name);
  if (!name || name === cx.project.name) return;
  try {
    cx.project = await cxApi("PATCH", `/projects/${cx.project.id}`, { name });
    renderProject();
    await Promise.all([loadProjects(), loadEvents()]);
  } catch (err) {
    alert(err.message);
  }
}

function openSettings() {
  $("settings-json").value = JSON.stringify(cx.project.settings || {}, null, 2);
  $("settings-error").hidden = true;
  $("settings-dialog").showModal();
}

async function saveSettings(ev) {
  ev.preventDefault();
  let settings;
  try {
    settings = JSON.parse($("settings-json").value || "{}");
  } catch (err) {
    flash($("settings-error"), `not JSON: ${err.message}`, true);
    $("settings-error").hidden = false;
    return;
  }
  try {
    cx.project = await cxApi("PATCH", `/projects/${cx.project.id}`, { settings });
    $("settings-dialog").close();
    renderProject();
    await loadEvents();
  } catch (err) {
    flash($("settings-error"), err.message, true);
    $("settings-error").hidden = false;
  }
}

async function archiveProject() {
  if (!confirm(`Archive "${cx.project.name}"? Its runs and history are kept; it can't be run again.`)) return;
  try {
    await cxApi("DELETE", `/projects/${cx.project.id}`);
    await selectProject(cx.project.id);
    await loadProjects();
  } catch (err) {
    alert(err.message);
  }
}

// ------------------------------------------------------------------ runs

function stopPolling() {
  if (cx.pollTimer) clearTimeout(cx.pollTimer);
  cx.pollTimer = null;
}

async function loadRuns(selectRunId = null) {
  cx.runs = await cxApi("GET", `/projects/${cx.project.id}/runs`);
  renderRuns();
  const active = cx.runs.find((r) => r.status === "queued" || r.status === "running");
  if (active) pollRun(active.id);
  const pick = selectRunId ? cx.runs.find((r) => r.id === selectRunId) : cx.runs.find((r) => r.status === "completed");
  if (pick && pick.status === "completed") await showRun(pick.id);
}

function renderRuns() {
  const body = $("runs-body");
  body.innerHTML = "";
  if (!cx.runs.length) body.innerHTML = '<tr><td colspan="7" class="hint">No runs yet.</td></tr>';
  for (const r of cx.runs) {
    const by = (r.summary && r.summary.by_status) || {};
    const tr = el("tr", { className: r.status === "completed" ? "clickable" : "" });
    if (cx.run && cx.run.id === r.id) tr.classList.add("selected");
    const st = r.status === "failed" ? `<span class="st-fail" title="${esc(r.error)}">failed</span>` : esc(r.status);
    tr.innerHTML = `<td>${esc(when(r.started_at))}</td><td>${esc(r.phase || "-")}</td><td>${st}</td>
      <td>${by.fail ?? ""}</td><td>${by.inconclusive ?? ""}</td><td>${by.pass ?? ""}</td><td></td>`;
    if (r.status === "completed") {
      tr.addEventListener("click", () => showRun(r.id));
      const rep = el("button", { type: "button", textContent: "Report" });
      rep.addEventListener("click", (ev) => {
        ev.stopPropagation();
        openReport(r.id);
      });
      tr.lastElementChild.append(rep);
    }
    body.append(tr);
  }
}

function progressText(run) {
  const p = run.progress || {};
  if (run.status === "queued") return "queued…";
  const of = p.equipment_total ? ` ${p.equipment_done}/${p.equipment_total} equipment` : "";
  return `running: ${p.stage || "starting"}${of}${p.points ? `, ${p.points} points` : ""}…`;
}

async function startRun() {
  const body = {};
  const wh = parseFloat($("run-window").value);
  if (!isNaN(wh)) body.window_hours = wh;
  $("run-btn").disabled = true;
  try {
    const run = await cxApi("POST", `/projects/${cx.project.id}/runs`, body);
    await loadEvents();
    cx.runs.unshift(run);
    renderRuns();
    pollRun(run.id);
  } catch (err) {
    if (err.status === 409 && err.data && err.data.run_id) {
      flash($("run-status"), "a run is already in progress - following it");
      pollRun(err.data.run_id);
    } else {
      flash($("run-status"), err.message, true);
      $("run-btn").disabled = false;
    }
  }
}

function pollRun(runId) {
  stopPolling();
  $("run-btn").disabled = true;
  const tick = async () => {
    let got;
    try {
      got = await cxApi("GET", `/runs/${runId}`);
    } catch (err) {
      flash($("run-status"), err.message, true);
      cx.pollTimer = setTimeout(tick, 5000);
      return;
    }
    const run = got.run;
    if (run.status === "queued" || run.status === "running") {
      flash($("run-status"), progressText(run));
      cx.pollTimer = setTimeout(tick, 1500);
      return;
    }
    cx.pollTimer = null;
    $("run-btn").disabled = !!cx.project.archived_at;
    if (run.status === "failed") flash($("run-status"), `run failed: ${run.error}`, true);
    else flash($("run-status"), `finished ${when(run.finished_at)}`);
    await loadRuns(run.status === "completed" ? run.id : null);
    await loadEvents();
    // a completed run may have closed or reopened issues
    if (typeof cxProjectHooks !== "undefined") for (const hook of cxProjectHooks) hook(cx.project);
  };
  tick();
}

// -------------------------------------------------------- results grid

const familyOf = (checkId) => checkId.split(":", 1)[0];

async function showRun(runId) {
  const got = await cxApi("GET", `/runs/${runId}`);
  cx.run = got.run;
  cx.results = got.results;
  cx.resultsPage = 0;
  setUrl();
  renderRuns();
  renderResults();
  const equip = cxParams.get("equip");
  if (equip && cxParams.get("run") === runId) {
    cxParams.delete("equip");
    openDrawer(`${equip}`, cx.results.filter((r) => r.equip_uri === equip));
  }
}

const RESULTS_PAGE = 40;

function equipLabel(uri) {
  return uri.replace(/^urn:equip:/, "");
}

function renderResults() {
  const run = cx.run;
  $("results-section").hidden = false;
  $("results-title").textContent = `Results · ${when(run.started_at)} · ${run.phase || "no phase"}`;
  $("results-freshness").textContent = (run.summary && run.summary.freshness) || "";
  const filter = $("results-filter").value;
  const search = $("results-search").value.trim().toLowerCase();
  const families = [...new Set(cx.results.map((r) => familyOf(r.check_id)))]
    .sort((a, b) => (FAMILY_ORDER.indexOf(a) + 99) % 99 - (FAMILY_ORDER.indexOf(b) + 99) % 99 || a.localeCompare(b));
  const byEquip = new Map();
  for (const r of cx.results) {
    if (!byEquip.has(r.equip_uri)) byEquip.set(r.equip_uri, []);
    byEquip.get(r.equip_uri).push(r);
  }
  const worst = (rs) => Math.min(...rs.map((r) => STATUS_ORDER[r.status] ?? 9));
  const equips = [...byEquip.keys()]
    .filter((e) => !filter || worst(byEquip.get(e)) <= STATUS_ORDER[filter])
    .filter((e) => !search || equipLabel(e).toLowerCase().includes(search))
    .sort((a, b) => worst(byEquip.get(a)) - worst(byEquip.get(b)) || a.localeCompare(b));
  // One page at a time (#75: a whole-org run was one ~7,500px table).
  const pages = Math.max(1, Math.ceil(equips.length / RESULTS_PAGE));
  cx.resultsPage = Math.min(cx.resultsPage || 0, pages - 1);
  const shown = equips.slice(cx.resultsPage * RESULTS_PAGE, (cx.resultsPage + 1) * RESULTS_PAGE);
  $("results-count").textContent = `${equips.length} of ${byEquip.size} equipment`;
  $("results-pager").hidden = pages <= 1;
  $("results-page").textContent = `page ${cx.resultsPage + 1} of ${pages}`;
  $("results-prev").disabled = cx.resultsPage === 0;
  $("results-next").disabled = cx.resultsPage >= pages - 1;

  const grid = $("results-grid");
  grid.innerHTML = "";
  const head = el("tr", {}, [el("th", { textContent: "Equipment" })]);
  for (const f of families) head.append(el("th", { textContent: FAMILY_LABEL[f] || f, title: f }));
  grid.append(el("thead", {}, [head]));
  const tbody = el("tbody");
  for (const e of shown) {
    const rs = byEquip.get(e);
    const tr = el("tr");
    const name = el("th", { className: "equip", textContent: equipLabel(e), title: e });
    name.addEventListener("click", () => openDrawer(e, rs));
    name.style.cursor = "pointer";
    tr.append(name);
    for (const f of families) {
      const cell = rs.filter((r) => familyOf(r.check_id) === f);
      const td = el("td", { className: cell.length ? "cell" : "" });
      if (cell.length) {
        const n = {};
        for (const r of cell) n[r.status] = (n[r.status] || 0) + 1;
        for (const s of ["fail", "inconclusive", "pass"]) {
          if (n[s]) td.append(el("span", { className: `chip chip-${s}`, textContent: String(n[s]), title: `${n[s]} ${s}` }));
        }
        if (!n.fail && !n.inconclusive && !n.pass) td.append(el("span", { className: "chip chip-not_applicable", textContent: "n/a" }));
        td.addEventListener("click", () => openDrawer(`${equipLabel(e)} · ${FAMILY_LABEL[f] || f}`, cell));
      }
      tr.append(td);
    }
    tbody.append(tr);
  }
  if (!equips.length) tbody.innerHTML = `<tr><td class="hint" colspan="${families.length + 1}">Nothing to show.</td></tr>`;
  grid.append(tbody);
}

// ---------------------------------------------------------------- drawer

function closeDrawer() {
  $("drawer").hidden = true;
}

function openDrawer(title, results) {
  $("drawer-title").textContent = title;
  const body = $("drawer-body");
  body.innerHTML = "";
  const sorted = [...results].sort((a, b) => (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9) || a.check_id.localeCompare(b.check_id));
  for (const r of sorted) body.append(resultCard(r));
  if (!sorted.length) body.textContent = "No results.";
  $("drawer").hidden = false;
}

function resultCard(r) {
  const card = el("div", { className: "cx-result" });
  const sev = r.severity ? ` <span class="hint">(${esc(r.severity)})</span>` : "";
  card.innerHTML = `<h4><span class="st-${esc(r.status)}">${esc(r.status.replace("_", " "))}</span> · ${esc(r.check_id)}${sev}</h4>
    <div class="hint">${esc(r.subject)}</div>
    ${r.symptom ? `<p>${esc(r.symptom)}</p>` : ""}
    ${r.candidates && r.candidates.length ? `<div>Candidate causes:<ul>${r.candidates.map((c) => `<li>${esc(c.cause)} <span class="hint">- ${esc(c.distinguish)}</span></li>`).join("")}</ul></div>` : ""}`;
  const points = r.subject.split("|").filter((s) => s.startsWith("urn:point:"));
  for (const p of points.slice(0, 2)) card.append(sparkline(p));
  const ev = el("details", {}, [el("summary", { textContent: "Evidence" }), el("pre", { textContent: JSON.stringify(r.evidence, null, 2) })]);
  card.append(ev);
  if (typeof cxResultActions !== "undefined") for (const act of cxResultActions) act(card, r);
  return card;
}

function numeric(v) {
  if (typeof v === "number") return v;
  if (typeof v === "boolean") return v ? 1 : 0;
  const s = String(v).toLowerCase();
  if (["active", "on", "true", "running"].includes(s)) return 1;
  if (["inactive", "off", "false", "stopped"].includes(s)) return 0;
  const n = parseFloat(s);
  return isNaN(n) ? null : n;
}

function sparkline(pointUri) {
  const fig = el("figure", { className: "cx-spark" });
  const canvas = el("canvas", { width: 500, height: 90 });
  const cap = el("figcaption", { className: "hint", textContent: `${pointUri.replace(/^urn:point:/, "")} - loading…` });
  fig.append(canvas, cap);
  const start = Date.parse(cx.run.window_start) / 1000;
  const end = Date.parse(cx.run.window_end) / 1000 + 1;
  const topic = pointUri.replace(/^urn:point:/, "");
  fetch(`${CX_INGEST_URL}/history?point=${encodeURIComponent(topic)}&start=${start}&end=${end}`, { headers: authHeaders() })
    .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`history ${res.status}`))))
    .then((rows) => {
      const pts = rows.map((x) => ({ ts: x.ts, value: numeric(x.value) })).filter((x) => x.value !== null);
      if (pts.length < 2) {
        cap.textContent = `${topic} - ${pts.length ? "one sample" : "no samples"} in the run window`;
        canvas.remove();
        return;
      }
      drawLineChart(canvas, pts, 14);
      cap.textContent = `${topic} - ${pts.length} samples in the run window`;
    })
    .catch((err) => {
      cap.textContent = `${topic} - ${err.message}`;
      canvas.remove();
    });
  return fig;
}

// ---------------------------------------------------------------- report

async function openReport(runId) {
  // The report needs the Authorization header, so a plain link can't
  // fetch it: fetch here, then show it from a blob URL.
  const win = window.open("", "_blank");
  try {
    const res = await cxApi("GET", `/runs/${runId}/report?format=html`, undefined, true);
    const url = URL.createObjectURL(new Blob([await res.text()], { type: "text/html" }));
    if (win) win.location = url;
    else location.href = url;
  } catch (err) {
    if (win) win.close();
    alert(err.message);
  }
}

// ------------------------------------------------------------------ gaps

async function loadGaps() {
  flash($("gaps-status"), "checking…");
  const body = $("gaps-body");
  try {
    const got = await cxApi("GET", `/projects/${cx.project.id}/gaps`);
    body.innerHTML = "";
    const rows = got.equipment.filter((g) => g.missing.length).sort((a, b) => b.missing.length - a.missing.length);
    for (const g of rows) {
      const missing = g.missing.map((m) => `<span class="chip ${m.severity === "required" ? "chip-fail" : "chip-inconclusive"}" title="${esc(m.classes.join(", "))}${m.message ? ` - ${esc(m.message)}` : ""}">${esc(m.name)}</span>`).join(" ");
      body.append(el("tr", { innerHTML: `<td title="${esc(g.equip_uri)}">${esc(g.dis)}</td><td>${esc(g.template)}</td><td>${g.matched}/${g.expected}</td><td>${missing}</td>` }));
    }
    if (!rows.length) body.innerHTML = '<tr><td colspan="4" class="hint">Every templated equipment has all its expected points.</td></tr>';
    const complete = got.equipment.length - rows.length;
    flash($("gaps-status"), `${rows.length} with gaps, ${complete} complete, ${got.without_template.length} equipment with no template`);
  } catch (err) {
    flash($("gaps-status"), err.message, true);
  }
}

// ---------------------------------------------------------------- events

async function loadEvents() {
  const list = $("events-list");
  try {
    const events = await cxApi("GET", `/projects/${cx.project.id}/events`);
    list.innerHTML = "";
    for (const e of events.slice(0, 50)) {
      const who = e.user_email || e.user_sub || "system";
      const d = e.detail || {};
      const extra = d.to ? ` → ${d.to}` : d.run_id ? ` (${d.run_id.slice(0, 8)})` : d.title ? `: ${d.title}` : "";
      list.append(el("li", { textContent: `${when(e.ts)} · ${who} · ${e.action}${extra}${d.note ? ` - ${d.note}` : ""}` }));
    }
    if (!events.length) list.textContent = "Nothing yet.";
  } catch (err) {
    list.textContent = err.message;
  }
}

// ------------------------------------------------------------------ init

function wire() {
  $("show-archived").addEventListener("change", () => loadProjects().catch((err) => flash($("projects-status"), err.message, true)));
  $("new-project-btn").addEventListener("click", openNewProject);
  $("project-form").addEventListener("submit", createProject);
  $("project-cancel").addEventListener("click", () => $("project-dialog").close());
  $("rename-project-btn").addEventListener("click", renameProject);
  $("settings-btn").addEventListener("click", openSettings);
  $("settings-form").addEventListener("submit", saveSettings);
  $("settings-cancel").addEventListener("click", () => $("settings-dialog").close());
  $("archive-project-btn").addEventListener("click", archiveProject);
  $("edit-phases-btn").addEventListener("click", openPhaseEditor);
  $("phases-form").addEventListener("submit", savePhases);
  $("phases-cancel").addEventListener("click", () => $("phases-dialog").close());
  $("phase-add").addEventListener("click", () => $("phases-edit-body").append(phaseRow({ key: "", name: "", status: "pending", isNew: true })));
  $("run-btn").addEventListener("click", startRun);
  $("results-filter").addEventListener("change", () => { cx.resultsPage = 0; renderResults(); });
  $("results-search").addEventListener("input", () => { cx.resultsPage = 0; renderResults(); });
  $("results-prev").addEventListener("click", () => { cx.resultsPage -= 1; renderResults(); });
  $("results-next").addEventListener("click", () => { cx.resultsPage += 1; renderResults(); });
  $("report-btn").addEventListener("click", () => cx.run && openReport(cx.run.id));
  $("gaps-btn").addEventListener("click", loadGaps);
  $("drawer-close").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (ev) => ev.key === "Escape" && closeDrawer());
}

async function cxInit() {
  try {
    const res = await fetch(`${AUTH_API_URL}/me`, { headers: authHeaders() });
    if (res.status === 401) {
      handleUnauthorized();
      return;
    }
    cx.me = res.ok ? await res.json() : { role: "viewer", site_ids: [] };
    applyRoles();
    await loadSites();
    await loadProjects();
    statusEl.textContent = `${cx.projects.length} project${cx.projects.length === 1 ? "" : "s"}`;
    const want = cxParams.get("project");
    if (want) await selectProject(want, cxParams.get("run"));
    else if (cx.projects.length === 1) await selectProject(cx.projects[0].id);
  } catch (err) {
    statusEl.textContent = err.message;
  }
}

wire();
initAuth(cxInit);

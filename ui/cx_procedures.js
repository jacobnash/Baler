// Commissioning test procedures (Baler#42): power-on, start-up and
// functional procedures run passively over history. Loaded after cx.js
// and cx_issues.js; registers itself on cxProjectHooks.

const procState = { list: [], lastRuns: {}, polls: {} };
const KIND_LABEL = { power_on: "power-on", startup: "start-up", functional: "functional", seasonal: "seasonal" };

cxProjectHooks.push(() => loadProcedures());

async function loadProcedures() {
  if (!cx.project) return;
  const body = $("procedures-body");
  try {
    procState.list = await cxApi("GET", `/projects/${cx.project.id}/procedures`);
    await Promise.all(procState.list.map(async (p) => {
      const runs = await cxApi("GET", `/procedures/${p.id}/runs`).catch(() => []);
      procState.lastRuns[p.id] = runs[0] || null;
    }));
    body.innerHTML = "";
    for (const p of procState.list) body.append(procedureRow(p));
    if (!procState.list.length) body.innerHTML = '<tr><td colspan="6" class="hint">No procedures yet.</td></tr>';
    $("seed-procedures-btn").hidden = !can("operator");
    flash($("procedures-status"), "");
  } catch (err) {
    flash($("procedures-status"), err.message, true);
  }
}

function lastResultText(run) {
  if (!run) return "never run";
  if (run.status === "queued" || run.status === "running") return `${run.status}…`;
  if (run.status === "failed") return `failed: ${run.error || ""}`;
  const by = (run.summary && run.summary.by_status) || {};
  const d = (run.diff && run.diff.counts) || {};
  const changes = [d.regressed ? `${d.regressed} regressed` : "", d.resolved ? `${d.resolved} resolved` : ""].filter(Boolean).join(", ");
  return `${by.fail || 0} fail · ${by.inconclusive || 0} inconclusive · ${by.pass || 0} pass${changes ? ` (${changes})` : ""}`;
}

function procedureRow(p) {
  const run = procState.lastRuns[p.id];
  const tr = el("tr");
  tr.innerHTML = `<td title="${esc(p.description || "")}">${esc(p.name)}</td><td>${esc(KIND_LABEL[p.kind] || p.kind)}</td>
    <td>${esc(p.equip_uris && p.equip_uris.length ? `${p.equip_uris.length} equipment` : p.equip_class)}</td>
    <td>${esc(p.schedule ? `every ${p.schedule.every_hours} h` : "on demand")}</td>
    <td>${esc(lastResultText(run))}</td><td></td>`;
  const actions = tr.lastElementChild;
  if (run && run.status === "completed") {
    const view = el("button", { type: "button", textContent: "View" });
    view.addEventListener("click", () => showTestRun(run.id));
    actions.append(view);
  }
  if (can("operator")) {
    const go = el("button", { type: "button", textContent: "Run…" });
    go.addEventListener("click", () => runProcedure(p));
    const sched = el("button", { type: "button", textContent: p.schedule ? "Unschedule" : "Schedule…" });
    sched.addEventListener("click", () => scheduleProcedure(p));
    actions.append(go, sched);
  }
  if (run && (run.status === "queued" || run.status === "running")) pollTestRun(p.id, run.id);
  return tr;
}

function localIso(d) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function runProcedure(p) {
  const dflt = new Date(Date.now() - p.window_hours * 3600 * 1000);
  const when_ = prompt(`"${p.name}": when was the equipment powered on / started? (local time, YYYY-MM-DD HH:MM)\n` +
    `It is judged on the ${p.window_hours} h of history from then.`, localIso(dflt));
  if (when_ === null) return;
  const t = new Date(when_.replace(" ", "T"));
  if (isNaN(t)) {
    alert("not a date and time");
    return;
  }
  try {
    const tr = await cxApi("POST", `/procedures/${p.id}/runs`, { started_at: t.toISOString() });
    await loadEvents();
    procState.lastRuns[p.id] = tr;
    pollTestRun(p.id, tr.id);
    await loadProcedures();
  } catch (err) {
    alert(err.message);
  }
}

async function scheduleProcedure(p) {
  let schedule = null;
  if (!p.schedule) {
    const h = prompt(`Run "${p.name}" automatically every how many hours?`, "24");
    if (h === null) return;
    schedule = { every_hours: parseFloat(h) };
  }
  try {
    await cxApi("PATCH", `/procedures/${p.id}`, { schedule });
    await loadProcedures();
  } catch (err) {
    alert(err.message);
  }
}

function pollTestRun(procId, runId) {
  if (procState.polls[runId]) return;
  procState.polls[runId] = true;
  const tick = async () => {
    try {
      const tr = await cxApi("GET", `/test-runs/${runId}`);
      if (tr.status === "queued" || tr.status === "running") {
        setTimeout(tick, 1500);
        return;
      }
      delete procState.polls[runId];
      await loadProcedures();
      await loadEvents();
      if (tr.status === "completed") showTestRun(runId);
    } catch {
      delete procState.polls[runId];
    }
  };
  setTimeout(tick, 800);
}

async function showTestRun(runId) {
  const tr = await cxApi("GET", `/test-runs/${runId}`);
  const view = $("test-run-view");
  view.hidden = false;
  $("test-run-title").textContent = `${tr.procedure.name} · started ${when(tr.started_at)}`;
  const d = (tr.diff && tr.diff.counts) || {};
  $("test-run-meta").textContent = `${tr.summary.equipment} equipment, window to ${when(tr.window_end)}${tr.summary.partial ? " (still in progress)" : ""}` +
    ` · phase ${tr.phase || "-"} · ${tr.triggered_by === "schedule" ? "scheduled" : "on demand"}` +
    (tr.previous_id ? ` · against the previous run: ${Object.entries(d).map(([k, v]) => `${v} ${k.replace("_", " ")}`).join(", ")}` : "");
  const changed = {};
  for (const c of (tr.diff && tr.diff.changes) || []) changed[`${c.equip_uri}|${c.step}`] = c.state;
  const grid = $("test-run-grid");
  grid.innerHTML = "";
  const head = el("tr", {}, [el("th", { textContent: "Equipment" })]);
  for (const st of tr.procedure.steps) head.append(el("th", { textContent: st.title, title: st.instruction || "" }));
  grid.append(el("thead", {}, [head]));
  const body = el("tbody");
  for (const e of tr.equipment) {
    const row = el("tr", {}, [el("th", { className: "equip", textContent: e.dis || e.equip_uri, title: e.equip_uri })]);
    for (const st of e.steps) {
      const td = el("td", { className: "cell" });
      const state = changed[`${e.equip_uri}|${st.id}`];
      td.append(el("span", { className: `chip chip-${st.status}`, textContent: st.status.replace("_", " ") }));
      if (state === "regressed" || state === "resolved") td.append(el("span", { className: "hint", textContent: ` ${state}` }));
      td.addEventListener("click", () => openDrawer(`${e.dis || e.equip_uri} · ${st.title}`, stepResults(tr, e, st)));
      row.append(td);
    }
    body.append(row);
  }
  if (!tr.equipment.length) body.innerHTML = '<tr><td class="hint">No equipment this procedure applies to.</td></tr>';
  grid.append(body);
  view.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// A step's criteria as evidence cards, in the drawer cx.js already has.
function stepResults(tr, e, st) {
  return st.criteria.map((c, i) => {
    // the rule as this run judged it: the snapshot on the run, else the verdict's own
    const crit = (tr.procedure.steps.find((s) => s.id === st.id) || { criteria: [] }).criteria[i] || c.criterion || {};
    const on = crit.point || crit.point_class || "";
    const tag = crit.sequence ? `${crit.sequence}: ` : "";
    const what = tag + (crit.check ? `check ${crit.check}${crit.point_class ? ` on ${crit.point_class}` : ""}${crit.strict ? " (strict)" : ""}`
      : crit.range ? `range ${crit.range.min ?? "-inf"}..${crit.range.max ?? "inf"} ${crit.range.unit || ""} on ${on}${crit.range.hours ? " (occupied hours)" : ""}`
      : crit.availability ? `available at least ${crit.availability.min_pct}% (a sample every ${crit.availability.max_gap_minutes} min) on ${on}`
      : crit.response ? `when ${crit.response.when.point || crit.response.when.point_class} = ${crit.response.when.equals}, ${crit.response.then.point || crit.response.then.point_class} moves ${crit.response.then.changes_by} within ${crit.response.then.within_minutes} min`
      : "rule not recorded");
    return {
      equip_uri: e.equip_uri, check_id: `criterion ${i + 1}`, subject: what, status: c.status, severity: null,
      symptom: c.failing.map((f) => f.symptom || `${f.check_id} ${f.status} on ${f.subject}`).join("; ") || null,
      candidates: [], evidence: { counts: c.counts, failing: c.failing },
    };
  });
}

$("seed-procedures-btn").addEventListener("click", async () => {
  try {
    const made = await cxApi("POST", `/projects/${cx.project.id}/procedures`, { seed: "all" });
    flash($("procedures-status"), made.length ? `added ${made.length} procedures` : "the standard procedures are already here");
    await Promise.all([loadProcedures(), loadEvents()]);
  } catch (err) {
    flash($("procedures-status"), err.message, true);
  }
});

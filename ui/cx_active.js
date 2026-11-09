// Commissioning active tests (Baler#45): the only Cx feature that commands
// equipment. Plan = a dry run listing every write; an admin confirms that
// exact run inside its window; abort and the kill switch relinquish.
// Loaded after cx.js; registers on cxProjectHooks.

const activeState = { status: null, runs: [], fleets: [], schedules: [], poll: null };

cxProjectHooks.push(() => loadActive());

async function loadActive() {
  if (!cx.project) return;
  try {
    activeState.status = await cxApi("GET", "/active/status");
    const s = activeState.status;
    $("active-status").textContent = s.writes_allowed
      ? `Active tests are enabled on this deployment: writes go at priority ${s.level} (manual operator), admin only, inside a scheduled window, and are relinquished afterwards.`
      : "Active tests are switched off on this deployment (BALER_CX_ALLOW_WRITES). You can still plan one to see exactly what it would write.";
    activeState.runs = await cxApi("GET", `/projects/${cx.project.id}/active-runs`);
    activeState.fleets = await cxApi("GET", `/projects/${cx.project.id}/fleet-runs`);
    renderActive();
    renderFleets();
    activeState.schedules = await cxApi("GET", `/projects/${cx.project.id}/active-schedules`);
    renderSchedules();
    if (can("admin") && $("fleet-test").options.length < 2) await loadFleetTests();
    if (can("admin") && !$("active-equip").options.length) await loadActiveEquip();
    const live = activeState.runs.some((r) => r.status === "running" || r.status === "confirmed")
      || activeState.fleets.some((f) => f.status === "running" || f.status === "confirmed");
    clearTimeout(activeState.poll);
    if (live) activeState.poll = setTimeout(loadActive, 3000);
  } catch (err) {
    $("active-status").textContent = err.message;
  }
}

async function loadActiveEquip() {
  const sel = $("active-equip");
  const inv = await cxApi("GET", `/projects/${cx.project.id}/inventory`);
  sel.innerHTML = "";
  for (const e of inv.equipment) sel.append(el("option", { value: e.id, textContent: e.dis }));
}

function activeResult(r) {
  if (!r.results || !r.results.length) return r.error || "";
  return r.results.map((x) => `${x.title}: ${x.status}`).join("; ") + (r.error ? ` (${r.error})` : "");
}

function renderActive() {
  const body = $("active-body");
  body.innerHTML = "";
  for (const r of activeState.runs) {
    const writes = (r.writes || []).filter((w) => w.action === "write").length;
    const tr = el("tr", { className: "clickable" });
    tr.innerHTML = `<td>${esc(when(r.created_at))}</td><td>${esc(r.equip_uri.replace(/^urn:equip:/, ""))}</td>
      <td class="${r.status === "failed" || r.status === "aborted" ? "st-fail" : r.status === "completed" ? "st-pass" : ""}">${esc(r.status)}</td>
      <td>${writes}/${r.plan.length}</td><td>${esc(activeResult(r))}</td><td></td>`;
    const actions = tr.lastElementChild;
    if (r.status === "planned" && can("admin")) actions.append(activeButton("Confirm…", () => confirmActive(r)));
    if (["planned", "confirmed", "running"].includes(r.status) && can("operator")) actions.append(activeButton(r.status === "planned" ? "Cancel" : "Abort", () => abortActive(r)));
    tr.addEventListener("click", () => showActive(r));
    body.append(tr);
  }
  if (!activeState.runs.length) body.innerHTML = '<tr><td colspan="6" class="hint">No active tests.</td></tr>';
}

function activeButton(text, fn) {
  const b = el("button", { type: "button", textContent: text });
  b.addEventListener("click", (ev) => {
    ev.stopPropagation();
    fn();
  });
  return b;
}

function showActive(r) {
  const plan = r.plan.map((w) => `<tr><td>${esc(w.step)}</td><td>${esc(w.point)}</td><td>${esc(w.value)}</td><td>${w.level}</td><td>${esc(w.hold_minutes)} min</td><td>${esc(when(w.expires_at))}</td></tr>`).join("");
  const log = (r.writes || []).map((w) => `<li>${esc(when(w.at))} · ${esc(w.action)} ${esc(w.point)}${w.value !== undefined ? ` = ${esc(w.value)}` : ""}${w.error ? ` - ${esc(w.error)}` : ""}</li>`).join("");
  $("active-detail").innerHTML = `<h3 class="cx-sub">Run ${esc(r.id)} · window ${esc(when(r.window_start))} - ${esc(when(r.window_end))}</h3>
    <table class="plan-table"><thead><tr><th>Step</th><th>Point</th><th>Value</th><th>Priority</th><th>Hold</th><th>Expires</th></tr></thead><tbody>${plan}</tbody></table>
    ${log ? `<p>What was commanded:</p><ul>${log}</ul>` : ""}
    ${r.before ? `<details><summary>Before-state</summary><pre>${esc(JSON.stringify(r.before, null, 2))}</pre></details>` : ""}`;
}

async function planActive() {
  const hours = parseFloat($("active-hours").value) || 1;
  const start = new Date();
  const end = new Date(start.getTime() + hours * 3600 * 1000);
  try {
    const run = await cxApi("POST", `/projects/${cx.project.id}/active-runs`, {
      equip_uri: $("active-equip").value, kind: "command_response", window_start: start.toISOString(), window_end: end.toISOString(),
    });
    await Promise.all([loadActive(), loadEvents()]);
    showActive(run);
  } catch (err) {
    alert(err.message);
  }
}

async function confirmActive(r) {
  const lines = r.plan.map((w) => `  ${w.point} = ${w.value} at priority ${w.level} for up to ${w.hold_minutes} min`).join("\n");
  const typed = prompt(`This commands real equipment:\n${lines}\n\nEach write is relinquished afterwards. Type the run id to confirm:\n${r.id}`);
  if (typed === null) return;
  try {
    await cxApi("POST", `/active-runs/${r.id}/confirm`, { confirm: typed.trim() });
    await Promise.all([loadActive(), loadEvents()]);
  } catch (err) {
    alert(err.message);
  }
}

async function abortActive(r) {
  try {
    await cxApi("POST", `/active-runs/${r.id}/abort`, {});
    await Promise.all([loadActive(), loadEvents()]);
  } catch (err) {
    alert(err.message);
  }
}

// ---------------------------------------------------------- fleet runs (Baler#275 d)

async function loadFleetTests() {
  const sel = $("fleet-test");
  for (const t of await cxApi("GET", "/active/library")) sel.append(el("option", { value: t.id, textContent: `${t.name} (${t.equip_class})` }));
}

function fleetTally(f) {
  return Object.entries(f.tally || {}).map(([k, n]) => `${n} ${k}`).join(", ");
}

function renderFleets() {
  const body = $("fleet-body");
  body.innerHTML = "";
  for (const f of activeState.fleets) {
    const tr = el("tr", { className: "clickable" });
    const bad = ["failed", "aborted", "stopped"].includes(f.status);
    tr.innerHTML = `<td>${esc(when(f.created_at))}</td><td>${esc(f.kind)}${f.retest_of ? " (retest)" : ""}</td>
      <td class="${bad ? "st-fail" : f.status === "completed" ? "st-pass" : ""}">${esc(f.status)}${f.stop_reason ? ` - ${esc(f.stop_reason)}` : ""}</td>
      <td>${f.members.length}: ${esc(fleetTally(f))}</td><td>${f.failures} of ${f.params.max_failures}</td><td></td>`;
    const actions = tr.lastElementChild;
    if (f.status === "planned" && can("admin")) actions.append(activeButton("Confirm…", () => confirmFleet(f)));
    if (["planned", "confirmed", "running"].includes(f.status) && can("operator")) actions.append(activeButton(f.status === "planned" ? "Cancel" : "Stop", () => abortFleet(f)));
    if (f.failures && !["planned", "confirmed", "running"].includes(f.status) && can("admin")) actions.append(activeButton("Retest failures", () => planFleet(f.id)));
    tr.addEventListener("click", () => showFleet(f));
    body.append(tr);
  }
  if (!activeState.fleets.length) body.innerHTML = '<tr><td colspan="6" class="hint">No fleet runs.</td></tr>';
}

function showFleet(f) {
  const members = f.members.map((m) => `<tr><td>${esc(m.dis || m.equip_uri)}</td><td>${esc(m.system.replace(/^urn:equip:/, ""))}</td><td>${esc(when(m.slot_start))}</td><td>${esc(m.outcome || m.state)}${m.note ? ` - ${esc(m.note)}` : ""}</td></tr>`).join("");
  const plan = f.plan.map((w) => `<tr><td>${esc(w.equip_uri.replace(/^urn:equip:/, ""))}</td><td>${esc(w.step)}</td><td>${esc(w.point)}</td><td>${esc(w.value)}</td><td>${w.level}</td><td>${esc(w.hold_minutes)} min</td><td>${esc(when(w.not_before))}</td></tr>`).join("");
  const skipped = (f.skipped || []).map((s) => `<li>${esc(s.equip_uri)}: ${esc(s.reason)}</li>`).join("");
  $("fleet-detail").innerHTML = `<h3 class="cx-sub">Fleet ${esc(f.id)} · window ${esc(when(f.window_start))} - ${esc(when(f.window_end))}</h3>
    <p>${f.members.length} members, ${f.plan.length} writes; at most ${f.params.concurrency_per_system} per system and ${f.params.max_concurrent} at once, ${f.params.stagger_seconds} s apart; stops after ${f.params.max_failures} failures. Plan code: <code>${esc(f.plan_hash)}</code></p>
    ${skipped ? `<p>Skipped:</p><ul>${skipped}</ul>` : ""}
    <table class="plan-table"><thead><tr><th>Member</th><th>System</th><th>Not before</th><th>State</th></tr></thead><tbody>${members}</tbody></table>
    <details><summary>Every write (${f.plan.length})</summary>
    <table class="plan-table"><thead><tr><th>Equipment</th><th>Step</th><th>Point</th><th>Value</th><th>Priority</th><th>Hold</th><th>Not before</th></tr></thead><tbody>${plan}</tbody></table></details>`;
}

async function planFleet(retestOf) {
  const hours = parseFloat($("fleet-hours").value) || 8;
  const start = new Date();
  const body = {
    window_start: start.toISOString(), window_end: new Date(start.getTime() + hours * 3600 * 1000).toISOString(),
    concurrency_per_system: parseInt($("fleet-per-system").value, 10), max_concurrent: parseInt($("fleet-max").value, 10),
    stagger_seconds: parseInt($("fleet-stagger").value, 10), max_failures: parseInt($("fleet-failures").value, 10),
  };
  if (retestOf) body.retest_of = retestOf;
  else {
    body.equip_class = $("fleet-class").value.trim();
    if ($("fleet-test").value) Object.assign(body, { kind: "library", test: $("fleet-test").value });
  }
  try {
    const f = await cxApi("POST", `/projects/${cx.project.id}/fleet-runs`, body);
    await Promise.all([loadActive(), loadEvents()]);
    showFleet(f);
  } catch (err) {
    alert(err.message);
  }
}

async function confirmFleet(f) {
  const typed = prompt(`This commands real equipment: ${f.plan.length} writes on ${f.members.length} units at priority 8, `
    + `each relinquished afterwards (the full list is under the fleet's details).\n\nType the plan code to confirm exactly this plan:\n${f.plan_hash}`);
  if (typed === null) return;
  try {
    await cxApi("POST", `/fleet-runs/${f.id}/confirm`, { confirm: typed.trim() });
    await Promise.all([loadActive(), loadEvents()]);
  } catch (err) {
    alert(err.message);
  }
}

async function abortFleet(f) {
  try {
    await cxApi("POST", `/fleet-runs/${f.id}/abort`, {});
    await Promise.all([loadActive(), loadEvents()]);
  } catch (err) {
    alert(err.message);
  }
}

// ---------------------------------------------------- scheduled retests (Baler#275 e)

function renderSchedules() {
  const sel = $("sched-fleet");
  const keep = sel.value;
  sel.innerHTML = "";
  for (const f of activeState.fleets) sel.append(el("option", { value: f.id, textContent: `${when(f.created_at)} · ${f.kind} · ${f.members.length} units` }));
  if (keep) sel.value = keep;
  const body = $("sched-body");
  body.innerHTML = "";
  for (const s of activeState.schedules) {
    const t = s.timing;
    const tr = el("tr", { className: "clickable" });
    tr.innerHTML = `<td>${esc(s.name)}</td><td class="${s.status === "approved" ? "st-pass" : s.status === "revoked" ? "st-fail" : ""}">${esc(s.status)}</td>
      <td>${s.scope.equip_uris.length} (${esc(s.scope.mode === "failures" ? "failures only" : "all")})</td>
      <td>${t.window_minutes} min every ${t.every_days} days until ${esc(when(t.valid_until))}</td><td>${esc(s.next_window ? when(s.next_window) : "-")}</td><td></td>`;
    const actions = tr.lastElementChild;
    if (s.status === "proposed" && can("admin")) actions.append(activeButton("Approve…", () => approveSchedule(s)));
    if (["proposed", "approved"].includes(s.status) && can("operator")) actions.append(activeButton("Revoke", () => revokeSchedule(s)));
    tr.addEventListener("click", () => showSchedule(s));
    body.append(tr);
  }
  if (!activeState.schedules.length) body.innerHTML = '<tr><td colspan="6" class="hint">No scheduled retests.</td></tr>';
}

function showSchedule(s, preview) {
  const p = preview ? `<p>Next window's dry run: ${preview.members} units, ${preview.plan.length} writes (${esc(when(preview.window_start))} - ${esc(when(preview.window_end))}).</p>` : "";
  $("sched-detail").innerHTML = `<h3 class="cx-sub">${esc(s.name)} · ${esc(s.status)}</h3>
    <p>${esc(s.scope.kind)}${s.scope.test ? ` (${esc(s.scope.test)})` : ""} on ${s.scope.equip_uris.length} units; at most ${s.scope.concurrency_per_system} per system and ${s.scope.max_concurrent} at once, stops after ${s.scope.max_failures} failures. Approval code: <code>${esc(s.approval_hash)}</code>${s.approved_by ? ` · approved by ${esc(s.approved_by)} ${esc(when(s.approved_at))}` : ""}</p>${p}
    <details><summary>Equipment</summary><ul>${s.scope.equip_uris.map((u) => `<li>${esc(u)}</li>`).join("")}</ul></details>`;
}

async function proposeSchedule() {
  const f = activeState.fleets.find((x) => x.id === $("sched-fleet").value);
  if (!f || !$("sched-first").value || !$("sched-until").value) {
    alert("Pick a fleet run to repeat, a first window and an approval end date.");
    return;
  }
  const body = {
    name: `Retest of ${f.kind}`, equip_uris: f.members.map((m) => m.equip_uri), kind: f.params.kind, response_minutes: f.params.response_minutes,
    concurrency_per_system: f.params.concurrency_per_system, max_concurrent: f.params.max_concurrent,
    stagger_seconds: f.params.stagger_seconds, max_failures: f.params.max_failures, mode: $("sched-mode").value,
    first_window_start: new Date($("sched-first").value).toISOString(),
    window_minutes: Math.round((parseFloat($("sched-hours").value) || 8) * 60), every_days: parseInt($("sched-days").value, 10),
    valid_until: new Date(`${$("sched-until").value}T23:59:00`).toISOString(),
  };
  if (f.params.test) body.test = f.params.test;
  try {
    const s = await cxApi("POST", `/projects/${cx.project.id}/active-schedules`, body);
    await Promise.all([loadActive(), loadEvents()]);
    showSchedule(s, s.preview);
  } catch (err) {
    alert(err.message);
  }
}

async function approveSchedule(s) {
  const typed = prompt(`Approve ${s.name}: ${s.scope.equip_uris.length} units, ${s.timing.window_minutes}-minute windows every ${s.timing.every_days} days `
    + `until ${when(s.timing.valid_until)}. Each window commands real equipment, if active tests are switched on then.\n\nType the approval code:\n${s.approval_hash}`);
  if (typed === null) return;
  try {
    await cxApi("POST", `/active-schedules/${s.id}/approve`, { confirm: typed.trim() });
    await Promise.all([loadActive(), loadEvents()]);
  } catch (err) {
    alert(err.message);
  }
}

async function revokeSchedule(s) {
  if (!confirm(`Revoke ${s.name}? It stops scheduling, and a run it has going is stopped.`)) return;
  try {
    await cxApi("POST", `/active-schedules/${s.id}/revoke`, {});
    await Promise.all([loadActive(), loadEvents()]);
  } catch (err) {
    alert(err.message);
  }
}

$("sched-propose-btn").addEventListener("click", proposeSchedule);
$("fleet-plan-btn").addEventListener("click", () => planFleet(null));
$("active-plan-btn").addEventListener("click", planActive);
$("active-kill-btn").addEventListener("click", async () => {
  if (!confirm("Abort every running active test in your organization and relinquish its writes now?")) return;
  try {
    const got = await cxApi("POST", "/active/kill", {});
    alert(`${got.aborted.length} aborted (${(got.fleets || []).length} fleets), ${got.relinquished.length} relinquished`);
    await loadActive();
  } catch (err) {
    alert(err.message);
  }
});

// Commissioning active tests (Baler#45): the only Cx feature that commands
// equipment. Plan = a dry run listing every write; an admin confirms that
// exact run inside its window; abort and the kill switch relinquish.
// Loaded after cx.js; registers on cxProjectHooks.

const activeState = { status: null, runs: [], poll: null };

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
    renderActive();
    if (can("admin") && !$("active-equip").options.length) await loadActiveEquip();
    const live = activeState.runs.some((r) => r.status === "running" || r.status === "confirmed");
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

$("active-plan-btn").addEventListener("click", planActive);
$("active-kill-btn").addEventListener("click", async () => {
  if (!confirm("Abort every running active test in your organization and relinquish its writes now?")) return;
  try {
    const got = await cxApi("POST", "/active/kill", {});
    alert(`${got.aborted.length} aborted, ${got.relinquished.length} relinquished`);
    await loadActive();
  } catch (err) {
    alert(err.message);
  }
});

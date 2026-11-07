// The commissioning agent panel (Baler#46). Loaded after cx.js; registers
// on cxProjectHooks. The agent works through the same /cx/ API with the
// starter's access; every change it wants is a proposal accepted here.

const agentState = { session: null, poll: null, pending: null };
const PROPOSAL_LABEL = { issue: "Issue", procedure: "Test procedure", phase: "Phase change", spec_item: "Spec item", mapping: "Mapping", active_test: "Active test" };

cxProjectHooks.push(() => loadAgent());

async function loadAgent() {
  if (!cx.project) return;
  try {
    const sessions = await cxApi("GET", `/projects/${cx.project.id}/agent/sessions`);
    const mine = sessions.find((s) => s.created_by === (cx.me && cx.me.sub));
    agentState.session = mine ? await cxApi("GET", `/agent-sessions/${mine.id}`) : null;
    renderAgent();
    await loadProposals();
  } catch (err) {
    flash($("agent-status"), err.message, true);
  }
}

function proposalWhat(p) {
  const x = p.payload || {};
  switch (p.kind) {
    case "issue": return x.title || (x.from_result ? `${x.from_result.check_id} on ${x.from_result.subject}` : "");
    case "procedure": return x.template || x.name || "";
    case "phase": return `move to ${x.key}${x.note ? ` - ${x.note}` : ""}`;
    case "spec_item": return `${x.kind} ${x.tag}${x.brick_class ? ` (${x.brick_class})` : ""}`;
    case "mapping": return `${x.spec_item_id} → ${String(x.equip_uri || "").replace(/^urn:equip:/, "")}`;
    case "active_test": return `${String(x.equip_uri || "").replace(/^urn:equip:/, "")}, ${when(x.window_start)} - ${when(x.window_end)} (an admin still confirms it)`;
    default: return JSON.stringify(x);
  }
}

async function loadProposals() {
  const body = $("agent-proposals");
  const props = await cxApi("GET", `/projects/${cx.project.id}/agent/proposals`);
  body.innerHTML = "";
  for (const p of props.slice(0, 100)) {
    const tr = el("tr");
    const result = p.result ? (p.result.error || p.result.note || Object.entries(p.result).map(([k, v]) => `${k}: ${v}`).join(", ")) : "";
    tr.innerHTML = `<td>${esc(PROPOSAL_LABEL[p.kind] || p.kind)}</td><td>${esc(proposalWhat(p))}</td><td class="hint">${esc(p.reason)}</td>
      <td title="${esc(result)}">${esc(p.status)}</td><td></td>`;
    if (p.status === "proposed" && can("operator")) {
      const actions = tr.lastElementChild;
      for (const [text, verb] of [["Accept", "accept"], ["Reject", "reject"]]) {
        const b = el("button", { type: "button", textContent: text });
        b.addEventListener("click", async () => {
          try {
            await cxApi("POST", `/agent-proposals/${p.id}/${verb}`, {});
            await selectProject(cx.project.id); // what was accepted shows up everywhere
          } catch (err) {
            // a phase gate in "block" mode refuses it and lists what's unmet;
            // the proposal stays open to accept once the gate is met (#86)
            const unmet = err.data && err.data.gate ? err.data.gate.unmet.map((u) => `- ${u.message}`).join("\n") : "";
            alert(unmet ? `${err.message}\n\n${unmet}` : err.message);
          }
        });
        actions.append(b);
      }
    }
    body.append(tr);
  }
  if (!props.length) body.innerHTML = '<tr><td colspan="5" class="hint">No proposals yet.</td></tr>';
}

function renderAgent() {
  const box = $("agent-transcript");
  box.innerHTML = "";
  const s = agentState.session;
  if (!s) return;
  for (const t of s.transcript) {
    if (t.role === "tool") {
      box.append(el("div", { className: "msg tool", textContent: t.error ? `tool error: ${t.error}` : `→ ${t.tool}${t.input && Object.keys(t.input).length ? ` ${JSON.stringify(t.input)}` : ""}` }));
    } else {
      box.append(el("div", { className: `msg ${t.role}`, textContent: t.text }));
    }
  }
  box.scrollTop = box.scrollHeight;
  const working = s.status === "working";
  if (working && agentState.pending) box.append(el("div", { className: "msg user", textContent: agentState.pending }));
  if (!working) agentState.pending = null;
  $("agent-send").disabled = working;
  flash($("agent-status"), working ? "the agent is working…" : s.status === "failed" ? `failed: ${s.error}` : "", s.status === "failed");
  clearTimeout(agentState.poll);
  if (working) agentState.poll = setTimeout(pollAgent, 1500);
}

async function pollAgent() {
  try {
    agentState.session = await cxApi("GET", `/agent-sessions/${agentState.session.id}`);
    renderAgent();
    if (agentState.session.status !== "working") {
      await Promise.all([loadProposals(), loadEvents(), loadRuns()]);
    }
  } catch (err) {
    flash($("agent-status"), err.message, true);
  }
}

async function sendAgent() {
  const text = $("agent-message").value.trim();
  if (!text) return;
  try {
    agentState.session = agentState.session
      ? await cxApi("POST", `/agent-sessions/${agentState.session.id}/messages`, { message: text })
      : await cxApi("POST", `/projects/${cx.project.id}/agent/sessions`, { message: text });
    agentState.pending = text;
    $("agent-message").value = "";
    renderAgent();
  } catch (err) {
    flash($("agent-status"), err.message, true);
  }
}

$("agent-send").addEventListener("click", sendAgent);
$("agent-message").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) sendAgent();
});
$("agent-new").addEventListener("click", () => {
  agentState.session = null;
  renderAgent();
});

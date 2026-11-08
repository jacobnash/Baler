// Mapping review queue (Baler#184): the human end of the tagging ladder.
// Reads GET /ops/mapping/proposals and /ops/mapping/summary, answers with
// POST /ops/mapping/decide. ops_api scopes all three to the caller's sites.
const OPS_API_URL = new URLSearchParams(location.search).get("ops") || "/ops";
const INGEST_URL = new URLSearchParams(location.search).get("ingest") || "/ingest";

const statusEl = document.getElementById("status");
const summaryEl = document.getElementById("mapping-summary");
const groupsEl = document.getElementById("mapping-groups");
const mappingStatusEl = document.getElementById("mapping-status");

const STATE_LABELS = {
  convention: "By a naming convention",
  rule: "By a rule",
  site_rule: "By a site rule",
  memory: "By memory",
  model: "By the model step",
  person: "By a person",
  proposed: "Proposed",
  escalated: "Escalated",
  unmapped: "Unmapped",
};

let groups = [];

function escapeHtml(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function api(path, init = {}) {
  const res = await fetch(`${OPS_API_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...authHeaders(), ...(init.headers || {}) },
  });
  if (res.status === 401) {
    handleUnauthorized();
    throw new Error("signed out");
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `${path}: ${res.status}`);
  return body;
}

function renderSummary(s) {
  const cards = Object.entries(s.states).map(([state, n]) => {
    const kind = state === "unmapped" ? "warn" : state === "proposed" || state === "escalated" ? "attention" : "ok";
    return `<div class="ops-card state-${kind}"><div class="label">${escapeHtml(STATE_LABELS[state] || state)}</div>
      <div class="detail">${n}</div></div>`;
  });
  cards.unshift(`<div class="ops-card state-ok"><div class="label">Understood</div>
    <div class="detail">${s.understood_pct}% (${s.understood} of ${s.total} points)</div></div>`);
  summaryEl.innerHTML = cards.join("");
}

function evidenceHtml(g) {
  const ev = g.evidence || {};
  const values = Object.entries(ev.values || {}).map(([k, v]) => `${escapeHtml(k)}=${escapeHtml(v)}`).join(", ");
  const stats = ev.value_stats
    ? Object.entries(ev.value_stats).map(([k, v]) => `${escapeHtml(k)}=${escapeHtml(v)}`).join(", ")
    : "no history yet";
  const sibs = (ev.siblings || []).map((s) => escapeHtml(s.label) + (s.class ? ` <span class="hint">(${escapeHtml(s.class)})</span>` : "")).join("; ");
  const memoryAsk = (g.asks || []).find((a) => a.model === "memory");
  const nearest = memoryAsk && (memoryAsk.neighbours || []).length
    ? memoryAsk.neighbours.map((n) => `<li><code>${escapeHtml(n.label)}</code> → ${escapeHtml(n.brick_class)} <span class="hint">(${escapeHtml(n.scope)}, ${n.similarity})</span></li>`).join("")
    : "";
  const asks = (g.asks || []).filter((a) => a.model !== "memory").map((a) => {
    const said = (a.candidates || []).map((c) => escapeHtml(c.brick_class)).join(", ") || "nothing usable";
    const rej = (a.rejected || []).length ? ` - not Brick: ${a.rejected.map(escapeHtml).join(", ")}` : "";
    return `<li>${escapeHtml(a.model)}${a.variant !== undefined ? ` (ask ${a.variant + 1})` : ""}: ${said}${rej}</li>`;
  }).join("");
  return `<dl class="evidence">
      <dt>Label</dt><dd><code>${escapeHtml(ev.label || g.label)}</code></dd>
      <dt>Markers</dt><dd>${escapeHtml((ev.markers || []).join(", ") || "(none)")}</dd>
      <dt>Other tags</dt><dd>${values || "(none)"}</dd>
      <dt>Values</dt><dd>${stats}</dd>
      <dt>Same equipment</dt><dd>${sibs || "(none)"}</dd>
      <dt>Nearest confirmed</dt><dd>${nearest ? `<ul>${nearest}</ul>` : "none yet (memory learns from your answers)"}</dd>
      <dt>Ladder</dt><dd>${asks ? `<ul>${asks}</ul>` : "no model asked"}<div>Not auto-accepted: ${escapeHtml(g.refused_because)}</div></dd>
    </dl>`;
}

function groupHtml(g, i) {
  const cands = g.candidates.map((c) => `<li>
      <button type="button" class="accept" data-group="${i}" data-class="${escapeHtml(c.brick_class)}">Accept</button>
      <strong>${escapeHtml(c.brick_class)}</strong> <span class="hint">${escapeHtml(c.source || "")}: ${escapeHtml(c.reason)}</span></li>`).join("");
  const members = g.members.map((m) => `<li><code>${escapeHtml(m.label)}</code>${m.status === "escalated" ? ` <span class="badge">escalated to ${escapeHtml(m.assignee)}</span>` : ""}</li>`).join("");
  const tier = g.why_first ? `<span class="badge tier-${g.tier}">${escapeHtml(g.why_first)}</span>` : "";
  return `<article class="mapping-group" id="g-${i}">
    <header><h3><code>${escapeHtml(g.label)}</code> <span class="hint">${g.count} ${escapeHtml(g.kind)}${g.count === 1 ? "" : "s"}</span></h3>${tier}</header>
    <div class="mapping-cols">
      <div>
        <h4>Candidates</h4><ul class="cands">${cands || "<li class=hint>none</li>"}</ul>
        <div class="toolbar">
          <button type="button" class="other" data-group="${i}">Another class…</button>
          <button type="button" class="escalate" data-group="${i}">Escalate…</button>
          <button type="button" class="visit" data-group="${i}">Needs a site visit…</button>
        </div>
        <details><summary>${g.count} member${g.count === 1 ? "" : "s"}</summary><ul class="members">${members}</ul></details>
      </div>
      <div>
        <h4>Evidence</h4>
        <canvas class="spark" id="spark-${i}" width="360" height="90"></canvas>
        <p class="hint" id="spark-note-${i}"></p>
        ${evidenceHtml(g)}
      </div>
    </div>
  </article>`;
}

async function drawSpark(g, i) {
  const canvas = document.getElementById(`spark-${i}`);
  const note = document.getElementById(`spark-note-${i}`);
  if (g.kind !== "point") {
    canvas.hidden = true;
    return;
  }
  const point = g.members[0].uri.replace(/^urn:point:/, "");
  const end = Date.now() / 1000;
  const start = end - 86400;
  try {
    const res = await fetch(`${INGEST_URL}/history?point=${encodeURIComponent(point)}&start=${start}&end=${end}`, { headers: { ...authHeaders() } });
    const rows = res.ok ? await res.json() : [];
    const numeric = rows.filter((r) => typeof r.value === "number" || typeof r.value === "boolean")
      .map((r) => ({ ts: r.ts, value: typeof r.value === "boolean" ? Number(r.value) : r.value }));
    if (!numeric.length) {
      canvas.hidden = true;
      note.textContent = "No history in the last 24 h.";
      return;
    }
    drawLineChart(canvas, numeric, 14, g.members[0].unit || "", { start, end });
    note.textContent = `Last 24 h of ${g.members[0].label}.`;
  } catch {
    canvas.hidden = true;
  }
}

function renderGroups() {
  if (!groups.length) {
    groupsEl.innerHTML = `<p class="hint">Nothing waiting. Everything the ladder couldn't settle has been answered.</p>`;
    return;
  }
  groupsEl.innerHTML = groups.map(groupHtml).join("");
  groups.forEach((g, i) => drawSpark(g, i));
}

async function refresh() {
  statusEl.textContent = "refreshing…";
  const [summary, list] = await Promise.all([api("/mapping/summary"), api("/mapping/proposals")]);
  renderSummary(summary);
  groups = list;
  renderGroups();
  mappingStatusEl.textContent = `${groups.length} pattern${groups.length === 1 ? "" : "s"} waiting`;
  statusEl.textContent = `updated ${new Date().toLocaleTimeString()}`;
}

// --- answering ---------------------------------------------------------

const dialog = document.getElementById("decide-dialog");
const fields = {
  title: document.getElementById("decide-title"),
  text: document.getElementById("decide-text"),
  cls: document.getElementById("decide-class"),
  clsLabel: document.getElementById("decide-class-label"),
  classes: document.getElementById("decide-classes"),
  rule: document.getElementById("decide-rule"),
  ruleLabel: document.getElementById("decide-rule-label"),
  assignee: document.getElementById("decide-assignee"),
  assigneeLabel: document.getElementById("decide-assignee-label"),
  note: document.getElementById("decide-note"),
  error: document.getElementById("decide-error"),
};
let pending = null;

function openDialog(action, g, cls) {
  pending = { action, group: g };
  fields.error.hidden = true;
  fields.note.value = "";
  fields.cls.value = cls || "";
  fields.assignee.value = g.assignee || "";
  fields.classes.innerHTML = g.candidate_classes.map((c) => `<option value="${escapeHtml(c)}">`).join("");
  fields.clsLabel.hidden = action !== "accept";
  fields.ruleLabel.hidden = action !== "accept" || g.count < 2;
  fields.assigneeLabel.hidden = action !== "escalate";
  const n = `${g.count} ${g.kind}${g.count === 1 ? "" : "s"} like ${g.label}`;
  if (action === "accept") {
    fields.title.textContent = "Classify";
    fields.text.textContent = `Writes the class to ${n}. Checked against Brick; logged as an edit.`;
  } else if (action === "escalate") {
    fields.title.textContent = "Escalate to a specialist";
    fields.text.textContent = `Assigns ${n} to someone who can tell (the site's controls contractor, the MSI). They're notified through the webhooks and answer here.`;
  } else {
    fields.title.textContent = "Needs a site visit";
    fields.text.textContent = `Records that ${n} can't be identified without going there, and opens a Cx issue on the active project covering them.`;
  }
  dialog.showModal();
}

document.getElementById("decide-cancel").addEventListener("click", () => dialog.close());

document.getElementById("decide-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const { action, group } = pending;
  const body = { ids: group.members.map((m) => m.id), action, note: fields.note.value || undefined };
  if (action === "accept") {
    body.brick_class = fields.cls.value.trim();
    body.save_rule = group.count > 1 && fields.rule.checked;
  }
  if (action === "escalate") body.assignee = fields.assignee.value.trim();
  try {
    const out = await api("/mapping/decide", { method: "POST", body: JSON.stringify(body) });
    dialog.close();
    mappingStatusEl.textContent = action === "site_visit" && out.hint ? out.hint
      : action === "site_visit" ? `Cx issue ${out.cx_issue} opened.`
      : action === "escalate" ? `Escalated to ${out.assignee}; ${out.notified} webhook(s) notified.`
      : `${out.decided} classified as ${out.brick_class}${out.site_rules.length ? " and saved as a site rule" : ""}.`;
    await refresh();
  } catch (err) {
    fields.error.textContent = err.message;
    fields.error.hidden = false;
  }
});

groupsEl.addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-group]");
  if (!btn) return;
  const g = groups[Number(btn.dataset.group)];
  if (btn.classList.contains("accept")) openDialog("accept", g, btn.dataset.class);
  else if (btn.classList.contains("other")) openDialog("accept", g, "");
  else if (btn.classList.contains("escalate")) openDialog("escalate", g);
  else if (btn.classList.contains("visit")) openDialog("site_visit", g);
});

document.getElementById("mapping-refresh").addEventListener("click", () => refresh().catch((err) => { statusEl.textContent = err.message; }));

initAuth(async () => {
  try {
    await refresh();
  } catch (err) {
    statusEl.textContent = err.message;
  } finally {
    if (window.conventionsReady) window.conventionsReady(); // conventions.js
  }
});

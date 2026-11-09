const OPS_API_URL = new URLSearchParams(location.search).get("ops") || "/ops";

const statusEl = document.getElementById("status");
const chipEl = document.getElementById("system-chip");
const buildingEl = document.getElementById("building-summary");
const attentionEl = document.getElementById("attention-list");
const platformEl = document.getElementById("platform-chips");
const jobsEl = document.getElementById("jobs-chips");
const toolsEl = document.getElementById("tools-chips");
const emptyCtaEl = document.getElementById("empty-cta");
const alertsSectionEl = document.getElementById("alerts-section");
const alertsEl = document.getElementById("alerts-list");
const processesSectionEl = document.getElementById("processes-section");
const processesEl = document.getElementById("processes-list");

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function cardHtml(c) {
  const detail = c.detail ? `<div class="detail">${escapeHtml(c.detail)}</div>` : "";
  const label = c.href ? `<a href="${escapeHtml(c.href)}">${escapeHtml(c.label)}</a>` : escapeHtml(c.label);
  return `<div class="ops-card state-${c.state}">
    <div class="label">${label}</div>
    ${detail}
    <div class="state">${escapeHtml(c.state)}</div>
  </div>`;
}

function summaryLabel(summary, attentionCount) {
  if (summary === "ok") return "System: All green";
  if (summary === "down") return "System: Down";
  return `System: Needs attention (${attentionCount})`;
}

// The Building cards' states come from ops_status.building_cards - the
// same judgement that feeds Needs attention and the chip (Baler#64).
function renderBuilding(building) {
  buildingEl.innerHTML = (building.cards || []).map(cardHtml).join("");
}

// What baler.alerts has open, and per-process exception/restart counts
// (#277). Both lists are only sent to callers who see the whole install.
function renderAlerts(body) {
  const open = body.alerts || [];
  alertsSectionEl.hidden = open.length === 0;
  alertsEl.innerHTML = open.map((a) => cardHtml({
    label: a.title,
    detail: `${a.detail ? a.detail + " - " : ""}since ${a.opened_at}`,
    state: a.severity === "critical" ? "down" : "degraded",
  })).join("");
  const procs = (body.processes || []).filter((p) => p.errors > 0);
  processesSectionEl.hidden = procs.length === 0;
  processesEl.innerHTML = procs.map((p) => cardHtml({
    label: p.service,
    detail: `${p.errors} unhandled exception(s), last ${p.last_error_at}: ${p.last_error || ""}; ${p.starts} start(s)`,
    state: "degraded",
  })).join("");
}

async function refresh() {
  statusEl.textContent = "refreshing…";
  const res = await fetch(`${OPS_API_URL}/status`, { headers: { ...authHeaders() } });
  if (res.status === 401) {
    handleUnauthorized();
    return;
  }
  if (!res.ok) throw new Error(`ops status failed: ${res.status}`);
  const body = await res.json();

  chipEl.hidden = false;
  chipEl.className = `system-chip state-${body.summary}`;
  chipEl.textContent = summaryLabel(body.summary, body.attention.length);

  renderBuilding(body.building);
  renderAlerts(body);
  platformEl.innerHTML = body.platform.map(cardHtml).join("");
  jobsEl.innerHTML = body.jobs.map(cardHtml).join("");
  toolsEl.innerHTML = body.tools.map(cardHtml).join("");

  if (body.attention.length === 0) {
    attentionEl.className = "ops-list hint";
    attentionEl.textContent = "Nothing right now.";
  } else {
    attentionEl.className = "ops-list";
    attentionEl.innerHTML = body.attention.map(cardHtml).join("");
  }

  const noData = !body.building.last_ingest_at && body.building.points_seen_last_hour === 0;
  emptyCtaEl.hidden = !noData;
  statusEl.textContent = `updated ${new Date().toLocaleTimeString()}`;
}

function wireCreateOrg() {
  const loginForm = document.getElementById("login-form");
  const orgForm = document.getElementById("create-org-form");
  const showCreate = document.getElementById("show-create-org");
  const showLogin = document.getElementById("show-login");
  const orgError = document.getElementById("org-error");

  showCreate.addEventListener("click", () => {
    loginForm.hidden = true;
    orgForm.hidden = false;
  });
  showLogin.addEventListener("click", () => {
    orgForm.hidden = true;
    loginForm.hidden = false;
  });

  orgForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    orgError.hidden = true;
    const name = document.getElementById("org-name").value.trim();
    const email = document.getElementById("org-email").value.trim();
    const password = document.getElementById("org-password").value;
    try {
      const createRes = await fetch(`${AUTH_API_URL}/orgs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, admin_email: email, admin_password: password }),
      });
      if (!createRes.ok) {
        const errBody = await createRes.json().catch(() => ({}));
        throw new Error(errBody.error || `create org failed: ${createRes.status}`);
      }
      const loginRes = await fetch(`${AUTH_API_URL}/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      if (!loginRes.ok) throw new Error("org created but login failed");
      const loginBody = await loginRes.json();
      setToken(loginBody.token);
      hideLoginGate();
      await refresh();
      setInterval(() => refresh().catch(() => {}), 15000);
    } catch (err) {
      orgError.textContent = err.message;
      orgError.hidden = false;
    }
  });
}

wireCreateOrg();

initAuth(async () => {
  initUsers(); // users.js: your account, and the Users section for admins
  try {
    await refresh();
    setInterval(() => refresh().catch(() => {}), 15000);
  } catch (err) {
    statusEl.textContent = err.message;
  }
});

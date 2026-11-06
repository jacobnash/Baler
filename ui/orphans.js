// Ops -> Orphaned points: points nothing maintains any more, for an admin
// to review and remove (ops_api /orphans, src/baler/orphans.py). Loaded
// after sources.js (reuses ago()) and users.js (openConfirm, showError).

const ORPHANS_URL = (new URLSearchParams(location.search).get("ops") || "/ops") + "/orphans";
const orphanState = { list: [] };

const ORPHAN_KIND = {
  derivation_gone: "Derivation deleted",
  no_equip: "No equipment",
};

async function orphansApi(method, path, body) {
  const headers = { ...authHeaders() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`${ORPHANS_URL}${path}`, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
  if (res.status === 401) {
    handleUnauthorized();
    throw new Error("session ended - sign in again");
  }
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (res.status === 403) throw new Error("only an admin can do that");
  if (!res.ok) throw new Error((data && data.error) || text.trim() || `${method} failed (${res.status})`);
  return data;
}

function selectedOrphans() {
  return [...document.querySelectorAll("#orphans-body input[type=checkbox]:checked")].map((b) => b.value);
}

function updateOrphanButtons() {
  const n = selectedOrphans().length;
  const btn = document.getElementById("orphans-remove-btn");
  btn.disabled = n === 0;
  btn.textContent = n ? `Remove ${n} selected` : "Remove selected";
}

function renderOrphans() {
  const body = document.getElementById("orphans-body");
  body.innerHTML = "";
  for (const o of orphanState.list) {
    const tr = document.createElement("tr");
    const pick = document.createElement("td");
    const box = Object.assign(document.createElement("input"), { type: "checkbox", value: o.id });
    box.setAttribute("aria-label", `select ${o.dis || o.id}`);
    box.addEventListener("change", updateOrphanButtons);
    pick.appendChild(box);
    const name = document.createElement("td");
    name.textContent = o.dis || o.id;
    name.title = o.id;
    const kind = document.createElement("td");
    kind.textContent = ORPHAN_KIND[o.kind] || o.kind;
    kind.title = o.reason;
    const last = document.createElement("td");
    last.textContent = o.last_ts ? `${o.last_value} (${ago(o.last_ts)})` : "no data";
    tr.append(pick, name, kind, last);
    body.appendChild(tr);
  }
  document.getElementById("orphans-status").textContent = orphanState.list.length
    ? `${orphanState.list.length} orphaned point${orphanState.list.length === 1 ? "" : "s"}`
    : "None - every point is attached and maintained.";
  document.getElementById("orphans-all").checked = false;
  updateOrphanButtons();
}

async function loadOrphans() {
  orphanState.list = await orphansApi("GET", "");
  renderOrphans();
}

let orphansWired = false;

async function initOrphans() {
  if (!orphansWired) {
    orphansWired = true;
    document.getElementById("orphans-all").addEventListener("change", (ev) => {
      for (const b of document.querySelectorAll("#orphans-body input[type=checkbox]")) b.checked = ev.target.checked;
      updateOrphanButtons();
    });
    document.getElementById("orphans-refresh-btn").addEventListener("click", () => loadOrphans().catch((err) => {
      document.getElementById("orphans-status").textContent = err.message;
    }));
    document.getElementById("orphans-remove-btn").addEventListener("click", () => {
      const ids = selectedOrphans();
      const purge = document.getElementById("orphans-purge").checked;
      openConfirm(`Remove ${ids.length} orphaned point${ids.length === 1 ? "" : "s"}?`,
        purge ? "Their records and all their history are deleted. This can't be undone."
          : "Their records are deleted; their history rows are kept. Logged in the edit history.",
        purge ? "delete history" : null, async () => {
          const res = await orphansApi("POST", "/remove", { ids, purge_history: purge });
          await loadOrphans();
          if (res.skipped.length) {
            document.getElementById("orphans-status").textContent += ` (${res.skipped.length} skipped: no longer orphaned)`;
          }
        });
    });
  }
  document.getElementById("orphans-section").hidden = false;
  try {
    await loadOrphans();
  } catch (err) {
    document.getElementById("orphans-status").textContent = err.message;
  }
}

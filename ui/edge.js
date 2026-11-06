// Ops -> Edge nodes (Baler#36): MQTT broker accounts for Windrows that
// publish from outside this host (a Raspberry Pi at a site). Admin-only,
// like Users. Loaded after users.js and reuses its authApi, showSecret and
// openConfirm. Each account belongs to one site and the broker lets it
// publish only under that site's topic prefixes, plus its own command
// channel cmd/<username>/#.

const edgeState = { accounts: [], sites: [], connect: {} };

function edgeSiteName(id) {
  const site = edgeState.sites.find((s) => s.id === id);
  return site ? site.name : id;
}

function edgeConnectText(username, password) {
  const c = edgeState.connect || {};
  const host = c.host || "<this server's address>";
  const lines = [];
  if (c.tls_port) lines.push(`MQTT over TLS: ${host}:${c.tls_port}`);
  lines.push(`MQTT: ${host}:${c.port || 1883}${c.tls_port ? " (unencrypted - prefer TLS)" : ""}`);
  lines.push(`Username: ${username}`);
  if (password) lines.push(`Password: ${password}`);
  return lines.join("\n");
}

function renderEdge() {
  const body = $("edge-body");
  body.innerHTML = "";
  for (const acct of edgeState.accounts) {
    const tr = document.createElement("tr");
    const name = document.createElement("td");
    name.textContent = acct.username;
    if (acct.description) name.title = acct.description;
    const site = document.createElement("td");
    site.textContent = edgeSiteName(acct.site_id);
    const prefixes = document.createElement("td");
    const s = edgeState.sites.find((x) => x.id === acct.site_id);
    prefixes.textContent = s && s.topic_prefixes.length ? s.topic_prefixes.join(", ") : "-";
    const when = document.createElement("td");
    when.textContent = new Date(acct.rotated_at || acct.created_at).toLocaleDateString() + (acct.rotated_at ? " (rotated)" : "");
    const actions = document.createElement("td");
    actions.className = "actions";
    const rotate = Object.assign(document.createElement("button"), { type: "button", textContent: "New password" });
    rotate.addEventListener("click", () => openConfirm(`New password for ${acct.username}?`,
      "The old password stops working now: the node disconnects until it's given the new one.", null, async () => {
        const res = await authApi("POST", `/broker-accounts/${encodeURIComponent(acct.id)}/rotate`);
        edgeState.connect = res.connect || edgeState.connect;
        showSecret("New broker password", edgeConnectText(acct.username, null), res.password);
        await loadEdge();
      }));
    const revoke = Object.assign(document.createElement("button"), { type: "button", textContent: "Revoke" });
    revoke.addEventListener("click", () => openConfirm(`Revoke ${acct.username}?`,
      "The node is disconnected and can't connect again. This can't be undone (create a new account instead).",
      acct.username, async () => {
        await authApi("DELETE", `/broker-accounts/${encodeURIComponent(acct.id)}`);
        await loadEdge();
      }));
    actions.append(rotate, revoke);
    tr.append(name, site, prefixes, when, actions);
    body.appendChild(tr);
  }
  $("edge-status").textContent = edgeState.accounts.length
    ? `${edgeState.accounts.length} account${edgeState.accounts.length === 1 ? "" : "s"}`
    : "No edge nodes yet.";
}

async function loadEdge() {
  const [res, sites] = await Promise.all([authApi("GET", "/broker-accounts"), authApi("GET", "/sites")]);
  edgeState.accounts = res.accounts;
  edgeState.connect = res.connect || {};
  edgeState.sites = sites;
  renderEdge();
}

function openEdgeDialog() {
  $("edge-form").reset();
  showError($("edge-error"), "");
  const select = $("edge-site");
  select.innerHTML = "";
  for (const site of edgeState.sites) {
    const opt = Object.assign(document.createElement("option"), { value: site.id, textContent: site.name });
    if (!site.topic_prefixes.length) {
      opt.disabled = true;
      opt.textContent += " (no topic prefix yet)";
    }
    select.appendChild(opt);
  }
  const usable = edgeState.sites.filter((s) => s.topic_prefixes.length);
  if (!usable.length) showError($("edge-error"), "Give a site a topic prefix first: it's what the node may publish under.");
  else select.value = usable[0].id;
  $("edge-dialog").showModal();
}

let edgeWired = false;

async function initEdge() {
  if (!edgeWired) {
    edgeWired = true;
    $("new-edge-btn").addEventListener("click", openEdgeDialog);
    $("edge-cancel").addEventListener("click", () => $("edge-dialog").close());
    $("edge-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const username = $("edge-username").value.trim().toLowerCase();
      try {
        const res = await authApi("POST", "/broker-accounts", {
          site_id: $("edge-site").value, username, description: $("edge-description").value.trim() || null,
        });
        $("edge-dialog").close();
        edgeState.connect = res.connect || edgeState.connect;
        showSecret("Edge node created", edgeConnectText(username, null), res.password);
        await loadEdge();
      } catch (err) {
        showError($("edge-error"), err.message);
      }
    });
  }
  $("edge-section").hidden = false;
  try {
    await loadEdge();
  } catch (err) {
    $("edge-status").textContent = err.message;
  }
}

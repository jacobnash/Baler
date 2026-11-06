// Ops -> Your account and Users (Baler#15). No build step, no framework -
// same as every other ui/ file. Loaded before ops.js, whose initAuth
// callback calls initUsers() once a token exists.
//
// The gateway decides who may do what (gateway/njs/policy.js): every
// /auth/users and /auth/sites route is admin-only, so this page only shows
// the Users section when /auth/me says role == admin, and treats a 403 as
// "not for you" rather than an error. Destructive actions are disabled on
// your own row and on the org's last admin; the API refuses them too.

const ROLE_HELP = {
  viewer: "Viewer: sees equipment, points, history, alarms and devices. Changes nothing.",
  operator: "Operator: a viewer who can also write points (pointWrite/hisWrite), ack and snooze alarms, and edit rules and derivations.",
  admin: "Admin: everything, plus users, sites, API keys and devices (BACnet/Modbus connections and credentials).",
};

const usersState = { me: null, users: [], sites: [], editing: null, confirmAction: null };
const $ = (id) => document.getElementById(id);

async function authApi(method, path, body) {
  const headers = { ...authHeaders() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`${AUTH_API_URL}${path}`, {
    method, headers, body: body !== undefined ? JSON.stringify(body) : undefined,
  });
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
  if (!res.ok) throw new Error((data && data.error) || text.trim() || `${method} ${path} failed (${res.status})`);
  return data;
}

// 20 characters from an unambiguous alphabet, from the browser's CSPRNG.
function generatePassword() {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789";
  const bytes = crypto.getRandomValues(new Uint32Array(20));
  return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join("");
}

function showError(el, message) {
  el.textContent = message;
  el.hidden = !message;
}

function badge(text, cls) {
  return Object.assign(document.createElement("span"), { className: `badge ${cls}`, textContent: text });
}

function showSecret(title, intro, value) {
  $("secret-title").textContent = title;
  $("secret-intro").textContent = intro;
  $("secret-value").textContent = value;
  $("secret-copy").textContent = "Copy";
  $("secret-dialog").showModal();
}

// --- your account ----------------------------------------------------------

function wireAccount() {
  $("change-password-btn").addEventListener("click", () => {
    $("password-form").reset();
    showError($("pw-error"), "");
    $("password-dialog").showModal();
  });
  $("pw-cancel").addEventListener("click", () => $("password-dialog").close());
  $("password-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const current = $("pw-current").value;
    const next = $("pw-new").value;
    if (next !== $("pw-again").value) return showError($("pw-error"), "the new passwords don't match");
    try {
      const { token } = await authApi("POST", "/me/password", { current_password: current, new_password: next });
      setToken(token); // every session ended, this one too: carry on with the fresh token
      $("password-dialog").close();
      $("account-who").textContent += " - password changed.";
    } catch (err) {
      showError($("pw-error"), err.message);
    }
  });
}

// --- users (admins) --------------------------------------------------------

function siteName(id) {
  const site = usersState.sites.find((s) => s.id === id);
  return site ? site.name : id;
}

function adminCount() {
  return usersState.users.filter((u) => u.role === "admin" && !u.disabled).length;
}

function renderUsers() {
  const body = $("users-body");
  body.innerHTML = "";
  for (const user of usersState.users) {
    const tr = document.createElement("tr");
    const isMe = user.id === usersState.me.sub;
    const lastAdmin = user.role === "admin" && !user.disabled && adminCount() <= 1;

    const email = document.createElement("td");
    email.textContent = user.email;
    if (isMe) email.append(" ", badge("you", ""));
    if (user.disabled) {
      email.append(" ", badge("disabled", "user-disabled"));
      tr.className = "user-disabled-row";
    }
    const role = document.createElement("td");
    role.appendChild(badge(user.role, `role-${user.role}`));
    const sites = document.createElement("td");
    sites.textContent = user.site_ids.length ? user.site_ids.map(siteName).join(", ") : "Whole organization";
    const added = document.createElement("td");
    added.textContent = new Date(user.created_at).toLocaleDateString();

    const actions = document.createElement("td");
    actions.className = "actions";
    const edit = Object.assign(document.createElement("button"), { type: "button", textContent: "Edit" });
    edit.addEventListener("click", () => openUserDialog(user));
    const reset = Object.assign(document.createElement("button"), { type: "button", textContent: "Reset password" });
    reset.disabled = isMe;
    reset.title = isMe ? "Use Change my password above" : "";
    reset.addEventListener("click", () => confirmResetPassword(user));
    const del = Object.assign(document.createElement("button"), { type: "button", textContent: "Delete" });
    del.disabled = isMe || lastAdmin;
    del.title = isMe ? "You can't delete your own account" : lastAdmin ? "The organization's last admin" : "";
    del.addEventListener("click", () => confirmDelete(user));
    const toggle = Object.assign(document.createElement("button"), {
      type: "button", textContent: user.disabled ? "Enable" : "Disable",
    });
    toggle.disabled = !user.disabled && (isMe || lastAdmin);
    toggle.title = isMe ? "You can't disable your own account" : lastAdmin && !user.disabled ? "The organization's last admin" : "";
    toggle.addEventListener("click", () => (user.disabled ? setDisabled(user, false) : confirmDisable(user)));
    actions.append(edit, reset, toggle, del);

    tr.append(email, role, sites, added, actions);
    body.appendChild(tr);
  }
  const off = usersState.users.filter((u) => u.disabled).length;
  $("users-status").textContent = `${usersState.users.length} users, ${adminCount()} active admin${adminCount() === 1 ? "" : "s"}` +
    (off ? `, ${off} disabled` : "");
}

async function loadUsers() {
  const [users, sites] = await Promise.all([authApi("GET", "/users"), authApi("GET", "/sites")]);
  usersState.users = users;
  usersState.sites = sites;
  renderUsers();
}

function renderSiteChoices(selected) {
  const list = $("user-sites-list");
  list.innerHTML = "";
  if (!usersState.sites.length) {
    list.appendChild(Object.assign(document.createElement("p"), { className: "hint", textContent: "No sites yet - users see the whole organization." }));
    return;
  }
  for (const site of usersState.sites) {
    const label = document.createElement("label");
    const box = Object.assign(document.createElement("input"), { type: "checkbox", value: site.id, checked: selected.includes(site.id) });
    label.append(box, site.name);
    list.appendChild(label);
  }
}

function openUserDialog(user) {
  usersState.editing = user || null;
  const isMe = user && user.id === usersState.me.sub;
  const lastAdmin = user && user.role === "admin" && adminCount() <= 1;
  $("user-form").reset();
  showError($("user-error"), "");
  $("user-dialog-title").textContent = user ? `Edit ${user.email}` : "New user";
  $("user-email-label").hidden = Boolean(user);
  $("user-email").required = !user;
  $("user-role").value = user ? user.role : "viewer";
  $("user-role").disabled = Boolean(isMe || lastAdmin);
  $("user-role").title = isMe ? "You can't change your own role" : lastAdmin ? "The organization's last admin" : "";
  $("user-role-help").textContent = ROLE_HELP[$("user-role").value];
  $("user-password-note").hidden = Boolean(user);
  $("user-submit").textContent = user ? "Save" : "Create";
  renderSiteChoices(user ? user.site_ids : []);
  $("user-dialog").showModal();
}

function chosenSites() {
  return [...$("user-sites-list").querySelectorAll("input:checked")].map((b) => b.value);
}

function wireUserDialog() {
  $("user-role").addEventListener("change", () => {
    $("user-role-help").textContent = ROLE_HELP[$("user-role").value];
  });
  $("user-cancel").addEventListener("click", () => $("user-dialog").close());
  $("user-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const editing = usersState.editing;
    try {
      if (editing) {
        const patch = { site_ids: chosenSites() };
        if (!$("user-role").disabled) patch.role = $("user-role").value;
        await authApi("PATCH", `/users/${encodeURIComponent(editing.id)}`, patch);
        $("user-dialog").close();
      } else {
        const email = $("user-email").value.trim();
        const password = generatePassword();
        await authApi("POST", "/users", { email, password, role: $("user-role").value, site_ids: chosenSites() });
        $("user-dialog").close();
        showSecret("User created", `Give ${email} this password to sign in with. They can change it under Ops -> Your account.`, password);
      }
      await loadUsers();
    } catch (err) {
      showError($("user-error"), err.message);
    }
  });
}

function openConfirm(title, text, expect, action) {
  usersState.confirmAction = action;
  $("confirm-form").reset();
  showError($("confirm-error"), "");
  $("confirm-title").textContent = title;
  $("confirm-text").textContent = text;
  $("confirm-expect").textContent = expect || "";
  $("confirm-type-label").hidden = !expect;
  $("confirm-input").dataset.expect = expect || "";
  $("confirm-dialog").showModal();
}

function confirmDelete(user) {
  openConfirm(`Delete ${user.email}?`, "They're signed out everywhere and can't sign in again. This can't be undone.",
    user.email, async () => {
      await authApi("DELETE", `/users/${encodeURIComponent(user.id)}`);
    });
}

async function setDisabled(user, disabled) {
  try {
    await authApi("PATCH", `/users/${encodeURIComponent(user.id)}`, { disabled });
    await loadUsers();
  } catch (err) {
    $("users-status").textContent = err.message;
  }
}

function confirmDisable(user) {
  openConfirm(`Disable ${user.email}?`, "They're signed out everywhere and can't sign in until you enable them again. Nothing is deleted.",
    null, async () => {
      await authApi("PATCH", `/users/${encodeURIComponent(user.id)}`, { disabled: true });
    });
}

function confirmResetPassword(user) {
  openConfirm(`Reset ${user.email}'s password?`, "A new password is generated and shown once; they're signed out everywhere.",
    null, async () => {
      const password = generatePassword();
      await authApi("PUT", `/users/${encodeURIComponent(user.id)}/password`, { password });
      showSecret("Password reset", `${user.email}'s new password:`, password);
    });
}

function wireConfirmDialogs() {
  $("confirm-cancel").addEventListener("click", () => $("confirm-dialog").close());
  $("confirm-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const expect = $("confirm-input").dataset.expect;
    if (expect && $("confirm-input").value.trim().toLowerCase() !== expect.toLowerCase()) {
      return showError($("confirm-error"), "that doesn't match");
    }
    try {
      await usersState.confirmAction();
      $("confirm-dialog").close();
      await loadUsers();
    } catch (err) {
      showError($("confirm-error"), err.message);
    }
  });
  $("secret-done").addEventListener("click", () => {
    $("secret-value").textContent = ""; // gone once dismissed
    $("secret-dialog").close();
  });
  $("secret-copy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("secret-value").textContent);
      $("secret-copy").textContent = "Copied";
    } catch {
      $("secret-copy").textContent = "Select and copy it";
    }
  });
}

let usersWired = false;

async function initUsers() {
  if (!usersWired) {
    usersWired = true;
    wireAccount();
    wireUserDialog();
    wireConfirmDialogs();
    $("new-user-btn").addEventListener("click", () => openUserDialog(null));
  }
  try {
    usersState.me = await authApi("GET", "/me");
  } catch (err) {
    $("account-who").textContent = err.message;
    return;
  }
  const me = usersState.me;
  $("account-who").textContent = `Signed in as ${me.email || "an API key"} (${me.role}).`;
  $("change-password-btn").hidden = me.kind !== "session";
  if (me.role !== "admin") return;
  initSources(); // sources.js: the source registry, admin-only like Users
  initEdge(); // edge.js: edge-node broker accounts, admin-only too
  $("users-section").hidden = false;
  try {
    await loadUsers();
  } catch (err) {
    $("users-status").textContent = err.message;
  }
}

// Ops -> Your account: two-step sign-in (TOTP + recovery codes), and for
// admins the org's requirement and resetting a user's second factor (#278).
// Loaded after users.js, which calls initMfa(me) once /auth/me has answered.
// Uses users.js's authApi, $, showError and showSecret.

const mfaState = { me: null, mfa: null, org: null };

function mfaDialog() {
  let d = $("mfa-dialog");
  if (d) return d;
  d = document.createElement("dialog");
  d.id = "mfa-dialog";
  d.innerHTML = `<form method="dialog" id="mfa-form">
      <h3 id="mfa-title"></h3>
      <div id="mfa-body"></div>
      <label id="mfa-password-row">Your password <input type="password" id="mfa-password" autocomplete="current-password" /></label>
      <label>Code from your authenticator app <input id="mfa-code" autocomplete="one-time-code" required /></label>
      <p id="mfa-error" class="hint" hidden></p>
      <div class="dialog-actions">
        <button type="button" id="mfa-cancel">Cancel</button>
        <button type="submit" id="mfa-submit"></button>
      </div>
    </form>`;
  document.body.appendChild(d);
  $("mfa-cancel").addEventListener("click", () => d.close());
  return d;
}

// One dialog for setup, turning off and new recovery codes: `run(code,
// password)` does the call; `needsPassword` shows the password field.
function openMfaDialog(title, bodyNodes, submitText, needsPassword, run) {
  const d = mfaDialog();
  $("mfa-title").textContent = title;
  $("mfa-body").replaceChildren(...bodyNodes);
  $("mfa-submit").textContent = submitText;
  $("mfa-password-row").hidden = !needsPassword;
  $("mfa-password").required = needsPassword;
  $("mfa-password").value = "";
  $("mfa-code").value = "";
  showError($("mfa-error"), "");
  $("mfa-form").onsubmit = async (ev) => {
    ev.preventDefault();
    try {
      await run($("mfa-code").value.trim(), $("mfa-password").value);
      d.close();
      await renderMfaAccount();
    } catch (err) {
      showError($("mfa-error"), err.message);
    }
  };
  d.showModal();
}

function showRecoveryCodes(codes) {
  showSecret("Recovery codes", "Each code signs you in once if you lose your phone. Keep them somewhere safe - they are shown only now.",
    codes.join("\n"));
}

async function startSetup() {
  const { secret, otpauth_uri: uri } = await authApi("POST", "/me/mfa/totp/setup");
  const intro = Object.assign(document.createElement("p"), {
    className: "hint",
    textContent: "In your authenticator app (any TOTP app), add an account with this key, or open the link on your phone. Then enter the 6-digit code it shows.",
  });
  const key = Object.assign(document.createElement("pre"), { className: "secret", textContent: secret.match(/.{1,4}/g).join(" ") });
  const link = Object.assign(document.createElement("a"), { href: uri, textContent: "Open in authenticator app" });
  openMfaDialog("Set up two-step sign-in", [intro, key, link], "Turn on", false, async (code) => {
    const { recovery_codes: codes, token } = await authApi("POST", "/me/mfa/totp/enable", { code });
    setToken(token); // a full session now: other sessions were ended
    if (mfaState.me && mfaState.me.mfa_enrollment_required) {
      location.hash = "";
      location.reload(); // the rest of the page needed the full session
      return;
    }
    showRecoveryCodes(codes);
  });
}

async function renderMfaAccount() {
  const box = $("mfa-account");
  if (!box || mfaState.me.kind !== "session") return;
  mfaState.mfa = await authApi("GET", "/me/mfa");
  const m = mfaState.mfa;
  box.replaceChildren();
  const status = document.createElement("span");
  if (m.enabled) {
    status.textContent = `Two-step sign-in is on (${m.recovery_codes_left} recovery code${m.recovery_codes_left === 1 ? "" : "s"} left). `;
    const regen = Object.assign(document.createElement("button"), { type: "button", textContent: "New recovery codes" });
    regen.addEventListener("click", () => openMfaDialog("New recovery codes", [Object.assign(document.createElement("p"), {
      className: "hint", textContent: "Your old recovery codes stop working.",
    })], "Make new codes", false, async (code) => {
      showRecoveryCodes((await authApi("POST", "/me/mfa/recovery-codes", { code })).recovery_codes);
    }));
    const off = Object.assign(document.createElement("button"), { type: "button", textContent: "Turn off" });
    off.disabled = m.required;
    off.title = m.required ? "Your organization requires two-step sign-in" : "";
    off.addEventListener("click", () => openMfaDialog("Turn off two-step sign-in", [], "Turn off", true,
      (code, password) => authApi("POST", "/me/mfa/disable", { code, password })));
    box.append(status, regen, " ", off);
  } else {
    status.textContent = m.required || mfaState.me.mfa_enrollment_required
      ? "Your organization requires two-step sign-in. Set it up to use Baler. "
      : "Two-step sign-in is off. Turn it on so a password alone can't get into your account. ";
    const setup = Object.assign(document.createElement("button"), { type: "button", textContent: "Set up two-step sign-in" });
    setup.addEventListener("click", () => startSetup().catch((err) => { status.textContent = err.message; }));
    box.append(status, setup);
  }
}

// --- admins ---------------------------------------------------------------

async function renderOrgMfa() {
  const box = $("org-mfa");
  if (!box) return;
  mfaState.org = await authApi("GET", "/org");
  const o = mfaState.org;
  box.replaceChildren();
  const label = document.createElement("label");
  const cb = Object.assign(document.createElement("input"), { type: "checkbox", checked: o.mfa_required });
  label.append(cb, " Require two-step sign-in for everyone in this organization");
  const note = Object.assign(document.createElement("span"), {
    className: "hint", textContent: ` ${o.users_with_mfa} of ${o.users} users have it on.`,
  });
  const err = Object.assign(document.createElement("p"), { className: "hint", hidden: true });
  cb.addEventListener("change", async () => {
    if (cb.checked && !confirm("Everyone without two-step sign-in is signed out and must set it up at their next sign-in. API keys and edge nodes are not affected. Continue?")) {
      cb.checked = false;
      return;
    }
    try {
      await authApi("PATCH", "/org", { mfa_required: cb.checked });
      await renderOrgMfa();
      await loadUsers();
    } catch (e) {
      cb.checked = !cb.checked;
      showError(err, e.message);
    }
  });
  box.append(label, note, err);
}

// users.js's renderUsers calls this per row: a badge, and "Reset 2-step".
function mfaUserRow(user, emailCell, actions) {
  if (!user.mfa_enabled) return;
  emailCell.append(" ", badge("2-step", ""));
  const reset = Object.assign(document.createElement("button"), { type: "button", textContent: "Reset 2-step" });
  reset.title = "For a lost phone: turns their two-step sign-in off and signs them out";
  reset.addEventListener("click", () => openConfirm(
    "Reset two-step sign-in",
    `${user.email} is signed out and their authenticator and recovery codes stop working. If the organization requires two-step sign-in, they set it up again at their next sign-in.`,
    null,
    () => authApi("DELETE", `/users/${encodeURIComponent(user.id)}/mfa`),
  ));
  actions.appendChild(reset);
}

async function initMfa(me) {
  mfaState.me = me;
  try {
    await renderMfaAccount();
  } catch (err) {
    $("mfa-account").textContent = err.message;
  }
  if (me.role === "admin" && !me.mfa_enrollment_required) {
    try {
      await renderOrgMfa();
    } catch (err) {
      $("org-mfa").textContent = err.message;
    }
  }
}

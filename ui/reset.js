// Password reset (#278): ask for a link, or, arriving from the emailed link
// (reset.html#token=...), set the new password. The token is read from the
// fragment and then removed from the address bar.
// ?auth= is honoured only as a same-origin path: the new password and the
// reset token are posted there, so a crafted link must not redirect them.
const AUTH_PARAM = new URLSearchParams(location.search).get("auth");
const AUTH = AUTH_PARAM && AUTH_PARAM.startsWith("/") && !AUTH_PARAM.startsWith("//") && !AUTH_PARAM.includes("\\")
  ? AUTH_PARAM : "/auth";
const el = (id) => document.getElementById(id);

function say(id, text) {
  el(id).textContent = text;
  el(id).hidden = !text;
}

async function post(path, body) {
  const res = await fetch(`${AUTH}${path}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || (res.status === 429 ? "too many tries - wait a minute" : `failed (${res.status})`));
  return data;
}

async function main() {
  const token = new URLSearchParams(location.hash.slice(1)).get("token");
  if (token) {
    history.replaceState(null, "", location.pathname + location.search);
    el("confirm-form").hidden = false;
    el("confirm-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const pw = el("new-password").value;
      if (pw !== el("new-password-again").value) return say("confirm-msg", "the passwords don't match");
      try {
        await post("/password-reset/confirm", { token, new_password: pw });
        el("confirm-form").querySelector("button").disabled = true;
        say("confirm-msg", "Password changed. Sign in with it now.");
        const a = Object.assign(document.createElement("a"), { href: "index.html", textContent: " Sign in" });
        el("confirm-msg").appendChild(a);
      } catch (err) {
        say("confirm-msg", err.message);
      }
    });
    return;
  }
  let selfService = false;
  try {
    const res = await fetch(`${AUTH}/password-reset/config`);
    selfService = res.ok && (await res.json()).self_service === true;
  } catch {
    selfService = false;
  }
  if (!selfService) {
    el("off-msg").hidden = false;
    return;
  }
  el("request-form").hidden = false;
  el("request-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      const data = await post("/password-reset", { email: el("request-email").value });
      say("request-msg", data.status);
    } catch (err) {
      say("request-msg", err.message);
    }
  });
}

main();

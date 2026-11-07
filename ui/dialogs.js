// One confirm dialog and one toast for every page - replacing native
// confirm()/prompt()/alert() and silent catch blocks. Plain globals, no
// build step, same as the rest of ui/.
//
//   toast("Connector created")                 // success, fades out
//   toast("Windrow refused it: ...", "error")  // stays until dismissed
//   if (await confirmDanger({ title: "Delete connector?", body: "...",
//                             action: "Delete", typeToConfirm: "fbf/x" })) ...

(function () {
  function ensureToastHost() {
    let host = document.getElementById("toast-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "toast-host";
      host.className = "toast-host";
      host.setAttribute("role", "status");
      host.setAttribute("aria-live", "polite");
      document.body.append(host);
    }
    return host;
  }

  window.toast = function toast(message, kind = "ok", ms = 4000) {
    const host = ensureToastHost();
    const t = document.createElement("div");
    t.className = `toast toast-${kind}`;
    const text = document.createElement("span");
    text.textContent = message;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "toast-close";
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "×";
    close.addEventListener("click", () => t.remove());
    t.append(text, close);
    host.append(t);
    if (kind !== "error") setTimeout(() => t.remove(), ms);
    return t;
  };

  // Resolves true only when the person confirms. With typeToConfirm the
  // action button stays disabled until they type that exact text - for
  // deletes that lose data or configuration.
  let confirmSeq = 0;
  window.confirmDanger = function confirmDanger({ title, body = "", action = "Delete", typeToConfirm = null, danger = true }) {
    return new Promise((resolve) => {
      const dlg = document.createElement("dialog");
      dlg.className = "confirm-dialog";
      // Named by its heading and described by its text, for screen readers.
      const id = `confirm-${++confirmSeq}`;
      dlg.setAttribute("aria-labelledby", `${id}-title`);
      const form = document.createElement("form");
      form.method = "dialog";
      const h = document.createElement("h2");
      h.id = `${id}-title`;
      h.textContent = title;
      form.append(h);
      if (body) {
        const p = document.createElement("p");
        p.id = `${id}-body`;
        p.textContent = body;
        dlg.setAttribute("aria-describedby", p.id);
        form.append(p);
      }
      let input = null;
      if (typeToConfirm) {
        const label = document.createElement("label");
        label.textContent = `Type ${typeToConfirm} to confirm`;
        input = document.createElement("input");
        input.autocomplete = "off";
        label.append(input);
        form.append(label);
      }
      const menu = document.createElement("menu");
      const ok = document.createElement("button");
      ok.type = "submit";
      ok.value = "ok";
      ok.textContent = action;
      if (danger) ok.className = "danger";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.textContent = "Cancel";
      menu.append(cancel, ok);
      form.append(menu);
      dlg.append(form);
      document.body.append(dlg);
      if (input) {
        ok.disabled = true;
        input.addEventListener("input", () => { ok.disabled = input.value.trim() !== typeToConfirm; });
      }
      const done = (v) => { dlg.close(); dlg.remove(); resolve(v); };
      cancel.addEventListener("click", () => done(false));
      dlg.addEventListener("cancel", (ev) => { ev.preventDefault(); done(false); });
      form.addEventListener("submit", (ev) => { ev.preventDefault(); done(!ok.disabled); });
      dlg.showModal();
      (input || cancel).focus();
    });
  };

  // The signed-in role, read from the token for showing/hiding controls
  // only - the gateway enforces it on every request regardless.
  window.tokenRole = function tokenRole() {
    try {
      const token = localStorage.getItem("td_token");
      const payload = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
      return payload.role || "viewer";
    } catch {
      return "viewer";
    }
  };
  const RANK = { viewer: 1, operator: 2, admin: 3 };
  window.roleAtLeast = (role) => (RANK[window.tokenRole()] || 0) >= RANK[role];
})();

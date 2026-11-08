// Site naming conventions (Baler#186), on the Mapping review page.
// POST /ops/conventions/preview (dry run), POST /ops/conventions (save),
// GET /ops/conventions, POST /ops/conventions/{id}/apply, DELETE.
// Uses api() and escapeHtml() from mapping.js.
(function () {
  const $ = (id) => document.getElementById(id);
  let editingId = "";

  function parseCodes(text) {
    const out = {};
    for (const line of text.split("\n")) {
      const t = line.trim();
      if (!t || t.startsWith("#")) continue;
      const i = t.indexOf("=");
      if (i < 1) throw new Error(`expected "code = Brick_Class": ${t}`);
      out[t.slice(0, i).trim()] = t.slice(i + 1).trim();
    }
    return out;
  }

  function codesText(codes) {
    return Object.entries(codes || {}).map(([k, v]) => `${k} = ${v}`).join("\n");
  }

  function body() {
    return {
      id: editingId || undefined,
      name: $("conv-name").value.trim(),
      scope_prefix: $("conv-scope").value.trim(),
      templates: $("conv-templates").value.split("\n").map((s) => s.trim()).filter(Boolean),
      point_codes: parseCodes($("conv-points").value),
      equip_codes: parseCodes($("conv-equips").value),
      only_unclassified: $("conv-only-unclassified").checked,
    };
  }

  function showError(err) {
    $("conv-error").textContent = err.message;
    $("conv-error").hidden = false;
  }

  function addCode(which, code) {
    const box = $(which === "equip" ? "conv-equips" : "conv-points");
    box.value = (box.value.trim() ? box.value.trim() + "\n" : "") + `${code} = `;
    box.focus();
    box.setSelectionRange(box.value.length, box.value.length);
  }

  function renderPreview(p) {
    const pct = p.points ? Math.round((100 * p.classified) / p.points) : 0;
    const rows = p.unknown_point_codes.slice(0, 40).map((c) => `<tr><td><code>${escapeHtml(c.code)}</code></td><td>${c.points}</td>
      <td>${c.examples.map((e) => `<code>${escapeHtml(e)}</code>`).join(", ")}</td>
      <td><button type="button" data-add="point" data-code="${escapeHtml(c.code)}">Say what it is</button></td></tr>`).join("");
    const equip = p.unknown_equip_codes.map((c) => `<li><code>${escapeHtml(c.code)}</code> (${c.points} points)
      <button type="button" data-add="equip" data-code="${escapeHtml(c.code)}">Say what it is</button></li>`).join("");
    const unmatched = p.unmatched_sample.map((l) => `<li><code>${escapeHtml(l)}</code></li>`).join("");
    $("conv-result").innerHTML = `
      <p><strong>${p.matched}</strong> of ${p.points} points match a template (${p.by_template.join(" / ")} per template);
        <strong>${p.classified}</strong> (${pct}%) would be classified; ${p.equipment} pieces of equipment found.</p>
      ${rows ? `<h4>Unknown point codes, most points first</h4><table><thead><tr><th>Code</th><th>Points</th><th>Examples</th><th></th></tr></thead><tbody>${rows}</tbody></table>` : ""}
      ${equip ? `<h4>Unknown equipment codes</h4><ul>${equip}</ul>` : ""}
      ${unmatched ? `<h4>No template matches these (a sample)</h4><ul>${unmatched}</ul>` : ""}`;
  }

  async function refreshSaved() {
    const list = await api("/conventions");
    if (!list.length) {
      $("conv-saved").className = "hint";
      $("conv-saved").textContent = "None yet.";
      return;
    }
    $("conv-saved").className = "";
    $("conv-saved").innerHTML = `<table><thead><tr><th>Name</th><th>Under</th><th>Templates</th><th>Codes</th><th></th></tr></thead><tbody>${
      list.map((c) => `<tr><td>${escapeHtml(c.name)}</td><td><code>${escapeHtml(c.scope_prefix || "everything")}</code></td>
        <td>${c.templates.length}</td><td>${Object.keys(c.point_codes).length} point, ${Object.keys(c.equip_codes).length} equip</td>
        <td><button type="button" data-edit="${escapeHtml(c.id)}">Edit</button>
            <button type="button" data-apply="${escapeHtml(c.id)}">Apply</button>
            <button type="button" data-delete="${escapeHtml(c.id)}">Delete</button></td></tr>`).join("")
    }</tbody></table>`;
    $("conv-saved").dataset.list = JSON.stringify(list);
  }

  $("conv-preview").addEventListener("click", async () => {
    $("conv-error").hidden = true;
    try {
      $("conv-status").textContent = "previewing…";
      renderPreview(await api("/conventions/preview", { method: "POST", body: JSON.stringify(body()) }));
      $("conv-status").textContent = "";
    } catch (err) {
      $("conv-status").textContent = "";
      showError(err);
    }
  });

  $("conv-save").addEventListener("click", async () => {
    $("conv-error").hidden = true;
    try {
      const saved = await api("/conventions", { method: "POST", body: JSON.stringify(body()) });
      editingId = saved.id;
      $("conv-status").textContent = `saved "${saved.name}"`;
      await refreshSaved();
    } catch (err) {
      showError(err);
    }
  });

  $("conv-result").addEventListener("click", (ev) => {
    const b = ev.target.closest("button[data-add]");
    if (b) addCode(b.dataset.add, b.dataset.code);
  });

  $("conv-saved").addEventListener("click", async (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    const list = JSON.parse($("conv-saved").dataset.list || "[]");
    try {
      if (b.dataset.edit) {
        const c = list.find((x) => x.id === b.dataset.edit);
        editingId = c.id;
        $("conv-name").value = c.name;
        $("conv-scope").value = c.scope_prefix;
        $("conv-templates").value = c.templates.join("\n");
        $("conv-points").value = codesText(c.point_codes);
        $("conv-equips").value = codesText(c.equip_codes);
      } else if (b.dataset.apply) {
        const out = await api(`/conventions/${encodeURIComponent(b.dataset.apply)}/apply`, { method: "POST", body: "{}" });
        $("conv-status").textContent = `classified ${out.points} point(s) and ${out.equipment} piece(s) of equipment`;
        await refresh();
      } else if (b.dataset.delete) {
        await api(`/conventions/${encodeURIComponent(b.dataset.delete)}`, { method: "DELETE" });
        if (editingId === b.dataset.delete) editingId = "";
        await refreshSaved();
      }
    } catch (err) {
      showError(err);
    }
  });

  // mapping.js's initAuth calls this once signed in (one login handler).
  window.conventionsReady = () => refreshSaved().catch(() => {});
})();

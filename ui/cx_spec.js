// Commissioning spec ingestion and spec<->device alignment (Baler#43).
// Loaded after cx.js; registers on cxProjectHooks.

const specState = { items: [], polls: {} };

cxProjectHooks.push(() => loadSpec().then(loadAlignment));

async function loadSpec() {
  if (!cx.project) return;
  await Promise.all([loadSpecDocs(), loadSpecItems()]);
}

async function loadSpecDocs() {
  const list = $("spec-docs");
  try {
    const docs = await cxApi("GET", `/projects/${cx.project.id}/spec/documents`);
    list.innerHTML = "";
    for (const d of docs.slice(0, 20)) {
      const how = d.method === "llm" ? "read by a model" : (d.media_type === "application/pdf" ? "tables read" : "imported");
      const st = d.status === "completed" ? `${d.items} items` : d.status === "failed" ? `failed: ${d.error}` : `${d.status}…`;
      list.append(el("li", { textContent: `${when(d.created_at)} · ${d.filename} (${how}) · ${st}${d.info && d.info.notes ? ` · note: ${d.info.notes}` : ""}` }));
      if (d.status === "queued" || d.status === "running") pollDoc(d.id);
    }
  } catch (err) {
    list.textContent = err.message;
  }
}

function pollDoc(id) {
  if (specState.polls[id]) return;
  specState.polls[id] = true;
  const tick = async () => {
    try {
      const d = await cxApi("GET", `/spec-documents/${id}`);
      if (d.status === "queued" || d.status === "running") {
        setTimeout(tick, 2000);
        return;
      }
      delete specState.polls[id];
      flash($("spec-status"), d.status === "completed" ? `${d.filename}: ${d.items} proposals to review` : `${d.filename}: ${d.error}`, d.status !== "completed");
      $("spec-filter").value = "proposed";
      await Promise.all([loadSpec(), loadEvents()]);
    } catch {
      delete specState.polls[id];
    }
  };
  setTimeout(tick, 1500);
}

function cite(src) {
  if (!src) return "";
  const where = src.page ? `p.${src.page}` : src.row ? `row ${src.row}` : "";
  return `${src.document || ""} ${where}`.trim();
}

async function loadSpecItems() {
  const body = $("spec-body");
  const status = $("spec-filter").value;
  try {
    specState.items = await cxApi("GET", `/projects/${cx.project.id}/spec${status ? `?status=${status}` : ""}`);
    body.innerHTML = "";
    for (const it of specState.items.slice(0, 500)) {
      const tr = el("tr");
      tr.innerHTML = `<td>${esc(it.kind)}</td><td>${esc(it.tag)}${it.equip_tag ? ` <span class="hint">on ${esc(it.equip_tag)}</span>` : ""}</td>
        <td>${esc(it.name)}</td><td>${esc(it.brick_class || "-")}</td>
        <td title="${esc((it.source && it.source.quote) || "")}">${esc(cite(it.source))}</td>
        <td>${it.confidence == null ? "" : Math.round(it.confidence * 100) + "%"}</td><td></td>`;
      if (can("operator")) {
        const actions = tr.lastElementChild;
        if (it.status !== "accepted") actions.append(specButton("Accept", () => reviewItem(it, { status: "accepted" })));
        if (it.status !== "rejected") actions.append(specButton("Reject", () => reviewItem(it, { status: "rejected" })));
        actions.append(specButton("Correct…", () => correctItem(it)));
      }
      body.append(tr);
    }
    if (!specState.items.length) body.innerHTML = '<tr><td colspan="7" class="hint">Nothing here.</td></tr>';
    $("spec-accept-all").disabled = !specState.items.some((i) => i.status !== "accepted");
  } catch (err) {
    body.innerHTML = `<tr><td colspan="7" class="st-fail">${esc(err.message)}</td></tr>`;
  }
}

function specButton(text, fn) {
  const b = el("button", { type: "button", textContent: text });
  b.addEventListener("click", fn);
  return b;
}

async function reviewItem(it, body) {
  try {
    await cxApi("PATCH", `/spec-items/${it.id}`, body);
    await loadSpecItems();
  } catch (err) {
    alert(err.message);
  }
}

async function correctItem(it) {
  const tag = prompt("Tag", it.tag);
  if (tag === null) return;
  const cls = prompt("Brick class (blank to leave)", it.brick_class || "");
  if (cls === null) return;
  const body = { tag, status: "accepted" };
  if (cls && cls !== it.brick_class) body.brick_class = cls;
  await reviewItem(it, body);
}

async function uploadSpec(file) {
  const ext = file.name.split(".").pop().toLowerCase();
  flash($("spec-status"), `uploading ${file.name}…`);
  try {
    if (ext === "csv") {
      const got = await cxApi("POST", `/projects/${cx.project.id}/spec/import`, { format: "csv", data: await file.text(), filename: file.name });
      flash($("spec-status"), `${file.name}: ${got.items} items (${got.unclassified} without a Brick class, ${got.skipped_rows} rows skipped)`);
      $("spec-filter").value = "accepted";
    } else {
      const b64 = await new Promise((resolve, reject) => {
        const r = new FileReader();
        r.onload = () => resolve(String(r.result).split(",", 2)[1]);
        r.onerror = () => reject(new Error("could not read the file"));
        r.readAsDataURL(file);
      });
      if (ext === "xlsx") {
        const got = await cxApi("POST", `/projects/${cx.project.id}/spec/import`, { format: "xlsx", data: b64, filename: file.name });
        flash($("spec-status"), `${file.name}: ${got.items} items`);
        $("spec-filter").value = "accepted";
      } else if (ext === "pdf") {
        const doc = await cxApi("POST", `/projects/${cx.project.id}/spec/extract`, { data: b64, filename: file.name });
        flash($("spec-status"), `${file.name}: reading it - proposals appear here when it's done`);
        pollDoc(doc.id);
      } else {
        throw new Error("CSV, XLSX or PDF");
      }
    }
    await Promise.all([loadSpec(), loadEvents()]);
  } catch (err) {
    flash($("spec-status"), err.message, true);
  }
}

async function loadAlignment() {
  const body = $("align-body");
  const devs = $("align-deviations");
  flash($("align-status"), "aligning…");
  try {
    const got = await cxApi("GET", `/projects/${cx.project.id}/alignment`);
    body.innerHTML = "";
    for (const m of got.mappings) {
      const tr = el("tr");
      const why = m.basis.map((b) => b.detail).filter(Boolean).join("; ") + (m.alternatives.length ? ` · or: ${m.alternatives.map((a) => a.interpretation).join("; ")}` : "");
      const pts = m.points ? ` · points ${m.points.filter((p) => p.status === "present").length} present, ${m.points.filter((p) => p.status === "specified_absent").length} missing` : "";
      tr.innerHTML = `<td>${esc(m.spec_tag)}</td><td>${esc(m.equip_uri ? m.equip_uri.replace(/^urn:equip:/, "") : "-")}</td>
        <td class="${m.confidence === "unmatched" || m.confidence === "low" ? "st-fail" : m.confidence === "medium" ? "st-inconclusive" : "st-pass"}">${esc(m.confidence)}</td>
        <td class="hint">${esc(why + pts)}</td><td></td>`;
      if (can("operator") && m.equip_uri) {
        const actions = tr.lastElementChild;
        if (m.decision) {
          actions.append(specButton("Undo", async () => {
            await cxApi("DELETE", `/mappings/${m.decision.id}`).catch((e) => alert(e.message));
            loadAlignment();
          }));
        } else {
          actions.append(specButton("Confirm", () => decide(m, "confirmed", false)));
          actions.append(specButton("Confirm + write", () => decide(m, "confirmed", true)));
          actions.append(specButton("Reject", () => decide(m, "rejected", false)));
        }
      }
      body.append(tr);
    }
    if (!got.mappings.length) body.innerHTML = '<tr><td colspan="5" class="hint">No accepted spec equipment yet.</td></tr>';
    devs.innerHTML = "";
    for (const d of got.deviations) {
      devs.append(el("li", { className: d.severity === "warning" ? "st-inconclusive" : "hint",
        textContent: `${d.kind.replaceAll("_", " ")}: ${d.spec_tag || (d.spec_tags || []).join(", ") || (d.equip_uri || "").replace(/^urn:equip:/, "")} - ${d.interpretation}. ${d.to_settle || ""}` }));
    }
    const c = got.counts;
    flash($("align-status"), `${Object.entries(c).map(([k, v]) => `${v} ${k}`).join(", ") || "nothing to align"}${got.proposed_items ? ` · ${got.proposed_items} proposals still to review` : ""}`);
  } catch (err) {
    flash($("align-status"), err.message, true);
  }
}

async function decide(m, status, writeBack) {
  if (writeBack && !confirm(`Write ${m.spec_tag} into the graph on ${m.equip_uri} (as cxSpecTag, plus its Brick class if the equipment has none)? It's audited in the record editor's history.`)) return;
  try {
    await cxApi("POST", `/projects/${cx.project.id}/alignment/decisions`, { spec_item_id: m.spec_item_id, equip_uri: m.equip_uri, status, write_back: writeBack });
    await Promise.all([loadAlignment(), loadEvents()]);
  } catch (err) {
    alert(err.message);
  }
}

$("spec-file").addEventListener("change", (ev) => {
  const f = ev.target.files[0];
  ev.target.value = "";
  if (f) uploadSpec(f);
});
$("spec-filter").addEventListener("change", loadSpecItems);
$("align-btn").addEventListener("click", loadAlignment);
$("spec-accept-all").addEventListener("click", async () => {
  const ids = specState.items.filter((i) => i.status !== "accepted").map((i) => i.id);
  if (!ids.length || !confirm(`Accept ${ids.length} spec items?`)) return;
  try {
    await cxApi("POST", `/projects/${cx.project.id}/spec/review`, { ids, status: "accepted" });
    await loadSpecItems();
  } catch (err) {
    alert(err.message);
  }
});

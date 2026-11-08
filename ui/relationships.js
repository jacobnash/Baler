// Equipment relationship proposals (Baler#187), on the Mapping review page.
// GET /ops/mapping/relationships, POST /ops/mapping/relationships/decide.
// Uses api() and escapeHtml() from mapping.js.
(function () {
  const listEl = document.getElementById("relationships-list");
  const short = (uri) => String(uri).replace(/^urn:(point|equip):/, "");

  function rowHtml(r) {
    const what = r.predicate === "feeds"
      ? `<code>${escapeHtml(short(r.subject))}</code> feeds <code>${escapeHtml(short(r.objects[0]))}</code>`
      : `${r.objects.length} points under <code>${escapeHtml(short(r.subject))}</code> are one piece of equipment`;
    return `<tr><td>${what}</td><td class="hint">${escapeHtml((r.evidence && r.evidence.why) || "")}</td>
      <td><button type="button" data-rel="${escapeHtml(r.id)}" data-action="accept">Accept</button>
          <button type="button" data-rel="${escapeHtml(r.id)}" data-action="reject">Reject</button></td></tr>`;
  }

  async function refreshRelationships() {
    const rows = await api("/mapping/relationships");
    if (!rows.length) {
      listEl.className = "hint";
      listEl.textContent = "Nothing proposed.";
      return;
    }
    listEl.className = "";
    listEl.innerHTML = `<table><thead><tr><th>Proposal</th><th>Evidence</th><th></th></tr></thead><tbody>${rows.map(rowHtml).join("")}</tbody></table>`;
  }

  listEl.addEventListener("click", async (ev) => {
    const b = ev.target.closest("button[data-rel]");
    if (!b) return;
    try {
      await api("/mapping/relationships/decide", { method: "POST", body: JSON.stringify({ ids: [b.dataset.rel], action: b.dataset.action }) });
      await refreshRelationships();
    } catch (err) {
      listEl.insertAdjacentHTML("afterbegin", `<p class="field-error">${escapeHtml(err.message)}</p>`);
    }
  });

  window.relationshipsReady = () => refreshRelationships().catch(() => {});
})();

// Commissioning issues log and accepted risks (Baler#40). Loaded after
// cx.js and plugs into it through two hooks: cxProjectHooks (run when a
// project is selected) and cxResultActions (buttons on an evidence card).

const ISSUE_STATUS_LABEL = {
  open: "open", in_progress: "in progress", ready_for_retest: "resolved, pending verification", closed: "closed", risk_accepted: "risk accepted",
};
const issueState = { list: [], current: null };

const cxProjectHooks = [() => loadIssues(), () => loadRisks(), () => loadMirrorStatus()];

async function loadMirrorStatus() {
  const el_ = $("mirror-status");
  const cfg = (cx.project.settings || {}).fault_mirroring || {};
  if (!cfg.enabled) {
    el_.textContent = "Fault mirroring is off for this project (settings.fault_mirroring).";
    return;
  }
  try {
    const faults = await cxApi("GET", `/projects/${cx.project.id}/faults`);
    el_.textContent = `Fault mirroring is on (${cfg.equipment === "all" ? "all equipment" : "confirmed equipment"}): ${faults.length} open fault${faults.length === 1 ? "" : "s"} on the Alarms page from this project.`;
  } catch (err) {
    el_.textContent = err.message;
  }
}

const cxResultActions = [
  (card, r) => {
    if (r.status !== "fail" || !can("operator")) return;
    const tracked = issueState.list.find((i) => i.check_id === r.check_id && i.subject === r.subject && i.status !== "closed");
    const btn = el("button", { type: "button", textContent: tracked ? `Issue: ${ISSUE_STATUS_LABEL[tracked.status]}` : "Open issue" });
    btn.addEventListener("click", async () => {
      if (tracked) return openIssue(tracked.id);
      try {
        const issue = await cxApi("POST", `/projects/${cx.project.id}/issues`, {
          from_result: { run_id: cx.run.id, check_id: r.check_id, subject: r.subject },
        });
        btn.textContent = "Issue: open";
        await loadIssues();
        openIssue(issue.id);
      } catch (err) {
        if (err.status === 409 && err.data && err.data.issue_id) openIssue(err.data.issue_id);
        else alert(err.message);
      }
    });
    card.append(el("div", { className: "toolbar" }, [btn]));
  },
];

async function loadIssues() {
  if (!cx.project) return;
  const status = $("issues-filter").value;
  const body = $("issues-body");
  try {
    issueState.list = await cxApi("GET", `/projects/${cx.project.id}/issues`);
    const want = status ? status.split(",") : null;
    const rows = issueState.list.filter((i) => !want || want.includes(i.status));
    body.innerHTML = "";
    for (const i of rows) {
      const tr = el("tr", { className: "clickable" });
      const equip = i.equip_uri ? i.equip_uri.replace(/^urn:equip:/, "") : (i.detail && i.detail.equipment) || "";
      tr.innerHTML = `<td class="${i.status === "open" ? "st-fail" : i.status === "closed" ? "st-pass" : ""}">${esc(ISSUE_STATUS_LABEL[i.status])}</td>
        <td>${esc(i.title)}${i.check_id ? ` <span class="hint">(${esc(i.check_id)})</span>` : ""}</td>
        <td>${esc(equip)}</td><td>${esc(i.severity || "")}</td><td>${esc(i.assignee || "")}</td>
        <td>${esc(i.source === "baler" ? "" : `${i.source} ${i.source_id || ""}`)}</td><td>${esc(when(i.updated_at))}</td>`;
      tr.addEventListener("click", () => openIssue(i.id));
      body.append(tr);
    }
    if (!rows.length) body.innerHTML = '<tr><td colspan="7" class="hint">No issues.</td></tr>';
    const open = issueState.list.filter((i) => i.status !== "closed").length;
    flash($("issues-status"), `${open} active, ${issueState.list.length - open} closed`);
  } catch (err) {
    flash($("issues-status"), err.message, true);
  }
}

// Who accepted a waiver: the name from their account, or a typed one
// (marked), or - on waivers from before names - the account email.
function acceptedBy(k) {
  if (!k.accepted_name) return k.accepted_email || k.accepted_by || "";
  return k.accepted_name_source === "account" ? k.accepted_name : `${k.accepted_name} (as typed)`;
}

async function loadRisks() {
  if (!cx.project) return;
  const body = $("risks-body");
  try {
    const risks = await cxApi("GET", `/projects/${cx.project.id}/risks`);
    body.innerHTML = "";
    for (const k of risks) {
      const tr = el("tr");
      tr.innerHTML = `<td>${esc(k.issue.title || k.issue_id)}</td><td>${esc(k.reason)}</td>
        <td>${esc(acceptedBy(k))}${k.accepted_name && k.accepted_email ? `<br><span class="hint">via ${esc(k.accepted_email)}</span>` : ""}</td><td>${esc(k.owner || "")}</td>
        <td>${esc(k.retest_trigger || "")}</td><td>${esc(k.review_by)}</td><td></td>`;
      if (can("admin")) {
        const rv = el("button", { type: "button", textContent: "Revoke" });
        rv.addEventListener("click", async () => {
          if (!confirm("Revoke this risk? The issue reopens.")) return;
          try {
            await cxApi("DELETE", `/risks/${k.id}`);
            await Promise.all([loadIssues(), loadRisks(), loadEvents()]);
          } catch (err) {
            alert(err.message);
          }
        });
        tr.lastElementChild.append(rv);
      }
      body.append(tr);
    }
    if (!risks.length) body.innerHTML = '<tr><td colspan="7" class="hint">No standing risks.</td></tr>';
  } catch (err) {
    body.innerHTML = `<tr><td colspan="7" class="st-fail">${esc(err.message)}</td></tr>`;
  }
}

function issueError(msg) {
  $("issue-error").textContent = msg;
  $("issue-error").hidden = !msg;
}

async function openIssue(id) {
  let issue;
  try {
    issue = await cxApi("GET", `/issues/${encodeURIComponent(id)}`);
  } catch (err) {
    alert(err.message);
    return;
  }
  issueState.current = issue;
  $("issue-dialog-title").textContent = issue.title;
  const d = issue.detail || {};
  const lines = [];
  if (issue.equip_uri) lines.push(`Equipment: ${issue.equip_uri}`);
  if (issue.check_id) {
    lines.push(`Check: ${issue.check_id} on ${issue.subject}. A passing run resolves it; you close it after a passing retest.`);
    if (issue.resolved_by) {
      const by = issue.resolved_by;
      const src = by.user_sub ? "fix claimed by a person" : by.test_run_id ? `test run ${by.test_run_id}` : `run ${by.run_id}`;
      lines.push(`Resolved ${issue.resolved_at || ""} (${src})`);
    }
    if (issue.status === "ready_for_retest") {
      lines.push(issue.retest
        ? `Retest passed: ${issue.retest.test_run_id ? "test run " + issue.retest.test_run_id : "run " + issue.retest.run_id} at ${issue.retest.at}. Ready to close.`
        : "Waiting for a retest: run the procedure or a verification run on data from after the fix.");
    }
  }
  if (d.symptom || d.description) lines.push(d.symptom || d.description);
  if (issue.source !== "baler") lines.push(`Imported from ${issue.source} (${issue.source_id})`);
  if (issue.closed_run) {
    const rt = issue.retest || {};
    lines.push(`Closed citing retest: ${rt.test_run_id ? "procedure test run" : "verification run"} ${String(issue.closed_run).slice(0, 8)} (${issue.closed_run})`);
  }
  if (issue.risk) {
    lines.push(`Risk accepted by ${acceptedBy(issue.risk) || "?"}${issue.risk.accepted_name && issue.risk.accepted_email ? ` (via ${issue.risk.accepted_email})` : ""}: ${issue.risk.reason} - review by ${issue.risk.review_by}`
      + (issue.risk.owner ? ` · owner ${issue.risk.owner}` : "") + (issue.risk.retest_trigger ? ` · retest when ${issue.risk.retest_trigger}` : ""));
  }
  $("issue-detail").innerHTML = lines.map((l) => `<p>${esc(l)}</p>`).join("");
  $("issue-title").value = issue.title;
  const st = $("issue-status");
  st.innerHTML = "";
  const targets = issue.status === "risk_accepted" ? ["risk_accepted"]
    : ["open", "in_progress", "ready_for_retest",
       ...(!issue.check_id || issue.status === "closed" || (issue.status === "ready_for_retest" && issue.retest) ? ["closed"] : [])];
  for (const s of targets) st.append(el("option", { value: s, textContent: ISSUE_STATUS_LABEL[s], selected: s === issue.status }));
  st.disabled = issue.status === "risk_accepted";
  $("issue-severity").value = issue.severity || "";
  $("issue-assignee").value = issue.assignee || "";
  $("issue-note").value = "";
  const editable = can("operator");
  for (const inp of $("issue-edit").querySelectorAll("input, select, textarea")) inp.disabled = !editable || inp.disabled;
  $("issue-save").hidden = !editable;
  $("issue-risk").hidden = !can("admin") || ["closed", "risk_accepted"].includes(issue.status);
  $("risk-reason").value = "";
  $("risk-owner").value = "";
  $("risk-retest").value = "";
  // The accepter is named by their account's display name; only an account
  // without one types a name, and a typed name is never labelled the CxA.
  const mine = (cx.me && cx.me.display_name) || "";
  $("risk-name").value = mine;
  $("risk-name").readOnly = Boolean(mine);
  $("risk-name-hint").textContent = mine ? "from your account" : "your account has no display name (an admin sets it in Ops -> Users): type how you sign";
  $("risk-review").value = "";
  const ev = $("issue-events");
  ev.innerHTML = "";
  for (const e of issue.events) {
    const who = e.user_email || e.user_sub || "system";
    const det = e.detail || {};
    const extra = det.status ? ` → ${ISSUE_STATUS_LABEL[det.status] || det.status}` : det.run_id ? ` (run ${det.run_id.slice(0, 8)})` : "";
    ev.append(el("li", { textContent: `${when(e.ts)} · ${who} · ${e.action}${extra}${det.note ? ` - ${det.note}` : ""}${det.reason ? ` - ${det.reason}` : ""}` }));
  }
  issueError("");
  $("issue-dialog").showModal();
}

async function saveIssue(ev) {
  ev.preventDefault();
  const i = issueState.current;
  const body = {};
  const title = $("issue-title").value.trim();
  if (title !== i.title) body.title = title;
  if ($("issue-status").value !== i.status && i.status !== "risk_accepted") body.status = $("issue-status").value;
  if (($("issue-severity").value || null) !== (i.severity || null)) body.severity = $("issue-severity").value || null;
  if ($("issue-assignee").value.trim() !== (i.assignee || "")) body.assignee = $("issue-assignee").value.trim() || null;
  if ($("issue-note").value.trim()) body.note = $("issue-note").value.trim();
  if (!Object.keys(body).length) {
    $("issue-dialog").close();
    return;
  }
  try {
    await cxApi("PATCH", `/issues/${i.id}`, body);
    $("issue-dialog").close();
    await loadIssues();
  } catch (err) {
    issueError(err.message);
  }
}

async function acceptRisk() {
  const i = issueState.current;
  const missing = [["risk-reason", "a reason"], ["risk-owner", "an owner"], ["risk-retest", "a retest trigger"],
    ["risk-name", "who is accepting it"], ["risk-review", "a review-by date"]].filter(([id]) => !$(id).value.trim());
  if (missing.length) {
    issueError(`A waiver needs ${missing.map(([, what]) => what).join(", ")}.`);
    $(missing[0][0]).focus();
    return;
  }
  try {
    await cxApi("POST", `/issues/${i.id}/risk`, { reason: $("risk-reason").value.trim(), review_by: $("risk-review").value,
      owner: $("risk-owner").value.trim(), retest_trigger: $("risk-retest").value.trim(),
      accepted_name: $("risk-name").value.trim() || undefined });
    $("issue-dialog").close();
    await Promise.all([loadIssues(), loadRisks(), loadEvents()]);
  } catch (err) {
    issueError(err.message);
  }
}

async function newIssue() {
  const title = prompt("Issue title");
  if (!title) return;
  try {
    const issue = await cxApi("POST", `/projects/${cx.project.id}/issues`, { title });
    await loadIssues();
    openIssue(issue.id);
  } catch (err) {
    alert(err.message);
  }
}

async function issuesFromRun() {
  if (!cx.run) return;
  if (!confirm("Open an issue for every failing result in this run that isn't tracked yet?")) return;
  try {
    const got = await cxApi("POST", `/runs/${cx.run.id}/issues`, {});
    flash($("issues-status"), `${got.opened.length} opened, ${got.already_tracked} already tracked`);
    await Promise.all([loadIssues(), loadEvents()]);
  } catch (err) {
    alert(err.message);
  }
}

async function exportIssues(format) {
  try {
    const shape = $("export-shape").value;
    const res = await cxApi("GET", `/projects/${cx.project.id}/issues/export?format=${format}&shape=${shape}`, undefined, true);
    const url = URL.createObjectURL(await res.blob());
    const a = el("a", { href: url, download: `cx-issues-${shape}.${format}` });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  } catch (err) {
    alert(err.message);
  }
}

async function importIssues(ev) {
  ev.preventDefault();
  const file = $("import-file").files[0];
  const err = $("import-error");
  err.hidden = true;
  if (!file) return;
  const text = await file.text();
  const body = { source: $("import-source").value.trim() };
  if (/\.json$/i.test(file.name) || /^\s*[[{]/.test(text)) {
    try {
      const data = JSON.parse(text);
      body.items = Array.isArray(data) ? data : data.items || data.issues || [];
    } catch (e) {
      err.textContent = `not JSON: ${e.message}`;
      err.hidden = false;
      return;
    }
  } else {
    body.csv = text;
  }
  try {
    const got = await cxApi("POST", `/projects/${cx.project.id}/issues/import`, body);
    $("import-dialog").close();
    const um = got.unmatched_equipment.length ? `; equipment not matched: ${got.unmatched_equipment.slice(0, 5).join(", ")}${got.unmatched_equipment.length > 5 ? "…" : ""}` : "";
    flash($("issues-status"), `imported from ${got.source}: ${got.created} new, ${got.updated} updated, ${got.unchanged} unchanged${um}`);
    await Promise.all([loadIssues(), loadEvents()]);
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  }
}

$("issues-filter").addEventListener("change", loadIssues);
$("ingest-faults-btn").addEventListener("click", async () => {
  try {
    const got = await cxApi("POST", `/projects/${cx.project.id}/faults/ingest`, { source: "fault_detector" });
    flash($("issues-status"), `faults: ${got.created} new issues, ${got.resolved} closed (fault resolved), ${got.reopened} reopened`);
    await Promise.all([loadIssues(), loadEvents()]);
  } catch (err) {
    alert(err.message);
  }
});
$("new-issue-btn").addEventListener("click", newIssue);
$("issues-from-run-btn").addEventListener("click", issuesFromRun);
$("issue-form").addEventListener("submit", saveIssue);
$("issue-cancel").addEventListener("click", () => $("issue-dialog").close());
$("risk-accept-btn").addEventListener("click", acceptRisk);
$("export-csv-btn").addEventListener("click", () => exportIssues("csv"));
$("export-json-btn").addEventListener("click", () => exportIssues("json"));
$("import-issues-btn").addEventListener("click", () => {
  $("import-error").hidden = true;
  $("import-dialog").showModal();
});
$("import-form").addEventListener("submit", importIssues);
$("import-cancel").addEventListener("click", () => $("import-dialog").close());

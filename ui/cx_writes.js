// Audited operator writes (Baler#213): command a point with a reason, a
// level (8-16) and an auto-relinquish time; change a BACnet Schedule or
// Calendar (admin) with a reason and a revert time. Every write is
// recorded with who, why, level and expiry, and ends on time by itself.
// Loaded after cx.js; registers on cxProjectHooks.

const writesState = { rows: [], points: [], targets: [] };

cxProjectHooks.push(() => loadWrites());

async function loadWrites() {
  if (!cx.project) return;
  try {
    const got = await cxApi("GET", `/projects/${cx.project.id}/writes`);
    writesState.rows = got.writes;
    renderWrites();
    if (can("operator") && !$("write-point").options.length) await loadWritablePoints(got.levels);
  } catch (err) {
    $("writes-status").textContent = err.message;
  }
}

async function loadWritablePoints(levels) {
  const inv = await cxApi("GET", `/projects/${cx.project.id}/inventory`);
  const sel = $("write-point");
  sel.innerHTML = "";
  for (const e of inv.equipment) {
    for (const p of e.points) {
      if ((p.tags || []).includes("writable")) sel.append(el("option", { value: p.id, textContent: `${e.dis} / ${p.dis || p.id}` }));
    }
  }
  if (!sel.options.length) sel.append(el("option", { value: "", textContent: "no writable points in this project" }));
  const lv = $("write-level");
  lv.innerHTML = "";
  for (const n of levels || [8]) lv.append(el("option", { value: n, textContent: n === 8 ? "8 (manual operator)" : String(n) }));
}

function writeTarget(r) {
  if (r.kind === "point") return r.target.replace(/^urn:point:/, "");
  return `${r.kind} ${r.value ? r.value.object : ""} on ${r.connection_id}`;
}

function writeValue(r) {
  if (r.kind === "point") return String(r.value);
  return Object.keys((r.value && r.value.fields) || {}).join(", ");
}

function renderWrites() {
  const body = $("writes-body");
  body.innerHTML = "";
  for (const r of writesState.rows) {
    const tr = el("tr");
    const ended = r.ended_at ? ` (${when(r.ended_at)}${r.end_reason ? ": " + r.end_reason : ""})` : "";
    tr.innerHTML = `<td>${esc(when(r.created_at))}</td><td>${esc(r.who || "")}</td><td>${esc(writeTarget(r))}</td>
      <td>${esc(writeValue(r))}</td><td>${r.level ?? ""}</td><td>${esc(r.expires_at ? when(r.expires_at) : "permanent")}</td>
      <td class="${r.status === "failed" || r.status === "conflict" ? "st-fail" : r.status === "active" ? "st-pass" : ""}">${esc(r.status)}${esc(ended)}</td>
      <td>${esc(r.reason)}</td><td></td>`;
    const actions = tr.lastElementChild;
    if (r.kind === "point" && r.status === "active" && can("operator")) actions.append(writeButton("Relinquish", () => endWrite(r, "relinquish")));
    if (r.kind !== "point" && ["active", "permanent"].includes(r.status) && can("admin")) actions.append(writeButton("Revert", () => endWrite(r, "revert")));
    body.append(tr);
  }
  if (!writesState.rows.length) body.innerHTML = '<tr><td colspan="9" class="hint">No operator writes.</td></tr>';
}

function writeButton(text, fn) {
  const b = el("button", { type: "button", textContent: text });
  b.addEventListener("click", (ev) => { ev.stopPropagation(); fn(); });
  return b;
}

async function endWrite(r, how) {
  const reason = prompt(`${how === "revert" ? "Revert" : "Relinquish"} ${writeTarget(r)} now. Why?`);
  if (!reason) return;
  try {
    await cxApi("POST", `/writes/${r.id}/${how}`, { reason });
    await loadWrites();
  } catch (err) {
    alert(err.message);
  }
}

async function sendPointWrite() {
  const body = {
    point: $("write-point").value, value: Number($("write-value").value), level: Number($("write-level").value),
    hold_minutes: Number($("write-hold").value), reason: $("write-reason").value.trim(),
  };
  if (!body.reason) { alert("Say why: the reason is recorded with the write."); return; }
  if (!confirm(`Command ${body.point.replace(/^urn:point:/, "")} = ${body.value} at priority ${body.level} for ${body.hold_minutes} min?\nIt is relinquished automatically when the time is up.`)) return;
  try {
    await cxApi("POST", `/projects/${cx.project.id}/writes`, body);
  } catch (err) {
    if (err.status === 409 && /replace: true/.test(err.message) && confirm(`${err.message}\n\nTake the level anyway? Their value is kept on the record.`)) {
      await cxApi("POST", `/projects/${cx.project.id}/writes`, { ...body, replace: true }).catch((e) => alert(e.message));
    } else {
      alert(err.message);
    }
  }
  $("write-reason").value = "";
  await loadWrites();
}

async function loadScheduleTargets() {
  const site = $("sched-site").value.trim(), instance = $("sched-instance").value.trim();
  try {
    writesState.targets = await cxApi("GET", `/projects/${cx.project.id}/schedule-targets?site=${encodeURIComponent(site)}&instance=${encodeURIComponent(instance)}`);
  } catch (err) {
    alert(err.message);
    return;
  }
  const sel = $("sched-object");
  sel.innerHTML = "";
  for (const c of writesState.targets) {
    for (const o of [...c.schedules, ...c.calendars]) {
      sel.append(el("option", { value: `${c.connection_id}|${o.object}`, textContent: `${c.topic_prefix}: ${o.name || o.object} (${o.object})` }));
    }
  }
  pickScheduleObject();
}

function pickScheduleObject() {
  const [cid, obj] = ($("sched-object").value || "|").split("|");
  const conn = writesState.targets.find((c) => c.connection_id === cid);
  const o = conn && [...conn.schedules, ...conn.calendars].find((x) => x.object === obj);
  if (!o) return;
  const fields = obj.startsWith("calendar") ? { dates: o.dates } : { exceptions: o.exceptions };
  $("sched-fields").value = JSON.stringify(fields, null, 2);
}

async function sendScheduleWrite() {
  const [cid, obj] = ($("sched-object").value || "|").split("|");
  let fields;
  try {
    fields = JSON.parse($("sched-fields").value);
  } catch {
    alert("The change is JSON: {\"exceptions\": [...]}, {\"weekly\": {...}} or {\"dates\": [...]} for a calendar.");
    return;
  }
  const permanent = $("sched-permanent").checked;
  const hours = Number($("sched-revert").value);
  const body = {
    site: $("sched-site").value.trim(), instance: $("sched-instance").value.trim(), connection_id: cid, object: obj,
    kind: obj.startsWith("calendar") ? "calendar" : "schedule", reason: $("sched-reason").value.trim(), ...fields,
  };
  if (permanent) body.permanent = true;
  else body.revert_at = new Date(Date.now() + hours * 3600 * 1000).toISOString();
  if (!confirm(`Write ${obj} on ${cid}${permanent ? " permanently" : `, reverting in ${hours} h`}?`)) return;
  try {
    await cxApi("POST", `/projects/${cx.project.id}/schedule-writes`, body);
    $("sched-reason").value = "";
  } catch (err) {
    alert(err.message);
  }
  await loadWrites();
}

$("write-send-btn").addEventListener("click", sendPointWrite);
$("sched-load-btn").addEventListener("click", loadScheduleTargets);
$("sched-object").addEventListener("change", pickScheduleObject);
$("sched-send-btn").addEventListener("click", sendScheduleWrite);

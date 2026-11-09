// Axon-style history query console - a browser surface over ingest_api's
// GET /his (see hisquery.py for the grammar). No build step, no framework,
// no chart library: same buildless spirit as every other file in ui/. The
// expression in the text box is the single source of truth - the span
// chips and rollup select just rewrite it, the way a SkySpark user would
// edit `readAll(ahu).hisRead(today)` by hand.
const params = new URLSearchParams(location.search);
// Relative by default so this works as served by the gateway at /ui/ -
// override with ?ingest=... when serving ui/ standalone (ui_server.py).
const INGEST_API_URL = apiBase("ingest", "/ingest");
// Spans ("today", "thisMonth") resolve server-side in this zone, so what
// the chart calls today is the user's calendar day, not the container's.
const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const REFRESH_MS = 10000;
const GRID_ROW_CAP = 2000;
const PALETTE = ["#2b6cb0", "#dd6b20", "#2f855a", "#b83280", "#6b46c1", "#c53030", "#0987a0", "#b7791f", "#4a5568", "#d69e2e"];

const statusEl = document.getElementById("status");
const exprEl = document.getElementById("expr");
const formEl = document.getElementById("query-form");
const runBtn = document.getElementById("run");
const errorEl = document.getElementById("error");
const summaryEl = document.getElementById("summary");
const resultsEl = document.getElementById("results");
const rollupEl = document.getElementById("rollup");
const liveEl = document.getElementById("live");
const spanChipsEl = document.getElementById("span-chips");
document.getElementById("tz").textContent = TZ;

const state = { view: "unit", result: null, hidden: new Set(), timer: null, charts: [] };

// --- expression rewriting ------------------------------------------------
// hisRead's argument may itself contain parens (today(), thisMonth()).
const HISREAD_RE = /\.hisRead\((?:[^()]|\([^()]*\))*\)/;
const HISROLLUP_RE = /\.hisRollup\([^)]*\)/;
const CHAIN_RE = /\.his(?:Read|Rollup)\(/;

// A bare filter ("air and temp") is valid input for the API, but the chips
// need something to hang .hisRead()/.hisRollup() off - wrap it in readAll().
function ensureReadAll(expr) {
  expr = expr.trim();
  if (!expr) return "readAll(point)";
  if (/^(read|readAll)\s*\(/.test(expr)) return expr;
  const at = expr.search(CHAIN_RE);
  const filter = (at >= 0 ? expr.slice(0, at) : expr).trim();
  const chain = at >= 0 ? expr.slice(at) : "";
  return `readAll(${filter})${chain}`;
}

// Swap one filter name for another, whole-token only (so fixing `temp`
// never touches `temperature`); used by the did-you-mean links.
function replaceName(expr, from, to) {
  const escaped = from.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return expr.replace(new RegExp(`(^|[^A-Za-z0-9_])${escaped}(?![A-Za-z0-9_])`, "g"), `$1${to}`);
}

function code(text) {
  const el = document.createElement("code");
  el.textContent = text;
  return el;
}

function withSpan(expr, span) {
  expr = ensureReadAll(expr);
  if (HISREAD_RE.test(expr)) return expr.replace(HISREAD_RE, `.hisRead(${span})`);
  const m = expr.match(HISROLLUP_RE);
  if (m) return `${expr.slice(0, m.index)}.hisRead(${span})${expr.slice(m.index)}`;
  return `${expr}.hisRead(${span})`;
}

function withRollup(expr, rollup) {
  expr = ensureReadAll(expr);
  if (HISROLLUP_RE.test(expr)) return expr.replace(HISROLLUP_RE, rollup ? `.hisRollup(${rollup})` : "");
  return rollup ? `${expr}.hisRollup(${rollup})` : expr;
}

function currentSpan(expr) {
  const m = expr.match(/\.hisRead\(((?:[^()]|\([^()]*\))*)\)/);
  return m ? m[1].trim().replace(/\(\)$/, "") : null;
}

function currentRollup(expr) {
  const m = expr.match(/\.hisRollup\(([^)]*)\)/);
  return m ? m[1].replace(/\s*,\s*/, ", ").trim() : "";
}

// Reflect whatever's in the box back onto the chips/select, so typing
// `.hisRead(lastMonth)` by hand lights up the lastMonth chip and vice versa.
function syncControls() {
  const expr = exprEl.value;
  const span = currentSpan(expr);
  for (const btn of spanChipsEl.querySelectorAll("button[data-span]")) {
    btn.classList.toggle("active", btn.dataset.span === span);
  }
  const rollup = currentRollup(expr);
  rollupEl.value = [...rollupEl.options].some((o) => o.value === rollup) ? rollup : "";
}

// --- fetching --------------------------------------------------------------

function showError(message) {
  errorEl.textContent = message;
  errorEl.hidden = false;
}

const MAX_SAMPLES = 200000;

async function run({ silent = false } = {}) {
  const expr = exprEl.value.trim();
  if (!expr) return;
  if (!silent) {
    runBtn.disabled = true;
    statusEl.textContent = "running…";
  }
  // maxSamples: past ~200k values in total the server averages into
  // buckets instead of shipping raw samples a chart can't draw anyway.
  // compact=1: history as parallel ts/value arrays (half the bytes, Baler#107),
  // turned back into the {ts, value} rows the page draws from below.
  const url = `${INGEST_API_URL}/his?expr=${encodeURIComponent(expr)}&tz=${encodeURIComponent(TZ)}&maxSamples=${MAX_SAMPLES}&compact=1`;
  try {
    const res = await fetch(url, { headers: authHeaders() });
    if (res.status === 401) {
      handleUnauthorized();
      return;
    }
    const body = await res.json();
    if (!res.ok) {
      showError(body.error || `request failed: ${res.status}`);
      statusEl.textContent = `error (${res.status})`;
      return;
    }
    errorEl.hidden = true;
    for (const s of body.series || []) {
      if (s.history && !Array.isArray(s.history)) s.history = s.history.ts.map((ts, i) => ({ ts, value: s.history.value[i] }));
    }
    state.result = body;
    // Shareable: the address bar always holds the query that's on screen.
    history.replaceState(null, "", `?expr=${encodeURIComponent(expr)}`);
    render();
  } catch (err) {
    showError(err.message);
    statusEl.textContent = "error";
  } finally {
    runBtn.disabled = false;
    scheduleLive();
  }
}

// Re-run on a timer while the span still includes "now" - a finished span
// (yesterday, lastMonth) can't change, so don't hammer the API for it.
function scheduleLive() {
  clearTimeout(state.timer);
  state.timer = null;
  const r = state.result;
  if (!liveEl.checked || !r || r.span.end * 1000 <= Date.now()) return;
  state.timer = setTimeout(() => run({ silent: true }), REFRESH_MS);
}

// --- formatting ------------------------------------------------------------

const fmtNum = (v) => (typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : String(v));
const fmtValue = (v, unit) => (v === null || v === undefined ? "—" : typeof v === "boolean" ? (v ? "on" : "off") : `${fmtNum(v)}${unit ? ` ${unit}` : ""}`);
const fmtTime = (ts) => new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: undefined });
const fmtDate = (ts) => new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
const fmtClock = (ts) => new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
const seriesLabel = (s) => (s.equipDis ? `${s.equipDis} · ${s.dis}` : s.dis);

function numericStats(history) {
  let min = Infinity, max = -Infinity, sum = 0, n = 0, last = null;
  for (const row of history) {
    const v = typeof row.value === "boolean" ? (row.value ? 1 : 0) : row.value;
    if (typeof v !== "number" || Number.isNaN(v)) continue;
    if (v < min) min = v;
    if (v > max) max = v;
    sum += v;
    n++;
    last = row.value;
  }
  return n ? { min, max, avg: sum / n, last, n } : null;
}

// --- rendering ------------------------------------------------------------

function render() {
  const r = state.result;
  const span = r.span;
  const pointCount = r.series.length;
  const withData = r.series.filter((s) => s.history.length).length;
  const truncated = r.series.filter((s) => s.truncated).length;
  statusEl.textContent = `${pointCount} point${pointCount === 1 ? "" : "s"}`;

  summaryEl.hidden = false;
  summaryEl.innerHTML = "";
  const parts = [
    `<span><code>${escapeHtml(r.filter)}</code> matched <strong>${r.matchedCount}</strong> ${r.matchedCount === 1 ? "entity" : "entities"} → <strong>${pointCount}</strong> point${pointCount === 1 ? "" : "s"} (${withData} with data)</span>`,
    `<span><strong>${escapeHtml(span.label)}</strong>: ${fmtTime(span.start)} – ${fmtTime(span.end)} (${escapeHtml(span.tz)})</span>`,
  ];
  if (r.rollup && r.rollup.auto) {
    parts.push(`<span>averaged per <strong>${escapeHtml(r.rollup.interval)}</strong> to fit the chart - narrow the filter or span, or pick a rollup, for raw samples (text points have no average)</span>`);
  } else if (r.rollup) {
    parts.push(`<span>rollup <strong>${escapeHtml(r.rollup.fold)}</strong> per <strong>${escapeHtml(r.rollup.interval)}</strong></span>`);
  }
  if (truncated) parts.push(`<span class="field-error">${truncated} series capped at ${r.limit.toLocaleString()} samples - add a .hisRollup(...) for long spans</span>`);
  // The server reads at most seriesLimit points per request (Baler#244);
  // this page charts the first of them, in point-id order.
  if (r.after) parts.push(`<span class="field-error">showing the first ${pointCount.toLocaleString()} of ${r.pointCount.toLocaleString()} points (at most ${r.seriesLimit.toLocaleString()} per query) - narrow the filter to chart the rest</span>`);
  summaryEl.insertAdjacentHTML("beforeend", parts.map((p) => p).join(""));
  if (r.matched.length) {
    const details = document.createElement("details");
    details.innerHTML = `<summary>matched entities</summary><ul>${r.matched.map((u) => `<li>${escapeHtml(u)}</li>`).join("")}${r.matchedCount > r.matched.length ? `<li>… ${r.matchedCount - r.matched.length} more</li>` : ""}</ul>`;
    summaryEl.appendChild(details);
  }

  state.charts = [];
  resultsEl.innerHTML = "";
  if (!pointCount) {
    resultsEl.innerHTML = `<p class="hint">Nothing to chart - the filter matched no points. Try the words a class name is made of (<code>zone and air and temperature</code>), a Brick class in any case (<code>air_handling_unit</code>, <code>Temperature_Sensor</code> - subclasses included), a Haystack marker (<code>ahu</code>, <code>temp</code>), or <code>point</code> for everything.</p>`;
    for (const hint of r.hints || []) {
      const p = document.createElement("p");
      p.className = "hint did-you-mean";
      p.append(`No Brick class or tag word spelled `, code(hint.token), ` - did you mean `);
      hint.suggestions.forEach((s, i) => {
        if (i) p.append(", ");
        const a = document.createElement("a");
        a.href = "#";
        a.textContent = s;
        a.addEventListener("click", (ev) => {
          ev.preventDefault();
          exprEl.value = replaceName(exprEl.value, hint.token, s);
          run();
        });
        p.appendChild(a);
      });
      p.append("?");
      resultsEl.appendChild(p);
    }
    return;
  }
  if (state.view === "grid") renderGrid(r);
  else if (state.view === "point") renderPerPoint(r);
  else renderByUnit(r);
}

const isBool = (s) => s.kind === "Bool";
const isStr = (s) => s.kind === "Str";

function renderByUnit(r) {
  const groups = new Map();
  for (const s of r.series) {
    if (isBool(s) || isStr(s)) continue;
    const key = s.unit || "";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  }
  const ordered = [...groups.entries()].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));
  ordered.forEach(([unit, list], i) => {
    list.forEach((s, j) => (s.color = PALETTE[(i * 3 + j) % PALETTE.length]));
    resultsEl.appendChild(chartCard({ title: unit || "no unit", subtitle: `${list.length} point${list.length === 1 ? "" : "s"}`, series: list, unit, r }));
  });
  for (const s of r.series.filter(isBool)) {
    s.color = PALETTE[0];
    resultsEl.appendChild(chartCard({ title: seriesLabel(s), subtitle: s.brickClass || "Bool", series: [s], unit: null, r, bool: true }));
  }
  for (const s of r.series.filter(isStr)) resultsEl.appendChild(strCard(s));
}

function renderPerPoint(r) {
  r.series.forEach((s, i) => {
    if (isStr(s)) {
      resultsEl.appendChild(strCard(s));
      return;
    }
    s.color = PALETTE[i % PALETTE.length];
    resultsEl.appendChild(chartCard({ title: seriesLabel(s), subtitle: [s.brickClass, s.unit].filter(Boolean).join(" · "), series: [s], unit: s.unit, r, bool: isBool(s), compact: true }));
  });
}

function chartCard({ title, subtitle, series, unit, r, bool = false, compact = false }) {
  const card = document.createElement("div");
  card.className = `chart-card${bool ? " bool" : ""}${compact ? " compact" : ""}`;
  const h3 = document.createElement("h3");
  h3.textContent = title;
  if (subtitle) h3.insertAdjacentHTML("beforeend", ` <span class="hint">${escapeHtml(subtitle)}</span>`);
  card.appendChild(h3);

  const canvas = document.createElement("canvas");
  card.appendChild(canvas);
  const tooltip = document.createElement("div");
  tooltip.className = "tooltip";
  card.appendChild(tooltip);

  const legend = document.createElement("ul");
  legend.className = "legend";
  for (const s of series) {
    const li = document.createElement("li");
    li.classList.toggle("muted", state.hidden.has(s.id));
    const stats = numericStats(s.history);
    const link = `his.html?expr=${encodeURIComponent(`readAll(id==@${s.id}).hisRead(${r.span.label})`)}`;
    // Click anywhere on the row to hide/show the line; the small ↗ at the
    // end is the only thing that navigates (to this point on its own).
    li.title = `${s.id}${s.brickClass ? `\n${s.brickClass}` : ""}\n${s.tags.join(" ")}\n(click to hide/show)`;
    li.innerHTML =
      `<span class="swatch" style="background:${s.color}"></span>` +
      `<span class="name">${escapeHtml(seriesLabel(s))}</span>` +
      (stats
        ? ` <span class="last">${escapeHtml(fmtValue(stats.last, unit))}</span>` +
          (bool || typeof stats.last === "boolean" ? "" : ` <span class="stats">min ${fmtNum(stats.min)} · avg ${fmtNum(stats.avg)} · max ${fmtNum(stats.max)}</span>`)
        : ` <span class="stats">no data in span</span>`) +
      (s.truncated ? ` <span class="field-error">truncated</span>` : "") +
      ` <a href="${link}" class="open-one" title="Open just this point">↗</a>`;
    li.addEventListener("click", (ev) => {
      if (ev.target.closest("a")) return;
      if (state.hidden.has(s.id)) state.hidden.delete(s.id);
      else state.hidden.add(s.id);
      li.classList.toggle("muted");
      chart.draw();
    });
    legend.appendChild(li);
  }
  card.appendChild(legend);

  const chart = new TimeChart(canvas, tooltip, series, { start: r.span.start, end: r.span.end, unit, bool });
  state.charts.push(chart);
  // Canvas needs to be in the DOM (with a laid-out width) before drawing.
  requestAnimationFrame(() => chart.draw());
  return card;
}

function strCard(s) {
  const card = document.createElement("div");
  card.className = "chart-card compact";
  card.innerHTML = `<h3>${escapeHtml(seriesLabel(s))} <span class="hint">${escapeHtml(s.brickClass || "Str")}</span></h3>`;
  // A string point is state, not a signal - list its transitions instead
  // of pretending there's a line to draw.
  const transitions = [];
  let prev;
  for (const row of s.history) {
    if (row.value !== prev) transitions.push(row);
    prev = row.value;
  }
  const ul = document.createElement("ul");
  ul.className = "str-list";
  for (const row of transitions.slice(-50).reverse()) {
    const li = document.createElement("li");
    li.textContent = `${fmtTime(row.ts)} — ${row.value}`;
    ul.appendChild(li);
  }
  if (!transitions.length) ul.innerHTML = `<li class="hint">no data in span</li>`;
  card.appendChild(ul);
  return card;
}

// --- his grid (ts + one column per point) + CSV ------------------------------

function buildGrid(r) {
  const tsSet = new Set();
  const cols = r.series.map((s) => {
    const byTs = new Map();
    for (const row of s.history) {
      byTs.set(row.ts, row.value);
      tsSet.add(row.ts);
    }
    return { s, byTs };
  });
  const tss = [...tsSet].sort((a, b) => a - b);
  return { cols, tss };
}

function renderGrid(r) {
  const { cols, tss } = buildGrid(r);
  const toolbar = document.createElement("div");
  toolbar.className = "grid-toolbar";
  toolbar.innerHTML = `<span>${tss.length.toLocaleString()} rows × ${cols.length} points${tss.length > GRID_ROW_CAP ? ` (showing first ${GRID_ROW_CAP.toLocaleString()}; CSV has everything)` : ""}</span>`;
  const dl = document.createElement("button");
  dl.type = "button";
  dl.textContent = "Download CSV";
  dl.addEventListener("click", () => downloadCsv(r, cols, tss));
  toolbar.appendChild(dl);
  resultsEl.appendChild(toolbar);

  const wrap = document.createElement("div");
  wrap.className = "grid-wrap";
  const table = document.createElement("table");
  const head = `<tr><th>ts</th>${cols.map(({ s }, i) => `<th title="${escapeHtml(s.id)}">${escapeHtml(s.dis)}<small>v${i}${s.unit ? ` · ${escapeHtml(s.unit)}` : ""}${s.equipDis ? ` · ${escapeHtml(s.equipDis)}` : ""}</small></th>`).join("")}</tr>`;
  const rows = tss.slice(0, GRID_ROW_CAP).map((ts) => `<tr><td>${fmtTime(ts)}</td>${cols.map(({ byTs }) => `<td>${byTs.has(ts) ? escapeHtml(fmtValue(byTs.get(ts), null)) : ""}</td>`).join("")}</tr>`);
  table.innerHTML = `<thead>${head}</thead><tbody>${rows.join("")}</tbody>`;
  wrap.appendChild(table);
  resultsEl.appendChild(wrap);
}

function downloadCsv(r, cols, tss) {
  const q = (v) => `"${String(v).replace(/"/g, '""')}"`;
  const lines = [["ts", ...cols.map(({ s }) => `${s.dis}${s.unit ? ` (${s.unit})` : ""}`)].map(q).join(",")];
  for (const ts of tss) {
    lines.push([new Date(ts * 1000).toISOString(), ...cols.map(({ byTs }) => (byTs.has(ts) ? byTs.get(ts) : ""))].map(q).join(","));
  }
  const blob = new Blob([lines.join("\n")], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${r.filter.replace(/[^A-Za-z0-9_-]+/g, "_")}-${r.span.label.replace(/[^A-Za-z0-9_-]+/g, "_")}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

// --- the chart ---------------------------------------------------------------

const PAD = { left: 52, right: 14, top: 10, bottom: 24 };
// Candidate x-axis tick spacings, seconds. Picked so a span gets ~5-10 ticks.
const TICK_STEPS = [60, 300, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800, 1209600, 2592000];

class TimeChart {
  constructor(canvas, tooltip, series, opts) {
    this.canvas = canvas;
    this.tooltip = tooltip;
    this.series = series;
    this.opts = opts;
    this.hoverT = null;
    canvas.addEventListener("mousemove", (ev) => this.onMove(ev));
    canvas.addEventListener("mouseleave", () => {
      this.hoverT = null;
      this.tooltip.style.display = "none";
      this.draw();
    });
  }

  visible() {
    return this.series.filter((s) => !state.hidden.has(s.id));
  }

  xOf(t, w) {
    const { start, end } = this.opts;
    return PAD.left + ((t - start) / (end - start || 1)) * w;
  }

  yRange() {
    if (this.opts.bool) return [0, 1];
    let min = Infinity, max = -Infinity;
    for (const s of this.visible()) {
      for (const row of s.history) {
        const v = typeof row.value === "boolean" ? (row.value ? 1 : 0) : row.value;
        if (typeof v !== "number") continue;
        if (v < min) min = v;
        if (v > max) max = v;
      }
    }
    if (min === Infinity) return [0, 1];
    if (min === max) return [min - 1, max + 1];
    const padV = (max - min) * 0.06;
    return [min - padV, max + padV];
  }

  draw() {
    const canvas = this.canvas;
    const dpr = window.devicePixelRatio || 1;
    const cw = canvas.clientWidth, ch = canvas.clientHeight;
    if (!cw || !ch) return;
    canvas.width = Math.round(cw * dpr);
    canvas.height = Math.round(ch * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cw, ch);

    const w = cw - PAD.left - PAD.right;
    const h = ch - PAD.top - PAD.bottom;
    const [minV, maxV] = this.yRange();
    const yOf = (v) => PAD.top + h - ((v - minV) / (maxV - minV || 1)) * h;
    const textColor = getComputedStyle(document.body).color;
    const gridColor = getComputedStyle(document.documentElement).getPropertyValue("--border").trim() || "#ccc";
    ctx.font = "11px -apple-system, 'Segoe UI', sans-serif";

    // y axis: ~4 "nice" ticks
    ctx.strokeStyle = gridColor;
    ctx.fillStyle = textColor;
    ctx.lineWidth = 1;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    if (this.opts.bool) {
      for (const [v, label] of [[0, "off"], [1, "on"]]) {
        ctx.fillText(label, PAD.left - 6, yOf(v));
      }
    } else {
      const step = niceStep((maxV - minV) / 4);
      for (let v = Math.ceil(minV / step) * step; v <= maxV + 1e-9; v += step) {
        const y = yOf(v);
        ctx.beginPath();
        ctx.moveTo(PAD.left, y);
        ctx.lineTo(PAD.left + w, y);
        ctx.stroke();
        ctx.fillText(fmtNum(+v.toFixed(6)), PAD.left - 6, y);
      }
    }

    // x axis
    const { start, end } = this.opts;
    const spanS = end - start;
    const stepS = TICK_STEPS.find((s) => spanS / s <= 10) || TICK_STEPS[TICK_STEPS.length - 1];
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const startDate = new Date(start * 1000);
    // Align ticks to local midnight so day boundaries land on a tick.
    const localMidnight = new Date(startDate.getFullYear(), startDate.getMonth(), startDate.getDate()).getTime() / 1000;
    let lastDay = null;
    for (let t = localMidnight; t <= end; t += stepS) {
      if (t < start) continue;
      const x = this.xOf(t, w);
      ctx.strokeStyle = gridColor;
      ctx.beginPath();
      ctx.moveTo(x, PAD.top);
      ctx.lineTo(x, PAD.top + h);
      ctx.stroke();
      const d = new Date(t * 1000);
      const dayKey = d.toDateString();
      const label = stepS >= 86400 ? fmtDate(t) : dayKey !== lastDay ? `${fmtDate(t)} ${fmtClock(t)}` : fmtClock(t);
      lastDay = dayKey;
      ctx.fillStyle = textColor;
      ctx.fillText(label, x, PAD.top + h + 6);
    }
    // frame
    ctx.strokeStyle = gridColor;
    ctx.strokeRect(PAD.left, PAD.top, w, h);

    // series
    for (const s of this.visible()) {
      if (!s.history.length) continue;
      ctx.strokeStyle = s.color;
      ctx.fillStyle = s.color;
      ctx.lineWidth = 1.5;
      ctx.lineJoin = "round";
      ctx.beginPath();
      const stepped = this.opts.bool || typeof s.history[0].value === "boolean";
      // A gap far longer than the usual spacing is missing data: lift the
      // pen there rather than draw a line that looks like readings.
      const gap = stepped ? Infinity : gapThreshold(s.history);
      let prevY = null;
      let prevTs = null;
      let runLen = 0; // samples in the current unbroken run
      const lone = []; // samples joined to no neighbour (Baler#55)
      let runStart = null;
      const endRun = () => {
        if (runLen === 1 && runStart) lone.push(runStart);
      };
      s.history.forEach((row) => {
        const v = typeof row.value === "boolean" ? (row.value ? 1 : 0) : row.value;
        if (typeof v !== "number") return;
        const x = this.xOf(row.ts, w);
        const y = yOf(v);
        if (prevY === null || row.ts - prevTs > gap) {
          endRun();
          ctx.moveTo(x, y);
          runLen = 1;
          runStart = [x, y];
        } else if (stepped) {
          ctx.lineTo(x, prevY);
          ctx.lineTo(x, y);
          runLen++;
        } else {
          ctx.lineTo(x, y);
          runLen++;
        }
        prevY = y;
        prevTs = row.ts;
      });
      if (!stepped) endRun();
      if (stepped) {
        // Carry the last state to the right edge (or "now", whichever is
        // first) so an on/off strip reads as a state, not a dot.
        const lastRow = s.history[s.history.length - 1];
        const edge = Math.min(end, Date.now() / 1000);
        if (edge > lastRow.ts && prevY !== null) ctx.lineTo(this.xOf(edge, w), prevY);
      }
      ctx.stroke();
      // A single sample (a source's first reading) or one isolated between
      // gaps is a path with no length - it draws nothing. Mark it.
      for (const [x, y] of lone) {
        ctx.beginPath();
        ctx.arc(x, y, 3, 0, Math.PI * 2);
        ctx.fill();
      }
      if (stepped) {
        ctx.globalAlpha = 0.15;
        ctx.lineTo(this.xOf(Math.min(end, Date.now() / 1000), w), yOf(0));
        ctx.lineTo(this.xOf(s.history[0].ts, w), yOf(0));
        ctx.closePath();
        ctx.fill();
        ctx.globalAlpha = 1;
      }
    }

    // hover crosshair
    if (this.hoverT !== null) {
      const x = this.xOf(this.hoverT, w);
      ctx.strokeStyle = textColor;
      ctx.globalAlpha = 0.5;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(x, PAD.top);
      ctx.lineTo(x, PAD.top + h);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;
      for (const s of this.visible()) {
        const row = nearest(s.history, this.hoverT);
        if (!row) continue;
        const v = typeof row.value === "boolean" ? (row.value ? 1 : 0) : row.value;
        if (typeof v !== "number") continue;
        ctx.fillStyle = s.color;
        ctx.beginPath();
        ctx.arc(this.xOf(row.ts, w), yOf(v), 3.5, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }

  onMove(ev) {
    const rect = this.canvas.getBoundingClientRect();
    const w = rect.width - PAD.left - PAD.right;
    const frac = (ev.clientX - rect.left - PAD.left) / w;
    if (frac < 0 || frac > 1) {
      this.hoverT = null;
      this.tooltip.style.display = "none";
      this.draw();
      return;
    }
    const { start, end } = this.opts;
    this.hoverT = start + frac * (end - start);
    const rows = this.visible()
      .map((s) => ({ s, row: nearest(s.history, this.hoverT) }))
      .filter(({ row }) => row);
    if (!rows.length) {
      this.tooltip.style.display = "none";
      this.draw();
      return;
    }
    // Show the timestamp of the sample nearest the cursor, not the cursor's
    // own interpolated time - that's what the values belong to.
    const tsShown = rows.reduce((best, { row }) => (Math.abs(row.ts - this.hoverT) < Math.abs(best - this.hoverT) ? row.ts : best), rows[0].row.ts);
    this.tooltip.innerHTML =
      `<div class="t">${fmtTime(tsShown)}</div>` +
      rows
        .map(({ s, row }) => `<div class="row"><span class="swatch" style="background:${s.color}"></span>${escapeHtml(s.dis)} <strong>${escapeHtml(fmtValue(row.value, s.unit))}</strong></div>`)
        .join("");
    const card = this.canvas.parentElement;
    const cardRect = card.getBoundingClientRect();
    const left = ev.clientX - cardRect.left + 14;
    this.tooltip.style.display = "block";
    const flip = left + this.tooltip.offsetWidth > cardRect.width - 8;
    this.tooltip.style.left = `${flip ? left - this.tooltip.offsetWidth - 28 : left}px`;
    this.tooltip.style.top = `${ev.clientY - cardRect.top + 12}px`;
    this.draw();
  }
}

// Binary search for the sample closest in time - history is oldest-first.
function nearest(history, t) {
  if (!history.length) return null;
  let lo = 0, hi = history.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (history[mid].ts < t) lo = mid + 1;
    else hi = mid;
  }
  const a = history[lo], b = history[lo - 1];
  return b && Math.abs(b.ts - t) < Math.abs(a.ts - t) ? b : a;
}

// Seconds between samples above which the line breaks: 5x the median
// spacing, at least a minute (the same rule as chart.js's drawLineChart).
function gapThreshold(history) {
  const steps = [];
  for (let i = 1; i < history.length; i++) {
    const d = history[i].ts - history[i - 1].ts;
    if (d > 0) steps.push(d);
  }
  if (!steps.length) return Infinity;
  steps.sort((a, b) => a - b);
  return Math.max(steps[Math.floor(steps.length / 2)] * 5, 60);
}

function niceStep(raw) {
  if (!raw || !Number.isFinite(raw)) return 1;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const n = raw / mag;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * mag;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// --- wiring -------------------------------------------------------------------

formEl.addEventListener("submit", (ev) => {
  ev.preventDefault();
  run();
});
exprEl.addEventListener("input", syncControls);

spanChipsEl.addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-span]");
  if (!btn) return;
  exprEl.value = withSpan(exprEl.value, btn.dataset.span);
  syncControls();
  run();
});

rollupEl.addEventListener("change", () => {
  exprEl.value = withRollup(exprEl.value, rollupEl.value);
  syncControls();
  run();
});

document.querySelectorAll(".view-tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.view = btn.dataset.view;
    document.querySelectorAll(".view-tabs button").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
    if (state.result) render();
  });
});

document.querySelectorAll("a[data-example]").forEach((a) => {
  a.addEventListener("click", (ev) => {
    ev.preventDefault();
    exprEl.value = a.dataset.example;
    syncControls();
    run();
  });
});

liveEl.addEventListener("change", scheduleLive);
window.addEventListener("resize", () => state.charts.forEach((c) => c.draw()));

function init() {
  statusEl.textContent = "ready";
  const initial = params.get("expr");
  if (initial) {
    exprEl.value = initial;
    syncControls();
    run();
  } else {
    exprEl.focus();
  }
}

initAuth(init);

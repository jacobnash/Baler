// No build step, no framework - same spirit as every other file in ui/.
// Extracted out of app.js (originally built for point-history charts on
// the equipment-tree page) so derivations.js can reuse it for test-case
// input-series charts without duplicating it.
// pad defaults to 30 (app.js's original 640x220 point-history chart); a
// smaller canvas (e.g. derivations.js's 220x70 test-case sparklines) needs
// a smaller pad passed explicitly or the plot area all but disappears.

// Value at quantile q (0..1) of an ascending-sorted array.
function quantile(sorted, q) {
  const i = Math.min(sorted.length - 1, Math.max(0, Math.round(q * (sorted.length - 1))));
  return sorted[i];
}

// The y-range to draw (Baler#61). One bad sample (86116 on a ~87 degF
// discharge temperature) used to set the whole axis, flattening the real
// signal into a line along the bottom. Scale to the 1st-99th percentile
// instead when the full range is far wider than that (more than 3x), and
// report how many samples fall outside so the chart can mark them and
// offer the full range. `full` forces min..max.
function chartRange(values, full = false) {
  const sorted = values.filter((v) => Number.isFinite(v)).sort((a, b) => a - b);
  if (!sorted.length) return { min: 0, max: 1, clipped: false, outliers: 0, fullMin: 0, fullMax: 1 };
  const fullMin = sorted[0];
  const fullMax = sorted[sorted.length - 1];
  const result = { min: fullMin, max: fullMax, clipped: false, outliers: 0, fullMin, fullMax };
  if (full || sorted.length < 20) return result;
  let lo = quantile(sorted, 0.01);
  let hi = quantile(sorted, 0.99);
  // Headroom so the band's own extremes aren't drawn on the frame.
  const margin = (hi - lo) * 0.1 || Math.max(Math.abs(hi) * 0.05, 1);
  lo -= margin;
  hi += margin;
  if (fullMax - fullMin <= (hi - lo) * 3) return result;
  lo = Math.max(lo, fullMin);
  hi = Math.min(hi, fullMax);
  const outliers = sorted.filter((v) => v < lo || v > hi).length;
  return { min: lo, max: hi, clipped: outliers > 0, outliers, fullMin, fullMax };
}

// Returns {clipped, outliers, fullMin, fullMax} so a caller can say
// "N samples off the scale" and offer opts.fullRange. opts.states names the
// 0 and 1 of a two-state point (["off", "on"]): the axis is labelled with
// them and the line is stepped, since a status holds until it changes.
function drawLineChart(canvas, history, pad = 30, unit = "", opts = {}) {
  const ctx = canvas.getContext("2d");
  const w = canvas.width - pad * 2;
  const h = canvas.height - pad * 2;

  const values = history.map((r) => r.value);
  const times = history.map((r) => r.ts);
  const range = opts.states
    ? { min: 0, max: 1, clipped: false, outliers: 0, fullMin: 0, fullMax: 1 }
    : chartRange(values, !!opts.fullRange);
  const minV = range.min;
  const maxV = range.max;
  const spanV = maxV - minV || 1;
  // A caller's window (start/end) wins, so "last 24 h" means the axis
  // spans 24 h even when the samples cover less of it.
  const minT = opts.start !== undefined ? opts.start : Math.min(...times);
  const maxT = opts.end !== undefined ? opts.end : Math.max(...times);
  const spanT = maxT - minT || 1;

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.strokeStyle = "currentColor";
  ctx.globalAlpha = 0.3;
  ctx.strokeRect(pad, pad, w, h);
  ctx.globalAlpha = 1;

  ctx.fillStyle = "currentColor";
  ctx.font = "11px sans-serif";
  const fmtV = (v) => (opts.states ? opts.states[v ? 1 : 0] : `${+v.toFixed(2)}${unit ? ` ${unit}` : ""}`);
  ctx.fillText(fmtV(maxV), 2, pad - 4);
  ctx.fillText(fmtV(minV), 2, pad + h + 12);

  // Time axis: start, middle, end - only where there's room (the
  // derivations page's sparklines pass a small pad and skip it).
  if (pad >= 20 && spanT > 1) {
    const sameDay = new Date(minT * 1000).toDateString() === new Date(maxT * 1000).toDateString();
    const fmtT = (t) => {
      const d = new Date(t * 1000);
      return sameDay ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
        : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    };
    const y = pad + h + 24;
    ctx.textAlign = "left";
    ctx.fillText(fmtT(minT), pad, y);
    ctx.textAlign = "center";
    ctx.fillText(fmtT(minT + spanT / 2), pad + w / 2, y);
    ctx.textAlign = "right";
    ctx.fillText(fmtT(maxT), pad + w, y);
    ctx.textAlign = "left";
  }

  // A gap much longer than the usual sample spacing is missing data: lift
  // the pen instead of drawing a straight line that looks like readings.
  const steps = times.slice(1).map((t, i) => t - times[i]).filter((d) => d > 0).sort((a, b) => a - b);
  const typical = steps.length ? steps[Math.floor(steps.length / 2)] : spanT;
  const gap = Math.max(typical * 5, 60);

  // Reads the real --accent token instead of a second hardcoded hex, so a
  // theme change to style.css's token doesn't leave this one line stale.
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#2b6cb0";
  const xOf = (t) => pad + ((t - minT) / spanT) * w;
  // Off-scale samples are pinned to the frame edge, drawn as a triangle
  // pointing the way the real value lies.
  const clampV = (v) => Math.min(maxV, Math.max(minV, v));
  const yOf = (v) => pad + h - ((clampV(v) - minV) / spanV) * h;
  const breakBefore = (i) => i === 0 || history[i].ts - history[i - 1].ts > gap;

  ctx.save();
  ctx.beginPath();
  ctx.rect(pad, pad, w, h);
  ctx.clip();
  ctx.strokeStyle = accent;
  ctx.fillStyle = accent;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  history.forEach((row, i) => {
    const x = xOf(row.ts);
    const y = yOf(row.value);
    if (breakBefore(i)) ctx.moveTo(x, y);
    else {
      if (opts.states) ctx.lineTo(x, yOf(history[i - 1].value));
      ctx.lineTo(x, y);
    }
  });
  ctx.stroke();
  // A sample joined to neither neighbour (the only sample, or one alone
  // between gaps) would draw nothing at all as a path - give it a dot.
  history.forEach((row, i) => {
    if (!breakBefore(i) || (i + 1 < history.length && !breakBefore(i + 1))) return;
    ctx.beginPath();
    ctx.arc(xOf(row.ts), yOf(row.value), 2.5, 0, Math.PI * 2);
    ctx.fill();
  });
  if (range.clipped) {
    ctx.fillStyle = "#c53030";
    for (const row of history) {
      if (row.value >= minV && row.value <= maxV) continue;
      const x = xOf(row.ts);
      const up = row.value > maxV;
      const y = up ? pad + 1 : pad + h - 1;
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(x - 4, y + (up ? 7 : -7));
      ctx.lineTo(x + 4, y + (up ? 7 : -7));
      ctx.closePath();
      ctx.fill();
    }
  }
  ctx.restore();
  return { clipped: range.clipped, outliers: range.outliers, fullMin: range.fullMin, fullMax: range.fullMax };
}

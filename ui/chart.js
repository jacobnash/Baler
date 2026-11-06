// No build step, no framework - same spirit as every other file in ui/.
// Extracted out of app.js (originally built for point-history charts on
// the equipment-tree page) so derivations.js can reuse it for test-case
// input-series charts without duplicating it.
// pad defaults to 30 (app.js's original 640x220 point-history chart); a
// smaller canvas (e.g. derivations.js's 220x70 test-case sparklines) needs
// a smaller pad passed explicitly or the plot area all but disappears.
function drawLineChart(canvas, history, pad = 30, unit = "") {
  const ctx = canvas.getContext("2d");
  const w = canvas.width - pad * 2;
  const h = canvas.height - pad * 2;

  const values = history.map((r) => r.value);
  const times = history.map((r) => r.ts);
  const minV = Math.min(...values);
  const maxV = Math.max(...values);
  const spanV = maxV - minV || 1;
  const minT = Math.min(...times);
  const maxT = Math.max(...times);
  const spanT = maxT - minT || 1;

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.strokeStyle = "currentColor";
  ctx.globalAlpha = 0.3;
  ctx.strokeRect(pad, pad, w, h);
  ctx.globalAlpha = 1;

  ctx.fillStyle = "currentColor";
  ctx.font = "11px sans-serif";
  const fmtV = (v) => `${+v.toFixed(2)}${unit ? ` ${unit}` : ""}`;
  ctx.fillText(fmtV(maxV), 2, pad - 4);
  ctx.fillText(fmtV(minV), 2, pad + h + 12);

  // Time axis: start, middle, end - only where there's room (the
  // derivations page's sparklines pass a small pad and skip it).
  if (pad >= 20 && history.length > 1) {
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
  ctx.strokeStyle = accent;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  history.forEach((row, i) => {
    const x = pad + ((row.ts - minT) / spanT) * w;
    const y = pad + h - ((row.value - minV) / spanV) * h;
    if (i === 0 || row.ts - history[i - 1].ts > gap) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

/* SVG chart primitives for the Gutter Macro Dashboard.
 *
 * Hand-rolled rather than pulled from a CDN: the whole payload is ~100 KB and the
 * only forms needed are a multi-series line and a single-series bar. That keeps the
 * page dependency-free, offline-capable, and small enough to audit.
 *
 * Conventions enforced here rather than left to callers:
 *   - 2px lines, solid hairline grid one shade off the surface, recessive axes.
 *   - Crosshair + tooltip on every line chart; per-bar tooltip on every bar chart.
 *   - Colour follows the entity: a series keeps its slot when others are filtered out.
 *   - Never two y-scales. Callers index to a common base instead.
 */

const NS = "http://www.w3.org/2000/svg";

export const PALETTE = {
  // Validated with dataviz/scripts/validate_palette.js in both modes.
  // Light: worst adjacent CVD dE 9.2, normal-vision 27.6. Dark: 9.4 / 26.5.
  categorical: {
    light: ["#2a78d6", "#eb6834", "#1baf7a"],
    dark: ["#3987e5", "#d95926", "#199e70"],
  },
  // Single-hue ordinal ramp for time-ordered curve snapshots. Monotone L, validated.
  ordinal: {
    light: ["#86b6ef", "#2a78d6", "#104281"],
    dark: ["#9ec5f4", "#3987e5", "#184f95"],
  },
};

export function isDark() {
  const attr = document.documentElement.getAttribute("data-theme");
  if (attr === "dark") return true;
  if (attr === "light") return false;
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

export function seriesColors(n, kind = "categorical") {
  const ramp = PALETTE[kind][isDark() ? "dark" : "light"];
  return Array.from({ length: n }, (_, i) => ramp[i % ramp.length]);
}

function el(name, attrs = {}) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  }
  return node;
}

/* ------------------------------------------------------------------ scales */

function niceTicks(lo, hi, count = 5) {
  if (!isFinite(lo) || !isFinite(hi)) return [0, 1];
  if (lo === hi) {
    const pad = Math.abs(lo) * 0.1 || 1;
    lo -= pad;
    hi += pad;
  }
  const raw = (hi - lo) / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm >= 7.5 ? 10 : norm >= 3.5 ? 5 : norm >= 1.5 ? 2 : 1) * mag;
  const start = Math.ceil(lo / step) * step;
  const ticks = [];
  for (let v = start; v <= hi + step * 1e-9; v += step) ticks.push(Number(v.toFixed(10)));
  return ticks;
}

/** One format for a whole axis, derived from the tick step rather than from each
 *  value's own magnitude. Formatting per value is what produces an axis reading
 *  "0.000 / 50.00 / 300.0 / 1,200" — four different formats on one scale. */
function tickFormatter(ticks, unitHint = "") {
  if (unitHint === "pct") {
    const step = ticks.length > 1 ? Math.abs(ticks[1] - ticks[0]) : 1;
    const d = step >= 1 ? 0 : 1;
    return (v) => `${v >= 0 ? "" : "-"}${Math.abs(v).toFixed(d)}%`;
  }
  const step =
    ticks.length > 1
      ? Math.abs(ticks[1] - ticks[0])
      : Math.abs(ticks[0]) || 1;
  const decimals = Math.min(Math.max(Math.ceil(-Math.log10(step)), 0), 4);
  return (v) =>
    v.toLocaleString("en-US", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    });
}

function fmtNum(v, unitHint = "") {
  if (v === null || v === undefined || !isFinite(v)) return "—";
  const abs = Math.abs(v);
  if (unitHint === "pct") return `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`;
  if (abs >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (abs >= 100) return v.toFixed(1);
  if (abs >= 1) return v.toFixed(2);
  return v.toFixed(3);
}

function fmtDate(ms, freq) {
  const d = new Date(ms);
  const opts =
    freq === "quarterly" || freq === "monthly"
      ? { year: "numeric", month: "short", timeZone: "UTC" }
      : { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" };
  return d.toLocaleDateString("en-US", opts);
}

/* -------------------------------------------------------------- line chart */

/** Multi-series time-series line chart with a shared crosshair.
 *  series: [{ key, label, color, freq, points: [[isoDate, value], ...] }]
 */
export function lineChart(container, series, opts = {}) {
  const {
    height = 260,
    unitHint = "",
    valueLabel = "",
    directLabel = true,
    zeroLine = false,
  } = opts;

  container.innerHTML = "";
  const width = Math.max(container.clientWidth || 640, 320);
  const isNarrow = width < 460;
  const m = {
    top: 14,
    right: directLabel && !isNarrow ? 64 : 16,
    bottom: 26,
    left: 46,
  };
  const iw = width - m.left - m.right;
  const ih = height - m.top - m.bottom;

  const prepared = series
    .map((s) => ({
      ...s,
      data: (s.points || [])
        .map(([d, v]) => [Date.parse(d + "T00:00:00Z"), v])
        .filter(([t, v]) => isFinite(t) && v !== null && isFinite(v)),
    }))
    .filter((s) => s.data.length > 0);

  if (!prepared.length) {
    container.innerHTML = `<p class="chart-empty">No data available.</p>`;
    return;
  }

  const xs = prepared.flatMap((s) => s.data.map((d) => d[0]));
  const ys = prepared.flatMap((s) => s.data.map((d) => d[1]));
  const x0 = Math.min(...xs);
  const x1 = Math.max(...xs);
  let y0 = Math.min(...ys);
  let y1 = Math.max(...ys);
  if (zeroLine) {
    y0 = Math.min(y0, 0);
    y1 = Math.max(y1, 0);
  }
  const ticks = niceTicks(y0, y1, height > 220 ? 5 : 4);
  y0 = Math.min(y0, ticks[0]);
  y1 = Math.max(y1, ticks[ticks.length - 1]);
  const fmtTick = tickFormatter(ticks, unitHint);

  const sx = (t) => (x1 === x0 ? 0 : ((t - x0) / (x1 - x0)) * iw);
  const sy = (v) => (y1 === y0 ? ih / 2 : ih - ((v - y0) / (y1 - y0)) * ih);

  const svg = el("svg", {
    viewBox: `0 0 ${width} ${height}`,
    width: "100%",
    height,
    role: "img",
    "aria-label": opts.ariaLabel || "Time series chart",
    class: "chart-svg",
  });
  const plot = el("g", { transform: `translate(${m.left},${m.top})` });

  // --- grid + y axis (solid hairlines, never dashed) ---
  for (const t of ticks) {
    const y = sy(t);
    if (y < -1 || y > ih + 1) continue;
    plot.appendChild(el("line", { x1: 0, x2: iw, y1: y, y2: y, class: "grid-line" }));
    const label = el("text", { x: -8, y: y + 4, class: "axis-label", "text-anchor": "end" });
    label.textContent = fmtTick(t);
    plot.appendChild(label);
  }
  if (zeroLine && y0 < 0 && y1 > 0) {
    plot.appendChild(el("line", { x1: 0, x2: iw, y1: sy(0), y2: sy(0), class: "zero-line" }));
  }

  // --- x axis ---
  const xTickCount = isNarrow ? 3 : 5;
  for (let i = 0; i < xTickCount; i++) {
    const t = x0 + ((x1 - x0) * i) / (xTickCount - 1);
    const label = el("text", {
      x: sx(t),
      y: ih + 18,
      class: "axis-label",
      "text-anchor": i === 0 ? "start" : i === xTickCount - 1 ? "end" : "middle",
    });
    label.textContent = new Date(t).toLocaleDateString("en-US", {
      year: "2-digit",
      month: "short",
      timeZone: "UTC",
    });
    plot.appendChild(label);
  }

  // --- series paths (2px, entity-stable colour) ---
  prepared.forEach((s) => {
    const d = s.data.map(([t, v], i) => `${i ? "L" : "M"}${sx(t).toFixed(2)},${sy(v).toFixed(2)}`).join("");
    plot.appendChild(el("path", { d, fill: "none", stroke: s.color, "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round", class: "series-line" }));
  });

  // --- selective direct labels: the endpoint only ---
  if (directLabel && !isNarrow) {
    const placed = [];
    prepared.forEach((s) => {
      const [t, v] = s.data[s.data.length - 1];
      let y = sy(v);
      while (placed.some((p) => Math.abs(p - y) < 12)) y += 12;  // avoid collisions
      placed.push(y);
      const label = el("text", { x: sx(t) + 6, y: y + 4, class: "endpoint-label" });
      label.setAttribute("fill", s.color);
      label.textContent = fmtNum(v, unitHint);
      plot.appendChild(label);
    });
  }

  // --- crosshair + hover markers ---
  const crosshair = el("line", { y1: 0, y2: ih, class: "crosshair", visibility: "hidden" });
  plot.appendChild(crosshair);
  const dots = prepared.map((s) => {
    const c = el("circle", { r: 4.5, fill: s.color, class: "hover-dot", visibility: "hidden" });
    plot.appendChild(c);
    return c;
  });

  const hit = el("rect", { x: 0, y: 0, width: iw, height: ih, fill: "transparent", class: "hit-area" });
  plot.appendChild(hit);
  svg.appendChild(plot);
  container.appendChild(svg);

  const tip = document.createElement("div");
  tip.className = "chart-tooltip";
  tip.hidden = true;
  container.appendChild(tip);

  function nearest(s, t) {
    let lo = 0, hi = s.data.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (s.data[mid][0] < t) lo = mid + 1; else hi = mid;
    }
    const a = s.data[Math.max(0, lo - 1)], b = s.data[lo];
    return Math.abs(a[0] - t) <= Math.abs(b[0] - t) ? a : b;
  }

  function move(ev) {
    const rect = svg.getBoundingClientRect();
    const scale = width / rect.width;
    const px = (ev.clientX - rect.left) * scale - m.left;
    if (px < 0 || px > iw) return hide();
    const t = x0 + (px / iw) * (x1 - x0);

    crosshair.setAttribute("x1", px);
    crosshair.setAttribute("x2", px);
    crosshair.setAttribute("visibility", "visible");

    const rows = prepared.map((s, i) => {
      const [pt, pv] = nearest(s, t);
      dots[i].setAttribute("cx", sx(pt));
      dots[i].setAttribute("cy", sy(pv));
      dots[i].setAttribute("visibility", "visible");
      return { s, pt, pv };
    });

    const when = fmtDate(rows[0].pt, prepared[0].freq);
    tip.innerHTML =
      `<div class="tip-date">${when}</div>` +
      rows
        .map(
          (r) =>
            `<div class="tip-row"><span class="tip-swatch" style="background:${r.s.color}"></span>` +
            `<span class="tip-label">${r.s.label}</span>` +
            `<span class="tip-value">${fmtNum(r.pv, unitHint)}${valueLabel}</span></div>`
        )
        .join("");
    tip.hidden = false;
    const tipW = tip.offsetWidth || 180;
    const leftPx = (px + m.left) / scale;
    tip.style.left = `${Math.min(Math.max(leftPx + 12, 4), rect.width - tipW - 4)}px`;
    tip.style.top = `8px`;
  }

  function hide() {
    crosshair.setAttribute("visibility", "hidden");
    dots.forEach((d) => d.setAttribute("visibility", "hidden"));
    tip.hidden = true;
  }

  hit.addEventListener("mousemove", move);
  hit.addEventListener("mouseleave", hide);
  hit.addEventListener("touchmove", (e) => { move(e.touches[0]); e.preventDefault(); }, { passive: false });
  hit.addEventListener("touchend", hide);
}

/* --------------------------------------------------------------- bar chart */

/** Single-series bar chart. bars: [{ label, value, muted, emphasis, tip }] */
export function barChart(container, bars, opts = {}) {
  const { height = 280, unitHint = "", color = null, labelEvery = 1 } = opts;
  container.innerHTML = "";
  if (!bars.length) {
    container.innerHTML = `<p class="chart-empty">No data available.</p>`;
    return;
  }

  const width = Math.max(container.clientWidth || 640, 320);
  const m = { top: 16, right: 12, bottom: 42, left: 52 };
  const iw = width - m.left - m.right;
  const ih = height - m.top - m.bottom;
  const base = color || seriesColors(1)[0];

  const values = bars.map((b) => b.value);
  const ticks = niceTicks(Math.min(0, ...values), Math.max(...values), 5);
  const y1 = Math.max(...ticks, ...values);
  const fmtTick = tickFormatter(ticks, unitHint);
  const sy = (v) => ih - (v / y1) * ih;

  // A 2px surface gap between adjacent bars, never a border around them.
  // Bars are also capped: four auctions stretched across a wide card become giant
  // saturated blocks, which read loud and childish. Past the cap the group is
  // centred rather than stretched.
  const MAX_BAR = 64;
  const rawSlot = iw / bars.length;
  const bw = Math.max(Math.min(rawSlot - 2, MAX_BAR), 1);
  const slot = Math.min(rawSlot, bw + 12);
  // When the capped bars need less room than the card offers, shrink the plot to
  // fit them rather than stretching gridlines across the empty space — a grid that
  // runs far past the last bar reads as missing data.
  const plotW = Math.min(iw, slot * bars.length);

  const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", height,
    role: "img", "aria-label": opts.ariaLabel || "Bar chart", class: "chart-svg" });
  const plot = el("g", { transform: `translate(${m.left},${m.top})` });

  for (const t of ticks) {
    const y = sy(t);
    plot.appendChild(el("line", { x1: 0, x2: plotW, y1: y, y2: y, class: "grid-line" }));
    const label = el("text", { x: -8, y: y + 4, class: "axis-label", "text-anchor": "end" });
    label.textContent = fmtTick(t);
    plot.appendChild(label);
  }

  const tip = document.createElement("div");
  tip.className = "chart-tooltip";
  tip.hidden = true;

  bars.forEach((b, i) => {
    const x = i * slot + (slot - bw) / 2;
    const y = sy(b.value);
    // 4px rounded data-end, anchored to the baseline (square bottom corners).
    const r = Math.min(4, bw / 2, Math.max(ih - y, 0));
    const h = ih - y;
    const d = h <= r
      ? `M${x},${ih}L${x},${ih}L${x + bw},${ih}Z`
      : `M${x},${ih}L${x},${y + r}Q${x},${y} ${x + r},${y}L${x + bw - r},${y}Q${x + bw},${y} ${x + bw},${y + r}L${x + bw},${ih}Z`;
    const path = el("path", { d, fill: base, class: b.muted ? "bar muted" : "bar" });
    if (b.emphasis) path.classList.add("emphasis");
    path.addEventListener("mouseenter", () => {
      tip.innerHTML =
        `<div class="tip-date">${b.label}</div>` +
        `<div class="tip-row"><span class="tip-swatch" style="background:${base}"></span>` +
        `<span class="tip-label">${opts.seriesLabel || "Value"}</span>` +
        `<span class="tip-value">${fmtNum(b.value, unitHint)}</span></div>` +
        (b.tip ? `<div class="tip-note">${b.tip}</div>` : "");
      tip.hidden = false;
      const rect = container.getBoundingClientRect();
      const px = ((x + bw / 2 + m.left) / width) * rect.width;
      const tipW = tip.offsetWidth || 180;
      tip.style.left = `${Math.min(Math.max(px - tipW / 2, 4), rect.width - tipW - 4)}px`;
      tip.style.top = "8px";
    });
    path.addEventListener("mouseleave", () => { tip.hidden = true; });
    plot.appendChild(path);

    if (i % labelEvery === 0 || i === bars.length - 1) {
      const label = el("text", { x: x + bw / 2, y: ih + 16, class: "axis-label", "text-anchor": "middle" });
      label.textContent = b.label;
      plot.appendChild(label);
    }
  });

  svg.appendChild(plot);
  container.appendChild(svg);
  container.appendChild(tip);
}

export { fmtNum, fmtDate, tickFormatter, niceTicks };

/* ------------------------------------------------------------- curve chart */

/** Forward-curve chart: x is contract tenor (an ordered category), not time.
 *  snapshots: [{ label, color, values: [number|null, ...] }] aligned to `xLabels`.
 *
 *  Separate from lineChart because the x-axis is ordinal — the gap between
 *  contract 1 and contract 2 is one contract, not a number of days, and spacing it
 *  by date would distort the shape the chart exists to show.
 */
export function curveChart(container, xLabels, snapshots, opts = {}) {
  const { height = 240, unitHint = "" } = opts;
  container.innerHTML = "";
  const usable = snapshots.filter((s) => s.values.some((v) => v !== null && isFinite(v)));
  if (!usable.length || xLabels.length < 2) {
    container.innerHTML = `<p class="chart-empty">No curve data available.</p>`;
    return;
  }

  const width = Math.max(container.clientWidth || 640, 320);
  const m = { top: 14, right: 16, bottom: 34, left: 46 };
  const iw = width - m.left - m.right;
  const ih = height - m.top - m.bottom;

  const all = usable.flatMap((s) => s.values).filter((v) => v !== null && isFinite(v));
  const ticks = niceTicks(Math.min(...all), Math.max(...all), 4);
  const fmtTick = tickFormatter(ticks, unitHint);
  const y0 = Math.min(...all, ticks[0]);
  const y1 = Math.max(...all, ticks[ticks.length - 1]);
  const sx = (i) => (xLabels.length === 1 ? iw / 2 : (i / (xLabels.length - 1)) * iw);
  const sy = (v) => (y1 === y0 ? ih / 2 : ih - ((v - y0) / (y1 - y0)) * ih);

  const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", height,
    role: "img", "aria-label": opts.ariaLabel || "Forward curve", class: "chart-svg" });
  const plot = el("g", { transform: `translate(${m.left},${m.top})` });

  for (const t of ticks) {
    const y = sy(t);
    if (y < -1 || y > ih + 1) continue;
    plot.appendChild(el("line", { x1: 0, x2: iw, y1: y, y2: y, class: "grid-line" }));
    const lab = el("text", { x: -8, y: y + 4, class: "axis-label", "text-anchor": "end" });
    lab.textContent = fmtTick(t);
    plot.appendChild(lab);
  }

  xLabels.forEach((label, i) => {
    const lab = el("text", { x: sx(i), y: ih + 18, class: "axis-label", "text-anchor": "middle" });
    lab.textContent = label;
    plot.appendChild(lab);
  });

  const tip = document.createElement("div");
  tip.className = "chart-tooltip";
  tip.hidden = true;

  usable.forEach((s) => {
    const pts = s.values
      .map((v, i) => (v === null || !isFinite(v) ? null : [sx(i), sy(v)]))
      .filter(Boolean);
    if (pts.length > 1) {
      plot.appendChild(el("path", {
        d: pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`).join(""),
        fill: "none", stroke: s.color, "stroke-width": 2,
        "stroke-linejoin": "round", "stroke-linecap": "round",
      }));
    }
    // Markers: >=8px, with a 2px surface ring so overlapping snapshots stay readable.
    s.values.forEach((v, i) => {
      if (v === null || !isFinite(v)) return;
      const dot = el("circle", { cx: sx(i), cy: sy(v), r: 4.5, fill: s.color, class: "hover-dot" });
      dot.setAttribute("visibility", "visible");
      dot.addEventListener("mouseenter", () => {
        tip.innerHTML =
          `<div class="tip-date">${xLabels[i]}</div>` +
          `<div class="tip-row"><span class="tip-swatch" style="background:${s.color}"></span>` +
          `<span class="tip-label">${s.label}</span>` +
          `<span class="tip-value">${fmtNum(v, unitHint)}</span></div>`;
        tip.hidden = false;
        const rect = container.getBoundingClientRect();
        const px = ((sx(i) + m.left) / width) * rect.width;
        const tipW = tip.offsetWidth || 170;
        tip.style.left = `${Math.min(Math.max(px - tipW / 2, 4), rect.width - tipW - 4)}px`;
        tip.style.top = "8px";
      });
      dot.addEventListener("mouseleave", () => { tip.hidden = true; });
      plot.appendChild(dot);
    });
  });

  svg.appendChild(plot);
  container.appendChild(svg);
  container.appendChild(tip);
}

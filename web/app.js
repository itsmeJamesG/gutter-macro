/* Gutter Macro Dashboard — panel rendering.
 *
 * The one editorial rule encoded here: raw levels are only plotted together when
 * every series in a panel shares the *same* units string. CPI internet services is
 * Dec 1988=100 while CPI electricity is 1982-84=100 — both say "index", neither is
 * comparable to the other, and drawing them on one axis invents a relationship.
 * Mixed-unit panels are indexed to 100 at the window start, which is a real
 * comparison on a single axis. See `canShowLevel`.
 */
import { lineChart, barChart, curveChart, seriesColors, PALETTE, isDark, fmtNum } from "./chart.js";

const VIEWS = {
  indexed: { label: "Indexed", hint: "=100 at window start", unitHint: "" },
  yoy: { label: "Year over year", hint: "% vs 12 months earlier", unitHint: "pct" },
  level: { label: "Level", hint: "as published", unitHint: "" },
};

let PAYLOAD = null;
let view = "indexed";
let range = 3;          // years; overwritten from the payload's default_range

/* ------------------------------------------------------------- transforms */

function canShowLevel(series) {
  const units = new Set(series.map((s) => s.units));
  return units.size === 1;
}

function toIndexed(points) {
  const base = points.find(([, v]) => v !== 0 && isFinite(v));
  if (!base) return [];
  return points.map(([d, v]) => [d, (v / base[1]) * 100]);
}

function toYoY(points) {
  // Date-based lookback, not a fixed row offset: the same code has to serve daily
  // gas prices and quarterly BEA data without knowing which it has.
  const out = [];
  const times = points.map(([d]) => Date.parse(d + "T00:00:00Z"));
  for (let i = 0; i < points.length; i++) {
    const target = times[i] - 365.25 * 864e5;
    let j = -1;
    for (let k = i - 1; k >= 0; k--) {
      if (times[k] <= target) { j = k; break; }
    }
    if (j < 0) continue;
    // Reject a reference point that is not really a year back.
    if (target - times[j] > 120 * 864e5) continue;
    const prev = points[j][1];
    if (!prev) continue;
    out.push([points[i][0], (points[i][1] / prev - 1) * 100]);
  }
  return out;
}

/** Narrow to the last `years` of data, measured from the payload's own newest
 *  observation rather than the viewer's clock — a stale payload or a different
 *  timezone must not shift the window under the data. */
function windowPoints(points, years, anchorISO) {
  if (!points.length) return points;
  const anchor = anchorISO
    ? Date.parse(anchorISO + "T00:00:00Z")
    : Date.parse(points[points.length - 1][0] + "T00:00:00Z");
  const cutoff = anchor - years * 365.25 * 864e5;
  const kept = points.filter(([d]) => Date.parse(d + "T00:00:00Z") >= cutoff);
  // A series that ended before the window opened keeps one point, so it reads as
  // "stopped here" rather than vanishing without explanation.
  return kept.length ? kept : points.slice(-1);
}

/** Transform and window, in the order each view actually requires.
 *
 *  The two are not commutative and getting it backwards is silent:
 *    yoy      compute on the FULL series, then window. Year-over-year needs a year
 *             of data before the window opens; windowing first makes the 1-year
 *             range render completely empty.
 *    indexed  window FIRST, then rebase. The base must be the start of the window
 *             the viewer chose, not the start of all history.
 *    level    window only.
 */
function prepare(points, mode, years, anchor) {
  if (mode === "yoy") return windowPoints(toYoY(points), years, anchor);
  if (mode === "indexed") return toIndexed(windowPoints(points, years, anchor));
  return windowPoints(points, years, anchor);
}

/* ----------------------------------------------------------------- render */

function statTile({ label, value, sub, tone }) {
  return `<div class="stat">
    <div class="stat-label">${label}</div>
    <div class="stat-value">${value}</div>
    <div class="stat-sub ${tone || ""}">${sub || ""}</div>
  </div>`;
}

function findSeries(key) {
  for (const p of PAYLOAD.panels) {
    const s = p.series.find((x) => x.key === key);
    if (s) return s;
  }
  return null;
}

function pct(v) {
  return v === null || v === undefined ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`;
}

function tone(v) {
  return v === null || v === undefined ? "" : v >= 0 ? "up" : "down";
}

function renderStats() {
  const host = document.getElementById("stats");
  const tiles = [];

  const cap = PAYLOAD.capacity;
  if (cap && cap.auctions.length) {
    const last = cap.auctions[cap.auctions.length - 1];
    const ref = cap.auctions.find((a) => a.delivery_year === 2024);
    const chg = ref ? (last.price / ref.price - 1) * 100 : null;
    tiles.push(statTile({
      label: `PJM capacity · DY ${last.label}`,
      value: `$${fmtNum(last.price)}`,
      sub: ref ? `${pct(chg)} vs DY ${ref.label}` : "per MW-day",
      tone: tone(chg),
    }));
  }

  const pairs = [
    ["wage_trades", "Specialty trade wages", (s) => `$${s.change.latest.toFixed(2)}`],
    ["bea_software_price", "Software price index", (s) => fmtNum(s.change.latest)],
    ["henry_hub_spot", "Henry Hub spot", (s) => `$${s.change.latest.toFixed(2)}`],
  ];
  for (const [key, label, fmt] of pairs) {
    const s = findSeries(key);
    if (!s || !s.change || s.change.latest === undefined) continue;
    tiles.push(statTile({
      label,
      value: fmt(s),
      sub: `${pct(s.change.yoy)} yoy`,
      tone: tone(s.change.yoy),
    }));
  }
  host.innerHTML = tiles.join("");
}

function legendHTML(series) {
  if (series.length < 2) return "";
  return `<div class="legend">${series
    .map((s) => `<span class="legend-item"><span class="legend-swatch" style="background:${s.color}"></span>${s.label}</span>`)
    .join("")}</div>`;
}

function tableHTML(series) {
  const dates = [...new Set(series.flatMap((s) => s.points.map(([d]) => d)))].sort().reverse();
  const lookup = series.map((s) => Object.fromEntries(s.points));
  const rows = dates.slice(0, 400).map((d) =>
    `<tr><th scope="row">${d}</th>${lookup
      .map((m) => `<td>${m[d] === undefined ? "—" : fmtNum(m[d])}</td>`)
      .join("")}</tr>`
  );
  return `<table class="data-table"><thead><tr><th scope="col">Date</th>${series
    .map((s) => `<th scope="col">${s.label}</th>`)
    .join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>`;
}

/* The Henry Hub curve: contract 1-4 settlements re-read as a shape across tenor,
 * for a few points in time. Snapshot dates use the single-hue ordinal ramp because
 * they are time-ordered, not separate identities. */
function curveSnapshots(panel) {
  const contracts = ["henry_hub_c1", "henry_hub_c2", "henry_hub_c3", "henry_hub_c4"]
    .map((k) => panel.series.find((s) => s.key === k))
    .filter(Boolean);
  if (contracts.length < 2) return null;

  const maps = contracts.map((c) => Object.fromEntries(c.points));
  // Only dates where every contract settled: a curve stitched from several dates
  // shows a shape that never existed.
  const complete = Object.keys(maps[0])
    .filter((d) => maps.every((m) => m[d] !== undefined))
    .sort();
  if (!complete.length) return null;

  const latest = complete[complete.length - 1];
  const pick = (daysBack) => {
    const target = Date.parse(latest + "T00:00:00Z") - daysBack * 864e5;
    const found = [...complete].reverse().find((d) => Date.parse(d + "T00:00:00Z") <= target);
    return found || null;
  };
  // The far snapshot follows the selected range, so changing range re-frames the
  // curve comparison instead of leaving it stuck a year back.
  const wanted = [
    [latest, "Latest"],
    [pick(30), "1 month ago"],
    [pick(range * 365.25), range === 1 ? "1 year ago" : `${range} years ago`],
  ].filter(([d]) => d);

  // De-duplicate when the history is too short for a distinct earlier snapshot.
  const seen = new Set();
  const chosen = wanted.filter(([d]) => !seen.has(d) && seen.add(d));
  const ramp = PALETTE.ordinal[isDark() ? "dark" : "light"];

  return {
    xLabels: contracts.map((_, i) => `M${i + 1}`),
    snapshots: chosen.map(([d, label], i) => ({
      label: `${label} (${d})`,
      // Darkest step for the latest snapshot, so recency reads as emphasis.
      color: ramp[ramp.length - 1 - i] || ramp[0],
      values: maps.map((m) => (m[d] === undefined ? null : m[d])),
    })),
  };
}

// One publication period, in years — the slack allowed before a series counts as
// starting late. Without it, quarterly BEA data is flagged as "short" at every
// range simply because it publishes a quarter behind: its window span is always
// a few months less than the window itself, which is lag, not missing history.
const PERIOD_YEARS = { daily: 0.02, monthly: 0.1, quarterly: 0.35, annual: 1.1 };

/** True when a series genuinely begins after the selected window opens — i.e. it
 *  has less history than the window, as opposed to merely reporting late. */
function startsInsideWindow(s) {
  if (!s.earliest || !PAYLOAD.latest_date) return false;
  const anchor = Date.parse(PAYLOAD.latest_date + "T00:00:00Z");
  const windowStart = anchor - range * 365.25 * 864e5;
  const slack = (PERIOD_YEARS[s.freq] ?? 0.1) * 365.25 * 864e5;
  return Date.parse(s.earliest + "T00:00:00Z") > windowStart + slack;
}

/** Colour is assigned from the panel's own full series list, never from the
 *  filtered survivors — a series keeps its hue when another drops out of range. */
function preparePanel(panel) {
  const levelOk = canShowLevel(panel.series);
  // Indexing exists to make series comparable. With one series there is nothing to
  // compare it against, and rebasing to 100 throws away the units for no gain —
  // so a lone series shows its level.
  const lone = panel.series.length === 1 && levelOk;
  let mode = view;
  if (view === "level" && !levelOk) mode = "indexed";
  else if (view === "indexed" && lone) mode = "level";
  const colors = seriesColors(panel.series.length);
  const prepared = panel.series
    .map((s, i) => ({
      ...s,
      color: colors[i],
      points: prepare(s.points, mode, range, PAYLOAD.latest_date),
    }))
    .filter((s) => s.points.length);
  const short = panel.series.filter((s) => startsInsideWindow(s));
  return { mode, levelOk, prepared, short };
}

function renderPanel(panel) {
  if (panel.key === "capacity") return renderCapacity(panel);
  if (!panel.series.length) return renderEmptyPanel(panel);

  const { mode, levelOk, prepared, short } = preparePanel(panel);

  const card = document.createElement("section");
  card.className = "card";
  const substituted = view === "level" && !levelOk;
  const loneLevel = view === "indexed" && mode === "level";
  card.innerHTML = `
    <header class="card-head">
      <h2>${panel.title}</h2>
      <p class="blurb">${panel.blurb}</p>
    </header>
    ${legendHTML(prepared)}
    <div class="chart-wrap"></div>
    ${substituted ? `<p class="substitution">Series here use different index bases, so
      levels are not comparable — showing indexed instead.</p>` : ""}
    ${loneLevel ? `<p class="substitution">A single series has nothing to be indexed
      against — showing its level in ${panel.series[0].units}.</p>` : ""}
    ${short.length ? `<p class="substitution">Less history than the ${range}-year window:
      ${short.map((s) => `${s.label} starts ${s.earliest}`).join("; ")}.</p>` : ""}
    <details class="notes"><summary>Notes &amp; sources</summary>
      <ul>${panel.series.map((s) =>
        `<li><strong>${s.label}</strong> <span class="mono">${s.source_id}</span>
         — ${s.units}${s.seasonal ? ", seasonally adjusted" : ""}.
         ${s.note.replace(/\s+/g, " ")}</li>`).join("")}
      </ul>
      ${panel.missing.length ? `<p class="missing">Not yet configured:
        ${panel.missing.join(", ")}.</p>` : ""}
    </details>
    <details class="notes"><summary>Table view</summary>${tableHTML(prepared)}</details>
  `;
  document.getElementById("panels").appendChild(card);

  lineChart(card.querySelector(".chart-wrap"), prepared, {
    unitHint: VIEWS[mode].unitHint,
    zeroLine: mode === "yoy",
    ariaLabel: `${panel.title}: ${prepared.map((s) => s.label).join(", ")}`,
  });

  const curve = panel.key === "gas" ? curveSnapshots(panel) : null;
  if (curve) {
    const block = document.createElement("div");
    block.className = "curve-block";
    block.innerHTML =
      `<h3 class="sub-head">Henry Hub forward curve</h3>
       <p class="blurb">Contract months 1-4, $/MMBtu. Upward slope is contango.</p>
       <div class="legend">${curve.snapshots
         .map((s) => `<span class="legend-item"><span class="legend-swatch" style="background:${s.color}"></span>${s.label}</span>`)
         .join("")}</div>
       <div class="curve-wrap"></div>`;
    card.querySelector(".chart-wrap").after(block);
    curveChart(block.querySelector(".curve-wrap"), curve.xLabels, curve.snapshots, {
      unitHint: "",
      ariaLabel: "Henry Hub forward curve by contract month",
    });
  }

  card._render = () => renderPanelChart(card, panel);
  return card;
}

function renderPanelChart(card, panel) {
  const { mode, prepared } = preparePanel(panel);
  lineChart(card.querySelector(".chart-wrap"), prepared, {
    unitHint: VIEWS[mode].unitHint,
    zeroLine: mode === "yoy",
  });
}

function renderEmptyPanel(panel) {
  const card = document.createElement("section");
  card.className = "card card-empty";
  const reasons = panel.missing
    .map((k) => `<li><span class="mono">${k}</span> — ${PAYLOAD.status[k] || "not built"}</li>`)
    .join("");
  card.innerHTML = `
    <header class="card-head"><h2>${panel.title}</h2>
    <p class="blurb">${panel.blurb}</p></header>
    <div class="awaiting">
      <p>Awaiting data.</p>
      <ul>${reasons}</ul>
    </div>`;
  document.getElementById("panels").appendChild(card);
  return card;
}

function renderCapacity(panel) {
  const cap = PAYLOAD.capacity;
  const card = document.createElement("section");
  card.className = "card";
  if (!cap) return renderEmptyPanel(panel);

  // Auctions are yearly events, so the range filter counts delivery years rather
  // than measuring back from a date. A floor of five keeps the chart from
  // degenerating into one or two bars at the short ranges — a one-bar bar chart is
  // a stat tile with extra steps, and these auctions only move once a year anyway.
  const MIN_AUCTIONS = 5;
  const shownYears = Math.max(range, MIN_AUCTIONS);
  const lastYear = cap.auctions[cap.auctions.length - 1].delivery_year;
  const shown = cap.auctions.filter((a) => a.delivery_year > lastYear - shownYears);
  const floored = shownYears > range;

  card.innerHTML = `
    <header class="card-head">
      <h2>${panel.title}</h2>
      <p class="blurb">${panel.blurb}</p>
    </header>
    <div class="chart-wrap"></div>
    ${floored ? `<p class="substitution">Auctions clear once a year, so this shows the
      last ${shownYears} delivery years rather than ${range}.</p>` : ""}
    <details class="notes"><summary>Notes &amp; sources</summary>
      <ul><li><strong>${cap.label}</strong> — ${cap.units}. ${cap.note.replace(/\s+/g, " ")}</li>
      <li>Bars are shaded lighter where the capacity product definition changed, because
      the price either side of that break is not like-for-like.</li></ul>
    </details>`;
  document.getElementById("panels").appendChild(card);

  const bars = shown.map((a, i) => ({
    label: String(a.delivery_year).slice(2) + "/" + a.label.slice(-2),
    value: a.price,
    muted: !a.comparable_to_prior,
    emphasis: a.delivery_year >= 2025,
    tip: `${a.product} product${a.comparable_to_prior ? "" : " — definition changed, not comparable to the prior year"}`,
  }));
  barChart(card.querySelector(".chart-wrap"), bars, {
    unitHint: "",
    seriesLabel: "Clearing price ($/MW-day)",
    labelEvery: bars.length > 14 ? 3 : bars.length > 8 ? 2 : 1,
    ariaLabel: "PJM base residual auction clearing price by delivery year",
  });
  card._render = () => renderCapacity(panel);
  return card;
}

/* ------------------------------------------------------------------- boot */

function renderAll() {
  document.getElementById("panels").innerHTML = "";
  renderStats();
  PAYLOAD.panels.forEach(renderPanel);
}

function setRange(next) {
  range = next;
  document.querySelectorAll("[data-range]").forEach((b) =>
    b.setAttribute("aria-pressed", String(Number(b.dataset.range) === next))
  );
  renderAll();
}

function setView(next) {
  view = next;
  document.querySelectorAll("[data-view]").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.view === next))
  );
  document.getElementById("view-hint").textContent = VIEWS[next].hint;
  renderAll();
}

function setTheme(next) {
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem("gutter-theme", next); } catch {}
  document.getElementById("theme-toggle").textContent = next === "dark" ? "Light" : "Dark";
  renderAll();
}

async function boot() {
  try { 
    const saved = localStorage.getItem("gutter-theme");
    if (saved) document.documentElement.setAttribute("data-theme", saved);
  } catch {}

  // Relative on purpose: the same page is served from the site root locally and
  // from /<repo>/ on GitHub Pages. An absolute path 404s on the latter.
  const res = await fetch("./data.json");
  if (!res.ok) {
    document.getElementById("panels").innerHTML =
      `<section class="card"><p class="awaiting">The first build is still running. Reload in a moment.</p></section>`;
    return;
  }
  PAYLOAD = await res.json();

  document.getElementById("generated").textContent =
    new Date(PAYLOAD.generated_at).toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short" });
  document.getElementById("window").textContent =
    `data through ${PAYLOAD.latest_date}`;

  const problems = Object.entries(PAYLOAD.status).filter(([, v]) => v !== "ok");
  if (problems.length) {
    document.getElementById("status-bar").innerHTML =
      `<details><summary>${problems.length} series not loaded</summary><ul>${problems
        .map(([k, v]) => `<li><span class="mono">${k}</span> — ${v}</li>`).join("")}</ul></details>`;
  }

  // Range buttons come from the payload, so a narrower build never offers a range
  // it has no data for.
  const rangeHost = document.getElementById("range-controls");
  rangeHost.innerHTML = (PAYLOAD.ranges || [1, 3, 5, 10])
    .map((r) => `<button type="button" data-range="${r}" aria-pressed="false">${r}Y</button>`)
    .join("");
  rangeHost.querySelectorAll("[data-range]").forEach((b) =>
    b.addEventListener("click", () => setRange(Number(b.dataset.range)))
  );
  range = PAYLOAD.default_range || 3;

  document.querySelectorAll("[data-view]").forEach((b) =>
    b.addEventListener("click", () => setView(b.dataset.view))
  );
  const toggle = document.getElementById("theme-toggle");
  toggle.addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme");
    const isDarkNow = current === "dark" ||
      (!current && window.matchMedia("(prefers-color-scheme: dark)").matches);
    setTheme(isDarkNow ? "light" : "dark");
  });
  toggle.textContent =
    document.documentElement.getAttribute("data-theme") === "dark" ? "Light" : "Dark";

  setRange(range);
  setView("indexed");

  let t;
  window.addEventListener("resize", () => { clearTimeout(t); t = setTimeout(renderAll, 180); });
}

boot();
export { canShowLevel, toIndexed, toYoY, windowPoints, prepare, PERIOD_YEARS };

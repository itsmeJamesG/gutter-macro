/* Tests for the dashboard's client-side transforms.
 *
 * Run: node --test tests/js/
 *
 * These cover the one place the front end makes an editorial claim: whether a set of
 * series may be drawn on a shared axis at all. Getting that wrong produces a chart
 * that looks fine and means nothing.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// The module imports ./chart.js and calls boot() at load, neither of which works
// outside a browser. Pull out the pure exports by evaluating just those functions.
const src = readFileSync(new URL("../../app/static/app.js", import.meta.url), "utf8");
const body = src
  .slice(src.indexOf("function canShowLevel"), src.indexOf("/* ----------------------------------------------------------------- render */"));
const { canShowLevel, toIndexed, toYoY, windowPoints, prepare } = await import(
  "data:text/javascript," +
    encodeURIComponent(
      body + "\nexport { canShowLevel, toIndexed, toYoY, windowPoints, prepare };"
    )
);

const mk = (units) => ({ units });

/* ------------------------------------------------------------ canShowLevel */

test("identical units may share a raw axis", () => {
  assert.equal(canShowLevel([mk("$/MWh"), mk("$/MWh")]), true);
});

test("mixed units may not share a raw axis", () => {
  assert.equal(canShowLevel([mk("$/hour"), mk("index")]), false);
});

test("different index bases may not share a raw axis", () => {
  // The trap this rule exists for: both say "index", neither is comparable.
  assert.equal(
    canShowLevel([mk("index Dec 1988=100"), mk("index 1982-84=100")]),
    false
  );
});

test("a single series can always show its level", () => {
  assert.equal(canShowLevel([mk("$/MMBtu")]), true);
});

/* --------------------------------------------------------------- toIndexed */

test("indexing rebases the first point to 100", () => {
  const out = toIndexed([["2023-01-01", 50], ["2023-02-01", 75]]);
  assert.deepEqual(out, [["2023-01-01", 100], ["2023-02-01", 150]]);
});

test("indexing skips a leading zero rather than dividing by it", () => {
  const out = toIndexed([["2023-01-01", 0], ["2023-02-01", 4], ["2023-03-01", 8]]);
  assert.equal(out[1][1], 100);
  assert.equal(out[2][1], 200);
});

test("indexing an all-zero series yields nothing rather than NaN", () => {
  assert.deepEqual(toIndexed([["2023-01-01", 0]]), []);
});

test("indexing preserves point count and dates", () => {
  const pts = [["2023-01-01", 2], ["2023-02-01", 3], ["2023-03-01", 4]];
  const out = toIndexed(pts);
  assert.equal(out.length, 3);
  assert.deepEqual(out.map(([d]) => d), pts.map(([d]) => d));
});

/* ------------------------------------------------------------------- toYoY */

function monthly(n, fn) {
  return Array.from({ length: n }, (_, i) => {
    const d = new Date(Date.UTC(2023, i, 1)).toISOString().slice(0, 10);
    return [d, fn(i)];
  });
}

test("year over year compares against twelve months back", () => {
  const out = toYoY(monthly(14, (i) => (i < 12 ? 100 : 110)));
  const last = out[out.length - 1];
  assert.ok(Math.abs(last[1] - 10) < 1e-9);
});

test("year over year emits nothing for the first twelve months", () => {
  assert.deepEqual(toYoY(monthly(11, () => 100)), []);
});

test("year over year works on quarterly spacing", () => {
  const pts = Array.from({ length: 8 }, (_, i) => {
    const d = new Date(Date.UTC(2023, i * 3, 1)).toISOString().slice(0, 10);
    return [d, i === 7 ? 120 : 100];
  });
  const out = toYoY(pts);
  assert.ok(out.length > 0);
  assert.ok(Math.abs(out[out.length - 1][1] - 20) < 1e-9);
});

test("year over year refuses a reference point far from a year back", () => {
  // A seven-year gap must not be treated as a year-over-year comparison.
  const out = toYoY([["2019-01-01", 100], ["2026-08-01", 200]]);
  assert.deepEqual(out, []);
});

test("year over year skips a zero denominator", () => {
  const pts = monthly(14, (i) => (i === 0 ? 0 : 100));
  const out = toYoY(pts);
  assert.ok(out.every(([, v]) => Number.isFinite(v)));
});

test("year over year of a flat series is zero", () => {
  const out = toYoY(monthly(24, () => 42));
  assert.ok(out.every(([, v]) => Math.abs(v) < 1e-9));
});

test("a halving reads as minus fifty percent", () => {
  const out = toYoY(monthly(14, (i) => (i < 12 ? 200 : 100)));
  assert.ok(Math.abs(out[out.length - 1][1] + 50) < 1e-9);
});


/* -------------------------------------------------------------- windowing */

function daily(from, days, fn) {
  const t0 = Date.parse(from + "T00:00:00Z");
  return Array.from({ length: days }, (_, i) => [
    new Date(t0 + i * 864e5).toISOString().slice(0, 10),
    fn(i),
  ]);
}

test("windowing keeps only the last N years", () => {
  const pts = daily("2016-09-15", 3660, (i) => i);     // ~10 years
  const out = windowPoints(pts, 1, "2026-09-15");

  assert.ok(out.length > 350 && out.length < 380);
  assert.ok(out[0][0] >= "2025-09-14");
});

test("windowing measures from the given anchor, not the series' own end", () => {
  // The anchor is the newest observation across the whole payload, so a lagging
  // series is cut at the same date as everything else rather than each panel
  // silently choosing its own window.
  const pts = monthly(60, (i) => i);                   // 2023-01 .. 2027-12
  const fromAnchor = windowPoints(pts, 1, "2024-01-01");
  const fromOwnEnd = windowPoints(pts, 1, pts[pts.length - 1][0]);

  assert.equal(fromAnchor[0][0], "2023-01-01");
  assert.equal(fromOwnEnd[0][0], "2026-12-01");
});

test("windowing falls back to the series end when no anchor is given", () => {
  const pts = monthly(36, (i) => i);
  assert.deepEqual(windowPoints(pts, 1, null), windowPoints(pts, 1, pts[pts.length - 1][0]));
});

test("windowing a series that ended early keeps one point", () => {
  // Vanishing entirely would read as "never collected", a different claim.
  const out = windowPoints([["2015-01-01", 5]], 1, "2026-09-15");
  assert.deepEqual(out, [["2015-01-01", 5]]);
});

test("windowing an empty series stays empty", () => {
  assert.deepEqual(windowPoints([], 3, "2026-09-15"), []);
});

test("a wider range returns at least as many points", () => {
  const pts = monthly(130, (i) => i);
  const one = windowPoints(pts, 1, pts[pts.length - 1][0]);
  const ten = windowPoints(pts, 10, pts[pts.length - 1][0]);

  assert.ok(ten.length > one.length);
});

/* ----------------------------------------------- prepare: ordering matters */

test("year over year survives the 1-year range", () => {
  // The bug this guards: windowing before computing yoy leaves nothing to compare
  // against, and the 1-year chart renders empty.
  const pts = monthly(36, (i) => 100 + i);
  const out = prepare(pts, "yoy", 1, pts[pts.length - 1][0]);

  assert.ok(out.length >= 11, `expected a full year of yoy points, got ${out.length}`);
  assert.ok(out.every(([, v]) => Number.isFinite(v)));
});

test("year over year is computed before windowing, not after", () => {
  const pts = monthly(36, (i) => (i < 24 ? 100 : 110));
  const windowedFirst = toYoY(windowPoints(pts, 1, pts[pts.length - 1][0]));
  const correct = prepare(pts, "yoy", 1, pts[pts.length - 1][0]);

  assert.ok(correct.length > windowedFirst.length);
});

test("indexing rebases to the start of the selected window", () => {
  // Not to the start of all history — the base must be what the viewer chose.
  const pts = monthly(36, (i) => 100 + i);             // 100 .. 135
  const oneYear = prepare(pts, "indexed", 1, pts[pts.length - 1][0]);
  const threeYear = prepare(pts, "indexed", 3, pts[pts.length - 1][0]);

  assert.equal(oneYear[0][1], 100);
  assert.equal(threeYear[0][1], 100);
  // Same final value, different bases -> different indexed endpoints.
  assert.ok(oneYear[oneYear.length - 1][1] < threeYear[threeYear.length - 1][1]);
});

test("indexing at a narrower range gives a larger base value", () => {
  const pts = monthly(36, (i) => 100 + i);
  const one = prepare(pts, "indexed", 1, pts[pts.length - 1][0]);
  const three = prepare(pts, "indexed", 3, pts[pts.length - 1][0]);

  assert.equal(one.length, 13);
  assert.equal(three.length, 36);
});

test("level view only windows", () => {
  const pts = monthly(36, (i) => 100 + i);
  const out = prepare(pts, "level", 1, pts[pts.length - 1][0]);

  assert.equal(out[out.length - 1][1], 135);
  assert.equal(out.length, 13);
});

test("every range and view combination yields finite values", () => {
  const pts = monthly(130, (i) => 100 + Math.sin(i) * 10 + i);
  for (const years of [1, 3, 5, 10]) {
    for (const mode of ["indexed", "yoy", "level"]) {
      const out = prepare(pts, mode, years, pts[pts.length - 1][0]);
      assert.ok(out.length > 0, `${mode} @ ${years}Y produced nothing`);
      assert.ok(
        out.every(([d, v]) => typeof d === "string" && Number.isFinite(v)),
        `${mode} @ ${years}Y produced a non-finite value`
      );
    }
  }
});

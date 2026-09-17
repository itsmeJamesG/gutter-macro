/* Visual verification for the dashboard. Needs a running server:
 *
 *   MACRO_DATA_PATH=$PWD/site/data.json uv run uvicorn app.main:app --port 8099
 *   node tests/js/screenshot.mjs
 *
 * Checks what the palette validator cannot: layout, overflow, console errors, and
 * that every range x view combination actually renders something. An empty panel
 * at 1Y year-over-year is the signature of transform/window ordering being flipped,
 * and it throws no error.
 *
 * Needs playwright and a chromium build:
 *   npm i -D playwright && npx playwright install chromium
 *   (on Debian/Ubuntu also: sudo apt-get install -y libnss3 libnspr4)
 *
 * PLAYWRIGHT_CHROMIUM sets an explicit browser path if the bundled one is missing.
 */
import { chromium } from 'playwright';

const EXE = process.env.PLAYWRIGHT_CHROMIUM || undefined;
const URL = process.env.DASHBOARD_URL || 'http://127.0.0.1:8099/';
const browser = await chromium.launch(EXE ? { executablePath: EXE } : {});
const page = await browser.newPage({ viewport: { width: 1280, height: 1400 }, deviceScaleFactor: 2 });
const errs = [];
page.on('pageerror', e => errs.push(`PAGEERROR ${e.message}`));
page.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });
await page.goto(URL, { waitUntil: 'networkidle' });
await page.waitForTimeout(800);

async function stats() {
  return page.evaluate(() => {
    const out = [];
    document.querySelectorAll('#panels .card').forEach(card => {
      const title = card.querySelector('h2')?.textContent ?? '?';
      const paths = [...card.querySelectorAll('path.series-line')];
      const bars = card.querySelectorAll('path.bar').length;
      const pts = paths.reduce((n, p) => n + (p.getAttribute('d').match(/[ML]/g) || []).length, 0);
      const xlab = [...card.querySelectorAll('text.axis-label')].map(t => t.textContent);
      out.push({ title, lines: paths.length, pts, bars, firstX: xlab.find(t => /\d{2}$/.test(t) && t.includes(' ')) });
    });
    return out;
  });
}

for (const years of ['1', '3', '5', '10']) {
  await page.click(`[data-range="${years}"]`);
  await page.waitForTimeout(400);
  const s = await stats();
  const tot = s.reduce((n, x) => n + x.pts, 0);
  console.log(`${years}Y indexed  total pts=${String(tot).padStart(5)}  bars=${s.reduce((n,x)=>n+x.bars,0)}  gas.firstX=${s.find(x=>x.title.includes('gas'))?.firstX}`);
}

console.log('');
for (const view of ['indexed', 'yoy', 'level']) {
  for (const years of ['1', '10']) {
    await page.click(`[data-range="${years}"]`);
    await page.click(`[data-view="${view}"]`);
    await page.waitForTimeout(400);
    const s = await stats();
    const empty = s.filter(x => x.lines === 0 && x.bars === 0 && !x.title.includes('Wholesale')).map(x => x.title);
    console.log(`${view.padEnd(8)} ${years.padStart(2)}Y  pts=${String(s.reduce((n,x)=>n+x.pts,0)).padStart(5)}  emptyPanels=${empty.length ? empty.join(',') : 'none'}`);
  }
}

await page.click('[data-range="10"]'); await page.click('[data-view="yoy"]');
await page.waitForTimeout(500);
await page.screenshot({ path: 'shot-10y-yoy.png', fullPage: true });
await page.click('[data-range="1"]');
await page.waitForTimeout(500);
await page.screenshot({ path: 'shot-1y-yoy.png', fullPage: true });
await browser.close();
if (errs.length) {
  console.error('\nERRORS:\n  ' + errs.join('\n  '));
  process.exitCode = 1;
} else {
  console.log('\nno errors');
}

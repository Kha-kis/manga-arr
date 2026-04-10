/**
 * Screenshot capture tool for visual verification during redesign.
 * Usage: node screenshot.js <page-path> <output-name>
 * Example: node screenshot.js / library-index
 */
const { chromium } = require('playwright');

const BASE = 'http://127.0.0.1:6789';
const pagePath   = process.argv[2] || '/';
const outputName = process.argv[3] || 'screenshot';

async function run() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
  });
  const page = await context.newPage();

  page.on('pageerror', err => console.log('PAGE_ERR:', err.message));

  await page.goto(BASE + pagePath, { waitUntil: 'networkidle', timeout: 30000 });
  // Let fonts + Alpine settle
  await new Promise(r => setTimeout(r, 1200));

  const path = `/opt/manga-arr/tests/screenshots/${outputName}.png`;
  await page.screenshot({ path, fullPage: true });
  console.log(`Screenshot saved: ${path}`);

  // Also measure a few key elements
  const metrics = await page.evaluate(() => {
    const m = {};
    const head = document.querySelector('.page-head');
    if (head) m.pageHeadHeight = Math.round(head.getBoundingClientRect().height);
    const hero = document.querySelector('.hero-title, .page-head h1');
    if (hero) {
      const cs = getComputedStyle(hero);
      m.heroFont = cs.fontFamily;
      m.heroSize = cs.fontSize;
      m.heroWeight = cs.fontWeight;
    }
    const nums = document.querySelectorAll('.editorial-num .num');
    if (nums.length) {
      const cs = getComputedStyle(nums[0]);
      m.numFont = cs.fontFamily;
      m.numSize = cs.fontSize;
      m.numCount = nums.length;
    }
    const cards = document.querySelectorAll('.manga-card');
    m.cardCount = cards.length;
    if (cards.length) {
      const cs = getComputedStyle(cards[0]);
      m.cardRadius = cs.borderRadius;
    }
    return m;
  });
  console.log('Metrics:', JSON.stringify(metrics, null, 2));

  await browser.close();
}
run().catch(e => { console.error(e); process.exit(1); });

const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:6789';
const pagePath = process.argv[2] || '/';
const outputName = process.argv[3] || 'top';

async function run() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
  const page = await context.newPage();
  await page.goto(BASE + pagePath, { waitUntil: 'networkidle', timeout: 30000 });
  await new Promise(r => setTimeout(r, 1200));
  const path = `/opt/manga-arr/tests/screenshots/${outputName}.png`;
  await page.screenshot({ path, fullPage: false });  // just the viewport
  console.log(`Screenshot (viewport): ${path}`);
  await browser.close();
}
run().catch(e => { console.error(e); process.exit(1); });

/** First-run browser-auth smoke test against the isolated empty database. */
const { chromium } = require('playwright');

const BASE = process.env.MANGARR_TEST_BASE || 'http://127.0.0.1:16789';

async function run() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 375, height: 812 } });
  const page = await context.newPage();
  const errors = [];
  page.on('console', message => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('pageerror', error => errors.push(error.message));

  const response = await page.goto(BASE + '/', {
    waitUntil: 'domcontentloaded',
    timeout: 30000,
  });
  const pathname = new URL(page.url()).pathname;
  if (response.status() !== 200 || pathname !== '/setup') {
    throw new Error(`first run ended at ${pathname} with HTTP ${response.status()}`);
  }

  await page.waitForSelector('#setup-username:focus');
  await page.fill('#setup-username', 'browser-admin');
  const usernameIsValid = await page.$eval(
    '#setup-username',
    input => input.checkValidity(),
  );
  if (!usernameIsValid) throw new Error('valid setup username failed native validation');
  const result = await page.evaluate(() => {
    const controls = [...document.querySelectorAll('.auth-panel input:not([type="hidden"])')];
    const submit = document.querySelector('.auth-panel button[type="submit"]');
    return {
      heading: document.querySelector('h1')?.textContent.trim(),
      controls: controls.map(control => ({
        id: control.id,
        height: control.getBoundingClientRect().height,
      })),
      submitHeight: submit?.getBoundingClientRect().height || 0,
      overflows: document.documentElement.scrollWidth > window.innerWidth + 1,
    };
  });

  if (result.heading !== 'Create administrator') throw new Error('setup heading missing');
  if (result.controls.length !== 3) throw new Error('setup controls missing');
  if (result.controls.some(control => control.height < 44)) {
    throw new Error(`setup control below 44px: ${JSON.stringify(result.controls)}`);
  }
  if (result.submitHeight < 44) throw new Error('setup submit target below 44px');
  if (result.overflows) throw new Error('setup page overflows mobile viewport');
  if (errors.length) throw new Error(`setup page errors: ${errors.join('; ')}`);

  await browser.close();
  console.log('[OK] First-run setup page redirects, focuses, fits, and meets touch targets');
}

run().catch(error => {
  console.error('[FAIL] First-run setup page:', error.message);
  process.exit(1);
});

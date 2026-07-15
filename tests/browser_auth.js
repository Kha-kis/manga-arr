const USERNAME = process.env.MANGARR_TEST_USERNAME || 'browser-admin';
const PASSWORD = process.env.MANGARR_TEST_PASSWORD || 'mangarr-browser-test-password';

async function authenticate(page, baseUrl) {
  await page.goto(baseUrl + '/login', {
    waitUntil: 'domcontentloaded',
    timeout: 30000,
  });
  if (new URL(page.url()).pathname === '/setup') {
    throw new Error('test administrator is not seeded');
  }
  await page.fill('#login-username', USERNAME);
  await page.fill('#login-password', PASSWORD);
  await Promise.all([
    page.waitForURL(url => !url.pathname.endsWith('/login'), { timeout: 30000 }),
    page.click('button[type="submit"]'),
  ]);
}

module.exports = { authenticate };

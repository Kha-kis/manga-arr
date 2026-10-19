const fs = require('node:fs');
const path = require('node:path');
const { authenticate } = require('./browser_auth');

async function allSettledOrThrow(tasks) {
  const settled = await Promise.allSettled(tasks);
  const failed = settled.find(result => result.status === 'rejected');
  if (failed) throw failed.reason;
  return settled.map(result => result.value);
}

async function concurrentSessions(browser, baseUrl, exercise, options = {}) {
  const sessions = [];
  const errors = [];
  const begun = Date.now();
  const artifactRoot = options.artifactRoot;
  const login = options.authenticate || authenticate;
  const origin = new URL(baseUrl).origin;
  const routes = new Set(['/login', '/setup', '/', '/series/40']);
  let failed = false;

  function route(url) {
    try {
      const parsed = new URL(url);
      return parsed.origin === origin && routes.has(parsed.pathname) ? parsed.pathname : null;
    } catch (_) { return null; }
  }

  function record(session, event) {
    if (session.events.length < 200) session.events.push({ ms: Date.now() - begun, ...event });
  }

  async function retainFailure() {
    if (!artifactRoot) return;
    const directory = path.join(artifactRoot, 'e3-3');
    fs.mkdirSync(directory, { mode: 0o700 });
    await Promise.all(sessions.map(async session => {
      if (session.page) {
        session.route = route(session.page.url());
        try {
          await session.page.screenshot({ path: path.join(directory, `session-${session.index}.png`), timeout: 5000 });
        } catch (_) { record(session, { type: 'screenshot-unavailable' }); }
      }
      if (session.tracing) {
        try {
          await session.context.tracing.stop({ path: path.join(directory, `session-${session.index}.zip`) });
          session.tracing = false;
        } catch (_) { record(session, { type: 'trace-unavailable' }); }
      }
    }));
    // Raw errors, URLs, headers, bodies and credentials never enter this summary.
    fs.writeFileSync(path.join(directory, 'events.json'), JSON.stringify({
      sessions: sessions.map(({ index, events, route: currentRoute }) => ({ index, route: currentRoute, events })),
    }, null, 2), { mode: 0o600 });
    console.log('[E3.3] Private failure artifacts retained in ' + directory);
  }

  try {
    const pages = await allSettledOrThrow([0, 1, 2].map(async index => {
      const context = await browser.newContext();
      const session = { index, context, events: [], tracing: false };
      sessions.push(session);
      if (artifactRoot) {
        await context.tracing.start({ screenshots: true, snapshots: true, sources: false });
        session.tracing = true;
      }
      const page = await context.newPage();
      session.page = page;
      page.on('pageerror', error => {
        errors.push(error.message);
        record(session, { type: 'pageerror' });
      });
      page.on('request', request => {
        const target = route(request.url());
        if (target) record(session, { type: 'request', route: target, method: request.method() });
      });
      page.on('response', response => {
        const target = route(response.url());
        if (target) record(session, { type: 'response', route: target, method: response.request().method(), status: response.status() });
      });
      page.on('requestfailed', request => {
        const target = route(request.url());
        if (target) record(session, { type: 'requestfailed', route: target, method: request.method() });
      });
      page.on('framenavigated', frame => {
        const target = route(frame.url());
        if (target) record(session, { type: 'navigation', route: target });
      });
      record(session, { type: 'ready' });
      return page;
    }));
    await allSettledOrThrow(pages.map(async (page, index) => {
      const session = sessions.find(item => item.index === index);
      record(session, { type: 'login-start' });
      await login(page, baseUrl);
      record(session, { type: 'login-complete' });
    }));
    await exercise(pages, errors);
  } catch (error) {
    failed = true;
    try { await retainFailure(); }
    catch (_) { console.warn('[E3.3] Failure artifact capture unavailable'); }
    throw error;
  } finally {
    const closed = await Promise.allSettled(sessions.map(async session => {
      try {
        if (session.tracing) await session.context.tracing.stop();
      } finally {
        await session.context.close();
      }
    }));
    if (!failed && closed.some(result => result.status === 'rejected')) {
      throw new Error('E3.3 context cleanup failed');
    }
  }
}

module.exports = { concurrentSessions };

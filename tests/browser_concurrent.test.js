const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { test } = require('node:test');

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'mangarr-concurrent-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}

function fakeBrowser() {
  const contexts = [];
  return {
    contexts,
    async newContext() {
      const page = new EventEmitter();
      page.url = () => 'http://127.0.0.1/login?secret=do-not-record';
      page.screenshot = async ({ path: file }) => fs.writeFileSync(file, 'screenshot');
      const context = {
        page,
        closed: false,
        tracing: {
          async start() { context.tracingStarted = true; },
          async stop(options) {
            if (options?.path) fs.writeFileSync(options.path, 'trace');
          },
        },
        async newPage() { return page; },
        async close() { context.closed = true; },
      };
      contexts.push(context);
      return context;
    },
  };
}

test('captures early page errors and closes every concurrent context on failure', async t => {
  const { concurrentSessions } = require('./browser_concurrent');
  const root = fixture(t);
  const browser = fakeBrowser();
  const original = new Error('original login failure');
  await assert.rejects(concurrentSessions(browser, 'http://127.0.0.1', async () => {}, {
    artifactRoot: root,
    authenticate: async page => {
      assert.equal(page.listenerCount('pageerror'), 1);
      page.emit('pageerror', new Error('pre-login failure with secret'));
      throw original;
    },
  }), error => error === original);
  assert.equal(browser.contexts.length, 3);
  assert.ok(browser.contexts.every(c => c.closed));
  const report = JSON.parse(fs.readFileSync(path.join(root, 'e3-3', 'events.json')));
  assert.equal(report.sessions.length, 3);
  assert.ok(report.sessions.every(s => s.events.some(e => e.type === 'pageerror')));
  assert.ok(!JSON.stringify(report).includes('secret'));
  assert.ok(fs.existsSync(path.join(root, 'e3-3', 'session-0.zip')));
  assert.equal(fs.statSync(path.join(root, 'e3-3')).mode & 0o777, 0o700);
});

test('records only allowlisted request routes and status, never credentials or query strings', async t => {
  const { concurrentSessions } = require('./browser_concurrent');
  const root = fixture(t);
  const browser = fakeBrowser();
  await assert.rejects(concurrentSessions(browser, 'http://127.0.0.1', async () => {
    throw new Error('retain evidence');
  }, {
    artifactRoot: root,
    authenticate: async page => {
      page.emit('response', {
        url: () => 'http://127.0.0.1/login?password=secret',
        status: () => 303,
        request: () => ({ method: () => 'POST' }),
      });
      page.emit('response', {
        url: () => 'https://other.example/api/private-secret',
        status: () => 401,
        request: () => ({ method: () => 'GET' }),
      });
    },
  }));
  const text = fs.readFileSync(path.join(root, 'e3-3', 'events.json'), 'utf8');
  const report = JSON.parse(text);
  assert.ok(report.sessions[0].events.some(e => e.route === '/login' && e.status === 303));
  assert.ok(!text.includes('secret'));
  assert.ok(!text.includes('other.example'));
});

test('successful sessions retain no artifacts, expose early errors, and remain truly concurrent', async t => {
  const { concurrentSessions } = require('./browser_concurrent');
  const root = fixture(t);
  const browser = fakeBrowser();
  let started = 0;
  let release;
  const barrier = new Promise(resolve => { release = resolve; });
  await concurrentSessions(browser, 'http://127.0.0.1', async (pages, errors) => {
    assert.equal(pages.length, 3);
    assert.equal(errors.length, 3);
  }, {
    artifactRoot: root,
    authenticate: async page => {
      page.emit('pageerror', new Error('early error'));
      if (++started === 3) release();
      await barrier;
    },
  });
  assert.ok(browser.contexts.every(c => c.closed));
  assert.deepEqual(fs.readdirSync(root), []);
});

test('partial context setup failure closes contexts that did open', async () => {
  const { concurrentSessions } = require('./browser_concurrent');
  const browser = fakeBrowser();
  const create = browser.newContext.bind(browser);
  let calls = 0;
  browser.newContext = async () => {
    if (++calls === 2) throw new Error('context creation failed');
    return create();
  };
  await assert.rejects(concurrentSessions(browser, 'http://127.0.0.1', async () => {}), /context creation failed/);
  assert.equal(browser.contexts.length, 2);
  assert.ok(browser.contexts.every(c => c.closed));
});

test('artifact failures do not replace the original failure or skip context cleanup', async t => {
  const { concurrentSessions } = require('./browser_concurrent');
  const root = fixture(t);
  const blocked = path.join(root, 'file');
  fs.writeFileSync(blocked, 'not a directory');
  const browser = fakeBrowser();
  const original = new Error('original failure');
  await assert.rejects(concurrentSessions(browser, 'http://127.0.0.1', async () => { throw original; }, {
    artifactRoot: blocked,
    authenticate: async () => {},
  }), error => error === original);
  assert.ok(browser.contexts.every(c => c.closed));
});

test('failed screenshots and trace stop preserve the original error and close all contexts', async t => {
  const { concurrentSessions } = require('./browser_concurrent');
  const root = fixture(t);
  const browser = fakeBrowser();
  const create = browser.newContext.bind(browser);
  browser.newContext = async () => {
    const context = await create();
    context.page.screenshot = async () => { throw new Error('screenshot failed'); };
    context.tracing.stop = async () => { throw new Error('trace stop failed'); };
    return context;
  };
  const original = new Error('original login failure');
  await assert.rejects(concurrentSessions(browser, 'http://127.0.0.1', async () => {}, {
    artifactRoot: root,
    authenticate: async () => { throw original; },
  }), error => error === original);
  assert.ok(browser.contexts.every(c => c.closed));
  const report = JSON.parse(fs.readFileSync(path.join(root, 'e3-3', 'events.json')));
  assert.ok(report.sessions.every(s => s.events.some(e => e.type === 'trace-unavailable')));
  assert.ok(report.sessions.every(s => s.events.some(e => e.type === 'screenshot-unavailable')));
});

async function readTrace(file) {
  const { yauzl } = require('playwright-core/lib/zipBundle');
  return new Promise((resolve, reject) => {
    yauzl.open(file, { lazyEntries: true }, (error, archive) => {
      if (error) return reject(error);
      archive.on('error', reject);
      archive.on('end', () => reject(new Error('missing trace events')));
      archive.on('entry', entry => {
        if (!entry.fileName.endsWith('.trace')) return archive.readEntry();
        archive.openReadStream(entry, (error, stream) => {
          if (error) { archive.close(); reject(error); return; }
          const chunks = [];
          stream.on('error', reject);
          stream.on('data', chunk => chunks.push(chunk));
          stream.on('end', () => {
            archive.close();
            resolve(Buffer.concat(chunks).toString('utf8'));
          });
        });
      });
      archive.readEntry();
    });
  });
}

for (const duringLogin of [false, true]) {
test('real browser retains readable failure evidence before teardown, duringLogin=' + duringLogin, async t => {
  const { concurrentSessions } = require('./browser_concurrent');
  const { chromium } = require('playwright');
  const root = fixture(t);
  const server = http.createServer((req, res) => {
    if (req.method === 'POST') {
      req.resume();
      res.writeHead(303, { Location: '/home' });
      res.end();
      return;
    }
    res.setHeader('Content-Type', 'text/html');
    res.end(req.url === '/login'
      ? '<form method="post"><input id="login-username"><input id="login-password" type="password"><button type="submit">Login</button></form>'
      : '<h1>Isolated test</h1>');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const browser = await chromium.launch({ headless: true });
  t.after(() => browser.close());
  const base = `http://127.0.0.1:${server.address().port}`;
  const options = { artifactRoot: root };
  if (duringLogin) options.authenticate = async (page, baseUrl) => {
    await page.goto(baseUrl + '/login');
    await page.click('#missing-submit', { timeout: 50 });
  };
  await assert.rejects(concurrentSessions(browser, base, async () => {
    throw new Error('intentional diagnostic probe');
  }, options), duringLogin ? /Timeout/ : /intentional diagnostic probe/);
  assert.equal(browser.contexts().length, 0);
  for (let i = 0; i < 3; i++) {
    const trace = await readTrace(path.join(root, 'e3-3', `session-${i}.zip`));
    const events = trace.trim().split('\n').map(line => JSON.parse(line));
    assert.ok(events.some(e => e.type === 'before' && e.method === (duringLogin ? 'click' : 'fill')));
    const screenshot = fs.readFileSync(path.join(root, 'e3-3', `session-${i}.png`));
    assert.equal(screenshot.subarray(1, 4).toString(), 'PNG');
  }
});
}

function runnerProbe(t, scenario = {}) {
  const root = fixture(t);
  const repo = path.join(root, 'repo');
  const bin = path.join(root, 'bin');
  fs.mkdirSync(path.join(repo, 'tests'), { recursive: true });
  fs.mkdirSync(bin);
  fs.copyFileSync(path.join(__dirname, 'run_isolated_browser.sh'), path.join(repo, 'tests/run_isolated_browser.sh'));
  fs.writeFileSync(path.join(repo, 'docker-compose.test.yml'), 'services: {}\n');
  fs.writeFileSync(path.join(repo, 'tests/seed_test_db.sh'), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
  const calls = path.join(root, 'docker-calls');
  const id = 'a'.repeat(64);
  fs.writeFileSync(path.join(bin, 'docker'), `#!/usr/bin/env node
const fs = require('node:fs');
const args = process.argv.slice(2);
const scenario = ${JSON.stringify(scenario)};
fs.appendFileSync(${JSON.stringify(calls)}, JSON.stringify(args) + '\\n');
if (args[0] === 'inspect') {
  if (args.join(' ').includes('Health.Status')) console.log(scenario.healthFailure ? 'starting' : 'healthy');
  else if (args.join(' ').includes('com.docker.compose.project.working_dir')) console.log(scenario.wrongDirectory ? '/other' : ${JSON.stringify(repo)});
  else if (args.join(' ').includes('com.docker.compose.project')) console.log(scenario.wrongProject ? 'other-project' : 'mangarr-test');
  else if (args.join(' ').includes('.Id')) console.log('${id}');
  else console.log('{}');
} else if (args[0] === 'compose' && args.includes('up') && scenario.startupFailure) process.exit(9);
else if (args[0] === 'compose' && args.includes('down') && scenario.downFailure) process.exit(12);
else if (args[0] === 'compose' && args.includes('ps')) {
  if (!scenario.exited || args.includes('--all')) console.log('${id}');
} else if (args[0] === 'logs') {
  console.log('owned test container diagnostic');
  if (scenario.logFailure) process.exit(11);
} else if (args[0] === 'ps' && scenario.occupied && (!scenario.stoppedOnly || args.includes('-a'))) console.log('mangarr-test');
`, { mode: 0o755 });
  const realNode = process.execPath;
  // The docker stub uses the real node binary; browser entrypoints use this stub.
  fs.writeFileSync(path.join(bin, 'node'), `#!/bin/sh
case "$1" in
  *browser_e2e.js) exit 7 ;;
  *browser_auth_setup.js|*browser_smoke.js|*browser_integration.js|*browser_settings_e2e.js) exit 0 ;;
  *) exec ${JSON.stringify(realNode)} "$@" ;;
esac
`, { mode: 0o755 });
  fs.writeFileSync(path.join(bin, 'sleep'), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
  let output;
  assert.throws(() => execFileSync('bash', ['tests/run_isolated_browser.sh', 'e2e'], {
    cwd: repo,
    env: { ...process.env, PATH: bin + ':' + process.env.PATH },
    stdio: 'pipe',
  }), error => {
    output = error.stdout.toString();
    return error.status === (scenario.startupFailure ? 9 : scenario.healthFailure || scenario.occupied ? 1 : 7);
  });
  const commands = fs.readFileSync(calls, 'utf8').trim().split('\n').map(JSON.parse);
  return { repo, commands, id, output };
}

for (const scenario of [{}, { startupFailure: true, exited: true }, { exited: true }, { logFailure: true }, { downFailure: true }, { healthFailure: true }]) {
test('isolated runner retains owned logs before teardown and original failure status: ' + JSON.stringify(scenario), t => {
  const { repo, commands, id, output } = runnerProbe(t, scenario);
  const logIndex = commands.findIndex(args => args[0] === 'logs');
  const downIndex = commands.findIndex(args => args[0] === 'compose' && args.includes('down'));
  assert.ok(logIndex >= 0 && logIndex < downIndex);
  assert.ok(commands[logIndex].includes(id));
  const artifactRuns = fs.readdirSync(path.join(repo, '.test-artifacts'));
  assert.equal(artifactRuns.length, 1);
  const logs = fs.readFileSync(path.join(repo, '.test-artifacts', artifactRuns[0], 'container.log'), 'utf8');
  assert.match(logs, /owned test container diagnostic/);
  assert.ok(!output.includes('owned test container diagnostic'));
});
}

for (const scenario of [{ wrongProject: true }, { wrongDirectory: true }]) {
test('isolated runner refuses diagnostics from mismatched ownership: ' + JSON.stringify(scenario), t => {
  const { commands } = runnerProbe(t, scenario);
  assert.ok(!commands.some(args => args[0] === 'logs'));
});
}

for (const stoppedOnly of [false, true]) {
test('isolated runner never tears down a pre-existing container, stoppedOnly=' + stoppedOnly, t => {
  const { commands } = runnerProbe(t, { occupied: true, stoppedOnly });
  assert.ok(!commands.some(args => args[0] === 'compose'));
});
}

test('CI failure fallback never emits raw container logs or config listings', () => {
  const workflow = fs.readFileSync(path.join(__dirname, '../.github/workflows/test.yml'), 'utf8');
  assert.doesNotMatch(workflow, /docker logs|ls -la \.test-config/);
  assert.match(workflow, /events\.json/);
});

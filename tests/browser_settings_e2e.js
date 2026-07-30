/**
 * Isolated-only Settings E2E regression.
 *
 * This suite mutates settings and root folders. It must run last against the
 * disposable isolated database and must never fall back to the live app.
 */
const EXPECTED_BASE = 'http://127.0.0.1:16789';
const BASE = (process.env.MANGARR_TEST_BASE || '').replace(/\/+$/, '');
const CONTAINER = process.env.MANGARR_TEST_CONTAINER || '';

if (BASE !== EXPECTED_BASE || CONTAINER !== 'mangarr-test') {
  console.error(
    '[FAIL] Settings E2E requires MANGARR_TEST_BASE=http://127.0.0.1:16789 '
    + 'and MANGARR_TEST_CONTAINER=mangarr-test',
  );
  process.exit(2);
}

const { chromium } = require('playwright');
const { authenticate } = require('./browser_auth');

const results = [];

function ok(name) {
  results.push({ name, pass: true });
  console.log('  [OK]   ' + name);
}

function fail(name, detail) {
  results.push({ name, pass: false, detail });
  console.log('  [FAIL] ' + name + (detail ? ': ' + detail : ''));
}

function requireResult(condition, message) {
  if (!condition) throw new Error(message);
}

async function run() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  await authenticate(page, BASE);

  const consoleErrors = [];
  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  page.on('pageerror', error => consoleErrors.push('PAGEERROR: ' + error.message));

  const rootLabelPrefix = 'RC8 Browser Root';
  const baselineRootLabel = 'RC8 Existing Default Root';
  const rootA = `${rootLabelPrefix} A`;
  const rootB = `${rootLabelPrefix} B`;
  let initialRootState = null;
  let originalRootState = null;
  let syntheticBaselineRoot = false;

  async function submitHtmxSettings(formSelector) {
    const form = page.locator(formSelector);
    const actionPath = await form.evaluate(node => new URL(node.action).pathname);
    const responsePromise = page.waitForResponse(response => (
      new URL(response.url()).pathname === actionPath
      && response.request().method() === 'POST'
    ));
    const afterRequestPromise = page.evaluate(selector => new Promise((resolve, reject) => {
      const formNode = document.querySelector(selector);
      const timer = setTimeout(() => {
        document.body.removeEventListener('htmx:afterRequest', listener);
        reject(new Error('timed out waiting for successful htmx:afterRequest'));
      }, 10000);
      const listener = event => {
        if (!event.detail || event.detail.elt !== formNode) return;
        clearTimeout(timer);
        document.body.removeEventListener('htmx:afterRequest', listener);
        resolve(event.detail.successful === true);
      };
      document.body.addEventListener('htmx:afterRequest', listener);
    }), formSelector);
    await form.evaluate(node => {
      node.requestSubmit(node.querySelector('button[type="submit"]'));
    });
    const [response, successful] = await Promise.all([responsePromise, afterRequestPromise]);
    requireResult(successful, `HTMX save to ${actionPath} was not successful`);
    return response;
  }

  async function submitAbortedHtmxSettings(formSelector) {
    const form = page.locator(formSelector);
    const expectedConsoleStart = consoleErrors.length;
    const abortSettingsPost = async route => {
      const request = route.request();
      if (request.method() === 'POST'
          && new URL(request.url()).pathname === '/settings') {
        await route.abort('failed');
      } else {
        await route.continue();
      }
    };
    await page.route('**/settings', abortSettingsPost);
    try {
      const failedRequest = page.waitForEvent('requestfailed', {
        predicate: request => (
          request.method() === 'POST'
          && new URL(request.url()).pathname === '/settings'
        ),
        timeout: 10000,
      });
      const afterRequest = page.evaluate(selector => new Promise((resolve, reject) => {
        const formNode = document.querySelector(selector);
        const timer = setTimeout(() => {
          document.body.removeEventListener('htmx:afterRequest', listener);
          reject(new Error('timed out waiting for failed htmx:afterRequest'));
        }, 10000);
        const listener = event => {
          if (!event.detail || event.detail.elt !== formNode) return;
          clearTimeout(timer);
          document.body.removeEventListener('htmx:afterRequest', listener);
          resolve(event.detail.successful !== true);
        };
        document.body.addEventListener('htmx:afterRequest', listener);
      }), formSelector);
      await form.evaluate(node => {
        node.requestSubmit(node.querySelector('button[type="submit"]'));
      });
      const [, unsuccessful] = await Promise.all([failedRequest, afterRequest]);
      requireResult(unsuccessful, 'aborted HTMX request was not reported as unsuccessful');
    } finally {
      await page.unroute('**/settings', abortSettingsPost);
      const inducedErrors = consoleErrors.splice(expectedConsoleStart);
      const unexpectedErrors = inducedErrors.filter(error => (
        !error.includes('ERR_FAILED')
        && error !== 'htmx:afterRequest'
        && error !== 'htmx:sendError'
      ));
      consoleErrors.push(...unexpectedErrors);
    }
  }

  async function beforeUnloadArmed() {
    return page.evaluate(() => {
      const event = new Event('beforeunload', { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented || event.returnValue === '';
    });
  }

  async function setKomgaScan(checked) {
    await page.locator('#komga_scan_enabled').evaluate((node, value) => {
      node.checked = value;
      node.dispatchEvent(new Event('change', { bubbles: true }));
    }, checked);
  }

  async function submitPlainForm(form, confirm = false) {
    const actionPath = await form.evaluate(node => new URL(node.action).pathname);
    const responsePromise = page.waitForResponse(response => (
      new URL(response.url()).pathname === actionPath
      && response.request().method() === 'POST'
    ));
    const navigationPromise = page.waitForNavigation({
      waitUntil: 'domcontentloaded',
      timeout: 30000,
    });
    await form.locator('button').click();
    if (confirm) {
      await page.waitForSelector('#globalConfirmModal.show', { timeout: 3000 });
      await page.evaluate(() => document.getElementById('globalConfirmOk').click());
    }
    const [response, navigation] = await Promise.all([responsePromise, navigationPromise]);
    requireResult(
      response.status() === 303,
      `${actionPath} plain POST returned HTTP ${response.status()}`,
    );
    requireResult(
      navigation && navigation.ok() && new URL(page.url()).pathname === '/settings',
      `${actionPath} did not redirect to a successful settings page`,
    );
  }

  async function addRootFolder(path, label, isDefault = false) {
    const form = page.locator('form[action="/settings/root-folders/add"]');
    await form.locator('input[name="path"]').fill(path);
    await form.locator('input[name="label"]').fill(label);
    if (isDefault) await form.locator('input[name="is_default"]').check();
    await submitPlainForm(form);
  }

  function rootRows() {
    return page.locator('#panel-media > .panel tbody tr');
  }

  async function readRootState() {
    return rootRows().evaluateAll(rows => rows.map(row => {
      const cells = row.querySelectorAll(':scope > td');
      return {
        path: cells[0].textContent.trim(),
        label: cells[1].textContent.trim(),
        isDefault: Boolean(row.querySelector('.badge-status')),
      };
    }).sort((a, b) => a.path.localeCompare(b.path)));
  }

  async function rootRowByPath(path) {
    const rows = rootRows();
    const paths = await rows.locator('td:first-child').allTextContents();
    const index = paths.findIndex(value => value.trim() === path);
    requireResult(index >= 0, `root folder not found: ${path}`);
    return rows.nth(index);
  }

  async function deleteRootFolderByPath(path) {
    const row = await rootRowByPath(path);
    const deleteForm = row.locator('form[action$="/delete"]');
    requireResult(await deleteForm.count() === 1, `root folder has no delete form: ${path}`);
    await submitPlainForm(deleteForm, true);
  }

  async function deleteTemporaryRootFolders() {
    await page.goto(BASE + '/settings', { waitUntil: 'domcontentloaded', timeout: 30000 });
    for (let attempt = 0; attempt < 4; attempt++) {
      const row = rootRows().filter({ hasText: rootLabelPrefix }).first();
      if (await row.count() === 0) return;
      const deleteForm = row.locator('form[action$="/delete"]');
      requireResult(await deleteForm.count() === 1, 'temporary root folder has no delete form');
      await submitPlainForm(deleteForm, true);
    }
    throw new Error('temporary root folders remained after cleanup');
  }

  async function restoreOriginalRootState() {
    await deleteTemporaryRootFolders();
    const expectedDefault = originalRootState.find(root => root.isDefault);
    let currentState = await readRootState();
    const currentDefault = currentState.find(root => root.isDefault);
    if (expectedDefault && (!currentDefault || currentDefault.path !== expectedDefault.path)) {
      const row = await rootRowByPath(expectedDefault.path);
      await submitPlainForm(row.locator('form[action$="/default"]'));
      currentState = await readRootState();
    }
    requireResult(
      JSON.stringify(currentState) === JSON.stringify(originalRootState),
      `root snapshot changed: expected ${JSON.stringify(originalRootState)}, got ${JSON.stringify(currentState)}`,
    );
  }

  console.log('\n=== S4.1: Settings forms, partial saves, and dirty tracking ===');
  try {
    await page.goto(BASE + '/settings', { waitUntil: 'domcontentloaded', timeout: 30000 });
    const originalCategory = await page.locator(
      '#settings-form input[name="category"]',
    ).inputValue();
    const originalKomgaScan = await page.locator('#komga_scan_enabled').isChecked();
    const freshMinSeeders = await page.locator(
      '#settings-form input[name="min_seeders"]',
    ).inputValue();
    requireResult(freshMinSeeders === '0', `fresh min_seeders was ${freshMinSeeders}, expected 0`);
    ok('Fresh effective Minimum Seeders value is 0');

    initialRootState = await readRootState();
    if (initialRootState.length === 0) {
      await addRootFolder(
        '/tmp/mangarr-rc8-existing-default-root',
        baselineRootLabel,
        true,
      );
      syntheticBaselineRoot = true;
    }
    originalRootState = await readRootState();
    requireResult(
      originalRootState.length > 0
      && originalRootState.filter(root => root.isDefault).length === 1,
      `expected a pre-existing default root: ${JSON.stringify(originalRootState)}`,
    );
    ok('Root-folder regression starts with a pre-existing default root');

    await addRootFolder('/tmp/mangarr-rc8-browser-root-a', rootA);
    await addRootFolder('/tmp/mangarr-rc8-browser-root-b', rootB);

    const formState = await page.evaluate(() => {
      const settingsForm = document.getElementById('settings-form');
      const saveButton = document.querySelector('#panel-media button.btn-ember[type="submit"]');
      const rootFolderForms = Array.from(
        document.querySelectorAll('form[action^="/settings/root-folders/"]'),
      );
      return {
        saveOwner: saveButton && saveButton.form ? saveButton.form.id : null,
        actions: rootFolderForms.map(form => new URL(form.action).pathname),
        allPlainPost: rootFolderForms.every(form => (
          form.method.toLowerCase() === 'post' && !form.hasAttribute('hx-post')
        )),
        nestedCount: settingsForm
          ? rootFolderForms.filter(form => settingsForm.contains(form)).length : null,
      };
    });
    requireResult(formState.saveOwner === 'settings-form', JSON.stringify(formState));
    requireResult(formState.nestedCount === 0, JSON.stringify(formState));
    requireResult(formState.allPlainPost, JSON.stringify(formState));
    ok('Media Save is owned by #settings-form and root forms remain outside it');

    const temporaryDeleteCount = await rootRows().filter({ hasText: rootLabelPrefix })
      .locator('form[action$="/delete"]').count();
    requireResult(
      formState.actions.includes('/settings/root-folders/add')
      && formState.actions.some(action => action.endsWith('/default'))
      && temporaryDeleteCount === 2,
      `missing parsed root-folder forms: ${JSON.stringify(formState.actions)}`,
    );
    ok('Add, Set Default, and both temporary Delete forms are plain POST forms');

    const rootBRow = rootRows().filter({ hasText: rootB }).first();
    await submitPlainForm(rootBRow.locator('form[action$="/default"]'));
    requireResult(
      await rootRows().filter({ hasText: rootB })
        .locator('.badge-status').filter({ hasText: 'Default' }).count() === 1,
      'Set Default fallback did not persist',
    );
    ok('Set Default works through plain POST fallback');

    await submitPlainForm(
      rootRows().filter({ hasText: rootB }).locator('form[action$="/delete"]'),
      true,
    );
    requireResult(
      await rootRows().filter({ hasText: rootB }).count() === 0,
      'Delete fallback did not remove the root folder',
    );
    ok('Add and Delete work through plain POST fallback');

    const hiddenValues = await page.locator(
      '#panel-metadata form [name="komga_scan_enabled"]',
    ).evaluateAll(nodes => nodes.map(node => ({ type: node.type, value: node.value })));
    requireResult(
      hiddenValues.length === 2
      && hiddenValues[0].type === 'hidden'
      && hiddenValues[0].value === 'false'
      && hiddenValues[1].type === 'checkbox',
      `invalid Komga checkbox fallback fields: ${JSON.stringify(hiddenValues)}`,
    );

    const testKomgaScan = !originalKomgaScan;
    await setKomgaScan(testKomgaScan);
    const metadataResponse = await submitHtmxSettings('#panel-metadata form');
    requireResult(metadataResponse.ok(), `Metadata save returned HTTP ${metadataResponse.status()}`);
    await page.reload({ waitUntil: 'domcontentloaded' });
    requireResult(
      await page.locator('#komga_scan_enabled').isChecked() === testKomgaScan,
      'Metadata setting did not persist through its own form',
    );
    requireResult(
      await page.locator('#settings-form input[name="min_seeders"]').inputValue() === '0',
      'unrelated Metadata save changed fresh Minimum Seeders',
    );
    ok('Metadata save preserves the fresh Minimum Seeders value of 0');

    const testCategory = `${originalCategory || 'manga'}-settings-e2e`;
    await page.locator('#settings-form input[name="category"]').fill(testCategory);
    await submitAbortedHtmxSettings('#settings-form');
    requireResult(
      await beforeUnloadArmed(),
      'failed HTMX save cleared the unsaved-changes warning',
    );
    ok('Failed HTMX save keeps beforeunload armed');

    const mediaResponse = await submitHtmxSettings('#settings-form');
    requireResult(mediaResponse.ok(), `Media save returned HTTP ${mediaResponse.status()}`);
    requireResult(
      !(await beforeUnloadArmed()),
      'successful HTMX save left the unsaved-changes warning armed',
    );
    ok('Successful HTMX retry clears beforeunload');

    await page.reload({ waitUntil: 'domcontentloaded' });
    requireResult(
      await page.locator('#settings-form input[name="category"]').inputValue() === testCategory,
      'Media category did not persist after reload',
    );
    requireResult(
      await page.locator('#komga_scan_enabled').isChecked() === testKomgaScan,
      'Media save reset the unrelated Metadata setting',
    );
    requireResult(
      await page.locator('#settings-form input[name="min_seeders"]').inputValue() === '0',
      'Media save changed Minimum Seeders from 0',
    );
    ok('Media save persists without resetting unrelated Metadata or Minimum Seeders');
  } catch (error) {
    fail('Settings workflow', error.message);
  } finally {
    const cleanupErrors = [];
    const acceptBeforeUnload = dialog => {
      if (dialog.type() === 'beforeunload') dialog.accept();
      else dialog.dismiss();
    };
    page.on('dialog', acceptBeforeUnload);
    if (originalRootState !== null) {
      try {
        await restoreOriginalRootState();
        ok('Original root-folder list and default restored');
      } catch (error) {
        cleanupErrors.push(`Root-folder restore: ${error.message}`);
      }
    }
    if (syntheticBaselineRoot) {
      try {
        await deleteTemporaryRootFolders();
        const baselinePath = '/tmp/mangarr-rc8-existing-default-root';
        const currentState = await readRootState();
        if (currentState.some(root => root.path === baselinePath)) {
          await deleteRootFolderByPath(baselinePath);
        }
        const restoredInitialState = await readRootState();
        requireResult(
          JSON.stringify(restoredInitialState) === JSON.stringify(initialRootState),
          `initial root state not restored: ${JSON.stringify(restoredInitialState)}`,
        );
        ok('Synthetic pre-existing root fixture removed');
      } catch (error) {
        cleanupErrors.push(`Baseline root cleanup: ${error.message}`);
      }
    }
    page.off('dialog', acceptBeforeUnload);
    if (cleanupErrors.length) {
      fail('Settings root cleanup', cleanupErrors.join('; '));
    }
  }

  if (consoleErrors.length === 0) {
    ok('Zero unexpected console errors');
  } else {
    fail(`${consoleErrors.length} unexpected console errors`, consoleErrors.slice(0, 5).join('; '));
  }

  await browser.close();

  console.log('\n' + '='.repeat(60));
  const passed = results.filter(result => result.pass).length;
  const total = results.length;
  console.log(`RESULTS: ${passed}/${total} passed`);
  if (passed < total) {
    console.log('\nFailures:');
    results.filter(result => !result.pass).forEach(result => {
      console.log(`  - ${result.name}${result.detail ? ': ' + result.detail : ''}`);
    });
    process.exit(1);
  }
}

run().catch(error => {
  console.error('FATAL:', error);
  process.exit(2);
});

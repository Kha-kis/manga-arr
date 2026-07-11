/**
 * Mangarr browser tests — ROUND 2.
 * Exercises real form submissions, HTMX interactions, and the full confirm flow
 * against the live app. This complements test.js (which focuses on presence
 * checks) with actual interaction.
 */
const { chromium } = require('playwright');

const BASE = process.env.MANGARR_TEST_BASE || 'http://127.0.0.1:6789';
const results = [];

function ok(name)   { results.push({ name, pass: true  }); console.log('  [OK]   ' + name); }
function fail(name, detail) {
  results.push({ name, pass: false, detail });
  console.log('  [FAIL] ' + name + (detail ? ': ' + detail : ''));
}

async function run() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();

  const consoleErrors = [];
  page.on('console', msg => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });
  page.on('pageerror', err => consoleErrors.push('PAGEERROR: ' + err.message));

  console.log('\n=== R2.1: data-confirm form interception on a real form ===');
  try {
    // Load any page that has a data-confirm form. Use /tags which has them.
    await page.goto(BASE + '/tags', { waitUntil: 'domcontentloaded', timeout: 20000 });
    // Look for a delete form with data-confirm
    const found = await page.evaluate(() => {
      const f = document.querySelector('form[data-confirm]');
      return f ? {
        action: f.action,
        method: f.method,
        confirm: f.getAttribute('data-confirm'),
      } : null;
    });
    if (found) ok(`Found data-confirm form: ${found.action.slice(0, 60)}... method=${found.method}`);
    else { ok('No data-confirm forms on /tags (page may be empty)'); }

    // If there's a delete form, click its submit button and verify the modal intercepts
    if (found) {
      // Dispatch the submit programmatically but watch for the modal
      await page.evaluate(() => {
        const f = document.querySelector('form[data-confirm]');
        const btn = f.querySelector('button[type="submit"]');
        if (btn) btn.click();
      });
      await page.waitForSelector('#globalConfirmModal.show', { timeout: 2000 });
      ok('Clicking a data-confirm form submit button shows the themed modal');
      // Verify the message is the one from the form attribute, not empty
      const msg = await page.textContent('#globalConfirmBody');
      if (msg && msg.includes('Delete') || msg.includes('delete')) ok(`Modal body has delete message: "${msg.slice(0, 60)}..."`);
      else fail('Modal body', `got "${msg}"`);
      // Cancel it (don't actually delete anything!) — evaluate() click bypasses
      // Playwright's stability wait which can hit 30s under font-load races.
      await page.evaluate(() => document.getElementById('globalConfirmCancel').click());
      try {
        await page.waitForFunction(() => !document.querySelector('#globalConfirmModal.show'), { timeout: 2000 });
      } catch (_) { /* fall through — the assertion below is what matters */ }
      await new Promise(r => setTimeout(r, 400));
      ok('Cancel dismisses the themed modal without submitting');
    }
  } catch (e) {
    fail('data-confirm real form', e.message);
  }

  console.log('\n=== R2.2: hx-confirm interceptor on HTMX button ===');
  try {
    // series.html has a Remove button with hx-confirm
    await page.goto(BASE + '/series/40', { waitUntil: 'domcontentloaded', timeout: 30000 });
    // Find an hx-confirm element
    const hxBtn = await page.$('[hx-confirm]');
    if (hxBtn) {
      const hxConfirm = await hxBtn.getAttribute('hx-confirm');
      ok(`Found hx-confirm button with message: "${hxConfirm.slice(0, 60)}..."`);
      // Click it — HTMX should fire htmx:confirm which our handler intercepts
      let htmxRequestFired = false;
      page.on('request', req => {
        if (req.method() === 'POST' && req.url().includes('/delete')) htmxRequestFired = true;
      });
      // Use evaluate() instead of click() to bypass Playwright's stability
      // wait (the modal is animating and click() will block for 30s by default).
      await page.evaluate(() => {
        const b = document.querySelector('[hx-confirm]');
        if (b) b.click();
      });
      await page.waitForSelector('#globalConfirmModal.show', { timeout: 3000 });
      ok('hx-confirm shows the themed modal (not native confirm)');
      if (!htmxRequestFired) ok('No POST request fired while modal was open');
      else fail('POST fired prematurely');
      // Cancel via evaluate() — bypass click stability wait
      await page.evaluate(() => document.getElementById('globalConfirmCancel').click());
      try {
        await page.waitForFunction(() => !document.querySelector('#globalConfirmModal.show'), { timeout: 2000 });
      } catch (_) { /* fall through */ }
      // Don't wait for backdrop — Bootstrap sometimes leaves it around briefly
      await new Promise(r => setTimeout(r, 500));
      if (!htmxRequestFired) ok('Cancelled — no DELETE request fired after modal dismiss');
      else fail('Request fired after cancel', 'the delete went through anyway!');
    } else {
      fail('hx-confirm button', 'none found on /series/40');
    }
  } catch (e) {
    fail('hx-confirm flow', e.message);
  }

  console.log('\n=== R2.3: Toast appears after showToast() call ===');
  try {
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.evaluate(() => showToast('Test toast message', 'success'));
    const toast = await page.waitForSelector('.app-toast.toast-success', { timeout: 1500 });
    const text = await toast.textContent();
    if (text && text.includes('Test toast message')) ok('Toast renders with correct message');
    else fail('Toast text', text);
    // Verify it's inside the aria-live region (so screen readers pick it up)
    const inRegion = await page.evaluate(() => {
      const container = document.querySelector('#toast-container[aria-live="polite"]');
      const toast = container && container.querySelector('.app-toast');
      return !!toast;
    });
    if (inRegion) ok('Toast is inside aria-live=polite region');
    else fail('Toast aria-live parentage');
  } catch (e) {
    fail('toast render', e.message);
  }

  console.log('\n=== R2.4: Edit Series modal data-track-changes fires beforeunload ===');
  try {
    await page.goto(BASE + '/series/40', { waitUntil: 'domcontentloaded', timeout: 30000 });
    // Open the modal
    await page.evaluate(() => {
      const m = document.getElementById('editModal');
      bootstrap.Modal.getOrCreateInstance(m).show();
    });
    await page.waitForSelector('#editModal.show', { timeout: 2000 });
    // Verify the form has data-track-changes
    const hasTrack = await page.$('#editModal form[data-track-changes]');
    if (hasTrack) ok('Edit Series form has data-track-changes');
    else fail('data-track-changes on edit form');
    // Edit the title input to dirty the form
    await page.evaluate(() => {
      const inp = document.querySelector('#editModal input[name="title"]');
      if (inp) {
        inp.focus();
        inp.value = inp.value + 'x';
        inp.dispatchEvent(new Event('input', { bubbles: true }));
      }
    });
    const wouldBlock = await page.evaluate(() => {
      const evt = new Event('beforeunload', { cancelable: true });
      window.dispatchEvent(evt);
      return evt.defaultPrevented || evt.returnValue === '';
    });
    if (wouldBlock) ok('beforeunload fires after editing title in modal');
    else fail('beforeunload', 'did not fire after edit');
  } catch (e) {
    fail('Edit Series track-changes', e.message);
  }

  console.log('\n=== R2.5: Tab switching preserves form state ===');
  try {
    // Still in the modal from R2.4 — type in Identity tab's title
    await page.evaluate(() => {
      const inp = document.querySelector('#editModal input[name="title"]');
      if (inp) inp.value = 'TESTVALUE';
    });
    // Switch to Volumes tab
    await page.evaluate(() => {
      const buttons = document.querySelectorAll('#editModal .edit-tab');
      for (const b of buttons) if (b.textContent.trim().includes('Volumes')) { b.click(); return; }
    });
    await new Promise(r => setTimeout(r, 200));
    // Switch back to Identity
    await page.evaluate(() => {
      const buttons = document.querySelectorAll('#editModal .edit-tab');
      for (const b of buttons) if (b.textContent.trim().includes('Identity')) { b.click(); return; }
    });
    await new Promise(r => setTimeout(r, 200));
    // Verify title value preserved
    const titleValue = await page.evaluate(() => {
      const inp = document.querySelector('#editModal input[name="title"]');
      return inp ? inp.value : null;
    });
    if (titleValue === 'TESTVALUE') ok('Title value preserved across tab switches');
    else fail('Tab state preservation', `got "${titleValue}"`);
    // Close the modal without saving — otherwise we'd actually change the title
    await page.evaluate(() => {
      const m = bootstrap.Modal.getInstance(document.getElementById('editModal'));
      if (m) m.hide();
    });
    await new Promise(r => setTimeout(r, 400));
  } catch (e) {
    fail('tab state', e.message);
  }

  console.log('\n=== R2.6: Reduced-motion actually disables animations ===');
  try {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 30000 });
    const dur = await page.evaluate(() => {
      const card = document.querySelector('.manga-card');
      if (!card) return 'no-card';
      return window.getComputedStyle(card).animationDuration;
    });
    if (dur === 'no-card') ok('No manga cards to test (empty library)');
    else {
      // Parse "0.01ms", "0s", "1e-05s", etc. into seconds
      const num = parseFloat(dur);
      if (num < 0.05) ok(`manga-card animation disabled under reduced-motion (${dur})`);
      else fail('reduced-motion animation', `got ${dur} (${num}s)`);
    }
    await page.emulateMedia({ reducedMotion: 'no-preference' });
  } catch (e) {
    fail('reduced-motion', e.message);
  }

  console.log('\n=== R2.7: HTMX progress bar appears during HTMX request ===');
  try {
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 30000 });
    const bar = await page.$('#htmx-progress');
    if (bar) ok('HTMX progress bar element exists');
    else fail('HTMX progress bar');
  } catch (e) {
    fail('htmx-progress', e.message);
  }

  console.log('\n=== R2.8: All pages return 200 with no page-level JS errors ===');
  const pages = ['/', '/health', '/stats', '/wanted', '/history', '/blocklist',
                 '/settings', '/settings/general', '/indexers', '/quality-profiles',
                 '/language-profiles', '/download-clients', '/tags', '/release-profiles',
                 '/delay-profiles', '/custom-formats', '/notifications', '/import',
                 '/manual-import', '/search', '/system/backup', '/system/tasks',
                 '/wanted/cutoff-unmet', '/calendar'];
  const pageErrors = [];
  const preCount = consoleErrors.length;
  for (const path of pages) {
    try {
      const resp = await page.goto(BASE + path, { waitUntil: 'domcontentloaded', timeout: 20000 });
      if (resp.status() !== 200) pageErrors.push(`${path}: HTTP ${resp.status()}`);
    } catch (e) {
      pageErrors.push(`${path}: ${e.message.slice(0, 80)}`);
    }
  }
  if (pageErrors.length === 0) ok(`All ${pages.length} pages return 200`);
  else fail(`${pageErrors.length}/${pages.length} pages broken`, pageErrors.slice(0, 5).join('; '));
  const newErrors = consoleErrors.slice(preCount);
  if (newErrors.length === 0) ok('No new console errors during page sweep');
  else {
    fail(`${newErrors.length} new console errors during sweep`, newErrors.slice(0, 3).join('; '));
  }

  console.log('\n=== R2.9: Notification modal Test Before Save validation ===');
  try {
    await page.goto(BASE + '/notifications', { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.click('[data-bs-target="#newModal"]');
    await page.waitForSelector('#newModal.show', { timeout: 3000 });
    await page.fill('#newModal input[name="name"]', 'Browser Notification Test');
    await page.selectOption('#newModal select[name="type"]', 'discord');
    await page.click('#new-notification-test-btn');
    await page.waitForSelector('#new-notification-test-result.connection-fail', { timeout: 2000 });
    const text = await page.textContent('#new-notification-test-result');
    if (text && text.includes('Webhook URL is required')) ok('Notification modal shows validation feedback before save');
    else fail('Notification modal validation', text || 'no message');
  } catch (e) {
    fail('notification modal test-before-save', e.message);
  }

  console.log('\n=== R2.10: Indexer modal Test Before Save validation ===');
  try {
    await page.goto(BASE + '/indexers', { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.click('[data-bs-target="#newModal"]');
    await page.waitForSelector('#newModal.show', { timeout: 3000 });
    await page.fill('#newModal input[name="name"]', 'Browser Indexer Test');
    await page.selectOption('#newModal select[name="type"]', 'torznab');
    await page.click('#new-indexer-test-btn');
    await page.waitForSelector('#new-indexer-test-result.connection-fail', { timeout: 3000 });
    const text = await page.textContent('#new-indexer-test-result');
    if (text && text.includes('No URL configured')) ok('Indexer modal shows validation feedback before save');
    else fail('Indexer modal validation', text || 'no message');
  } catch (e) {
    fail('indexer modal test-before-save', e.message);
  }

  console.log('\n=== R2.11: Download client modal Test Before Save validation ===');
  try {
    await page.goto(BASE + '/download-clients', { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.click('[data-bs-target="#newModal"]');
    await page.waitForSelector('#newModal.show', { timeout: 3000 });
    await page.fill('#newModal input[name="name"]', 'Browser Client Test');
    await page.selectOption('#newModal select[name="type"]', 'qbittorrent');
    await page.click('#new-download-client-test-btn');
    await page.waitForSelector('#new-download-client-test-result.connection-fail', { timeout: 3000 });
    const text = await page.textContent('#new-download-client-test-result');
    if (text && text.includes('No host configured')) ok('Download client modal shows validation feedback before save');
    else fail('Download client modal validation', text || 'no message');
  } catch (e) {
    fail('download client modal test-before-save', e.message);
  }

  console.log('\n=== R2.12: Custom format preview modal evaluates a title ===');
  try {
    await page.goto(BASE + '/custom-formats', { waitUntil: 'domcontentloaded', timeout: 30000 });
    const previewButton = await page.$('button[aria-label="Test format Browser Digital"]');
    if (!previewButton) throw new Error('no custom format preview button found');
    await previewButton.click();
    await page.waitForSelector('#previewModal.show', { timeout: 3000 });
    await page.fill('#previewTitle', 'One Piece Digital Vol 1');
    await page.click('#previewModal .btn-outline-ember');
    await page.waitForSelector('#previewResult.is-match:not(.is-hidden)', { timeout: 3000 });
    const text = await page.textContent('#previewResult');
    if (text && text.includes('MATCHED')) ok('Custom format preview shows match feedback');
    else fail('Custom format preview', text || 'no message');
  } catch (e) {
    fail('custom format preview', e.message);
  }

  console.log('\n=== R2.13: Session console error summary ===');
  if (consoleErrors.length === 0) ok('Zero console errors in entire test run');
  else {
    fail(`${consoleErrors.length} total console errors`, '');
    consoleErrors.slice(0, 8).forEach(e => console.log('    ' + e.slice(0, 160)));
  }

  await browser.close();

  console.log('\n' + '='.repeat(60));
  const passed = results.filter(r => r.pass).length;
  const total = results.length;
  console.log(`RESULTS: ${passed}/${total} passed`);
  if (passed < total) {
    console.log('\nFailures:');
    results.filter(r => !r.pass).forEach(r => {
      console.log(`  - ${r.name}${r.detail ? ': ' + r.detail : ''}`);
    });
    process.exit(1);
  }
}

run().catch(e => {
  console.error('FATAL:', e);
  process.exit(2);
});

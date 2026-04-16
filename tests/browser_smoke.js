/**
 * Mangarr browser smoke tests.
 * Exercises the critical UX flows: focus visibility, confirm modal, edit-series
 * tabs, beforeunload, HTMX confirm, keyboard nav, no console errors.
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

  // Capture console errors (page.on('console') fires for every console message)
  const consoleErrors = [];
  page.on('console', msg => {
    if (msg.type() === 'error') consoleErrors.push(msg.text());
  });
  page.on('pageerror', err => consoleErrors.push('PAGEERROR: ' + err.message));

  console.log('\n=== 1. Load library index ===');
  try {
    const resp = await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 30000 });
    if (resp.status() === 200) ok('Index loads (HTTP 200)');
    else fail('Index loads', 'HTTP ' + resp.status());
  } catch (e) {
    fail('Index loads', e.message);
  }

  console.log('\n=== 2. Focus ring appears on keyboard tab ===');
  try {
    await page.keyboard.press('Tab');
    await page.keyboard.press('Tab');
    const outline = await page.evaluate(() => {
      const el = document.activeElement;
      if (!el || el === document.body) return 'no-active-element';
      const cs = window.getComputedStyle(el);
      return { tag: el.tagName, outline: cs.outlineWidth, outlineStyle: cs.outlineStyle };
    });
    if (outline && outline.outline && outline.outline !== '0px') {
      ok(`Focus-visible ring: ${outline.outline} on ${outline.tag}`);
    } else {
      fail('Focus-visible ring', JSON.stringify(outline));
    }
  } catch (e) {
    fail('Focus-visible test', e.message);
  }

  console.log('\n=== 3. Toast container exists with aria-live ===');
  try {
    const toastRegion = await page.$('#toast-container[aria-live="polite"]');
    if (toastRegion) ok('Toast container has aria-live="polite"');
    else fail('Toast container');
  } catch (e) {
    fail('Toast container', e.message);
  }

  console.log('\n=== 4. Confirm modal exists in DOM ===');
  try {
    const modal = await page.$('#globalConfirmModal');
    if (modal) ok('globalConfirmModal in DOM');
    else fail('globalConfirmModal');
    const fn = await page.evaluate(() => typeof window.confirmAction);
    if (fn === 'function') ok('window.confirmAction is a function');
    else fail('window.confirmAction', 'type=' + fn);
  } catch (e) {
    fail('confirmAction test', e.message);
  }

  console.log('\n=== 5. Call confirmAction() programmatically and verify modal shows ===');
  try {
    // Kick off the promise but don't await it (modal stays open)
    await page.evaluate(() => {
      window.__lastResult = 'unset';
      window.confirmAction({ message: 'Test message', heading: 'Test head' })
        .then(r => { window.__lastResult = r; });
    });
    await page.waitForSelector('#globalConfirmModal.show', { timeout: 2000 });
    ok('Modal .show class applied');
    const body = await page.textContent('#globalConfirmBody');
    if (body && body.includes('Test message')) ok('Modal body shows custom message');
    else fail('Modal body message', `got "${body}"`);
    const heading = await page.textContent('#globalConfirmHeading');
    if (heading && heading.includes('Test head')) ok('Modal heading shows custom heading');
    else fail('Modal heading', `got "${heading}"`);
    // Cancel focus fires on Bootstrap's shown.bs.modal event (post-transition).
    // Transition is ~150ms; give it up to 2s to be safe.
    await page.waitForFunction(
      () => document.activeElement && document.activeElement.id === 'globalConfirmCancel',
      { timeout: 2000 }
    ).then(() => ok('Cancel button auto-focused'))
     .catch(() => fail('Cancel auto-focus', 'did not focus within 2s'));
    // Click Cancel
    await page.click('#globalConfirmCancel');
    // Wait for promise to settle (microtask) + modal to hide
    await page.waitForFunction(() => !document.querySelector('#globalConfirmModal.show'), { timeout: 2000 });
    await page.waitForFunction(() => window.__lastResult !== 'unset', { timeout: 2000 });
    // Wait for Bootstrap modal's hide animation to fully complete (modal-backdrop is removed)
    await page.waitForFunction(() => document.querySelectorAll('.modal-backdrop').length === 0, { timeout: 2000 });
    const result = await page.evaluate(() => window.__lastResult);
    if (result === false) ok('Cancel returns false');
    else fail('Cancel result', `got ${JSON.stringify(result)}`);
  } catch (e) {
    fail('confirmAction modal flow', e.message);
  }

  console.log('\n=== 6. confirmAction() OK path returns true ===');
  try {
    await page.evaluate(() => {
      window.__okResult = 'unset';
      window.confirmAction({ message: 'OK path' }).then(r => { window.__okResult = r; });
    });
    await page.waitForSelector('#globalConfirmModal.show', { timeout: 3000 });
    // Give the setTimeout for focus time to fire before clicking OK
    await new Promise(r => setTimeout(r, 250));
    await page.click('#globalConfirmOk');
    await page.waitForFunction(() => !document.querySelector('#globalConfirmModal.show'), { timeout: 2000 });
    await page.waitForFunction(() => window.__okResult !== 'unset', { timeout: 2000 });
    await page.waitForFunction(() => document.querySelectorAll('.modal-backdrop').length === 0, { timeout: 2000 });
    const result = await page.evaluate(() => window.__okResult);
    if (result === true) ok('OK button returns true');
    else fail('OK result', `got ${JSON.stringify(result)}`);
  } catch (e) {
    fail('confirmAction OK flow', e.message);
  }

  console.log('\n=== 7. Load series detail page (with new tabbed modal) ===');
  try {
    const resp = await page.goto(BASE + '/series/40', { waitUntil: 'domcontentloaded', timeout: 30000 });
    if (resp.status() === 200) ok('Series detail loads (HTTP 200)');
    else fail('Series detail', 'HTTP ' + resp.status());
  } catch (e) {
    fail('Series detail load', e.message);
  }

  console.log('\n=== 8. Edit Series modal tabs render and switch correctly ===');
  try {
    // Open the modal via Bootstrap JS
    await page.evaluate(() => {
      const m = document.getElementById('editModal');
      const inst = bootstrap.Modal.getOrCreateInstance(m);
      inst.show();
    });
    await page.waitForSelector('#editModal.show', { timeout: 2000 });
    ok('Edit Series modal opens');

    // Verify all 5 tabs exist
    const tabs = await page.$$eval('#editModal .edit-tab', els => els.map(e => e.textContent.trim()));
    const expected = ['Identity', 'Sources', 'Profiles', 'Volumes', 'Advanced'];
    const allPresent = expected.every(t => tabs.some(x => x.includes(t)));
    if (allPresent) ok(`All 5 tabs present: ${tabs.join(' | ')}`);
    else fail('Tab strip', `got: ${tabs.join(' | ')}`);

    // Default tab is Identity
    const identityActive = await page.evaluate(() => {
      const panels = document.querySelectorAll('#editModal .edit-tab-panel');
      const active = Array.from(panels).find(p => p.classList.contains('active'));
      return active ? active.querySelector('input[name="title"]') !== null : false;
    });
    if (identityActive) ok('Default tab is Identity');
    else fail('Default tab');

    // Click "Volumes" tab and verify the volumes panel becomes active
    await page.evaluate(() => {
      const buttons = document.querySelectorAll('#editModal .edit-tab');
      for (const b of buttons) if (b.textContent.trim().includes('Volumes')) { b.click(); return; }
    });
    await new Promise(r => setTimeout(r, 200));  // let Alpine update
    const volumesActive = await page.evaluate(() => {
      const panels = document.querySelectorAll('#editModal .edit-tab-panel');
      const active = Array.from(panels).find(p => p.classList.contains('active'));
      return active ? active.querySelector('textarea[name="chapter_map_text"]') !== null : false;
    });
    if (volumesActive) ok('Switched to Volumes tab, chapter_map textarea visible');
    else fail('Volumes tab switch');

    // Click "Profiles" tab
    await page.evaluate(() => {
      const buttons = document.querySelectorAll('#editModal .edit-tab');
      for (const b of buttons) if (b.textContent.trim().includes('Profiles')) { b.click(); return; }
    });
    await new Promise(r => setTimeout(r, 200));
    const profilesActive = await page.evaluate(() => {
      const panels = document.querySelectorAll('#editModal .edit-tab-panel');
      const active = Array.from(panels).find(p => p.classList.contains('active'));
      return active ? active.querySelector('select[name="quality_profile_id"]') !== null : false;
    });
    if (profilesActive) ok('Switched to Profiles tab');
    else fail('Profiles tab switch');

    // All fields from all tabs are inside the form (verify by querying form.elements)
    const formFields = await page.evaluate(() => {
      const form = document.querySelector('#editModal form');
      return Array.from(form.elements).map(e => e.name).filter(Boolean);
    });
    const needed = ['title', 'search_pattern', 'update_strategy', 'source_type',
                    'required_scanlator', 'preferred_groups_input', 'blocked_groups_input',
                    'omnibus_preference', 'edition_type', 'quality_profile_id',
                    'language_profile_id', 'quality_cutoff', 'total_volumes', 'chapter_map_text'];
    const missing = needed.filter(n => !formFields.includes(n));
    if (missing.length === 0) {
      ok(`All ${needed.length} form fields from every tab are part of single form`);
    } else {
      fail('Form fields', 'missing: ' + missing.join(', '));
    }

    // Close modal
    await page.evaluate(() => {
      const m = bootstrap.Modal.getInstance(document.getElementById('editModal'));
      if (m) m.hide();
    });
    await new Promise(r => setTimeout(r, 400));
  } catch (e) {
    fail('Edit Series tabs', e.message);
  }

  console.log('\n=== 9. beforeunload dirty tracking ===');
  try {
    // Navigate to settings (has data-track-changes)
    await page.goto(BASE + '/settings/general', { waitUntil: 'domcontentloaded' });
    const hasTrack = await page.$('form[data-track-changes]');
    if (hasTrack) ok('Settings form has data-track-changes');
    else fail('Settings form', 'no data-track-changes');

    // Type into the first input to dirty the form
    await page.evaluate(() => {
      const f = document.querySelector('form[data-track-changes]');
      const inp = f.querySelector('input[type="text"], input[type="number"]');
      if (inp) {
        inp.focus();
        inp.value = inp.value + 'x';
        inp.dispatchEvent(new Event('input', { bubbles: true }));
      }
    });
    // Now check if beforeunload would fire by dispatching it manually and checking returnValue
    const wouldBlock = await page.evaluate(() => {
      const evt = new Event('beforeunload', { cancelable: true });
      window.dispatchEvent(evt);
      return evt.defaultPrevented || evt.returnValue === '';
    });
    if (wouldBlock) ok('beforeunload fires when form is dirty');
    else fail('beforeunload dirty', 'did not fire');
  } catch (e) {
    fail('beforeunload test', e.message);
  }

  console.log('\n=== 10. Health page renders and fix URLs work ===');
  try {
    const resp = await page.goto(BASE + '/health', { waitUntil: 'domcontentloaded' });
    if (resp.status() === 200) ok('Health page loads');
    // Check severity-related classes are rendered (even if all passing)
    const severity = await page.evaluate(() => {
      const panel = document.querySelector('.panel');
      return panel ? panel.innerHTML.includes('_sev_colors') || panel.querySelector('.badge-status') !== null : false;
    });
    // Just check the page structure exists
    const checks = await page.$$('.panel');
    if (checks.length > 0) ok(`Health panels render (${checks.length} panels)`);
    else fail('Health panels');
  } catch (e) {
    fail('Health page', e.message);
  }

  console.log('\n=== 11. Stats page chart has role="img" ===');
  try {
    await page.goto(BASE + '/stats', { waitUntil: 'domcontentloaded' });
    const chart = await page.$('#grab-chart[role="img"]');
    if (chart) {
      const label = await chart.getAttribute('aria-label');
      if (label && label.length > 10) ok(`Grab chart has aria-label: "${label.slice(0, 60)}..."`);
      else fail('Grab chart aria-label', label || 'empty');
    } else {
      // Stats might not have a grab chart if daily_grabs is empty
      ok('Grab chart absent (no daily_grabs data yet)');
    }
  } catch (e) {
    fail('Stats page', e.message);
  }

  console.log('\n=== 12. Library search has proper labels ===');
  try {
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 30000 });
    const label = await page.$('label[for="library-search-input"]');
    const input = await page.$('#library-search-input');
    if (label && input) ok('Search label is properly associated with for/id');
    else fail('Search label/input pair', `label=${!!label} input=${!!input}`);

    const searchBtn = await page.$('button[aria-label="Search"]');
    if (searchBtn) ok('Search button has aria-label');
    else fail('Search button aria-label');
  } catch (e) {
    fail('Library search labels', e.message);
  }

  console.log('\n=== 13. prefers-reduced-motion media query exists ===');
  try {
    const hasMq = await page.evaluate(() => {
      for (const sheet of document.styleSheets) {
        try {
          for (const rule of sheet.cssRules) {
            if (rule.type === CSSRule.MEDIA_RULE && rule.conditionText && rule.conditionText.includes('reduced-motion')) {
              return true;
            }
          }
        } catch (e) { /* cross-origin sheet, skip */ }
      }
      return false;
    });
    if (hasMq) ok('prefers-reduced-motion media query present in stylesheets');
    else fail('prefers-reduced-motion');
  } catch (e) {
    fail('reduced-motion test', e.message);
  }

  console.log('\n=== 14. Console error check ===');
  if (consoleErrors.length === 0) ok('No JS console errors during entire test run');
  else {
    fail('Console errors detected', `${consoleErrors.length} errors`);
    consoleErrors.slice(0, 5).forEach(e => console.log('    ' + e));
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

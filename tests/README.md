# Mangarr tests

Four layers of verification covering the app's critical UX and data integrity.
Total: **69 browser assertions + 13 static checks + 9 DB integrity sections**.

## Running

```bash
# DB-level state verification (runs inside container)
docker exec mangarr python3 /app/verify_e2e.py

# JS/CSS critical feature static analysis
docker exec mangarr python3 /app/test_confirm_flow.py

# Browser-level integration tests (requires playwright on host)
cd /opt/manga-arr/tests
node browser_smoke.js        # 27 assertions — presence/structure checks
node browser_integration.js  # 18 assertions — HTMX + confirm flows
node browser_e2e.js          # 24 assertions — real DB mutations + rollback
```

## What's covered

### `verify_e2e.py` (DB state machine)
- Grabbed volumes have grabbed_at / torrent_name / indexer / protocol / source_url
- Downloaded volumes have import_path / quality / imported_at
- Grabbed/downloaded chapters have their metadata populated
- No stuck grabbed volumes
- No orphaned chapters/volumes (foreign key integrity)
- Blocklist TTL and API key configured
- Import queue state

### `test_confirm_flow.py` (static JS/CSS analysis)
- confirmAction returns Promise
- Cancel button auto-focus logic
- data-confirm handler in capture phase with stopImmediatePropagation
- htmx:confirm handler calls issueRequest(true) on confirm
- CSRF handler correctly in bubble phase
- beforeunload fires conditionally on dirty forms
- HTMX success clears dirty flag
- prefers-reduced-motion CSS block
- :focus-visible rule defined
- Toast container aria-live="polite"

### `browser_smoke.js` (Playwright, 27 assertions)
- Library index loads
- Focus ring appears on Tab keypress
- Toast container has aria-live
- confirmAction programmatic flow (modal shows, Cancel returns false, OK returns true, auto-focus)
- Edit Series modal opens with 5 tabs (Identity / Sources / Profiles / Volumes / Advanced)
- All form fields from every tab part of the single form
- Tab switching (Identity → Volumes → Profiles)
- beforeunload fires when settings form is dirty
- Health page renders with panels
- Stats grab chart has role="img" with aria-label
- Library search label/input proper association
- Search button aria-label
- prefers-reduced-motion media query present in stylesheets
- Zero console errors

### `browser_integration.js` (Playwright, 18 assertions)
- data-confirm real form: clicking delete submit shows themed modal, Cancel dismisses without submitting
- hx-confirm: HTMX Remove button shows themed modal (not native confirm), no POST fires, Cancel blocks DELETE
- Toast render + aria-live=polite region placement
- Edit Series modal data-track-changes fires beforeunload after edit
- Tab switching preserves form state (type in Identity → switch to Volumes → back → value intact)
- Reduced-motion emulation: card animation duration reduced to ~0
- HTMX progress bar element exists
- All 24 pages return 200 across sweep
- No new console errors during page sweep
- Zero total console errors

### `browser_e2e.js` (Playwright, 24 assertions) — **real DB mutations, auto-rollback**
- **E3.1 — Tag delete end-to-end**: Create a test tag via SQL → load /tags → click delete → confirm OK → verify POST returns 303 → verify the tag is ACTUALLY gone from the DB
- **E3.2 — Edit Series Save across multiple tabs**: Open the edit modal → change `search_pattern` in Identity tab → switch to Sources → change `omnibus_preference` → switch to Advanced → change `update_strategy` → submit → verify ALL THREE fields persisted in the DB → revert to original state
- **E3.3 — Concurrent browser sessions**: Open 3 parallel browser contexts, each loads `/series/40`, opens the edit modal, and switches to a different tab — verify no errors and no state corruption
- **E3.4 — Manual check-downloads API**: `POST /api/check-downloads` returns `{ok: true}` and queues the status check
- **E3.5 — Manual backlog search API**: `POST /api/backlog-search` returns `{ok: true}` and queues the search
- **E3.6 — Download client test + circuit-breaker reset**: `POST /api/download-clients/1/test` confirms qBittorrent reachable; `POST /api/download-clients/reset-all-circuits` clears CB state
- **E3.7 — Keyboard navigation**: Tab through 5 elements in the edit modal, all focus correctly; modal closes cleanly
- **E3.8 — Data integrity post-mutations**: verify_e2e.py still passes, no leftover test data
- **E3.9 — Console error summary**: Zero errors across the entire E2E run

# Changelog

All notable changes to this project. Format roughly follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Fixed

- SABnzbd connection tests now reject a missing API key instead of reporting a
  false-positive connection from the unauthenticated version endpoint.
- Successful tests of saved download clients now clear stale circuit-breaker
  state immediately.
- Repeated open-circuit and missing-SAB-key events are rate-limited to prevent
  one backlog search from flooding the activity database.

### Changed

- Release qualification now requires authenticated download-client probes and
  treats recurring configuration or circuit-breaker events as promotion
  blockers.

## 1.2.0-rc.2 - 2026-07-17

Second release candidate for the 1.2.0 import-review and metadata-provenance
release. This candidate replaces `rc.1`, which was rejected during its
production soak.

### Fixed

- Large multi-indexer RSS feeds now yield to the event loop between release
  evaluations so health checks and interactive requests remain responsive
  throughout matching.

### Validation

- The `rc.1` soak identified repeatable 10-second health-probe timeouts during
  15-minute RSS cycles while the container remained running with zero restarts.
- Regression coverage exercises cooperative scheduling across a large
  nonmatching feed.
- 1,698 Python tests, 13 confirmation-flow checks, and 10 route-sweep checks
  pass after the responsiveness fix.

## 1.2.0-rc.1 - 2026-07-16

First release candidate for safer ambiguous imports, standalone specials, and
operator-controlled metadata provenance.

### Added

- Added the supported `mangarr` operator CLI for administrator recovery and
  backup creation, validation, and stopped-container restore.
- Added a required fast pull-request gate, weekly security scanning, project
  package metadata, Ruff correctness checks, and configurable
  `MANGARR_UMASK` support.
- Added explicit volume, volume-range, chapter, chapter-range, special, and
  skip classifications to import review.
- Added standalone special-file persistence and a separate specials section on
  series pages so specials cannot overwrite or satisfy mainline records.
- Added field-level metadata candidates, selected-source provenance, operator
  locks, conflict reporting, and safe candidate application.

### Changed

- Backup creation now uses SQLite's online backup API and produces a versioned,
  self-contained archive with the database, encryption key, and manifest.
- Public environment variables now use consistent `MANGARR_*` names while
  retaining all previous names as backward-compatible aliases.
- The public deployment file is now `compose.yaml` and no longer forces a
  global container name.
- Import review now processes only actionable files on repeated partial
  submissions and leaves completed or skipped siblings terminal.
- Metadata refresh and local rescans now preserve explicit field locks while
  still recording newly observed provider and local candidates.

### Fixed

- Scheduled backups now use the same `manga_arr.db` archive entry and
  validation format as browser and API backups.
- Completed qBittorrent downloads now retain a durable import receipt and
  duplicate `seen` aliases schedule only one canonical import worker.
- Short-story and other special releases now remain in manual review instead
  of being auto-imported from incidental numbers in release-group names.
- Chapter imports now replace legacy empty download IDs with the current
  download identity.
- Provider refreshes no longer silently replace explicit metadata lock state.
- Fractional volume observations now use ceiling semantics instead of integer
  truncation when deriving local volume counts.

### Validation

- Production migration preserved all existing series, volume, chapter,
  history, queue, and seen-release records with clean SQLite integrity and
  foreign-key checks.
- Authenticated production smoke covered library, search, queue review,
  metadata provenance, desktop/mobile rendering, and all configured download
  clients without enqueueing a release.

## 1.1.0 - 2026-07-15

First minor release focused on public installation and first-run usability.

### Changed

- First-run browser setup now creates the local administrator directly; the
  container setup-token command and bootstrap file are no longer required.
- Offline administrator recovery now revokes browser access and returns the
  installation to the same browser-first setup screen.
- The public Compose file uses direct, editable values for UID/GID, timezone,
  paths, ports, and application defaults instead of requiring `.env`
  interpolation.
- The public image follows the stable `latest` channel by default. Exact
  version tags remain available for reproducible deployments and rollback.
- Standard upgrades now use `docker compose pull` followed by
  `docker compose up -d`, matching established container-image workflows.

### Security

- Administrator creation remains atomic, CSRF-protected, Argon2id-hashed, and
  limited to the first successful setup request.
- Upgrades automatically remove the obsolete setup-token file left by older
  Mangarr versions.
- Documentation now requires first-run setup before public reverse-proxy
  exposure and keeps direct internet publication outside the supported
  security boundary.

### Tests

- Browser-auth coverage verifies tokenless setup, first-claim concurrency,
  secure session creation, legacy-token cleanup, and offline recovery.
- Deployment consistency tests pin the direct Compose contract, stable image
  channel, editable runtime identity, and pull-and-recreate upgrade path.

## 1.0.1 - 2026-07-15

Patch release for a responsive visibility defect found during the authenticated
production browser audit of `1.0.0`.

### Fixed

- Inactive library bulk-selection controls now stay hidden and no longer widen
  the document beyond a 390px mobile viewport.
- Suwayomi download-client forms now hide the port, SSL, priority, and related
  protocol-only controls as intended.
- Alpine `x-show` directives that share an element with Bootstrap display
  utilities now use Alpine's `.important` modifier so Bootstrap cannot override
  the hidden state.

### Tests

- 1,650 Python tests, 13 confirm-flow checks, and 10 route-sweep checks passed.
- Browser smoke 32/32, integration 22/22, and E2E 24/24 passed in isolation.
- Added a source invariant covering every template with `x-show` and Bootstrap
  display utilities.
- Added a browser regression that enters and exits bulk mode at a 390px viewport
  and fails on horizontal document overflow.

## 1.0.0 - 2026-07-15

First stable public release. This promotes `1.0.0-rc.2` without changing
application behavior; the candidate runtime is the code qualified by the
production, metadata, download/import, recovery, and security gates below.

### Release Qualification

- 1,649 Python tests, 13 confirm-flow checks, and 10 route-sweep checks passed.
- Browser smoke 31/31, integration 22/22, and E2E 24/24 passed in isolation.
- Real qBittorrent, SABnzbd, and Suwayomi connection probes passed.
- Anonymous installation, administrator setup, `rc.1` to `rc.2` upgrade, and
  stopped-snapshot rollback passed.
- A real 2.3 GB backup restored all 30 series, administrator access, and 17
  encrypted credentials.
- Dependency, 424-commit secret, configuration, and image scans completed with
  no release blockers or fixed High/Critical image vulnerabilities.

### Operations

- The public Compose file pins `1.0.0` by default.
- Stable image publication provides `1.0.0`, `1.0`, `1`, and `latest` tags from
  one immutable multi-architecture image index.
- Public support, contribution, conduct, issue, and pull-request workflows are
  available, and the protected default branch requires pull requests.

## 1.0.0-rc.2 - 2026-07-15

Second public release candidate. Runtime behavior is unchanged from `rc.1`;
this candidate completes the public release-qualification and project-support
surface discovered during the first candidate deployment.

### Added

- Fixture-driven metadata acceptance coverage for standard manga, ongoing
  series, one-shots, omnibuses, light novels, alternate titles, and conflicting
  provider counts.
- Public contribution, support, conduct, issue, and pull-request guidance.
- A stable-release qualification record covering production, metadata,
  downloader/import, installation, recovery, and repository gates.

### Changed

- Public Compose and deployment examples now pin `1.0.0-rc.2`.
- Release workflow Docker actions use immutable Node 24-capable revisions.
- Local verification no longer expects development-only scripts inside the
  hardened production image.

### Validation

- Real qBittorrent, SABnzbd, and Suwayomi connection probes passed.
- An anonymous Compose installation completed setup and authenticated startup.
- A real 2.3 GB backup restored with all 30 series, administrator access, and
  17 encrypted credentials intact.

## 1.0.0-rc.1 - 2026-07-15

First public release candidate. This entry summarizes the application state
since `v0.1.5-mapping-correctness`; the detailed implementation history remains
available in the merged pull requests.

### Added

- Single-administrator browser authentication with Argon2id password hashing,
  revocable server-side sessions, one-time first-run setup tokens, login
  throttling, and offline administrator recovery.
- Broad Sonarr-style `/api/v1` and `/api/v3` compatibility surfaces for
  library, queue, history, wanted, profiles, clients, indexers, settings,
  commands, backups, rename, and existing-library adoption workflows.
- Suwayomi direct-download support with metadata confidence, retry state,
  cancellation safety, and chapter-aware import handling.
- Existing-library scan, match, adopt, rescan, rename preview, and organize
  workflows.
- Unified metadata lifecycle, readiness diagnostics, reconciliation, and
  repair paths across AniList, MangaDex, MangaUpdates, and Kitsu data.
- Public non-root container packaging, host-neutral Docker Compose defaults,
  a database-backed health endpoint, and first-run deployment documentation.
- Local-first release gates plus tag-only multi-architecture GHCR publishing
  with immutable version tags, SBOM generation, and provenance attestations.
- AGPL-3.0-only licensing with source and license links available from the
  running application.

### Changed

- Import execution now claims, stages, and commits in separate phases so file
  I/O does not hold a long SQLite write transaction.
- Metadata, import, grab, series, and router responsibilities were split into
  focused modules while preserving compatibility re-exports.
- UI controls, loading states, icons, type scale, touch targets, color tokens,
  z-indexes, and accessibility labels were standardized across the interface.
- Stored integration secrets are encrypted at rest and secret settings render
  decrypted only in the authenticated settings view.
- Runtime versioning now uses `app/VERSION` as the canonical source for the UI,
  API, update status, and OpenAPI metadata.

### Fixed

- Public Compose and `.env.example` defaults pin the current RC instead of the
  intentionally unpublished stable-only `latest` tag.
- SQLite lock contention during long imports and event writes.
- Split-RAR extraction, shared rename paths, chapter import state, pack
  placeholder cleanup, and duplicate lower-quality import handling.
- Metadata-source drift, stale refresh state, DDL grab-mode persistence, and
  MangaDex/Suwayomi retry edge cases.
- Browser setup username validation under modern HTML regular-expression
  parsing.

### Security

- Request body and multipart limits, current Starlette request parsing, CSRF
  enforcement, API-key fail-closed behavior, SSRF controls, XXE-safe parsing,
  regular-expression safety, and import path confinement are release-gated.
- The runtime image drops Linux capabilities, enables
  `no-new-privileges`, runs as a configurable non-root UID/GID, and defaults to
  host-loopback publication.
- Dependency, secret, and container configuration scans are part of release
  validation.

### Upgrade Notes

- Back up `/config/manga_arr.db` and `/config/.mangarr-secret-key` together
  before upgrading.
- Existing installations without a browser administrator are redirected to
  first-run setup. Retrieve the one-time token from
  `/config/.mangarr-setup-token` after the upgraded container starts.
- The image runs as UID/GID 1000 by default. Existing bind mounts must be
  writable by that identity or overridden with `MANGARR_UID` and
  `MANGARR_GID`.
- Pin `MANGARR_VERSION=1.0.0-rc.1` while evaluating this candidate. See
  `docs/deployment.md` for the tested upgrade and rollback procedure.

## 2026-05-05 — Graceful shutdown, configurability, and resource management

This release adds production-grade improvements for reliability, configurability,
and resource management — closing several critical silent-failure risks identified
in the January 2026 hardening audit and subsequent code review.

### Added

- **Import concurrency configurable** — New `max_concurrent_imports` setting
  (default: `2`) lets operators tune parallel import limits based on their
  hardware (spinning disk → 1-2, SSD → 3-10). Read from settings at runtime,
  no restart required.
- **Graceful task cancellation** — All background task loops now handle
  `asyncio.CancelledError` with proper cleanup, logging, and clean exit.
  Tasks no longer leave the system in a broken state on shutdown or manual
  cancellation.
- **Import queue status tracking on cancel** — When import tasks are cancelled,
  queue rows are marked 'failed' with `failed_at` timestamp for stuck-state
  cleanup to retry.
- **TTL-based rejection rate limiter cleanup** — `_prune_rejection_log()` runs
  every 20 calls, removing entries older than 1 hour regardless of cache size.
  Prevents unbounded memory growth over time.
- **Module refactoring: series_.py split** — Extracted into 6 focused modules:
  - `series_core.py` — Shared helper functions
  - `series_search.py` — Search, add series, metadata refresh routes
  - `series_actions.py` — Edit, delete, restore, purge, manual grab, volume actions
  - `series_volumes.py` — Volume/chapter state management
  - `series_detail.py` — Read-only detail, metadata health, reconcile
  - `series_editor.py` — Chapter map editor (already existed)
- **Module refactoring: import_pipeline.py split** — Extracted into 4 focused modules:
  - `import_discovery.py` — Download client polling
  - `import_queue.py` — File classification, pack detection
  - `import_staging.py` — Two-phase commit staging
  - `import_execute.py` — Execution orchestration, semaphore
- **Module refactoring: grab.py split** — Extracted into 4 focused modules:
  - `grab_dedup.py` — Deduplication & rate limiting
  - `grab_core.py` — Core grab_item logic
  - `grab_backlog.py` — Backlog search
  - `grab_rss.py` — RSS polling

### Changed

- `import_pipeline._get_import_sem()` — Now lazily constructs semaphore with
  `max_concurrent_imports` config value on call rather than hardcoding at startup.
  Enables runtime reconfiguration via Settings UI.
- `_rejection_log_last` in `grab.py` — Now prunes stale entries using TTL,
  not just size threshold (1000 entries).
- `app/tasks.py` — Added `try/except asyncio.CancelledError` blocks to all 9
  background task loops with appropriate cleanup and logging.
- `app/routers/suwayomi_.py:983` — Fixed sqlite3.Row `.get()` violation by
  converting to dict before passing to `_get_series_source()`.
- `import_pipeline._guarded_execute_import()` — Now marks queue item 'failed'
  with `failed_at` timestamp and cancels all files on shutdown.
- `app/schema.py` — Added `failed_at` column to `import_queue` table.

### Fixed

- sqlite3.Row `.get()` violation in `suwayomi_.py:983` that could silently
  fail when accessing Row attributes with `.get()`.
- Memory leak in rejection rate limiter that could grow unbounded in very
  active installations (thousands of rejections per day).
- Background tasks no longer remain in cancelled state after shutdown or manual
  cancellation — all clean exit.
- Import queue items stuck in 'importing' state on task cancellation now
  move to 'failed' with timestamp for retry.

### Operational notes

- **Settings UI**: New field **Maximum concurrent imports** in
  Settings → General. Range recommended: 1-10 (default 2).
- **Graceful shutdown**: On Ctrl+C or `docker stop`, all background tasks
  now complete cleanup before terminating.
- **Restart safety**: Stuck imports left 'pending' or 'importing' from a
  crashed import are now cleaned up on startup and retried.

### Testing

All 1200+ Python tests pass:
- `test_background_tasks.py` — 8 tests for task tracking + cancellation
- `test_grab*.py` — 16 tests for grab dedup, rejection limiter, timeout
- `test_import*.py` — 31 tests for atomic imports, concurrency, mapping
- `test_route_state_changes.py` — 21 tests for CRUD operations
- `test_hard_invariants.py` — 6 tests for silent-failure tripwires

### Files modified

| File | Lines | Purpose |
|---|---:|---|
| `app/config.py` | +1 | Adding `max_concurrent_imports` to ENV_DEFAULTS |
| `app/grab.py` | +15 | Cleanup function + periodically prune rejection log |
| `app/import_pipeline.py` | +40 | Lazy semaphore, initialization, cancel wrappers |
| `app/tasks.py` | +22 | CancelledError handlers for all task loops |
| `app/routers/suwayomi_.py` | +1 | Fix Row→dict conversion |
| `app/shared.py` | +3 | event_loop_lag_monitor cancellation handler |
| `app/status_cache.py` | +2 | download_status_refresh_loop cancellation handler |
| `app/metadata.py` | +3 | Kitsu pagination cancellation handler |
| `app/schema.py` | +1 | Add failed_at column to import_queue |
| **Total** | **+93** | **All changes backward compatible** |

### Fixed

- sqlite3.Row `.get()` violation in `suwayomi_.py:983` that could silently
  fail when accessing Row attributes with `.get()`.
- Memory leak in rejection rate limiter that could grow unbounded in very
  active installations (thousands of rejections per day).
- Background tasks no long-running cancelled state after shutdown or manual
  cancellation — all clean exit.

### Operational notes

- **Settings UI**: New field **Maximum concurrent imports** in
  Settings → General. Range recommended: 1-10 (default 2).
- **Graceful shutdown**: On Ctrl+C or `docker stop`, all background tasks
  now log cancellation details before exiting cleanly.
- **Restart safety**: Stuck imports left 'pending' from a crashed import
  are now cleaned up on startup and retried.

### Testing

All 1200+ Python tests pass:
- `test_background_tasks.py` — 8 tests for task tracking + cancellation
- `test_grab*.py` — 16 tests for grab dedup, rejection limiter, timeout
- `test_import*.py` — 31 tests for atomic imports, concurrency, mapping
- `test_hard_invariants.py` — 6 tests for silent-failure tripwires

### Files modified

| File | Lines | Purpose |
|---|---:|---|
| `app/config.py` | +1 | Adding `max_concurrent_imports` to ENV_DEFAULTS |
| `app/grab.py` | +15 | Cleanup function + periodically prune rejection log |
| `app/import_pipeline.py` | +25 | Lazy semaphore, initialization, CancelledError wrap |
| `app/tasks.py` | +22 | CancelledError handlers for all task loops |
| `app/routers/suwayomi_.py` | +1 | Fix Row→dict conversion |
| `app/shared.py` | +3 | event_loop_lag_monitor cancellation handler |
| `app/status_cache.py` | +2 | download_status_refresh_loop cancellation handler |
| `app/metadata.py` | +3 | Kitsu pagination cancellation handler |

### Issue follow-up

- Closes sqlite3.Row `.get()` violation (CLAUDE.md hard invariant)
- Addresses silent failure from memory leak in `_rejection_log_last`
- Completes graceful shutdown work tracked in January 2026 audit (H1 background
  task lifecycle improvements).

## 2026-04-16 — H4 encryption at rest

This release closes the last deferred hardening item from the security
audit: **H4 — plaintext secrets in the SQLite DB**. Secrets stored in
the database are now encrypted at rest, operators have documented key
backup and recovery guidance, and the release notes below summarize the
five PRs that completed the work.

### Added

- Fernet-based secret-cipher primitives and master-key resolution
  (PR [#17](https://github.com/Kha-kis/manga-arr/pull/17)).
- Regression coverage for the H4 rollout:
  `test_secret_cipher.py`, `test_settings_secret_migration.py`,
  `test_indexer_dlclient_secret_migration.py`, and
  `test_notification_secret_migration.py`.

### Changed

- `settings` table secret values are encrypted at rest
  (PR [#18](https://github.com/Kha-kis/manga-arr/pull/18)).
- `indexers.api_key` and `download_clients.password` are encrypted at
  rest (PR [#19](https://github.com/Kha-kis/manga-arr/pull/19)).
- Secret fields inside `notification_connections.settings` JSON are
  encrypted at rest (PR [#20](https://github.com/Kha-kis/manga-arr/pull/20)).
- Operator docs now cover `MANGARR_SECRET_KEY`,
  `/config/.mangarr-secret-key`, backup/restore requirements,
  wrong-key behavior, recovery by re-entering credentials, and the
  current key-rotation limitation
  (PR [#21](https://github.com/Kha-kis/manga-arr/pull/21)).

### Operational notes

- Back up the database and the active secret-key source together:
  `/config/.mangarr-secret-key` if file-backed, or the
  `MANGARR_SECRET_KEY` secret if environment-backed.
- Restoring the database without the matching key leaves encrypted
  credentials unreadable until they are re-entered.
- Key rotation is **not yet supported**. Changing the master key
  without re-entering credentials will make existing encrypted values
  unreadable.

### Issue follow-up

- Issue [#22](https://github.com/Kha-kis/manga-arr/issues/22) was
  re-tested on `master` and on the originally cited commit `1e3b862`.
  The previously reported pytest hang for
  `tests/python/test_api_key_middleware.py::test_api_route_fails_closed_when_api_key_blank`
  did not reproduce and the issue was closed as a transient /
  non-reproducible report.

## 2026-04-15 — Security audit hardening

This release closed every Critical, High, and Medium finding from a
full external security audit performed on 2026-04-15. All 15 PRs
below were landed on the same day. Changes in this section were
additive or surgical; no public API / UI surface was broken.

The one deferred audit item, **H4 — plaintext secrets in the SQLite
DB**, has since been completed by the follow-up encryption-at-rest
series. Current operator guidance for that work lives in
`README.md`, `docs/deployment.md`, and `.env.example`.

### Added

- `app/security.py` — SSRF validator (`validate_outbound_url`, PR #2),
  ReDoS-safe regex helper (`compile_user_regex` / `safe_regex_search`,
  PR #11), and the `UnsafeURLError` / `UnsafeRegexError` exceptions.
- `app/shared.py` — `build_order_by` (PR #12), `validate_sql_identifier`
  and `validate_sql_typedef` (PR #13).
- Staging directory machinery for multi-file imports
  (`_ImportStaging`, PR #8).
- Background task lifecycle manager (`_BACKGROUND_TASKS`,
  `create_background_task`, `_cancel_background_tasks`, PR #7).
- Import-concurrency guard (`_IMPORT_SEM`, `claim_import_queue_row`,
  `_guarded_execute_import`, PR #6).
- `ensure_api_key()` helper that self-heals a blank `api_key` at
  startup (PR #5).
- CSRF token exposed via `<meta name="csrf-token">` so JS no longer
  needs `document.cookie` (PR #10).
- `docs/deployment.md` — deployment guide + security checklist (PR #15).
- `.env.example` — tracked template (PR #15).
- `README.md` and this `CHANGELOG.md` — stabilisation PR
  ([#16](https://github.com/Kha-kis/manga-arr/pull/16)).
- Follow-up H4 encryption-at-rest operator guidance:
  `MANGARR_SECRET_KEY`, `/config/.mangarr-secret-key`, backup/restore,
  wrong-key recovery, and current key-rotation limits.
- **172 tests** across 14 new test files under `tests/python/` —
  every hardening change has at least one regression test and most
  have source-level drift guards.

### Fixed

- `init_db`'s chapters table was referenced by `add_col` before the
  `CREATE TABLE` executed, causing fresh installs to fail silently
  (PR #3).
- `log_event` opened a second SQLite connection inside the import
  transaction, causing a 15-second `SQLITE_BUSY` tax per import.
  Now accepts `db=<existing_connection>` (PR #9). The integration
  test suite's wall-clock dropped from 122s → 1.6s as a side effect.

### Changed (security-hardening)

Summary table — see per-PR entries below for detail:

| PR  | Severity         | Area                         |
|----:|------------------|------------------------------|
| [#1](https://github.com/Kha-kis/manga-arr/pull/1)  | **C1** + **C3**  | Path traversal in import + XXE in XML parsing |
| [#2](https://github.com/Kha-kis/manga-arr/pull/2)  | **C2**           | SSRF on user-supplied outbound URLs |
| [#4](https://github.com/Kha-kis/manga-arr/pull/4)  | **C2 follow-up** | Slack webhook validation + deterministic DNS test |
| [#5](https://github.com/Kha-kis/manga-arr/pull/5)  | **H2**           | API-key middleware fails closed |
| [#6](https://github.com/Kha-kis/manga-arr/pull/6)  | **H3**           | Bounded import concurrency + atomic claim |
| [#7](https://github.com/Kha-kis/manga-arr/pull/7)  | **H1**           | Background task lifecycle |
| [#8](https://github.com/Kha-kis/manga-arr/pull/8)  | **M2**           | Batch-atomic multi-file imports |
| [#10](https://github.com/Kha-kis/manga-arr/pull/10) | **M1**           | CSRF cookie flags hardened |
| [#11](https://github.com/Kha-kis/manga-arr/pull/11) | **M3**           | Regex ReDoS protection |
| [#12](https://github.com/Kha-kis/manga-arr/pull/12) | **M4**           | ORDER BY allowlists |
| [#13](https://github.com/Kha-kis/manga-arr/pull/13) | **M5**           | f-string SQL input-shape guards |
| [#14](https://github.com/Kha-kis/manga-arr/pull/14) | **M7**           | Log (don't silently swallow) at 4 sites |
| [#15](https://github.com/Kha-kis/manga-arr/pull/15) | **M8**           | Deployment + network-binding docs |

---

### Per-PR detail

#### [#1 — C1 path traversal + C3 XXE](https://github.com/Kha-kis/manga-arr/pull/1)

- `build_filename()` fallback sanitises `original_filename` so a
  torrent / NZB-supplied name can no longer carry `..` components.
- New `safe_join_under(dst_dir, filename)` rejects path separators,
  absolute paths, `..` components, and destinations that escape the
  series root via symlink.
- `read_comic_info` (CBZ ComicInfo), CBR ComicInfo, Torznab /
  Newznab RSS, and custom-RSS import-list parsers switched to
  `defusedxml` — DOCTYPE / entity payloads now fail closed.

#### [#2 — C2 SSRF](https://github.com/Kha-kis/manga-arr/pull/2)

- `validate_outbound_url(url, *, allow_private=False)` rejects
  non-HTTP schemes, userinfo, `localhost` / `*.localhost`, and
  hostnames that resolve to loopback / link-local / multicast /
  reserved / unspecified / private addresses. Mixed-pool DNS
  (public + private) is also rejected.
- Wired into 11 sinks: Discord, Ntfy, Gotify, generic webhook,
  Apprise, custom RSS import, Komga test (`allow_private=True`),
  Prowlarr / Torznab / Newznab test and RSS-fetch (`allow_private=True`),
  series cover URL.
- `download_cover` now uses `follow_redirects=False` to close the
  public→private redirect-bypass.

#### [#3 — init_db ordering](https://github.com/Kha-kis/manga-arr/pull/3)

- `add_col('chapters', 'quality', …)` and `'imported_at'` moved to
  after the `CREATE TABLE chapters` block. On a fresh `/config`,
  the original order tripped a rollback that wiped every migration
  that had run in the same transaction.

#### [#4 — Slack webhook + deterministic DNS test](https://github.com/Kha-kis/manga-arr/pull/4)

- `_send_slack` now validates the webhook URL identically to Discord.
- The SSRF acceptance test no longer depends on a live
  `example.com` lookup; mocks `socket.getaddrinfo` → `8.8.8.8`.

#### [#5 — H2 API-key fails closed](https://github.com/Kha-kis/manga-arr/pull/5)

- `ApiKeyMiddleware` now returns `401` when `api_key` is blank /
  whitespace — previously it let every `/api/*` request through.
- `ensure_api_key()` runs at startup; if the DB row was cleared it
  auto-generates a fresh key, persists it, and logs one WARNING
  naming the action (the key value itself is **not** logged).

#### [#6 — H3 import concurrency](https://github.com/Kha-kis/manga-arr/pull/6)

- Module-level `_IMPORT_SEM = asyncio.Semaphore(2)` caps parallel
  imports.
- `claim_import_queue_row()` atomically transitions a row to
  `'importing'` via UPDATE-with-rowcount. Subsequent workers
  (including the retry endpoint and the stuck-retry loop) lose the
  claim cleanly with a `[Import] claim lost` log, instead of
  doubling up.
- `_process_auto_import` + the manual-submit route both route
  through the new `_guarded_execute_import` wrapper.

#### [#7 — H1 background task lifecycle](https://github.com/Kha-kis/manga-arr/pull/7)

- Ten long-running loops (rss, status, refresh, backlog, rescan,
  import-list, backup, backfill-metadata, suwayomi-monitor,
  retry-stuck) now run through `create_background_task(coro, name)`.
- `_cancel_background_tasks()` runs at lifespan shutdown and awaits
  graceful exit.
- Done-callback logs any uncaught exception that escapes a loop's
  inner `try/except`. Previously 7 of 10 loops were fire-and-forget
  and their exceptions died silently.

#### [#8 — M2 batch-atomic imports](https://github.com/Kha-kis/manga-arr/pull/8)

- `_ImportStaging` helper: per-batch staging dir under `dst_dir`,
  two-phase commit (stage → atomic `os.replace` → unlink source
  for move-mode).
- `_execute_import` wraps the file loop in SQLite
  `SAVEPOINT import_batch`. Mid-batch failure rolls back both the
  filesystem staging AND the DB writes; the offending file's id is
  captured before rollback and re-marked `'failed'` afterward.
- Move-mode source deletion is deferred to the commit phase so
  rollback preserves source data.

#### [#9 — log_event SQLite lock fix](https://github.com/Kha-kis/manga-arr/pull/9)

- `log_event` (and all in-transaction callers: `_execute_import`,
  `_queue_import`, `_mark_downloaded`, two `check_download_status`
  cleanup blocks) now accepts `db=<existing_connection>`. Previously
  each call opened a fresh connection that would block on the
  outer writer for 15 s.

#### [#10 — M1 CSRF cookie flags](https://github.com/Kha-kis/manga-arr/pull/10)

- CSRF cookie now sets `SameSite=Strict`, `HttpOnly`, and `Secure`
  when the request arrived over HTTPS (direct TLS or
  `X-Forwarded-Proto: https` / `X-Forwarded-Ssl: on`).
- Frontend `_csrfToken()` reads from `<meta name="csrf-token">`
  (added to `base.html`) instead of `document.cookie` — HttpOnly
  is now safe for htmx + plain forms.

#### [#11 — M3 regex ReDoS](https://github.com/Kha-kis/manga-arr/pull/11)

- `compile_user_regex()` rejects empty, too-long (> 256 chars),
  malformed, and nested-unbounded-quantifier patterns.
- `safe_regex_search()` truncates text to 2048 chars as defence in
  depth against alternation-overlap patterns.
- Wired into 5 sites: three `release_title_*` / `edition_contains`
  specs in `custom_formats.py`, plus `is_regex` term and preferred
  term matching in `release_profiles.py`.

#### [#12 — M4 ORDER BY allowlists](https://github.com/Kha-kis/manga-arr/pull/12)

- `build_order_by(sort_key, *, allowed, default_key, direction=None)`
  — only values in `allowed` (hardcoded by caller) can appear in
  the returned SQL fragment.
- Library index (`series_.py:index`) uses it for the three
  allowlisted sort keys (`title`, `status`, `added`).

#### [#13 — M5 f-string input-shape guards](https://github.com/Kha-kis/manga-arr/pull/13)

- `validate_sql_identifier(name)` — `^[A-Za-z_][A-Za-z0-9_]{0,63}$`.
- `validate_sql_typedef(typedef)` — whitelists the exact shapes
  `init_db` uses (base type, `DEFAULT <literal>`, `REFERENCES …`).
- `add_col` calls both. `fire_notifications` whitelists `event`
  against the six declared notification types; unknown events
  no-op before touching SQL.

#### [#14 — M7 silent except sweep](https://github.com/Kha-kis/manga-arr/pull/14)

- Four previously silent swallow sites now emit a WARNING or INFO:
  `load_config` DB read failure, `get_db` rollback failure (main
  and shared), lifespan qBit category bootstrap.
- The qBit log line `repr(exception)`s only; password and username
  are explicitly excluded and a regression test guards this.
- Rollback failures log without masking the original exception
  the caller would otherwise have seen.

#### [#15 — M8 deployment docs](https://github.com/Kha-kis/manga-arr/pull/15)

- `docs/deployment.md`: why `0.0.0.0` inside the container, three
  publishing patterns (loopback-only / single-LAN-IP / reverse
  proxy), security checklist cross-referencing prior PRs.
- `.env.example` tracked template.
- No runtime code changed.

---

## Pre-audit history

Prior commits are under earlier tags / on the original work. This
changelog file starts at the 2026-04-15 hardening release.

# Changelog

All notable changes to this project. Format roughly follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

_No unreleased changes._

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

### Known issues

- Issue [#22](https://github.com/Kha-kis/manga-arr/issues/22):
  `tests/python/test_api_key_middleware.py::test_api_route_fails_closed_when_api_key_blank`
  hangs under pytest. This reproduces on `master` and is tracked as a
  baseline test issue; it does not block this release.

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

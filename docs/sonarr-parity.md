# Sonarr Parity Inventory

Last reviewed: 2026-07-10

This inventory compares Mangarr against the Sonarr/Servarr feature surface
using current Mangarr routes, templates, tests, and the public Servarr docs.
Parity here means "the same operational job exists for manga management",
not exact TV terminology, exact Sonarr UI, or identical plugin breadth.

## Reference Surface

- Sonarr overview: RSS monitoring, automatic grabbing, sorting/renaming, and
  quality upgrades:
  <https://wiki.servarr.com/sonarr>
- Sonarr settings: media management, root folders, profiles, indexers,
  download clients, remote path mappings, import lists/list exclusions,
  connect, metadata, general settings, backups, updates, and UI settings:
  <https://wiki.servarr.com/sonarr/settings>
- Sonarr search and release ranking behavior:
  <https://wiki.servarr.com/sonarr/faq>
- Sonarr quick start: root folders, completed download handling, existing
  library import, manual import, and manage/remap workflows:
  <https://wiki.servarr.com/sonarr/quick-start-guide>
- Sonarr environment overrides, including auth, logging, PostgreSQL, server,
  and update namespaces:
  <https://wiki.servarr.com/sonarr/environment-variables>

## Implemented Or Close Enough

| Area | Mangarr evidence | Status |
| --- | --- | --- |
| Library and series management | `/`, `/search`, `/series/{id}`, `/series-editor`, tags, aliases, monitored state, root-folder assignment | Covered |
| Wanted and cutoff-unmet work queues | `/wanted`, `/wanted/cutoff-unmet`, `grab-wanted`, `grab-all-wanted` | Covered |
| Calendar | `/calendar` | Covered |
| Manual search and grab | Series/volume search APIs, grab-release, complete-pack search, Suwayomi DDL search | Covered |
| Queue/activity/history | `/queue`, `/activity`, `/history`, queue reset/remove/blocklist/category actions | Covered |
| Manual import | `/manual-import`, `/import`, review/process/retry/skip/dismiss, `/api/manual-import/*` tests | Covered |
| Failed/stalled download handling | Queue reset, blocklist, mark-failed, stuck retry/self-heal tasks | Mostly covered |
| Quality profiles | `/quality-profiles`, quality definitions, profile score matrix | Covered |
| Custom formats | `/custom-formats`, import/export JSON, per-profile scores, preview | Covered |
| Release profiles | `/release-profiles`, must/must-not/preferred/tag/indexer logic | Covered |
| Delay profiles | `/delay-profiles`, tag targeting, preferred protocol, bypass-if-highest-quality | Covered |
| Language profiles | `/language-profiles` | Covered |
| Indexers | `/indexers`, Prowlarr sync, Torznab/Newznab testing, per-sub-indexer toggles, manga categories | Covered |
| Download clients | `/download-clients`, qBittorrent, SABnzbd, Suwayomi, client options, tests | Mostly covered |
| Remote path mappings | `remote_path_mappings` schema/table, download-client UI, create/delete tests | Covered |
| Import lists | `/import-lists`, sync routes, scheduled import-list task | Covered |
| Notifications/connect | `/notifications`, Discord, Ntfy, Gotify, Apprise, Pushover, Pushbullet, Slack, email, webhooks, Komga scan | Covered |
| System tasks/status/logs/backups | `/system/status`, `/system/tasks`, `/logs`, `/system/backup`, scheduled backups, General settings for log level and URL base | Mostly covered |
| Root-folder free-space display | `/system/status` disk-space panel, `/stats` disk usage summary | Covered |
| Health and maintenance | `/health`, recycle bin, metadata health/reconcile tools, API key regeneration | Covered |
| API authentication | `/api/*` API-key middleware, CSRF bypass for API-key clients, fail-closed tests | Covered |
| API v1 seed | `/api/v1/system/status`, `/api/v1/series`, `/api/v1/series/lookup`, `/api/v1/series/{id}`, `/api/v1/calendar`, `/api/v1/queue`, `/api/v1/history`, `/api/v1/wanted`, `/api/v1/wanted/cutoff`, `/api/v1/blocklist`, `/api/v1/command`, `/api/v1/rootfolder`, `/api/v1/qualityprofile`, `/api/v1/qualitydefinition`, `/api/v1/languageprofile`, `/api/v1/customformat`, `/api/v1/releaseprofile`, `/api/v1/delayprofile`, `/api/v1/indexer`, `/api/v1/downloadclient`, `/api/v1/downloadclient/remotepathmapping`, `/api/v1/importlist`, `/api/v1/importlistexclusion`, `/api/v1/tag`, plus `POST /api/v1/series`, `PATCH /api/v1/series/{id}`, `DELETE /api/v1/series/{id}`, `POST /api/v1/series/{id}/restore`, `POST /api/v1/command`, `POST /api/v1/rootfolder`, `POST /api/v1/rootfolder/{id}/default`, `DELETE /api/v1/rootfolder/{id}`, `POST /api/v1/qualityprofile`, `PATCH /api/v1/qualityprofile/{id}`, `PUT /api/v1/qualityprofile/{id}`, `POST /api/v1/qualityprofile/{id}/default`, `DELETE /api/v1/qualityprofile/{id}`, `POST /api/v1/languageprofile`, `PATCH /api/v1/languageprofile/{id}`, `PUT /api/v1/languageprofile/{id}`, `POST /api/v1/languageprofile/{id}/default`, `DELETE /api/v1/languageprofile/{id}`, `POST /api/v1/releaseprofile`, `PATCH /api/v1/releaseprofile/{id}`, `PUT /api/v1/releaseprofile/{id}`, `DELETE /api/v1/releaseprofile/{id}`, `POST /api/v1/delayprofile`, `PATCH /api/v1/delayprofile/{id}`, `PUT /api/v1/delayprofile/{id}`, `DELETE /api/v1/delayprofile/{id}`, `DELETE /api/v1/blocklist`, `DELETE /api/v1/blocklist/{id}`, `POST /api/v1/history/{id}/failed`, `DELETE /api/v1/history/failed`, `DELETE /api/v1/history/{id}`, `POST /api/v1/queue/grabbed/{volume_id}/reset`, `DELETE /api/v1/queue/pending/{pending_id}`, `DELETE /api/v1/queue/import/failed`, `DELETE /api/v1/queue/import/{queue_id}`, `POST /api/v1/queue/import/{queue_id}/skip`, and `POST /api/v1/queue/import/{queue_id}/retry` with response-contract tests | Initial slice covered |
| Rename/organize files | `/api/v1/rename/series/{id}/preview`, `POST /api/v1/rename/series/{id}`, and the series-page HTMX rename panel preview selected renames, report conflicts, rename safe files, and update import paths | Mostly covered |
| Existing-library adoption | `/api/v1/rootfolder/{id}/unmappedfolders` reports root-folder child directories not mapped to known series; `/api/v1/rootfolder/{id}/unmappedfolders/matches` proposes metadata matches; `POST /api/v1/rootfolder/{id}/unmappedfolders/adopt` creates a series for a selected folder, selected metadata, and existing files; Settings → Root Folders exposes scan/match/adopt controls | Mostly covered |
| Import-list exclusions | `import_list_exclusions` table, `/import-lists` management UI, and sync-time skip logic by source/external ID or normalized title | Covered |
| Minimum free-space guard | Media Management `minimum_free_space_mb` setting blocks imports before staging when the destination would fall below the configured reserve | Covered |

## True Parity Gaps

### 1. Sonarr-Compatible REST API Coverage

Mangarr now has an initial `/api/v1/*` surface for external automation
clients, including series create/list/detail/lookup, manga-native calendar
buckets, profiles, root folders, queue, history, wanted, cutoff-unmet,
blocklist, commands, system status, series patch/delete/restore, and command execution.
Quality-, language-, release-, and delay-profile mutations and profile/configuration read
coverage includes quality, quality-definition, language, custom-format,
release-profile, delay-profile, indexer, download-client,
remote-path-mapping, import-list, import-list-exclusion, and tag endpoints.
Series reads support common filters, sorting, and header-based paging. Queue,
history, wanted, cutoff-unmet, and blocklist reads support common filters and
paging. Remaining API gaps are deeper compatibility, richer filtering on other
resources, and broader mutation coverage.

Recommended scope:

1. Extend remaining read resources with richer filters/paging and any
   integration-needed detail fields.
2. Add broader mutation endpoints behind response-contract tests.
3. Keep the existing `X-Api-Key` behavior and `/api` CSRF bypass.
4. Treat exact Sonarr field names as compatibility affordances, not a reason
   to leak TV-specific concepts into the manga domain.

### 2. Rename/Organize Existing Library Files

Mangarr imports files, converts/stages archives, writes ComicInfo metadata,
and now has backend rename preview/execution endpoints plus a series-page HTMX
workflow for selected file renames. Remaining gaps are broader library-level
bulk organize views and any future advanced rename options.

Recommended scope:

1. Add a library-level bulk organize view if operators need cross-series
   rename batches.
2. Keep per-series selected renames as the default safe workflow.
3. Reuse the existing staging/import safety patterns rather than moving files
   while holding long SQLite write locks.

### 3. Existing Library Import / Unmapped Folder Adoption

Mangarr supports root folders, root-folder disk usage, manual import,
unmapped-folder scans per root folder, an API endpoint that adopts a selected
unmapped folder into a series before rescanning existing files, and Settings
UI controls for scanning, matching, and adopting folders. Selected metadata
can seed search pattern, external IDs, cover, status, description, counts, and
year. Remaining gaps are advanced matching controls and folder rename/custom
path handling when the preferred metadata title does not match the existing
folder name.

Recommended scope:

1. Add advanced metadata disambiguation controls for ambiguous folder names.
2. Design explicit folder rename/custom-path handling for cases where the
   preferred metadata title differs from the existing folder name.
3. Keep ad-hoc download-folder manual import separate from existing organized
   library adoption.

### 4. Backup Restore Workflow

Mangarr can create, download, retain, and delete backups. The restore guidance
is documented, and the app warns about secret-key requirements, but there is
not an in-app restore flow equivalent to Sonarr's backup/restore workflow.

Recommended scope:

1. Keep restore as documented/manual unless real deployments need UI restore.
2. If adding UI restore, require upload validation, explicit shutdown/restart
   guidance, and secret-key compatibility checks.
3. Do not restore over a live DB without a deliberately designed maintenance
   mode.

### 5. Media-Management Permissions And Import Options

Sonarr exposes advanced import/rename options such as chmod/chown, hardlink/copy
preferences, extra-file import, and custom import scripts. Mangarr now includes
hardlink/move/copy import modes and a configurable minimum-free-space guard, and
otherwise relies mostly on container/user/filesystem setup plus its archive
staging pipeline.

Recommended scope:

1. Document container UID/GID behavior before adding chmod/chown settings.
2. Treat custom import scripts as low priority until there is a concrete user
   need.
3. Keep hardlink/copy behavior scoped to manga archive workflows; do not copy
   Sonarr options that do not map cleanly.

### 6. General/System Settings Breadth

Mangarr covers API keys, backups, logging views, root folders, log-level
controls, URL-base storage, selected environment defaults, and deployment
documentation. Sonarr also exposes broader settings for proxy, analytics,
updates, detailed syslog, UI date/style preferences, and server options.

Recommended scope:

1. Add only deployment-relevant settings first: proxy guidance, additional
   documented environment overrides, and any server options that map cleanly
   to Docker Compose.
2. Keep analytics out unless explicitly desired.
3. Treat UI date/style preferences as low priority because Mangarr's current
   UI has one maintained theme.

### 7. Download-Client Breadth

Mangarr covers the clients used by this project, but Sonarr supports many more
download clients and exposes more per-client advanced options.

Recommended scope:

1. Keep qBittorrent, SABnzbd, and Suwayomi robust before adding clients.
2. Add new clients only when backed by user demand and integration tests.
3. Consider Transmission/Deluge after API and rename workflows are complete.

### 8. Built-In Updater

Sonarr has an in-app updater. Mangarr is deployed by Docker Compose and should
not mutate its own running installation by default.

Recommended scope:

1. Treat in-app binary update as intentionally out-of-scope for Docker
   deployments.
2. Optionally add an update-available indicator that links to release notes,
   without editing the running install.

### 9. PostgreSQL Backend

Sonarr documents PostgreSQL environment settings. Mangarr is SQLite-first and
has active work specifically focused on SQLite contention and short write
transactions.

Recommended scope:

1. Keep SQLite as the supported backend.
2. Revisit PostgreSQL only if concurrent-write pressure remains after the
   import lock refactor and queue/event write reductions.

## Prioritized Execution Plan

1. Continue API parity coverage. This unlocks external automation and is easy
   to test without file I/O risk.
2. Rename planner dry-run. This is the largest remaining user-facing Sonarr
   workflow gap and should start read-only.
3. Existing-library advanced matching/path handling. Build on the current
   scan, match proposal, and adoption workflow.
4. API mutation endpoints. Add after read endpoints and route contracts are
   stable.
5. General settings polish: additional proxy guidance, server options, and
   selected env overrides.
6. Optional backup restore UI, extra download clients, update indicator, and
   PostgreSQL evaluation. These should wait for explicit deployment/user demand.

## Non-Goals

- TV-specific episode/season concepts that do not map cleanly to manga
  volumes, chapters, editions, and packs.
- Exact Sonarr UI copy or layout.
- In-app self-update that mutates a Docker Compose deployment.
- A compatibility promise that every Sonarr third-party tool will work without
  adapter changes.

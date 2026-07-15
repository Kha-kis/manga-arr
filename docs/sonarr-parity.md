# Sonarr Parity Inventory

Last reviewed: 2026-07-11

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
| Download clients | `/download-clients`, qBittorrent, SABnzbd, Suwayomi, client options, connection tests, circuit resets | Mostly covered |
| Remote path mappings | `remote_path_mappings` schema/table, download-client UI, create/delete tests | Covered |
| Import lists | `/import-lists`, sync routes, scheduled import-list task | Covered |
| Notifications/connect | `/notifications`, Discord, Ntfy, Gotify, Apprise, Pushover, Pushbullet, Slack, email, webhooks, Komga scan | Covered |
| System tasks/status/logs/backups/updates | `/system/status`, `/system/tasks`, `/logs`, `/system/backup`, scheduled backups, Docker-safe update status, General settings for log level and URL base | Mostly covered |
| Root-folder free-space display | `/system/status` disk-space panel, `/stats` disk usage summary | Covered |
| Health and maintenance | `/health`, recycle bin, metadata health/reconcile tools, API key regeneration | Covered |
| API authentication | `/api/*` API-key middleware, CSRF bypass for API-key clients, fail-closed tests | Covered |
| Sonarr API version alias | `/api/v3/*` rewrites to the tested `/api/v1/*` surface while preserving API-key authentication and query strings; Sonarr-style `episode` and `episodefile` read aliases map to chapter and media-file inventory, including numeric episodefile detail IDs | Covered |
| API v1 seed | `/api/v1/system/status`, `/api/v1/system/update`, `/api/v1/health`, `/api/v1/diskspace`, `/api/v1/config/host`, `/api/v1/config/mediamanagement`, `/api/v1/config/indexer`, `/api/v1/config/downloadclient`, `/api/v1/config/ui`, `/api/v1/config/naming`, `/api/v1/config/metadata`, `/api/v1/system/task`, `/api/v1/system/backup`, `/api/v1/system/backup/{filename}/download`, `/api/v1/log`, `/api/v1/series`, `/api/v1/series/lookup`, `/api/v1/series/{id}`, `/api/v1/mediafile`, `/api/v1/mediafile/{id}`, `/api/v1/episodefile`, `/api/v1/episodefile/{id}`, `/api/v1/volume`, `/api/v1/volume/{id}`, `/api/v1/chapter`, `/api/v1/chapter/{id}`, `/api/v1/episode`, `/api/v1/episode/{id}`, `/api/v1/calendar`, `/api/v1/queue`, `/api/v1/queue/status`, `/api/v1/history`, `/api/v1/wanted`, `/api/v1/wanted/cutoff`, `/api/v1/wanted/cutoffunmet`, `/api/v1/blocklist`, `/api/v1/command`, `/api/v1/command/{id}`, `/api/v1/rootfolder`, `/api/v1/notification`, `/api/v1/qualityprofile`, `/api/v1/qualitydefinition`, `/api/v1/language`, `/api/v1/language/{code}`, `/api/v1/languageprofile`, `/api/v1/customformat`, `/api/v1/releaseprofile`, `/api/v1/delayprofile`, `/api/v1/indexer`, `/api/v1/downloadclient`, `/api/v1/downloadclient/remotepathmapping`, `/api/v1/importlist`, `/api/v1/importlistexclusion`, `/api/v1/tag`, plus `POST /api/v1/series`, `PATCH /api/v1/series/{id}`, `DELETE /api/v1/series/{id}`, `POST /api/v1/series/{id}/restore`, `POST /api/v1/command`, `POST /api/v1/system/backup`, `POST /api/v1/system/backup/{filename}/validate`, `DELETE /api/v1/system/backup/{filename}`, `PATCH /api/v1/config/host`, `PATCH /api/v1/config/mediamanagement`, `PATCH /api/v1/config/indexer`, `PATCH /api/v1/config/downloadclient`, `PATCH /api/v1/config/ui`, `PATCH /api/v1/config/naming`, `PATCH /api/v1/config/metadata`, `POST /api/v1/rootfolder`, `POST /api/v1/rootfolder/{id}/default`, `DELETE /api/v1/rootfolder/{id}`, `POST /api/v1/notification`, `PATCH /api/v1/notification/{id}`, `PUT /api/v1/notification/{id}`, `DELETE /api/v1/notification/{id}`, `POST /api/v1/qualityprofile`, `PATCH /api/v1/qualityprofile/{id}`, `PUT /api/v1/qualityprofile/{id}`, `POST /api/v1/qualityprofile/{id}/default`, `DELETE /api/v1/qualityprofile/{id}`, `POST /api/v1/languageprofile`, `PATCH /api/v1/languageprofile/{id}`, `PUT /api/v1/languageprofile/{id}`, `POST /api/v1/languageprofile/{id}/default`, `DELETE /api/v1/languageprofile/{id}`, `POST /api/v1/customformat`, `PATCH /api/v1/customformat/{id}`, `PUT /api/v1/customformat/{id}`, `DELETE /api/v1/customformat/{id}`, `POST /api/v1/releaseprofile`, `PATCH /api/v1/releaseprofile/{id}`, `PUT /api/v1/releaseprofile/{id}`, `DELETE /api/v1/releaseprofile/{id}`, `POST /api/v1/delayprofile`, `PATCH /api/v1/delayprofile/{id}`, `PUT /api/v1/delayprofile/{id}`, `DELETE /api/v1/delayprofile/{id}`, `POST /api/v1/indexer`, `PATCH /api/v1/indexer/{id}`, `PUT /api/v1/indexer/{id}`, `DELETE /api/v1/indexer/{id}`, `POST /api/v1/downloadclient`, `POST /api/v1/downloadclient/{id}/test`, `POST /api/v1/downloadclient/{id}/reset-circuit`, `POST /api/v1/downloadclient/reset-all-circuits`, `PATCH /api/v1/downloadclient/{id}`, `PUT /api/v1/downloadclient/{id}`, `DELETE /api/v1/downloadclient/{id}`, `POST /api/v1/downloadclient/remotepathmapping`, `PATCH /api/v1/downloadclient/remotepathmapping/{id}`, `PUT /api/v1/downloadclient/remotepathmapping/{id}`, `DELETE /api/v1/downloadclient/remotepathmapping/{id}`, `POST /api/v1/importlist`, `POST /api/v1/importlist/sync`, `PATCH /api/v1/importlist/{id}`, `PUT /api/v1/importlist/{id}`, `POST /api/v1/importlist/{id}/sync`, `DELETE /api/v1/importlist/{id}`, `POST /api/v1/importlistexclusion`, `PATCH /api/v1/importlistexclusion/{id}`, `PUT /api/v1/importlistexclusion/{id}`, `DELETE /api/v1/importlistexclusion/{id}`, `PATCH /api/v1/tag/{label}`, `PUT /api/v1/tag/{label}`, `DELETE /api/v1/tag/{label}`, `DELETE /api/v1/blocklist`, `DELETE /api/v1/blocklist/{id}`, `POST /api/v1/history/{id}/failed`, `DELETE /api/v1/history/failed`, `DELETE /api/v1/history/{id}`, `POST /api/v1/queue/grabbed/{volume_id}/reset`, `DELETE /api/v1/queue/pending/{pending_id}`, `DELETE /api/v1/queue/import/failed`, `DELETE /api/v1/queue/import/{queue_id}`, `POST /api/v1/queue/import/{queue_id}/skip`, and `POST /api/v1/queue/import/{queue_id}/retry` with response-contract tests | Initial slice covered |
| API v1 notification operations | `POST /api/v1/notification/test` tests draft settings and `POST /api/v1/notification/{id}/test` runs the same saved-connection test sender as the notifications UI, including secret decryption and API-key authentication | Covered |
| API v1 indexer operations | `POST /api/v1/indexer/test` tests draft settings and `POST /api/v1/indexer/{id}/test` runs the same saved-indexer connectivity probe as the settings UI, including stored-secret decryption and API-key authentication | Covered |
| API v1 download-client operations | `POST /api/v1/downloadclient/test` tests draft settings, while saved connection tests and circuit-breaker resets mirror the Download Clients UI operations | Covered |
| API v1 custom-format operations | `POST /api/v1/customformat/{id}/preview` evaluates a saved custom format against a proposed release title using the same matcher as the Custom Formats UI | Covered |
| Rename/organize files | `/organize`, `/api/v1/rename/library/preview`, `POST /api/v1/rename/library`, `/api/v1/rename/series/{id}/preview`, `POST /api/v1/rename/series/{id}`, and the series-page HTMX rename panel preview selected renames, report conflicts, rename safe files, and update import paths | Mostly covered |
| Existing-library adoption | `/api/v1/rootfolder/{id}/unmappedfolders` reports root-folder child directories not mapped to known series; `/api/v1/rootfolder/{id}/unmappedfolders/matches` proposes metadata matches; `POST /api/v1/rootfolder/{id}/unmappedfolders/adopt` creates a series for a selected folder, selected metadata, and existing files; Settings → Root Folders exposes scan/match/adopt controls | Mostly covered |
| Import-list exclusions | `import_list_exclusions` table, `/import-lists` management UI, and sync-time skip logic by source/external ID or normalized title | Covered |
| Minimum free-space guard | Media Management `minimum_free_space_mb` setting blocks imports before staging when the destination would fall below the configured reserve | Covered |

## True Parity Gaps

### 1. Sonarr-Compatible REST API Coverage

Mangarr now has an initial `/api/v1/*` surface for external automation
clients, including series create/list/detail/lookup, manga-native calendar
buckets, profiles, root folders, queue, history, wanted, cutoff-unmet,
blocklist, commands, system status, task list, backup list/create/download/
delete/validate, series patch/delete/restore, and command execution. Sonarr-style `/api/v3/*`
requests are rewritten to the same tested handlers so clients that hard-code
Servarr's v3 prefix can reuse the compatibility surface. Sonarr-style
`episode` and `episodefile` reads are aliased to Mangarr's chapter and media
file inventory, and episode-file detail reads accept numeric IDs in addition to
Mangarr's unambiguous `volume-{id}` / `chapter-{id}` identifiers.
Quality-, language-, custom-format, release-, and delay-profile mutations and profile/configuration read
coverage includes host, media-management, indexer-config,
download-client-config, UI, naming, metadata refresh cadence, quality,
quality-definition, language, custom-format, release-profile, delay-profile,
notification, indexer, download-client, remote-path-mapping, import-list,
import-list-exclusion, and tag endpoints.
Quality-definition size/title mutations are covered for the fields Mangarr
already exposes in the UI.
Root-folder update mutations cover path, label/name, and default selection.
Host and media-management config mutations cover the settings Mangarr already
stores for its General and Media Management forms.
Indexer and download-client config mutations cover the backed global settings
for RSS interval, download working folder, and remove-completed behavior.
UI and naming config mutations cover the backed date display and rename format
settings.
Metadata config mutations cover the backed automatic refresh interval.
Notification, indexer, download-client, remote-path-mapping, import-list,
import-list-exclusion, and tag rename/delete mutations are also covered.
Download-client connection tests and circuit-breaker resets are available
through the v1 API.
Individual detail reads are covered for root folders, notifications, profiles,
custom formats, indexers, download clients, remote-path mappings, import lists,
import-list exclusions, quality definitions, standalone languages, and tags.
Series reads support common filters, sorting, and header-based paging. Media
file, volume, and chapter inventory reads expose the manga-native equivalent of
Sonarr's episode and episode-file inventory, including series/status/quality
filters, file detail reads, and header-based paging. Queue,
history, wanted, cutoff-unmet, and blocklist reads support common filters and
paging. Indexer, download-client, import-list, notification, quality-profile,
language-profile, custom-format, release-profile, and delay-profile reads
support common filters and header-based paging. Root-folder, import-list
exclusion, quality-definition, supported-language, remote-path-mapping, and tag
reads also support filtered/paged responses. Common Servarr-style read aliases
such as
`/api/v1/queue/details`, `/api/v1/wanted/missing`, and
`/api/v1/wanted/cutoffunmet` map to the canonical Mangarr queue, wanted, and
cutoff-unmet responses, and `/api/v1/queue/status` exposes queue summary
counts. Command execution returns trackable command records, command detail
reads are available at `/api/v1/command/{id}`, and common Servarr command
aliases such as `RssSync` map to their canonical Mangarr tasks.
Remaining API gaps are deeper compatibility and broader mutation coverage.

Recommended scope:

1. Extend remaining read resources with richer filters/paging and any
   integration-needed detail fields.
2. Add broader mutation endpoints behind response-contract tests.
3. Keep the existing `X-Api-Key` behavior and `/api` CSRF bypass.
4. Treat exact Sonarr field names as compatibility affordances, not a reason
   to leak TV-specific concepts into the manga domain.

### 2. Rename/Organize Existing Library Files

Mangarr imports files, converts/stages archives, writes ComicInfo metadata,
and now has backend series-level rename preview/execution endpoints, a
library-level rename preview/execution API, a library-level organize page,
plus a series-page HTMX workflow for selected file renames. Remaining gaps
are future advanced rename options.

Recommended scope:

1. Keep per-series selected renames as the default safe workflow.
2. Reuse the existing staging/import safety patterns rather than moving files
   while holding long SQLite write locks.

### 3. Existing Library Import / Unmapped Folder Adoption

Mangarr supports root folders, root-folder disk usage, manual import,
unmapped-folder scans per root folder, an API endpoint that adopts a selected
unmapped folder into a series before rescanning existing files, and Settings
UI controls for scanning, matching, and adopting folders. Selected metadata
can seed search pattern, external IDs, cover, status, description, counts, and
year. Match proposals default to the folder name and can be rerun with a custom
metadata search query for ambiguous or abbreviated folders. Adopted folders are
pinned to their existing library folder leaf, so the series title can differ
from the on-disk folder without breaking rescans or imports.

Recommended scope:

1. Keep ad-hoc download-folder manual import separate from existing organized
   library adoption.

### 4. Backup Restore Workflow

Mangarr can create, download, retain, delete, and validate backups through both
the Backup page and `/api/v1/system/backup` endpoints. Current backup ZIPs use a
WAL-safe SQLite snapshot and include the database, matching encryption key, and
a version manifest. The supported `mangarr backup restore` command performs an
offline restore and keeps timestamped rollback files.

Recommended scope:

1. Keep restore offline; do not replace the live DB without a deliberately
   designed maintenance mode.
2. Continue accepting legacy database-only archives with clear secret-key
   compatibility warnings.
3. Treat browser-uploaded restore as unnecessary until deployments demonstrate
   a real need for it.

### 5. Media-Management Permissions And Import Options

Sonarr exposes advanced import/rename options such as chmod/chown, hardlink/copy
preferences, extra-file import, and custom import scripts. Mangarr now includes
hardlink/move/copy import modes, a configurable minimum-free-space guard, a
container-wide `MANGARR_UMASK`, and deployment documentation for UID/GID and
writable bind-mount ownership. Hardlinks retain the source inode's permissions.

Recommended scope:

1. Keep the documented container UID/GID ownership model current before adding
   chmod/chown settings.
2. Treat custom import scripts as low priority until there is a concrete user
   need.
3. Keep hardlink/copy behavior scoped to manga archive workflows; do not copy
   Sonarr options that do not map cleanly.

### 6. General/System Settings Breadth

Mangarr covers API keys, backups, logging views, root folders, log-level
controls, URL-base storage, selected indexer/download-client/UI/naming config
reads, selected environment defaults, outbound proxy deployment guidance, and
deployment documentation. Sonarr also exposes broader settings for analytics,
updates, detailed syslog, UI style preferences, and server options.

Recommended scope:

1. Add only deployment-relevant settings first: additional documented
   environment overrides and any server options that map cleanly to Docker
   Compose.
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
not mutate its own running installation by default. Mangarr now exposes a
Docker-safe update status card on `/system/status` and read-only
`/api/v1/system/update` metadata with release-note links.

Recommended scope:

1. Treat in-app binary update as intentionally out-of-scope for Docker
   deployments.
2. Keep the update surface read-only unless a non-Docker deployment model needs
   an explicit updater.

### 9. PostgreSQL Backend

Sonarr documents PostgreSQL environment settings. Mangarr is SQLite-first and
has active work specifically focused on SQLite contention and short write
transactions.

Recommended scope:

1. Keep SQLite as the supported backend.
2. Revisit PostgreSQL only if concurrent-write pressure remains after the
   import lock refactor and queue/event write reductions.

## Prioritized Execution Plan

1. Continue API compatibility only where integrations need specific fields,
   filters, paging, or command contracts.
2. Keep rename/organize and existing-library adoption conservative: preview
   first, preserve pinned folder names, and avoid risky bulk file movement.
3. Treat custom scripts, chmod/chown, extra download clients, live backup
   restore, and PostgreSQL as demand-driven deployment work.
4. Maintain Docker-first operations: documented environment overrides,
   read-only update status, release-note links, and manual image upgrades.

## Non-Goals

- TV-specific episode/season concepts that do not map cleanly to manga
  volumes, chapters, editions, and packs.
- Exact Sonarr UI copy or layout.
- In-app self-update that mutates a Docker Compose deployment.
- A compatibility promise that every Sonarr third-party tool will work without
  adapter changes.

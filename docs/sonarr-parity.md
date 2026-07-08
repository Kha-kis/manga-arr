# Sonarr Parity Inventory

Last reviewed: 2026-07-08

This inventory compares Mangarr against the Sonarr/Servarr feature
surface using the current Mangarr routes/templates and the public
Sonarr/Servarr docs. It is intentionally product-focused: parity means
"the same operational job exists for manga management", not that Mangarr
copies TV-specific language or every download-client plugin.

## Reference Surface

- Sonarr public feature list: calendar, manual search, automatic failed
  download handling, notifications, custom quality profiles, multiple
  series views, and built-in updater:
  <https://sonarr.tv/>
- Servarr Sonarr settings documentation: media management, root folders,
  quality profiles, custom formats, delay profiles, release profiles,
  download clients, import lists, backups, and related system settings.
  <https://wiki.servarr.com/sonarr/settings>

## Implemented Or Close Enough

| Area | Mangarr evidence | Status |
| --- | --- | --- |
| Library and series management | `/`, `/search`, `/series/{id}`, `/series-editor`, tags, aliases, monitored state, root-folder assignment | Covered |
| Wanted and cutoff-unmet work queues | `/wanted`, `/wanted/cutoff-unmet`, `grab-wanted`, `grab-all-wanted` | Covered |
| Calendar | `/calendar` | Covered |
| Manual search and grab | series/volume search APIs, grab-release, complete-pack search, Suwayomi DDL search | Covered |
| Queue/activity/history | `/queue`, `/activity`, `/history`, queue reset/remove/blocklist/category actions | Covered |
| Manual import | `/manual-import`, `/import`, queue review/process/retry/skip/dismiss | Covered |
| Failed/stalled download handling | queue reset, blocklist, mark-failed, stuck retry/self-heal tasks | Mostly covered |
| Quality profiles | `/quality-profiles`, quality definitions, profile score matrix | Covered |
| Custom formats | `/custom-formats`, import/export JSON, per-profile scores, preview | Covered |
| Release profiles | `/release-profiles`, must/must-not/preferred/tag/indexer logic | Covered |
| Delay profiles | `/delay-profiles`, tag targeting, preferred protocol, bypass-if-highest-quality | Covered |
| Language profiles | `/language-profiles` | Covered |
| Indexers | `/indexers`, Prowlarr sync, Torznab/Newznab testing, per-sub-indexer toggles | Covered |
| Download clients | `/download-clients`, qBittorrent, SABnzbd, Suwayomi, remote path mappings, options | Mostly covered |
| Import lists | `/import-lists`, sync routes, scheduled import-list task | Covered |
| Notifications/connect | `/notifications`, Discord, Ntfy, Gotify, Apprise, Pushover, Pushbullet, Slack, email, webhooks, Komga scan | Covered |
| System tasks/status/logs/backups | `/system/status`, `/system/tasks`, `/logs`, `/system/backup`, scheduled backups | Covered |
| Health and maintenance | `/health`, recycle bin, metadata health/reconcile tools, API key regeneration | Covered |

## True Parity Gaps

### 1. Sonarr-compatible REST API coverage

Mangarr has operational API endpoints, but not a broad Sonarr-compatible
API contract for automation clients. Missing or incomplete areas include
stable JSON endpoints for series CRUD, profiles, root folders, queue,
history, wanted/cutoff, blocklist, commands, and system status using
Sonarr-like request/response shapes.

Recommended scope:

1. Add a read-only API layer first: `/api/v1/series`, `/api/v1/queue`,
   `/api/v1/history`, `/api/v1/wanted`, `/api/v1/rootfolder`,
   `/api/v1/qualityprofile`, `/api/v1/system/status`.
2. Add mutation endpoints only after response contracts are covered by
   tests.
3. Keep the existing `X-Api-Key` behavior and `/api` CSRF bypass.

### 2. Rename/organize existing library files

Mangarr imports and writes ComicInfo metadata, and it has naming preview
helpers, but it does not expose a full Sonarr-style "preview rename /
bulk rename / organize existing files" workflow for already-imported
library files.

Recommended scope:

1. Build a dry-run rename planner for imported volumes/chapters.
2. Surface a per-series preview modal with old path, new path, conflict
   status, and selectable rows.
3. Execute selected renames atomically with history rows and rollback-safe
   DB updates.

### 3. Media-management permissions and ownership controls

Sonarr exposes import/rename permission controls such as chmod/chown.
Mangarr currently relies on container/user/filesystem setup and does not
have app-level chmod/chown controls.

Recommended scope:

1. Add optional file-permission settings only if a real deployment need
   appears.
2. Prefer documenting container UID/GID behavior before adding chmod/chown
   writes, because incorrect ownership mutation is risky in Docker mounts.

### 4. Root-folder free-space and unmapped-folder inspection

Mangarr supports root folders, default selection, and series assignment,
but does not yet provide the richer Sonarr-style root-folder inspection:
free-space display and unmapped folder discovery under each root.

Recommended scope:

1. Add free-space metrics to root folder rows.
2. Add an "unmapped folders" scan that lists directories not associated
   with a series.
3. Offer import/adopt actions only after read-only discovery is stable.

### 5. Import-list exclusions

Mangarr has import lists and sync, but no clearly surfaced Sonarr-style
import-list exclusion workflow for preventing a discovered item from
being re-added later.

Recommended scope:

1. Add an `import_list_exclusions` table keyed by provider/source and
   external manga ID.
2. Add "exclude" actions from import-list preview/sync results.
3. Make sync skip exclusions and expose an exclusions management page.

### 6. Download-client breadth and per-client advanced options

Mangarr covers the core clients used by this project, but Sonarr supports
many more clients and exposes more per-client options. For Mangarr, this
is a lower-value parity gap than API and media-management workflows.

Recommended scope:

1. Keep qBittorrent and SABnzbd robust before adding clients.
2. Add new clients only when backed by user demand and integration tests.
3. Consider Transmission/Deluge only after the API and rename workflows
   are complete.

### 7. Built-in updater

Sonarr has an in-app updater. Mangarr is deployed by Docker Compose and
does not currently expose an updater.

Recommended scope:

1. Treat in-app binary update as intentionally out-of-scope for Docker
   deployments.
2. Optionally add an update-available indicator that links to release
   notes, without mutating the running install.

## Prioritized Execution Plan

1. API parity read endpoints. This unlocks external automation and is
   easy to test without file I/O risk.
2. Root-folder free-space and unmapped-folder read-only inspection. This
   is useful, low-risk, and complements existing root-folder settings.
3. Rename planner dry-run. This needs careful path/conflict handling, so
   start read-only.
4. Import-list exclusions. Medium-sized DB and UI feature with clear
   tests.
5. API mutation endpoints. Add after read endpoints and route contracts
   are stable.
6. Optional file permission controls, extra download clients, and update
   indicator. These should wait until there is explicit deployment/user
   demand.

## Non-Goals

- TV-specific episode/season concepts that do not map cleanly to manga
  volumes, chapters, editions, and packs.
- Exact Sonarr UI copy or layout.
- In-app self-update that mutates a Docker Compose deployment.

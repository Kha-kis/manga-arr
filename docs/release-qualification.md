# Release Qualification

This document defines the evidence required before a Mangarr release candidate
can become a stable release. Passing unit tests alone is not sufficient.

## Release Under Test

- Release candidate: `1.2.0-rc.8`
- Stable target: `1.2.0`
- Qualified base: `1.1.0`
- Candidate image: `ghcr.io/kha-kis/manga-arr:1.2.0-rc.8`
- Platforms: `linux/amd64`, `linux/arm64`

## Production Evidence

- Mangarr has been used in production across the pre-1.0 release line since
  April 2026.
- The `1.2.0-rc.1` production soak found repeatable 10-second health-probe
  timeouts while CPU-bound RSS matching processed large multi-indexer feeds;
  that candidate was rejected rather than promoted.
- The `1.2.0-rc.2` soak completed 74 hourly samples with no health failures,
  restarts, critical logs, or database-integrity errors. It was still rejected:
  a missing SABnzbd API key passed the version-only connection probe, then
  opened the circuit breaker during each daily backlog search and produced
  hundreds of skipped-grab events.
- The `1.2.0-rc.3` soak exposed two independent blockers. SABnzbd had been
  changed from the shared `/data/usenet/complete` folder to its private
  `/config/Downloads/complete` folder, and backlog search matched a series
  alias inside an unrelated release's uploader tag. The deployment path was
  restored and the matcher now shares the boundary-aware RSS behavior.
- The `1.2.0-rc.4` live backlog pass confirmed the boundary-aware title fix,
  then exposed a separate parser defect: dot-delimited publication years such
  as `v63.2012` became decimal volume numbers. Mangarr was stopped before the
  remaining completed downloads could import. Reconciliation preserved all
  history and SAB files, quarantined 14 duplicate imports, corrected dedup
  mappings, and restored an integrity-clean database.
- The `1.2.0-rc.5` parser fixes preserved canonical volume numbers during
  production recovery, but equal-or-better completed downloads were recorded
  as failures and retried every five minutes. Two polls created 100 false
  failure records without changing canonical files. The candidate was stopped
  and rejected.
- The `1.2.0-rc.6` import receipt fix remained stable, but its soak exposed
  duplicate Prowlarr polling and an observability defect. Imported child rows
  polled directly while the enabled parent fanned out to the same Prowlarr IDs.
  Intermittent upstream timeouts were therefore recorded in pairs, and empty
  `httpx` exception strings produced blank application-error events. The
  candidate was rejected even though the container, database, canonical files,
  and import receipts remained stable.
- The `1.2.0-rc.7` candidate completed a 72-hour production soak with zero
  database-lock failures, tracebacks, HTTP 5xx responses, application errors,
  or container restarts. The only recurring external noise was 192 Suwayomi
  `No chapters found` responses, which did not affect Mangarr state.
- The `1.2.0-rc.8` candidate must complete a fresh operational soak with daily
  health, restart, log, integrity, import-path, and backlog-search evidence.
  Recurring configuration errors, inaccessible completed downloads, unrelated
  automatic grabs, malformed volume numbers, or download-client
  circuit-breaker transitions block promotion even when the container and
  health endpoint remain available.
- A completed download skipped because the canonical target has equal or better
  quality creates one terminal `import_skipped` receipt and does not reappear
  on later status polls.
- A queue item that cannot infer a safe volume remains in `needs_review`
  instead of being imported incorrectly or hidden by terminal evidence for an
  imported sibling.

## Metadata Acceptance

`tests/fixtures/metadata_acceptance.json` and
`tests/python/test_metadata_acceptance_corpus.py` cover:

- finished standard manga and automatic update-strategy convergence;
- ongoing series whose locally observed counts exceed provider counts;
- one-shot volume and chapter counts;
- omnibus and curated manual-count protection;
- light-novel count protection;
- alternate-title and genre curation;
- conflicting MangaUpdates counts without catalogue shrinkage.

The broader lifecycle gate also covers provider backoff, cached-map
preservation, cover validation, MangaDex manifests, half chapters, map drift,
reconciliation, and metadata-health rendering.

## Download And Import Matrix

| Area | Acceptance evidence |
| --- | --- |
| qBittorrent | Authentication/version probe, magnet and torrent handoff, missing-hash behavior, save-path routing, timeout, and circuit breaker |
| SABnzbd | API-key-aware connection test, authenticated queue probe, accepted and rejected NZB handoff, transport failure, timeout, circuit recovery, and queue mapping |
| Suwayomi | GraphQL connection probe, source/title confidence, chapter and volume jobs, retry exhaustion and recovery, filesystem import, and idempotency |
| Shared import | Search-to-library E2E, short SQLite claims, bounded concurrency, cancellation, atomic copy/move/hardlink staging, rollback, duplicate quality handling, ranges, packs, specials, and split RAR |

Live connection probes are read-only. They must never enqueue a release merely
to prove connectivity. A public version endpoint alone is insufficient: the
probe must exercise an authenticated operation that requires the configured
credential.

## Installation And Recovery

Before stable release, verify all of the following using the published image:

1. Anonymous clone and unmodified image resolution from the public Compose
   configuration.
2. Non-root startup against empty host directories.
3. Health, browser-first administrator creation, login, logout, and offline
   administrator recovery.
4. Restore of a real database together with its matching secret key.
5. Library counts, administrator login, and decryption of stored integration
   credentials after restore.
6. Upgrade from the previous stable release and rollback using the matching stopped
   `/config` snapshot.

## Stable Release Decision

The `1.2.0` release retains the full `1.1.0` qualification and adds explicit
ambiguous-import review, standalone specials, field-level metadata provenance,
and hardened backup/recovery conventions. The candidate must complete an
operational soak before stable promotion. Qualification evidence includes:

- `make release-local` passing from the exact tagged commit;
- browser smoke, integration, and E2E suites passing in isolation;
- dependency, secret, configuration, and image scans without release blockers;
- fresh-install and upgrade/rollback evidence;
- no recurring configuration errors or download-client circuit-breaker
  transitions during the production soak;
- public support, security, contribution, and conduct policies;
- a protected default branch and immutable annotated release tags;
- candidate publication verification that `1.2.0-rc.8` resolves to the tested
  multi-platform image digest without moving stable aliases;
- stable publication verification that `1.2.0`, `1.2`, `1`, and `latest`
  resolve to the same stable image digest.

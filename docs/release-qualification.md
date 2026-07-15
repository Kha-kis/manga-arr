# Release Qualification

This document defines the evidence required before a Mangarr release candidate
can become a stable release. Passing unit tests alone is not sufficient.

## Candidate Under Test

- Candidate: `1.0.0-rc.2`
- Previous candidate: `1.0.0-rc.1`
- Image: `ghcr.io/kha-kis/manga-arr:1.0.0-rc.2`
- Platforms: `linux/amd64`, `linux/arm64`

## Production Evidence

- Mangarr has been used in production across the pre-1.0 release line since
  April 2026.
- The release-candidate deployment is healthy with no restart loop.
- Available candidate logs contain no SQLite lock, traceback, fatal, or
  unhandled-error signatures.
- A queue item that cannot infer a safe volume remains in `needs_review`
  instead of being imported incorrectly.

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
| SABnzbd | Queue/version probe, accepted and rejected NZB handoff, transport failure, timeout, and queue mapping |
| Suwayomi | GraphQL connection probe, source/title confidence, chapter and volume jobs, retry exhaustion and recovery, filesystem import, and idempotency |
| Shared import | Search-to-library E2E, short SQLite claims, bounded concurrency, cancellation, atomic copy/move/hardlink staging, rollback, duplicate quality handling, ranges, packs, specials, and split RAR |

Live connection probes are read-only. They must never enqueue a release merely
to prove connectivity.

## Installation And Recovery

Before stable release, verify all of the following using the published image:

1. Anonymous clone and unmodified image resolution from the public Compose
   configuration.
2. Non-root startup against empty host directories.
3. Health, setup token permissions, administrator creation, login, and setup
   token removal.
4. Restore of a real database together with its matching secret key.
5. Library counts, administrator login, and decryption of stored integration
   credentials after restore.
6. Upgrade from the previous candidate and rollback using the matching stopped
   `/config` snapshot.

## Stable Release Decision

A stable release requires:

- `make release-local` passing from the exact tagged commit;
- browser smoke, integration, and E2E suites passing in isolation;
- dependency, secret, configuration, and image scans without release blockers;
- fresh-install and upgrade/rollback evidence;
- public support, security, contribution, and conduct policies;
- a protected default branch and an immutable published tag;
- `latest` resolving to the exact stable image digest.

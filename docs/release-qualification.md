# Release Qualification

This document defines the evidence required before a Mangarr release candidate
can become a stable release. Passing unit tests alone is not sufficient.

## Release Under Test

- Release candidate: `1.3.0-rc.2`
- Stable target: `1.3.0`
- Previous stable: `1.2.0`
- Candidate image: `ghcr.io/kha-kis/manga-arr:1.3.0-rc.2`
- Candidate digest: pending publication
- Platforms: `linux/amd64`, `linux/arm64`

The current stable release remains 1.2.0. RC1 is an immutable rejected
candidate whose publication and qualification evidence is preserved below.
RC2 is not yet tagged, published, deployed, or qualified. Stable 1.3.0 remains
blocked.

## Release Preparation Evidence

The release-preparation branch must record fresh evidence from its exact tree:

- focused release metadata and documentation consistency tests;
- `make test-release-safe` with Python, confirmation-flow, route-sweep, and all
  isolated browser suite counts;
- `make release-local`, including dependency, secret, configuration, image
  identity/content, and fixed High/Critical vulnerability gates;
- generated image tags containing only
  `ghcr.io/kha-kis/manga-arr:1.3.0-rc.2`.

Results are recorded in the candidate changelog and pull request after the
commands complete. A passing preparation branch does not qualify an unpublished
image or authorize stable promotion.

## 1.3 Metadata Acceptance

The 1.3 milestone establishes these operator-facing invariants:

- stored AniList and MangaUpdates identities anchor future enrichment;
- one unique stored-MAL match can anchor AniList resolution, while ambiguous
  title-only results fail closed and preserve cached metadata;
- every production creation and adoption path initializes deterministic title
  provenance and locking in the series-creation transaction;
- unlocking a local title relinquishes its recommendation dominance without
  mutating the title, and explicit candidate application transfers ownership;
- equal-value source drift can reconcile provenance without rewriting the
  application value or triggering value-change side effects;
- metadata application revalidates current values, candidates, conflicts, and
  locks before committing;
- existing-library matching reports equal-strength identity ambiguity while
  preserving explicit operator selection;
- AniList candidate confidence records the actual current identity evidence;
- downloaded and local count observations remain lower bounds, and ambiguous or
  failed provider resolution cannot remove cached metadata or local files.

`tests/python/test_metadata_milestone_lifecycle.py` is the final integrated
acceptance gate added by PR #369. Its three scenarios exercise production
creation/adoption routes, provenance and lock transitions, search and grab,
completed-download import, durable filesystem publication, rescan, and later
metadata refresh while replacing only external provider, indexer, cover, and
download-client I/O.

Publication year or format as automatic identity tie-break evidence, exact
provenance for bare numeric metadata searches, and historical/original
discovery-confidence storage remain intentionally deferred. The fail-closed
identity policy makes these non-blocking for 1.3.

## Candidate Qualification Gates

### Exact Commit And Publication

1. Merge the reviewed release-preparation PR without changing its qualified
   content.
2. Run `make release-local` from the exact merge commit.
3. Create immutable tag `v1.3.0-rc.2` on that exact commit only after the local
   gate passes.
4. Confirm the release workflow publishes both amd64 and arm64 manifests and
   record the resulting candidate digest.
5. Verify the image reports version `1.3.0-rc.2`, the tagged commit revision,
   the expected non-root user, and the allowlisted runtime files.
6. Confirm publication creates only `1.3.0-rc.2`; `1.3`, `1`, and `latest`
   must not move. Existing stable tags `1.2.0` and `1.2` must remain unchanged.

### Fresh Installation

1. Pull the candidate by exact version or digest from the public registry.
2. Start it as a non-root user against empty `/config` and library directories
   using the public Compose configuration.
3. Complete browser-first administrator creation, login, logout, and offline
   administrator recovery.
4. Verify `/healthz`, System Status version/revision, database integrity,
   foreign keys, and writable configured paths.
5. Add a representative series and complete metadata refresh,
   existing-library adoption, and search/grab/import workflows.

### Upgrade And Rollback

1. Stop stable 1.2.0 and create a matching copy or snapshot of realistic
   `/config` data, including the database and secret key.
2. Start the candidate against a copy of that snapshot and verify migrations,
   administrator login, stored credential decryption, library counts, provider
   identities, title ownership, downloaded state, and folder mappings.
3. Refresh representative existing libraries, including manual, local, and
   provider-owned titles, and verify no unexpected identity or ownership
   changes.
4. Exercise existing-library adoption and one representative
   search/grab/download/import/rescan lifecycle.
5. Stop the candidate, restore the matching stopped 1.2.0 `/config` snapshot,
   and verify rollback with the stable image. Never run 1.2.0 against a database
   already migrated by the candidate.

### Integrity And Operational Evidence

Record all of the following from the qualification environment:

- `PRAGMA integrity_check` returns `ok` and `PRAGMA foreign_key_check` returns
  no rows before and after the representative workflows;
- `/healthz` remains HTTP 200, with no unexpected HTTP 5xx responses,
  application errors, tracebacks, container restarts, or database-lock errors;
- metadata refresh preserves cached values during provider failures and makes
  no unexpected provider-ID, title-ownership, downloaded-count, or local-file
  changes;
- search/grab/import completes with exact download-client ownership and one
  durable imported result;
- each configured download client completes an authenticated operation that
  requires its stored credential; a public version endpoint alone is not
  sufficient evidence;
- existing-library adoption preserves the explicitly selected identity and
  folder mapping;
- no recurring configuration errors or circuit-breaker transitions occur for
  any download client;
- at least one full configured background polling/refresh cycle completes after
  the interactive checks, so scheduled behavior is observed rather than
  inferred from startup alone.

Qualification is evidence-driven. A fixed multi-week soak is not required by
policy, but any blocker or unexplained operational signal resets the affected
gate and requires a corrected candidate.

## Historical 1.2 Evidence

The 1.2.0 release was promoted from `1.2.0-rc.10` after more than 15 days of
production qualification. Its evidence included HTTP 200 health, zero
container restarts, database-lock failures, tracebacks, HTTP 5xx responses, or
application errors, schema version 5, `PRAGMA integrity_check` = `ok`, no
active recovery records, and a controlled qBittorrent-to-library import.

Earlier 1.2 candidates exposed and corrected health-probe starvation, SABnzbd
authentication and shared-path configuration, title-boundary matching,
publication-year volume parsing, terminal duplicate receipts, duplicate
Prowlarr polling, current Torznab parsing, protocol routing, and ambiguous
qBittorrent handoff recovery. That history remains useful regression context,
but neither the RC10 digest nor its production soak qualifies 1.3.0-rc.2.

## 1.3.0-rc.2 Qualification Plan

Status: **PENDING PUBLICATION AND FRESH QUALIFICATION**. RC2 must repeat every
RC1 gate that did not complete; no RC1 runtime result is inherited as RC2
evidence.

### RC1 blocker

- Configured qBittorrent 5.2.3 returned HTTP 204 with an empty login body.
- Protected read-only version and torrent-list APIs returned HTTP 200.
- RC1 required the historical `Ok.` login body and therefore rejected the
  configured downloader before authenticated operation could be qualified.

### RC2 correction

- Every production qBittorrent login response uses one central classifier.
- Historical HTTP 200 plus `Ok.` remains supported; empty HTTP 204 is
  provisional only.
- A valid read-only qBittorrent API response must prove a provisional session
  before Mangarr considers it usable, and no mutation can occur before proof.
- Status and import paths validate the HTTP status and torrent-list response
  shape before publishing status, processing completion, or cleaning orphans.
- Circuit-breaker thresholds, success handling, and exact download-client
  ownership remain unchanged.

### Required fresh RC2 evidence

1. Candidate image identity, digest, architectures, SBOM, and provenance.
2. Stable aliases remain on exact 1.2.0; no `1.3`, `1`, or `latest` RC2 alias.
3. Fresh-install startup, administrator lifecycle, health, integrity, and smoke.
4. Upgrade from a stopped 1.2.0 config and database copy.
5. Saved qBittorrent connection test against the configured client.
6. qBittorrent status polling through the proven session.
7. Controlled search, grab, completion, import, publication, and rescan.
8. Exact qBittorrent download-client ownership through that lifecycle.
9. Closed qBittorrent circuit breaker after healthy operation.
10. SABnzbd and Suwayomi credentialed read-only probes.
11. Candidate-specific metadata identity and ownership cases on the upgrade.
12. Rollback using the matching stopped 1.2.0 snapshot.
13. One complete configured background polling and refresh cycle.
14. Health, HTTP 5xx, application errors, database-lock errors, integrity, and
    foreign-key state throughout qualification.

The qBittorrent connection test, controlled import, upgraded metadata cases,
rollback, and operational background cycle must all be executed fresh against
the published RC2 image.

## 1.3.0-rc.1 Publication And Qualification Evidence

Status: **INCOMPLETE - RELEASE BLOCKER FOUND**. The candidate is rejected for
stable promotion. The hard-stop policy prevented later content, rollback, and
operational-cycle gates from running after the blocker was reproduced.

### Release identity and publication

- PR #370 merge and qualified commit:
  `adb486e5cc8beb5f8cb095dfbf690132b917ea68`
- Annotated tag: `v1.3.0-rc.1`; tag object:
  `e07284b49b97dd6776b8a4ea82eea7e68f6fc87e`
- Release workflow:
  [run 32863777924](https://github.com/Kha-kis/manga-arr/actions/runs/32863777924),
  successful for the exact release commit
- Published image:
  `ghcr.io/kha-kis/manga-arr@sha256:cac61b6e632418d63d3856de7eac5fe39421ae0bddc7a63ef1876bc0d22ce62c`
- The image contains `linux/amd64` and `linux/arm64` manifests and per-platform
  SBOM/provenance attestations. Exact-digest verification confirmed version
  `1.3.0-rc.1`, the release revision, non-root runtime, labels, and contents.
- Before and after publication, `1.2.0`, `1.2`, `1`, and `latest` all resolved
  to stable digest
  `sha256:2750ee8d8f6e5d08703a5bb9c145185052ef0cc13e0f2a76dbdef2e2040cf864`.
  Alias `1.3` was not published.
- GitHub prerelease:
  [Mangarr 1.3.0-rc.1](https://github.com/Kha-kis/manga-arr/releases/tag/v1.3.0-rc.1)

### Exact-commit local gate

`make release-local` passed from the merge commit: Ruff and format checks;
2,255 Python tests passed with 5 skipped; confirmation flow 13/13; route sweep
10/10; browser smoke 32/32; integration 22/22; E2E 29/29; settings 12/12.
`pip-audit` found no known vulnerabilities, gitleaks found no leaks in 413
commits, Trivy configuration found zero High/Critical issues, image identity
passed, and the fixed High/Critical image vulnerability scan found zero issues.

### Fresh installation

The exact digest started against empty isolated config and data directories as
UID/GID 1000 with zero restarts. Health returned HTTP 200; System Status showed
`V1.3.0-RC.1`; configured paths were writable; integrity check returned `ok`;
and the foreign-key check returned no rows. Browser-first administrator
creation, login, logout, offline reset, replacement creation, and replacement
login passed.

A real AniList lookup returned a unique 100-confidence Mob Psycho 100 match.
Creation persisted AniList `85189`, MAL `60783`, MangaUpdates `605012986`, API
title ownership, provider candidates, and counts. Optional chapter-map
enrichment returned no usable map and marked the series degraded while all
identity providers remained healthy and cached state remained intact.

An isolated Akira folder produced an ambiguous two-candidate top match. The
match endpoint did not adopt it. Explicitly choosing AniList `105483` persisted
that identity, retained the folder title as locked `local` ownership, and
mapped its controlled CBZ as downloaded. Integrity and foreign keys remained
clean. No HTTP 5xx or startup error was observed. A normal controlled shutdown
logged an asyncio `CancelledError` traceback from the Suwayomi monitor; this is
recorded for follow-up and was not the gate that stopped qualification.

### 1.2.0 upgrade and blocker

A stopped copy of the production-style 1.2.0 config was used. Baseline and
post-start state matched: 30 series, 797 volumes, 5,957 chapters, 736 downloaded,
61 wanted, 13,424 history rows, 596 seen rows, two terminal import-queue rows,
and zero active imports. Provider identities, titles, title provenance/locks,
root folders, encrypted integration rows, and library mappings were preserved.
Database integrity returned `ok` and foreign-key checks returned no rows.
Automatic RSS was disabled only in the qualification copy to prevent
uncontrolled grabs.

The authenticated download-client gate then failed for configured qBittorrent
5.2.3. Its authentication-bypass response is HTTP 204 with an empty body, while
read-only version and torrent-list endpoints return HTTP 200. Mangarr requires
the login body to contain `Ok`, so its connection test returned
`ok=false` / `HTTP 204`. Status, grab, and import paths use the same assumption.
The stopped 1.2.0 baseline already contained a qBittorrent circuit breaker at
three failures, confirming a pre-existing integration incompatibility rather
than migration damage. This is a release blocker because authenticated client
operation and stable circuit-breaker behavior cannot be qualified.

Per the hard-stop policy, SABnzbd/Suwayomi credential probes, controlled real
search/grab/download/import/rescan, candidate-specific upgraded metadata cases,
matched-snapshot rollback, and a complete operational background cycle were not
run. The candidate and baseline config copies were preserved for diagnosis.
The live stable service remained on exact 1.2.0, healthy with zero restarts and
HTTP 200; no candidate was run against its database.

## Stable Release Decision

Stable 1.3.0 is not yet approved. Promotion requires the published
`1.3.0-rc.2` image to satisfy the fresh-install, 1.2.0 upgrade and rollback,
metadata lifecycle, downloader, import, integrity, and operational gates above.
RC1 remains rejected and immutable. The RC2 digest and fresh results must be
recorded before a stable release PR is prepared.

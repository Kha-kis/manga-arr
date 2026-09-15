# Release Qualification

This document defines the evidence required before a Mangarr release candidate
can become a stable release. Passing unit tests alone is not sufficient.

## Release Under Test

- Release under qualification: `1.3.0`
- Qualified release candidate: `1.3.0-rc.2`
- Previous stable: `1.2.0`
- Published image: `ghcr.io/kha-kis/manga-arr:1.3.0`
- Stable digest: `sha256:f9b9d9785d23e8632af0430909b90867ce3759e2c0297cb15cffea9cddf0187f`
- Platforms: `linux/amd64`, `linux/arm64`

The 1.3.0 container is published and owns `1.3`, `1`, and `latest`. AniList
recovery checks passed on September 15, but final qualification and the GitHub
stable-release announcement are now blocked by fixable OS-package findings in
a refreshed image scan. The previous qualified stable is 1.2.0; production
remains pinned to its unchanged digest. RC1's rejection and RC2's successful
qualification remain separate historical evidence below.

## 1.3.0 Publication Evidence

Status: **IMAGE PUBLISHED; SECURITY GATE BLOCKED** (2026-09-15).
The September 10 AniList qualification hold is resolved; no GitHub stable
release has been announced.

- Reviewed preparation: [PR #375](https://github.com/Kha-kis/manga-arr/pull/375).
- Exact merge and tagged revision: `d79d6aae1424734b31a2ed18756f2ebdcfe54c6d`.
- Annotated tag: `v1.3.0`; tag object `b9252a763fff483779b7c8992677232749897b90`.
- [Release Image run 34502316638](https://github.com/Kha-kis/manga-arr/actions/runs/34502316638)
  succeeded on that revision.
- Index digest: `sha256:f9b9d9785d23e8632af0430909b90867ce3759e2c0297cb15cffea9cddf0187f`.
- amd64 manifest: `sha256:edf4d49df574fc11bdf282ebd8403049621d170a44e9d94a4e7f873398c6ffe4`.
- arm64 manifest: `sha256:6e11ac1e90abfabf7ce5d5c6e55cc97866a33b7c16be0d9e80bf7fc631e55f79`.
- Both platforms include SPDX 2.3 SBOMs and SLSA provenance identifying
  version 1.3.0 and the exact merge revision. Runtime smoke checks use amd64;
  arm64 manifest and attestations are verified, not runtime-tested here.

### Exact-Merge And Artifact Gates

On September 10, `make release-local` passed on the exact merge: 2,317 Python tests with 5
skipped; Ruff lint and formatting; 13/13 confirmation-flow and 10/10 route
checks; isolated browser smoke 32/32, integration 22/22, E2E 29/29, and
settings 12/12. Dependency audit and secret scan passed. Configuration and
image scans found no blocking High/Critical findings. Published-image
verification passed for version, revision, non-root user, and runtime files;
a separate Trivy scan of that exact digest found no fixed High/Critical
vulnerabilities on that date. This historical scan does not supersede the
September 15 security findings below.

Registry checks confirmed `1.3.0`, `1.3`, `1`, and `latest` all resolve to the
stable digest. `1.2.0` and `1.2` retain
`sha256:2750ee8d8f6e5d08703a5bb9c145185052ef0cc13e0f2a76dbdef2e2040cf864`.
Both RC digests below are unchanged. No existing Git release tag or
exact-version image was moved; only the documented moving aliases advanced.

### Published-Image Smoke Checks

The exact digest started with empty config/data directories as UID 1000,
using the public container conventions and a loopback-only test port. Browser
setup, login, logout/relogin, offline administrator reset, replacement setup,
System Status version, and `/healthz` passed. SQLite integrity was `ok`,
foreign-key violations were zero, and title provenance was present for the
newly created series. No live library data was used for the fresh install.

A separate upgrade copy came from the preserved stopped 1.2.0 config/key
snapshot. Preparation verified byte-identical copies and left the original
snapshot unchanged. In the copy only, series monitoring, RSS, import lists,
and download clients were disabled and DDL mode was set to `off`. The original
flag values were retained privately. One historical grabbed chapter and one
queued Suwayomi job remained unchanged; no work was cleared to make the audit
pass. The library mount was read-only, and client probes were explicit
read-only connectivity checks with the copied credentials.

Exact 1.2.0 started on that copy, a temporary administrator was created through
browser setup, and logout/relogin passed. After shutdown, the matching config
and key were preserved as a rollback snapshot. Exact published 1.3.0 then
started on the working copy and accepted the same administrator without reset
or recreation. Login/logout, System Status version, `/healthz`, and connection
checks for qBittorrent, SABnzbd, Suwayomi, and Prowlarr passed.

After upgrade and a forced provider-failure refresh, the complete database
audit still matched the stopped 1.2.0 baseline: 30 series, 797 volumes, 5,957
chapters, 736 downloaded and 61 wanted volumes, 13,424 history rows, 596 seen
rows, two terminal import records, and zero active imports. Provider/title
identity, title-provenance, root-folder, indexer, and client fingerprints were
unchanged. Integrity was `ok` with zero foreign-key violations.

An anchored series with cached description/cover references, two manual/local
fields, and two protected count fields received an actual AniList failure.
Its provider-attempt timestamp advanced, the AniList source became `degraded`
with a recorded error, and the series metadata status became `failed`;
persisted metadata, selected values/sources/locks, and downloaded volume/chapter
counts stayed identical. The last-success timestamp did not advance. This
checks cached cover references, not a new image-byte or visual comparison.

Fresh and upgrade browser checks reported no page errors or local HTTP 5xx
responses. Both qualification containers were stopped afterward. Production
and live downloader configuration were not changed. RC2's real acquisition,
import, rescan, scheduled-operation, and snapshot-restore evidence remains
recorded below; those full workflows were not repeated for this stable-image
smoke pass. The prepared stopped 1.2.0 rollback snapshot is retained.

### Historical AniList Qualification Hold

On 2026-09-10, a direct request to `https://graphql.anilist.co` returned HTTP
403 with an explicit message that its API was temporarily disabled due to
severe stability issues. The fresh AniList success-path smoke check therefore
did not pass. No substitute identity was accepted to make that check green.

Mangarr's search fell back to MangaUpdates. Explicit selection created the
intended work with its MangaUpdates identity, no inferred AniList identity,
and an API-owned title. The series/source metadata state reported the provider
failure. This verifies fallback behavior, not a successful AniList refresh.

That hold required live AniList search/add on a new empty fixture and anchored
refresh on the upgrade copy against the same digest after recovery. Those
checks passed on September 15, as recorded below. The outage evidence and
original fresh-install files were preserved; no tag or image was rebuilt.

The task API reports a normally returned refresh as `completed` even when
the metadata result is failed. Qualification checks must inspect the series
and source metadata states, not just command completion. Exposing that
domain-level failure in command results is a separate follow-up; no runtime
change was bundled into this publication-evidence update.

### AniList Recovery Verification

On 2026-09-15, the AniList endpoint returned HTTP 200 with the expected manga
identity. The remaining checks ran against the same published 1.3.0 digest,
not a new local build:

- A new empty config/data fixture passed administrator setup, login,
  logout/relogin, and System Status version checks. Live lookup and explicit
  add selected AniList 85189 / MAL 60783. Refresh retained both IDs and added
  the matching MangaUpdates identity. AniList, aliases, MangaUpdates, cover,
  and MangaDex-manifest source states were healthy.
- The fresh series had 16 volume rows and an initialized title selection.
  Its overall metadata state was `degraded` because the chapter-map provider
  returned no usable map. This is not an AniList failure or a claim that all
  provider data is complete. SQLite integrity was `ok`, foreign-key violations
  were zero, and no import was queued.
- The upgraded anchored series refreshed to `healthy`. AniList's attempt and
  success timestamps advanced, its failure count reset to zero, and the series
  last-success timestamp advanced. Persisted metadata, identities, title,
  field selections/locks, protected count fields, and downloaded counts matched
  the pre-refresh snapshot exactly.
- The database-wide upgrade audit still matched the retained September 10
  audit. Explicit credential probes for qBittorrent, SABnzbd, Suwayomi, and
  Prowlarr passed again. Browser checks reported no page errors or local HTTP
  5xx responses. Both qualification containers were stopped afterward;
  production and live downloader configuration remained unchanged.

These checks clear the AniList success-path gap. The full September 10 test
gate remains evidence for the unchanged release commit; it was not repeated
or presented as a new September 15 full-suite run.

### Current Security Blocker

The September 15 Trivy scan refreshed its vulnerability database and scanned
the exact published amd64 image with `--ignore-unfixed --severity HIGH,CRITICAL
--exit-code 1`. It exited 1 with **12 findings: 9 High and 3 Critical**. All
were in Debian packages; no Python-package findings appeared in this filtered
scan. Installed versions were independently checked with `dpkg-query` inside
the qualification container.

| Package | Installed | Fixed Version | Findings |
| --- | --- | --- | --- |
| `gzip` | `1.13-1` | `1.13-1+deb13u1` | 1 High |
| `libpcre2-8-0` | `10.46-1~deb13u1` | `10.46-1~deb13u2` | 2 High |
| `libsqlite3-0` | `3.46.1-7+deb13u1` | `3.46.1-7+deb13u2` | 2 High |
| `perl-base` | `5.40.1-6` | `5.40.1-6+deb13u1` | 4 High, 3 Critical |

Debian's tracker confirms the fixed package versions for
[gzip](https://security-tracker.debian.org/tracker/CVE-2026-41992),
[PCRE2](https://security-tracker.debian.org/tracker/CVE-2026-86145),
[SQLite](https://security-tracker.debian.org/tracker/CVE-2026-11822), and
[Perl](https://security-tracker.debian.org/tracker/CVE-2026-13221).
The complete scanner findings are CVE-2026-41992, CVE-2026-86145,
CVE-2026-89161, CVE-2026-11822, CVE-2026-11824, CVE-2026-13221,
CVE-2026-42496, CVE-2026-8376, CVE-2026-42497, CVE-2026-48962,
CVE-2026-57432, and CVE-2026-57433.

These are scanner severity/applicability results, not proof of an exploitable
Mangarr request path. No reachability assessment, severity waiver, or ignore
entry was used to bypass the release gate. The GitHub stable-release
announcement remains withheld. Prepare and review a new patch release with
the fixed OS packages, then qualify its new digest. Preserve immutable
`v1.3.0` and its exact-version image; a version-only change must not reuse a
stale cached package-install layer.

## 1.3.0 Stable Preparation

Historical preparation checklist, now merged as PR #375. The preparation changes
only `app/VERSION` and release documentation from qualified RC2. It introduces
no application behavior, schema, dependency, or image-build changes. RC2's
runtime evidence below remains candidate evidence, not proof of the new stable
artifact.

Publication and qualification sequence:

1. Complete `make release-local` on the preparation branch and record results
   in its pull request, including isolated browser and security/image gates.
2. Merge the reviewed release PR and repeat `make release-local` on the exact
   merge commit. Stop on a failed gate; do not tag or publish.
3. Verify stable metadata emits exactly `1.3.0`, `1.3`, `1`, and `latest`.
   Create annotated tag `v1.3.0` on the qualified merge commit only after all
   exact-commit gates pass. Never move an existing tag or exact-version image.
4. Verify the successful release workflow, amd64/arm64 manifests, SBOM,
   provenance, image version/revision, and exact stable digest. Confirm `1.3`,
   `1`, and `latest` resolve to it, while 1.2.0, 1.2, and both RC artifacts
   retain their recorded digests.
5. Smoke-test the exact stable digest on a fresh install and a separate upgrade
   copy, including login, metadata refresh, downloader connectivity, and
   database integrity. Preserve the matching stopped snapshot for rollback.
6. Publish the GitHub stable release with the exact digest and upgrade/rollback
   notes. Update the README's published-stable status and record stable
   artifact evidence separately from the RC2 results.

Production remains on 1.2.0 during preparation. A production upgrade requires
the verified stable artifact and a fresh stopped config snapshot.

## RC2 Release Preparation Evidence

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

Status: **COMPLETE**. The evidence below is entirely RC2-specific; no RC1
runtime result was inherited as RC2 evidence.

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

## 1.3.0-rc.2 Publication And Qualification Evidence

Status: **QUALIFIED**. Every mandatory RC2 gate completed against the published
exact-digest image. Stable 1.3.0 was not prepared or published by this work.

### Release identity and publication

- PR #373 merge and qualified commit:
  `88d3d1ddc8089815caa538fea1e74fef2d30d28a`
- Annotated tag: `v1.3.0-rc.2`; tag object:
  `c48bb75235a6aece637777a26c523319c7d75c08`
- Release workflow:
  [run 32890569706](https://github.com/Kha-kis/manga-arr/actions/runs/32890569706),
  successful for the exact release commit
- Published image:
  `ghcr.io/kha-kis/manga-arr@sha256:8011aaf983ad5a2ec0c85d263b59d6df3b8b1c461974363a81338a7fa32f17de`
- The index contains `linux/amd64` and `linux/arm64` manifests. Per-platform
  SBOM and provenance attestations were published, and exact-image verification
  confirmed version `1.3.0-rc.2`, the release revision, non-root runtime,
  labels, and allowlisted files.
- GitHub prerelease:
  [Mangarr 1.3.0-rc.2](https://github.com/Kha-kis/manga-arr/releases/tag/v1.3.0-rc.2)
- Before and after publication, `1.2.0`, `1.2`, `1`, and `latest` all resolved
  to stable digest
  `sha256:2750ee8d8f6e5d08703a5bb9c145185052ef0cc13e0f2a76dbdef2e2040cf864`.
  RC1 remained on
  `sha256:cac61b6e632418d63d3856de7eac5fe39421ae0bddc7a63ef1876bc0d22ce62c`,
  and alias `1.3` remained absent.

### Exact-commit local gate

`make release-local` completed outside the command sandbox from the exact merge
commit. Ruff and format checks passed; 2,317 Python tests passed with 5 skipped;
confirmation flow passed 13/13; route sweep passed 10/10; browser smoke passed
32/32; browser integration passed 22/22; browser E2E passed 29/29; and settings
regression passed 12/12. `pip-audit` found no known vulnerabilities, gitleaks
found no leaks across 416 commits, Trivy configuration and fixed image scans
found zero High/Critical issues, and release-image identity/content verification
passed.

The known host-only TestClient stall reproduced only inside the command
sandbox. It was not counted as a pass. The complete exact-commit gate above
finished in the documented outside-sandbox environment.

### Fresh installation and upgrade

The exact RC2 digest started as UID/GID 1000 against empty isolated config and
data directories with zero restarts. Health returned HTTP 200; System Status
showed version RC2, while the exact running image's OCI revision label separately
confirmed `88d3d1ddc8089815caa538fea1e74fef2d30d28a`; configured paths were writable;
integrity returned `ok`; foreign keys returned no rows; and startup had no
tracebacks or HTTP 5xx responses. Browser-first administrator creation, login,
logout, stopped-container recovery, replacement creation, and replacement login
passed. System Status does not currently render the OCI revision.

Qualification ruling: Phase 7's deployed-identity gate is satisfied by pairing
the version rendered by System Status with the exact running image's verified
OCI revision label. Exposing that revision in the application is an observability
follow-up, not an artifact-identity or data-safety failure.

A real unique AniList result was added and refreshed with its AniList and MAL
identities intact. A controlled existing-library folder returned an ambiguous
two-candidate proposal and was not adopted automatically. Explicit adoption
persisted the chosen identity, locked the local title, and associated the
controlled CBZ as downloaded. No unexpected recovery record was created.

The 1.2 upgrade used a fresh copy of the preserved stopped baseline, whose
database and secret-key hashes were reverified before use. Baseline and RC2
matched at 30 series, 797 volumes, 5,957 chapters, 736 downloaded, 61 wanted,
zero grabbed, 13,424 history rows, 596 seen rows, two terminal import rows, and
zero active imports. All 30 AniList, MAL, and MangaUpdates identities, all 30
title-provenance selections, root mappings, download-client identities, and
indexer identities retained their baseline fingerprints. Encrypted credentials
decrypted successfully; upgraded administrator login passed; no unexpected
duplicate, migration, or recovery record appeared; integrity returned `ok`; and
foreign keys returned no rows.

An initial qualification start occurred before RSS was disabled and grabbed one
release. The candidate was stopped immediately, that exact torrent was removed,
and the entire working database was discarded. Qualification restarted from a
new hash-matching copy of the pristine baseline, so no evidence or state from
that setup error was retained.

### qBittorrent and controlled lifecycle

Configured qBittorrent 5.2.3 returned the expected empty HTTP 204 login response.
RC2 treated it as provisional and required a successful HTTP 200 read-only
`/app/version` proof before use. The saved-client test reported connected. A
seeded three-failure breaker cleared through the normal successful saved-client
path; subsequent status polling remained live and healthy, and the breaker table
finished empty. The status cache successfully polled `/torrents/info`, displayed
`qBit live`, and the Health panel reported qBittorrent v5.2.3 healthy. The normal
HTTP 200 plus `Ok.` path remained covered by the release test suite.

One deliberately selected One Piece series ID 37, volume 111 release completed
the real stack from search through rescan. Mangarr selected download-client ID 1
and persisted exact download ID
`5c368ab155c203210109e5d3091007c9321f76a7`. The selected provider GUID and URL
were retained in the database and recorded without publishing private tracker
material as SHA-256
`1f9e37ce5a1ef704d59dbf955ba47b21ea9a99f2b1bd2fd98d7bda71d7322bd8`
and
`b46cc7807a71f1e147894e17b3e0c2366c397bd458e7e052478d6eac6bcb1fde`,
respectively. The intended release moved from wanted to grabbed to downloaded,
produced one seen row, one grab history row, one import history row, and one
215,511,425-byte canonical CBZ with `ComicInfo.xml`. The import queue and
publication journal had no active residue. A second status poll and second
rescan left one downloaded volume, one import, and the original association. The
exact qualification torrent and source payload were then removed while the
isolated imported copy remained intact.

Credentialed read-only probes also reported SABnzbd 5.1.1, Suwayomi connected
with 79 sources, and Prowlarr 2.5.2.5491. No download-client breaker recurred.

### Metadata, rollback, and operations

A real forced refresh of an upgraded anchored series completed healthy with
zero warnings or errors and no application-field change. AniList, MAL, and
MangaUpdates identities, title ownership, a locked manual volume count,
downloaded volume/chapter floors, and cached metadata remained intact.

A deterministic harness ran production metadata helpers against an online
SQLite backup of the upgraded database with controlled provider responses. All
12 required cases passed: AniList anchoring; MangaUpdates identity isolation;
ambiguous AniList fail-closed and cache preservation; manual and local title
locks; unlock without mutation; explicit ownership transfer; equal-value source
reconciliation; downloaded count floors; operator-controlled existing-library
ambiguity; and confidence as observability only. Nine bounded targeted tests
passed; Ruff, Bandit, and format checks passed; BasedPyright reported zero errors
and warnings; and the copied database retained clean integrity and foreign keys.

Rollback stopped RC2 and preserved its working copy, then started exact stable
1.2.0 against a fresh hash-matching copy of the original stopped snapshot. The
original administrator username and password hash matched before supported
offline recovery. Health, replacement login, all baseline counts and
fingerprints, integrity, foreign keys, library access, SABnzbd, Suwayomi, and
Prowlarr passed. Stable 1.2.0 reproduced its known qBittorrent HTTP 204 rejection,
as expected; it was never run against an RC2-migrated database. The original
production stable container was restored on the exact 1.2.0 digest and returned
healthy with zero restarts.

The first RC2 operational run lasted 22 minutes 17 seconds and covered repeated
automatic qBittorrent status polling, the real import worker and publication
flow, rescans, one explicit RSS/indexer cycle, one metadata refresh, health
scheduling, and recovery cleanup. A second scheduler-strengthening run retained
the real persisted `rss_interval=900` setting and observed RSS polls at 20:38:05
and 20:53:07 UTC, plus a scheduled import-list sync at 20:42:57. The two RSS
passes checked 979 and 977 releases. Every series was temporarily unmonitored,
so both passes grabbed zero and created no pending release. Monitoring and RSS
flags were then restored before stopping RC2.

Final operational evidence recorded zero container restarts, Python tracebacks,
HTTP 5xx responses, SQLite lock errors, recovery/replay failures, qBittorrent
authentication failures, breaker rows, integrity failures, or foreign-key
failures. Four application error events came from concurrent TorrentDay HTTP 410
responses during earlier search probes; indexer backoff activated and prevented
recurrence. Suwayomi also reported upstream `No chapters found` responses for
some titles without corrupting local state. These contained external-provider
conditions did not repeat as downloader, database, or identity failures.

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

## Historical RC2 Qualification Decision

RC1 remains rejected and immutable. RC2 completed the fresh-install, 1.2.0
upgrade and rollback, metadata lifecycle, downloader, import, integrity, and
operational gates above. That RC2 decision authorized separate stable
preparation; it did not qualify the later 1.3.0 image. Current stable-image
publication and its qualification hold are recorded at the top of this file.

1.3.0-rc.2 QUALIFIED

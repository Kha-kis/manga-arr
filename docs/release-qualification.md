# Release Qualification

This document defines the evidence required before a Mangarr release candidate
can become a stable release. Passing unit tests alone is not sufficient.

## 1.3.3-rc.1 Qualification

Status: **PUBLISHED; REPRESENTATIVE AMD64 CANDIDATE GATES AND INDEPENDENT REVIEW PASSED** (2026-10-10).
1.3.2 remains the current qualified stable release and owns the stable image aliases.

### Source Baseline

The eight reviewed fixes merged through
`b2fdf13d6d67d6ad5c5d3a58cc4d423ca4388090` (PRs #407, #408, #409, #410,
#411, #412, #414 and #416). PR #415 separately made the root-folder capacity
tests deterministic; it does not change application behavior.
On that exact pre-release-preparation master,
`make test-release-safe` passed: 3,948 Python tests, 17 existing skips,
Ruff/format checks, 13 confirmation checks, 10 route checks and 95 isolated
browser cases. Independent review verified the 23 changed Python files against
the reviewed composition with no new scoped type diagnostics (seven unchanged
baseline diagnostics). These are source-test results, not candidate-image
runtime evidence or a claim of globally clean typing.

PR #418 adds merged-volume ComicInfo enrichment at
`0325cd1f67fc155aa63178182097270033c0c039`; the preparation now includes nine
behavioral fixes. Its frozen patch, committed as
`ddadd317e929c0915821792b2cd93b5b9a6044c0`, passed `make test-release-safe`:
3,953 Python tests, 17 existing skips, 95 isolated browser cases, lint/format,
13 confirmation checks and 10 route checks. The merge tree is identical to that
fix commit, and its four Python-file hashes match this preparation. Independent
review found no blocking issue. Actual LSP and scoped CLI ran with no new scoped
type errors; 29 strict baseline errors remain and warnings increased from 986
to 1,053. This is not a globally clean type-check claim.

Two earlier full-gate attempts stopped at unchanged ownership fixtures under
nonstandard shell masks: 0002 permitted group writes and 0077 masked an explicit
0750 request to 0700. Both affected nodes pass on baseline and fix under standard
0022, as do ten broader controls; the unchanged complete rerun passed under 0022.
No production permissions, safety assertions or application code were changed
to resolve those environment failures.

### Release Head And Published Artifact

- Reviewed preparation head: `5c5fe8f9bd8a28aaf7182548cd80372c3c75c8ac`.
  `make release-local` passed: 3,953 Python tests, 17 existing skips, 95 isolated
  browsers, lint/format, confirmation/route checks, dependency/history-secret/
  configuration scans and local image verification/fixed High/Critical scanning.
- PR #417 merged at `fd5dc1d99bce0f65236e4a47e735bd1baa70a723`. Its tree is
  identical to that fully tested preparation head. Exact merge fast/security/
  image gates passed. The full gate is not relabeled as a literal merge-commit run.
- Annotated `v1.3.3-rc.1` tag object: `462903f13f84e1ebaed5990f47608b56b1d5e348`.
  It points at the exact merge above and has not been moved.
- [Release Image run 38024951805](https://github.com/Kha-kis/manga-arr/actions/runs/38024951805)
  succeeded at 2026-10-10T04:45:46Z.
- Published index: `sha256:282a196c7c92fe46bb280fbf01e51cd1630dfa5da168dba143f2bd11eb318fcd`.
- amd64 manifest: `sha256:aca5227eabd8657840e0fa93ef22b2df966d70b92db6089c07a25570bc6987ab`.
- arm64 manifest: `sha256:9605448b51ab2b6f74fa91156a1a17d67db22ffc79123cd5c6f6676d8f5106fb`.
- Both platforms identify version 1.3.3-rc.1 and the exact merge, with SPDX 2.3
  SBOMs containing 151 packages and SLSA provenance naming the workflow builder
  and build arguments. Build date: 2026-10-10T04:41:47Z.
- Published image version, OCI labels, non-root user and file-inventory checks
  passed. Dated fixed High/Critical scans passed on both exact platform artifacts;
  this does not claim all severities or future scan results are clean.
- `1.3.3-rc.1` is the only new image tag. `1.3.2`, `1.3`, `1` and `latest`
  still resolve to the qualified 1.3.2 index recorded below.

### Published Runtime Checks

- Fresh setup, login/logout/relogin, version/API/health and browser error checks
  passed on the published amd64 artifact.
- Upgrade from an October 10 stopped production 1.3.2 configuration copy and
  matching stopped rollback passed. Protected metadata, provenance, candidates,
  locks, provider IDs, library observations, integration configuration and
  credential decryption were preserved. The encryption key was unchanged.
  Upgrade and rollback had no protected-table or observation differences across
  42 protected tables and 18 positively decrypted credential records.
- Integration scheduling and two inherited queued Suwayomi jobs were disabled
  only in the private upgrade/rollback copies. Those rows and IDs were retained;
  inherited-job replay is excluded from this evidence. Rollback uses its matching
  pre-upgrade database/key, never the candidate-migrated database.
- A separate fresh Mangarr/Suwayomi pair completed a new full-volume download
  through the English MangaDex source: eight chapters, 254 pages, 352.2 seconds.
  All 66,264,365 source bytes were merged into a 71,197,671-byte CBZ. The exact
  image ordering/content, ZIP integrity, unique root ComicInfo.xml, current stored
  series/volume context, absent inherited chapter Number, and final imported
  history size passed. History records Suwayomi/DDL provenance.
- Source archives were mounted read-only in Mangarr. Real-run source hashes were
  captured after download/import completion, before qualification; this is not
  a measured pre/post-import hash comparison. Separate regression tests prove
  that boundary, and the real output matches every downloaded image.
- The fresh workflow used private new state, one job, a 512 MiB scratch limit,
  copy imports and unchanged deadlines. No existing production downloader jobs,
  mappings, settings or library files were changed. Owned containers were stopped,
  forwarding closed and scratch unmounted after the run, without OOM or restarts.

### Coverage Limits And Next Gates

The pinned Suwayomi version could not convert the current publisher APK
(Java verification failure). Fresh qualification used the unmodified publisher's
precompiled JAR, installed through the normal extension endpoint; it did not reuse
production extension state or bypass verification. This proves that compatible
extension path, not arbitrary extension/runtime compatibility.

Earlier upstream rate-limit/timeout and source `NoChapters` attempts remain
failed attempts, not passing qualification. A private test-client host missing its
scheme was corrected before the fresh workflow; public API host validation is a
separate follow-up. Fractional-volume ComicInfo formatting remains an independent
follow-up; this candidate does not change the existing formatter.

This candidate has representative amd64 HTTP/metadata/download/import coverage,
not native ARM runtime, every provider/source, live Suwayomi queue replay, or a
repeat of 1.3.2's NFS, administrator-reset and large-archive artifact qualification.
Historical 1.3.2 receipts below are not reassigned to this image. Stable promotion
requires independent evidence review and any remaining stable acceptance gates.

Production upgrade requires a new matching verified stopped backup and a
qualified artifact. The older qualification snapshot is not a fresh cutover
backup. An older image must not run against the candidate-migrated database.
Independent evidence review cleared the GitHub prerelease and conditionally
cleared Mangarr-only production RC qualification after that fresh backup. It did
not clear stable promotion. Production cutover and subsequent live smoke checks
are separate from the isolated results above.

### Operator Deployment Smoke

A Mangarr-only operator deployment on the published amd64 index above passed
health and 48 desktop/mobile UI/API checks. The browser allowed GET/HEAD/OPTIONS
only; filter/clear and selection-toolbar interactions passed without mutation
attempts, browser errors or failed responses. No library screenshots were taken.
The short-lived smoke session was revoked and the administrator was unchanged.

The first integration probe failed because AniList returned HTTP 429; the other
five providers passed. After more than 100 seconds of backoff, one unchanged,
bounded read-only reprobe passed all six: Prowlarr, qBittorrent, SABnzbd, Suwayomi,
MangaDex and AniList. Separate post-retry health, exact image, zero restart/OOM
and unchanged SAB/Suwayomi checks passed. This is composite evidence plus one
retry, not a successful exit of the original smoke wrapper. The failed receipt
is retained, and connection probes do not prove a production download/import.

Existing provider IDs, series titles and manual/locked selections were unchanged
against the new stopped snapshot, and the encryption key matched. Only Mangarr's
image pin changed; other downloader configuration and mappings were untouched.
Two existing Suwayomi jobs now report tracked upstream chapter errors for manual
review. Those jobs were not resumed/retried, and their underlying download failure
remains undiagnosed. The app log audit found no traceback or HTTP 5xx.

Independent review cleared retaining this RC deployment, not stable promotion.
The owner-preserving stopped clone was verified before migration. Complete
sparse archive creation, gzip integrity, full content/metadata comparison against
that frozen clone, unchanged database/key hashes and archive checksum completed
successfully after the service was running again. This new cutover snapshot is
distinct from the older stopped copy used for isolated upgrade/rollback
qualification. The previous pinned 1.3.2 image is retained for matching rollback.

### Third-Party RC Feedback

The [issue #406 reporter](https://github.com/Kha-kis/manga-arr/issues/406#issuecomment-6099624562)
reported a clean upgrade of a copied 154-series configuration from 1.3.2,
successful integrity checks, and operation as UID/GID 1024:100 with an NFS
library. Their source-switch and lookalike-folder checks selected the intended
Weeb Central files rather than stale MangaDex or similarly named directories.
They also reported working `Unknown_Episode. N.cbz` selection and a four-chapter
volume with ComicInfo. These are third-party observations, not additional
operator-run artifact qualification or a claim of support for every NAS policy.

Two preexisting loose-chapter problems were reported separately. Volume deletion
can reset chapter state despite surviving library files, and rescan does not
restore those files' chapter state. [Issue #421](https://github.com/Kha-kis/manga-arr/issues/421)
tracks that correctness fix and its regression/qualification gate before stable
promotion. [Issue #422](https://github.com/Kha-kis/manga-arr/issues/422) tracks a
separate contribution proposal for opt-in redundant library-file cleanup; it
does not authorize automatic deletion or changes to Suwayomi source downloads.
Neither follow-up is claimed fixed by the published 1.3.3-rc.1 artifact.

## 1.3.2 Stable Qualification

Status: **QUALIFIED; CURRENT STABLE** (2026-10-08).
All local and published-artifact gates passed, with independent final GO and
no required gaps. 1.3.1's historical qualification and exact immutable digest
below remain unchanged;
its former moving aliases now resolve to 1.3.2.

### Published Artifact Identity And Security

- Annotated `v1.3.2` tag object: `d2b50a969e33cb6c3776ddd7b5843081aedc7743`.
- Exact tagged/release revision: `091b2b12ea235eb0d62306bce94b7e5d70746f4c`.
- [Release Image run 37773879450](https://github.com/Kha-kis/manga-arr/actions/runs/37773879450)
  succeeded on that revision at 2026-10-08T12:05:16Z.
- Verified index: `sha256:4b8a312888ba116784b89a2479c5ca51dbf9e2fba881d658e368bfd082b4f590`.
- amd64 manifest: `sha256:76294610de84ce5c9758b4d8b81d3d310ba1760b39faffa809534abed9700d48`.
- arm64 manifest: `sha256:6c2a016e2e56f106e64b1d9802602db554067657657e7a0974f6741dc4da9f69`.
- Both platforms have verified SPDX 2.3 SBOMs with 151 packages and SLSA v1
  provenance naming the exact source, version 1.3.2, build date
  2026-10-08T12:00:48Z and the workflow builder above.
- Published image verification passed; fixed High/Critical scans passed on both
  platform artifacts. These are dated artifact/security checks, not final
  application-runtime qualification or a claim about every vulnerability severity.
- `1.3.2`, `1.3`, `1`, and `latest` resolve to the index above. Exact 1.3.1,
  1.3.0 and 1.2.0 images remain unchanged. Pin the exact version or index digest
  for reproducible deployment rather than relying on moving aliases.

### Reviewed Source Evidence

- Core PR #402 merged at `ce2ed1f8aac1cd10afa66ce5ff49c7482084a832`, tree
  `9f9aa3f920284bcedd0ab73cb608a77e8975360f`. The release-preparation version and
  documentation update merged as PR #403 at the exact tagged revision above.
- Main Python 3.13 full `make test-release-safe`: 3,465 passed, 17 skipped,
  0 failed; Ruff and format checks passed, confirmation flow 13/13, separate
  route sweep 10/10, isolated browser suite 95/95.
- Native Python 3.11 full `make test`: 3,465 passed, 17 skipped, 0 failed;
  confirmation flow 13/13 and separate route sweep 10/10. This did not repeat
  the browser suite. The first native run had three child-pytest dependency
  discovery failures and 3,462 passes. The unchanged cases and full rerun passed
  in the independently reviewed standard test venv, without source changes,
  shims, added skips, or package upgrades.
- Both full suites precede two EOF empty-line removals in
  `tests/python/test_publication_actor_cooperation_391.py` and
  `tests/python/test_publication_private_recovery_391.py`. Post-whitespace
  focused verification passed 29 tests; all 93 application files were unchanged.
  Committed-head fast verification passed 86 tests, 13 confirmation checks, and
  Compose validation. These focused receipts do not relabel the final test-file
  bytes or the release-doc update as the literal full-tested snapshot.
- Original ownership-race and unsupported-errno acceptance assertions remain
  preserved. Independently reviewed external NFS driver registration has 58
  local controls passing on both runtimes; these are preparation controls, not
  actual NFS workflow or final-image results.

### Completed Local Release Gates

- Unmodified `make release-local` on the exact tagged revision passed:
  3,465 Python tests, 17 existing skips, lint/format, 13 confirmation checks,
  10 route checks and 95 isolated browsers, plus dependency/secret/config and
  normal local-image security/identity gates. This later full gate includes the
  previously recorded EOF whitespace corrections and release-doc/version update.
- Actual normal local amd64 image passed the original six-set NFSv4.1 ledger,
  30/30 nodes (2/6/3/3/4/12), on one reviewed bundle with full source/native/
  test-support/qualification receipts before and after, UID1000/caps0 and local
  SQLite/config. This is workflow acceptance, not HTTP-lifespan qualification.
- Seven actual local-image phases passed: fresh setup; 1.2.0 cold/warm upgrades
  and matching stopped rollback; derived 1.3.1 cold/warm upgrades and matching
  stopped rollback. Protected state and credential decryption were preserved.
  Separate administrator reset/old-session revocation also passed.
- Actual local amd64 image large-archive checks passed 4/4 with real 1 GiB/
  20,000-entry scale under a 256 MiB measured-child address-space cap; peak RSS
  44.73 MiB. Decoder checks passed 13/13 on that local normal image.
- Normal local ARM build/scan passed. Temporary ARM guest decoder checks
  passed 13/13 with all 151 approved runtime-file proofs from the same original
  Dockerfile prefix; this is not normal published ARM-runtime execution.

These completed local-image results are not relabeled as published-artifact
workflow passes. Earlier harness/support/fixture/observer failures were retained
and independently reviewed corrections left application/runtime source unchanged.

### Published Runtime Evidence

- The original six-set NFSv4.1 workflow ledger passed 30/30 on the published
  image, with group counts 2/6/3/3/4/12 and unchanged guards, exact pins and full
  inventories, UID 1000/caps0 and local SQLite/config. This is workflow
  acceptance, not HTTP-lifespan qualification.
  Independent final audit verified all 12 before/after receipts, including the
  full 91 source/1,873 native/33 distribution/501 support/11 qualification maps.
- The original seven amd64 phases passed on the published artifact: fresh
  setup; 1.2.0 cold/warm upgrades and matching stopped rollback; derived 1.3.1
  cold/warm upgrades and matching stopped rollback. Frozen helpers and baselines
  were unchanged.
- Separate published-image administrator reset passed. The saved server session
  was rejected before and after replacement setup; the encryption key was
  unchanged, and re-login, health and page-error checks passed.
- Published amd64 large-archive checks passed 4/4 in 29.75 seconds, including
  real 1 GiB/20,000-entry scale, corruption/CRC/hash verification, aliases and
  source atomicity. Peak RSS was 45,396 KiB (44.33 MiB) under the 256 MiB
  measured-child address-space cap. This is separate from the earlier local
  image's 44.73 MiB receipt. Published amd64 decoder checks passed 13/13.
- ARM decoder checks passed 13/13 in a temporary emulated qualification stage
  FROM the exact published arm64 manifest above, with an unchanged qualification
  suffix and no-cache execution (14.9 seconds). It verified aarch64, Debian
  arm64, UID 1000, Python 3.14.7, all 151 runtime source files and the original
  test ASTs. This is not a native ARM hardware, ARM NFS or ARM HTTP qualification;
  the temporary qualification stage was not itself published.

### Release Evidence Status

| Gate | Status |
| --- | --- |
| Exact source, annotated tag, published platform/index identity and aliases | Verified |
| Both SPDX/SLSA attestations and published fixed High/Critical scans | Passed |
| Existing six-set actual NFS 30 on the published image/runtime | Passed 30/30; all 12 receipts independently verified |
| Published fresh/upgrade/matching rollback seven phases | Passed 7/7 |
| Published administrator reset/old-session revocation | Passed |
| Published amd64 large-archive and decoder checks | Passed 4/4 and 13/13 |
| Temporary emulated qualification FROM the exact published ARM image | Passed 13/13 within the scope above |
| Final independent audit/GO | Passed; no required gaps |

All local and published-artifact gates passed within their stated scopes. Final
independent audit/GO additionally verified the evidence; it was not inferred
from pass counts alone. Issues #378 and #391 are references, not closed by this
documentation update.
Production deployment is separate and has not been authorized by these records.

### NAS And Rollback Boundary

Configuration, SQLite, encryption key, and coordination lock must remain local.
Library/download recovery requires the documented hardlink and directory-fsync
support, participating actors, and private/trusted permission boundary, not
hostile private-directory tampering or online database/config replacement.

New recovery namespaces require controlled birth under an app-owned parent
excluding foreign entry writers, including through server ACLs. Durable proof
permits later shared `0770`/`0775` parents. Unknown shared roots are refused;
Mangarr does not chmod/chown existing parents or adopt unproven namespaces.
Existing exact legacy recovery proofs remain distinct from new allocation.

Stop Mangarr and its workers before snapshot or rollback. Retain the matching
stopped configuration/database, encryption key, and previous pinned image;
never run that older image against the migrated database or mix recovery records
with mismatched library/download state. Ambiguous proofs and occupied restore
paths retain artifacts and fences; do not delete them to unblock work.
See [Deployment and recovery](deployment.md) for the full procedure.

## 1.3.1 Stable Qualification

Status: **QUALIFIED** (2026-10-07).

- Git tag: `v1.3.1`
- Merge revision: `b653ce423abddaec1faa1e3283f32de503d335b6`
- Published image: `ghcr.io/kha-kis/manga-arr:1.3.1`
- Stable digest: `sha256:556c657abe60518695d799f00a2c38abd43d762d57b257cc6ef649d8f3c45b7e`
- Platforms: `linux/amd64`, `linux/arm64`
- Publication: [Release Image run 37661034801](https://github.com/Kha-kis/manga-arr/actions/runs/37661034801)

The patch makes the release version invalidate the OS-package update layer,
updates AnyIO from 4.13.0 to 4.14.2 for two security fixes, and removes
build-only `pip` from the runtime image so its vulnerable vendored `urllib3`
is not shipped. It retains the pinned Python base and makes no application,
API, schema, metadata, or downloader behavior changes.

### Source, Image, And Security Evidence

- PR #377 merged the exact reviewed head. `make release-local` then passed on
  the merge revision: 2,324 Python tests passed with 5 skipped; confirmation
  flow passed 13/13; route sweep passed 10/10; isolated browser smoke passed
  32/32, integration 22/22, E2E 29/29, and settings regression 12/12.
- Python 3.11 also passed the complete Python suite: 2,324 passed and 5 skipped.
- The tag workflow passed release metadata, dependency audit, image identity,
  fixed High/Critical vulnerability, immutable-tag, and multi-platform publish
  gates. A fresh scan of the published amd64 digest found zero fixed
  High/Critical OS or Python findings.
- The amd64 runtime reports AnyIO 4.14.2, has no importable or installed `pip`,
  imports Mangarr successfully, runs as the expected non-root user, and carries
  version `1.3.1` plus the exact merge revision in its OCI labels.
- The registry index contains amd64 and arm64 runtime manifests. Both platforms
  have SBOM and provenance attestations.
- At qualification, `1.3.1`, `1.3`, `1`, and `latest` resolved to the stable
  digest above. Exact 1.3.0 and 1.2.0 remain unchanged at
  `sha256:f9b9d9785d23e8632af0430909b90867ce3759e2c0297cb15cffea9cddf0187f`
  and `sha256:2750ee8d8f6e5d08703a5bb9c145185052ef0cc13e0f2a76dbdef2e2040cf864`.

### Published Amd64 Runtime Qualification

- A fresh isolated config completed startup, administrator creation, logout,
  login, authenticated page access, and health checks. SQLite integrity passed,
  foreign-key checks returned no rows, and logs contained no traceback,
  database-lock, or critical errors.
- A compact SQLite snapshot of the running 1.2.0 config was mounted separately
  with an empty `/data` directory and no network access. The published 1.3.1
  digest started successfully and preserved 30 series, 797 volumes, 5,970
  chapters, 360 metadata selections, 409 metadata candidates, all 30 AniList,
  MAL, and MangaUpdates identities, and the existing administrator record.
- The copied secret key decrypted both stored download-client credentials.
  Administrator recovery, replacement setup, logout, login, health, database
  integrity, foreign keys, and the same library/identity/provenance counts all
  passed afterward. No live downloader operation or production data mutation
  occurred.
- The exact 1.2.0 digest also started against a fresh matching snapshot for the
  rollback check. Health, login, 30-series preservation, integrity, foreign
  keys, and clean logs passed.

Production remained on the pinned 1.2.0 image throughout qualification.

## Historical 1.3.0 Release Evidence

- Release evaluated: `1.3.0`
- Qualified release candidate: `1.3.0-rc.2`
- Previous stable: `1.2.0`
- Published image: `ghcr.io/kha-kis/manga-arr:1.3.0`
- Stable digest: `sha256:f9b9d9785d23e8632af0430909b90867ce3759e2c0297cb15cffea9cddf0187f`
- Platforms: `linux/amd64`, `linux/arm64`

When 1.3.0 was published, it owned `1.3`, `1`, and `latest`. AniList recovery
checks passed on September 15, but its final qualification and GitHub
stable-release announcement were blocked by fixable OS-package findings in a
refreshed image scan. The qualified 1.3.1 release subsequently owned the moving
aliases; their current 1.3.2 publication status is recorded above. The exact
1.3.0 image remains immutable. RC1's rejection and RC2's successful
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
preparation; it did not qualify the later 1.3.0 image. That publication and
its historical qualification hold and the later 1.3.1 qualification are recorded
above, separately from the current 1.3.2 publication status.

1.3.0-rc.2 QUALIFIED

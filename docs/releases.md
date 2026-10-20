# Releases And Versioning

Mangarr uses Semantic Versioning for public releases.

## Version Source Of Truth

`app/VERSION` is the canonical application version. It is surfaced by:

- **System > Status**;
- `GET /api/v1/system/status` and its `/api/v3` alias;
- `GET /api/v1/system/update`;
- the FastAPI OpenAPI document.

Release commits must update `app/VERSION`, `CHANGELOG.md`, and the current
release shown in `README.md` together. Automated tests enforce SemVer syntax and
documentation consistency.

## 1.3.3-rc.10 Candidate Status

Preparation began on 2026-10-19 from reviewed
[PR #469](https://github.com/Kha-kis/manga-arr/pull/469) merge
`d4ee929a2f90206b7fc18d1da06878609f32ace6`, tree
`ee809a364106725428e66a006460058521745563`. That merge preserves the tested
integration tree. This preparation changes VERSION and four release documents
only; it does not change application, schema, provider, API, dependency or
Compose behavior.

RC10 includes merged-volume assembly evidence from
[#460](https://github.com/Kha-kis/manga-arr/pull/460), the default-off
loose-chapter quarantine/restore in #469, the publication lookup safeguard in
[#468](https://github.com/Kha-kis/manga-arr/pull/468), and the test/diagnostic
changes in [#470](https://github.com/Kha-kis/manga-arr/pull/470) and
[#471](https://github.com/Kha-kis/manga-arr/pull/471). Quarantine requires
verified current chapter and volume evidence; it neither deletes files nor
adopts historical files. Readers must exclude the quarantine directory and
cannot carry loose-chapter read progress to the volume.

**Published, unqualified and held.** [PR #473](https://github.com/Kha-kis/manga-arr/pull/473)
merge `55f3d6fcc8d8737f1d5180fe1063d863630c36f9` was published as the
[RC10 prerelease](https://github.com/Kha-kis/manga-arr/releases/tag/v1.3.3-rc.10).
Startup replay reproduced [#475](https://github.com/Kha-kis/manga-arr/issues/475).
Its correction is merged in [#476](https://github.com/Kha-kis/manga-arr/pull/476),
but is not present in the immutable RC10 image. The diagnostic-only correction
in [#477](https://github.com/Kha-kis/manga-arr/pull/477) also remains unshipped;
remount ownership and rollback-directory cleanup in
[#474](https://github.com/Kha-kis/manga-arr/issues/474) remain unresolved.

A replacement requires a reviewed preparation merge, a new immutable candidate,
and fresh artifact, cleanup/restore/recovery, runtime and full-day natural-repeat
qualification. Native ARM64 and representative NAS evidence remain unavailable,
not waived. No RC10 partial results or RC9 elapsed time transfer.
Production remains the RC5 owner trial; qualified stable and stable aliases
remain 1.3.2. See [RC10 qualification](release-qualification.md#133-rc10-qualification).

## 1.3.3-rc.9 Candidate Status

Preparation began from exact reviewed [PR #464](https://github.com/Kha-kis/manga-arr/pull/464)
merge `42f8c9d617f0922ca753e9421b57d9cfd8acb210`, tree
`8b5d2da21dbdf346890b7284a04c2f419dbea1f2`. It includes the managed Suwayomi
missing-volume fix in [#463](https://github.com/Kha-kis/manga-arr/pull/463) and
the client-independent chapter safeguard in #464. Recorded parent absence does
not prove independent child absence; acquisition fields, surviving loose files
and uncertain observations remain protected. The portable test fixture from
[#462](https://github.com/Kha-kis/manga-arr/pull/462) retains the real other-UID
attack without a private local-only image dependency.

Reviewed [PR #465](https://github.com/Kha-kis/manga-arr/pull/465) merge
`d52581c26911cf5e9f5965c5ff1a2036d9c022db` has the same tree as the tested
preparation. Annotated immutable `v1.3.3-rc.9` targets that merge. Release Image
[run 39039892066](https://github.com/Kha-kis/manga-arr/actions/runs/39039892066)
published both architectures on 2026-10-17; the
[GitHub prerelease](https://github.com/Kha-kis/manga-arr/releases/tag/v1.3.3-rc.9)
is non-latest and explicitly unqualified. Its immutable index is
`sha256:edf4c122b7b45c026bc6fc7e2134c611332e02cbf00aa23170cc99563f76fe17`.

Independent scoped audits accepted publication identity/attestation/security,
installed core/cache/file controls, 54 installed rescan cases, an adapted public
Compose install, a fresh eight-chapter download/import/rescan workflow, and a
new uninterrupted 87,600-second natural observation with two daily metadata
polls. These results belong only to that artifact, not RC8 or later `master`.

An additional native amd64 cached-image exercise passed unchanged stable
Compose and RC9 with only its image reference changed, including default port,
first setup and same-store restart. Its isolated VFS engine/loopback relay does
not qualify online pulls or default-host storage. Native ARM64 and representative
NAS remain unqualified. The publication lookup guard was subsequently fixed in
[#468](https://github.com/Kha-kis/manga-arr/pull/468); merged-volume evidence in
[#460](https://github.com/Kha-kis/manga-arr/pull/460) and default-off cleanup in
[#469](https://github.com/Kha-kis/manga-arr/pull/469) are also later source work,
not part of RC9. A replacement containing later runtime changes requires fresh
artifact qualification. No immutable tag is replaced or old soak time transferred.
Production remains the owner-controlled RC5 trial; qualified stable remains 1.3.2. See
[RC9 qualification](release-qualification.md#133-rc9-qualification).

## 1.3.3-rc.8 Candidate Status

Source preparation began on 2026-10-15 from exact reviewed
[PR #443](https://github.com/Kha-kis/manga-arr/pull/443) merge
`f7cf47b0bebc35e6d42a69c1abacccd641838b71`, tree
`5711265474d2d283a2abf8e1928a99326e67fb1c`. It includes the chapter-provenance
foundation and the independent IPv6/NZBGet URL fixes merged in
[#455](https://github.com/Kha-kis/manga-arr/pull/455) and
[#456](https://github.com/Kha-kis/manga-arr/pull/456). This preparation changes
only VERSION and four release documents; no further application, schema,
provider, dependency, API or Compose behavior changes.

Reviewed preparation `125e3f287a466c230967c050393e9c856590e321` passed one complete
local release gate: 4,694 Python tests /17 skips, 95 isolated browser cases,
confirmations/routes, configured lint/security and exact-revision image checks.
Independent source/evidence review, exact-head Fast CI and Codex review passed.
[PR #457](https://github.com/Kha-kis/manga-arr/pull/457) merge
`d5c012192da149118f662e064e503b4a704da563` has the identical reviewed tree
`7ea2a9f43354872b93b014cb37e267dd0b46636c`. Fresh merge fast/security/image
checks passed before immutable tagging; no second full-suite run is claimed.

Annotated `v1.3.3-rc.8` targets that exact merge. Release Image
[run 38793313727](https://github.com/Kha-kis/manga-arr/actions/runs/38793313727)
published linux/amd64 and linux/arm64. The
[GitHub prerelease](https://github.com/Kha-kis/manga-arr/releases/tag/v1.3.3-rc.8)
is explicitly non-latest and unqualified. Its immutable index is
`sha256:5357979f5b68400dd05cb6092304b6a54e95e0a3653ae914f42716976502da51`.

A fresh source-reviewed collector passed exact workflow/tag/platform identity,
SLSA/SPDX metadata and nonempty SBOM checks, native amd64 content verification,
and digest-pinned configured fixed HIGH/CRITICAL scans on both platforms.
Independent actual publication audit is GO. Fourteen protected registry tag
identities and 46 snapshotted Git refs were unchanged; stable/latest remain
1.3.2. This does not claim signing, native ARM execution or application/runtime
qualification. Scanner warnings and scope remain explicit in the evidence.

Fresh runtime/environment/platform gates are still required. RC7's interrupted
full-day run supplies no passing elapsed or daily-repeat credit and must not
be resumed or relabeled.
Production remains the owner-controlled RC5 trial; qualified stable and stable
aliases remain 1.3.2. See
[RC8 qualification](release-qualification.md#133-rc8-qualification).

## 1.3.3-rc.7 Candidate Status

Preparation began on 2026-10-14 from exact documentation [PR #442](https://github.com/Kha-kis/manga-arr/pull/442)
merge `45c05608bd8d0909057b83d589f2784722f47116`. It contains the reviewed
rescan corrections merged in [PR #445](https://github.com/Kha-kis/manga-arr/pull/445)
and [PR #447](https://github.com/Kha-kis/manga-arr/pull/447), alongside the earlier
automatic Suwayomi matching and source-linkage fixes. This preparation changes
only `app/VERSION` and four release documents; no application, schema, provider,
API, dependency or Compose behavior changes here.

Reviewed preparation `2a847a9731509f50576d49a49d53544f8bc40e35` passed the
complete local release gate: 4,519 Python tests /17 skips and 95 isolated browser
cases, confirmations/routes, lint/format, dependency/secret/configuration security
and exact-revision image checks. Exact-head CI and independent source review
passed. [PR #448](https://github.com/Kha-kis/manga-arr/pull/448) merge
`0465715975c2b633cfc9fb77073232b3de9829f2` has the identical reviewed tree;
fresh merge fast/security/image checks passed before immutable tagging.

Release Image [run 38546983055](https://github.com/Kha-kis/manga-arr/actions/runs/38546983055)
published the multiarch candidate and a [non-latest prerelease](https://github.com/Kha-kis/manga-arr/releases/tag/v1.3.3-rc.7)
was created. Fresh publication identity/attestation/security and unchanged-alias
checks passed independent actual audit. Ten installed-artifact rescan regressions
passed independent actual audit. Fresh core/cache/eleven file controls and twelve
installed-function linkage plus ten ASGI automatic-match controls passed;
independent core and linkage audits are GO. A fresh real-provider automatic-match
workflow downloaded eight chapters and imported a 254-page volume, with ComicInfo,
source/merged content hashes, history/provenance and protected-state rescan checks
passing. Time/environment/platform gates are still required.
The October 14-15 full-day observation subsequently failed: an unattended
Docker package upgrade interrupted the observer before the required elapsed
window and second daily refresh. Failed-run cleanup and protected-state
checks passed, not qualification. The failed receipts remain immutable.
Older receipts supply no RC7 runtime credit. See
[RC7 qualification](release-qualification.md#133-rc7-qualification).
Production remains RC5; qualified stable and stable aliases remain 1.3.2.
Preparation is not stable-promotion or production-cutover approval.

## 1.3.3-rc.6 Candidate Status

Prepared on 2026-10-12 from exact [PR #440](https://github.com/Kha-kis/manga-arr/pull/440)
merge `89e641b3c054ace0c0e8ad46a03cd6640d62c6b5`, tree-identical to reviewed
head `4d3c1ab6744ebd8c35a468c132a915ad48eec712`. `app/VERSION` identifies
1.3.3-rc.6. This preparation changes only VERSION and release documentation;
it carries forward reviewed fixes and the automatic Suwayomi matching correction.
Preparation `b5e0c3614ed6e4d561b61c8387bc4a3b87c2e659`
completed all required local subgates across the retained failed aggregate and
successful security/image resume. The reviewed [PR #441](https://github.com/Kha-kis/manga-arr/pull/441)
merge `a4dcd1d5c152a0025a1fa94d150653e1b584af5c` was subsequently tagged and
published as a non-latest prerelease. Independent publication and fresh scoped
core/cache/file/linkage, real automatic-match/download/import/rescan and short
natural background audits passed. The candidate remains unqualified: full-day
and remaining environment/platform gates require separate evidence. Earlier
candidate receipts supply no RC6 runtime credit.
See [RC6 qualification](release-qualification.md#133-rc6-qualification).
Production remains on RC5; 1.3.2 remains qualified stable and owns stable aliases.
Preparation alone did not authorize publication; the subsequent publication and
runtime runs had separate source-bound approvals. No stable promotion or RC6
production cutover is authorized by these scoped audits.

The confirmed missing-loose-chapter and recorded custom-volume rescan defects
([#444](https://github.com/Kha-kis/manga-arr/issues/444),
[#446](https://github.com/Kha-kis/manga-arr/issues/446)) block stable approval.
Source corrections merged in [#445](https://github.com/Kha-kis/manga-arr/pull/445)
and [#447](https://github.com/Kha-kis/manga-arr/pull/447) require a replacement
candidate with fresh qualification; they do not repair or replace the published
RC6 image.

## 1.3.3-rc.5 Candidate Status

This replacement candidate was published on 2026-10-12, but remains unqualified
for stable promotion or general deployment. A separately authorized owner-controlled
production trial is in progress. `app/VERSION` identifies 1.3.3-rc.5. It contains
PR #434's reviewed 11-line application correction. Legacy MangaDex linkage is
seeded only without persisted source links; explicit unlink clears a matching cached ID atomically
with source-row deletion. Unrelated identities and truly unlinked legacy fallback
are preserved. Historical ambiguous aliases require identity-backed manual review;
they are not automatically removed or reclassified.

Exact reviewed preparation `16df90d5e04da8a6f322725e606d772a14954266` passed
the full local release gate: 4,324 Python tests /17 existing skips and 95 isolated
browser cases, plus lint/format and security/image gates. PR #436 merge
`5a61f2060ff47a849212ca97abf9caa5fd8ac3a3` has the identical tree; fresh merge
checks passed 86 tests and 13/13 confirmations, security and exact-revision image
checks. Immutable annotated `v1.3.3-rc.5` was published by Release Image
[run 38204751356](https://github.com/Kha-kis/manga-arr/actions/runs/38204751356),
with index `sha256:6fd882a9d0cdb5a9f134e69cfa467cc9bf914af052941184a7ea0588cd9ac068`.
Independent publication identity/security and fresh actual core/cache/eleven
loose-file, twelve provider-free linkage and eight-chapter workflow audits are GO.
Both platform identities, attestations/SBOM and dated configured fixed High/Critical
scans passed; no native ARM execution credit follows.

The earlier rate-limited workflow remains failed and supplies no background seed.
The observed bounded short natural background cycle exited 0 and passed
independent actual audit. Its configured maximum window was 1020 seconds,
with 1006.429 seconds observed; this is
not literal 1020-second elapsed observation or full-day coverage. Representative
NAS, full natural 86400-second coverage and literal public Compose startup remain
pending. Exact identities, scoped results and platform boundaries are listed in
[RC5 qualification](release-qualification.md#133-rc5-qualification).
Stable aliases remain on 1.3.2. Production now runs the exact published RC5 image
in the owner trial after a new verified stopped backup and reviewed recovery plan.
The trial passed 48/48 desktop/mobile/API smoke checks and all six read-only
integration groups. Its initial scheduled metadata refresh preserved protected
content. A new guarded production DDL job completed 8/8 chapters and imported one
volume with all 254 pages verified; old queue rows and protected state were preserved.
Full daily-repeat coverage remains pending. No general deployment or stable
approval follows from publication or these scoped results. No tag/image may be
replaced; older candidate identities and failed receipts remain immutable.
The published RC5 automatic-match action has a confirmed unsupported SQLite-row
`.get()` failure. Its passing production workflow used an explicit source link,
not this action. PR #440's correction is carried by the RC6 preparation above;
it does not repair or replace RC5's immutable image.

## 1.3.3-rc.4 Candidate Status

This replacement candidate was published on 2026-10-12, but remains unqualified
for promotion or deployment. `app/VERSION` identifies 1.3.3-rc.4. Its source baseline
is PR #431 merge `39c57d63782ecb7161d78a9ae87b872f1e9bbc11`, tree-identical to
reviewed head `87aa9157bedcc10816afcb5e67f926f897d0872b`. Expected Suwayomi
monitor cancellation now logs DEBUG without a traceback; cancellation propagation,
real-error logging/recovery and monitor timing are unchanged. RC3's alias and
cached-provenance fixes are carried forward; optional file cleanup #422 is separate.

The exact reviewed preparation `2971dca1221f3c2bcac6b70e26d8e4c3db1d57fa`
passed 4,312 Python tests with 17 existing skips and 95 isolated browser cases.
PR #432 merge `850475085c04694e020ea5a808acb820d98d9503` has the identical
tree; fresh merge fast/security/image checks passed. Immutable annotated
`v1.3.3-rc.4` was published by Release Image
[run 38192482705](https://github.com/Kha-kis/manga-arr/actions/runs/38192482705),
with index `sha256:e90ba929287154f41ee6dc2c93f8afff1994e554e56e9c7f5c0d2dddda5b8666`.
Both platform identities, attestations/SBOM and dated fixed High/Critical scans passed.
Only the full candidate tag was published; the tag/image must not be replaced.

NEW published-artifact core setup/recovery/upgrade/pristine rollback, cache-failure
and 11 loose-file controls passed independent audit. A NEW eight-chapter workflow
exited 0 through download/import/rescan with all six metadata sources healthy
and passed independent final audit. The short background run FAILED before
collecting natural-observation samples. A separately confirmed alternative-source
restart/unlink provenance defect ([#433](https://github.com/Kha-kis/manga-arr/issues/433))
remains in the published image. [PR #434](https://github.com/Kha-kis/manga-arr/pull/434)
merged the source correction for a future artifact; it does not replace RC4.
Historical ambiguous aliases require identity-backed manual review, not automatic cleanup.
Representative NAS, full natural 86400-second refresh coverage and literal public
Compose startup remain pending. No native ARM runtime credit is claimed.
No RC3 or earlier runtime/local-image credit is inherited.
See exact identities, scoped results and remaining gates in
[RC4 qualification](release-qualification.md#133-rc4-qualification).

RC1/RC2/RC3 tags, images and historical evidence remain unchanged. 1.3.2 retains
`1.3`, `1` and `latest`; production stays on the separately deployed RC1 until
a separately authorized, qualified cutover with a matching stopped backup.

## 1.3.3-rc.3 Candidate Status

The candidate was published on 2026-10-11 from exact PR #430 merge
`ba367ab7d5f776ed11f2989e6adb49b1ffec7d81`, but remains unqualified and is not
cleared for promotion or deployment. Release Image
[run 38152373789](https://github.com/Kha-kis/manga-arr/actions/runs/38152373789)
published only `1.3.3-rc.3`, with index
`sha256:b485170bc1a91d79a2e85edc877eaad3bff0a12f64507d546cf21eaf74796481`.
The tag/image must not be rebuilt or replaced.

Scoped core setup/upgrade/rollback, network-disabled cache protection and 11
loose-file controls passed. A new eight-chapter workflow resolved all six metadata
sources without failures and independently verified all 254 page hashes,
ComicInfo and history bytes after download/import. Its overall result failed:
rescan returned HTTP403 because the private helper omitted the CSRF header, and
normal monitor shutdown logged ERROR with a cancellation traceback. These are
separate harness and application defects, not a successful rescan/workflow.

PR #431's logging correction is merged for a new candidate; it does not alter
RC3's artifact or failed receipts. Background remains unexecuted without a NEW
successful workflow archive; representative NAS and full recurring-refresh
coverage remain pending. See [RC3 qualification](release-qualification.md#133-rc3-qualification).

## 1.3.3-rc.2 Candidate Status

This replacement candidate image is published, but runtime qualification
reproduced [#425](https://github.com/Kha-kis/manga-arr/issues/425).
RC2 is not cleared for promotion or deployment and will not be rebuilt in place.
The immutable tagged source identifies 1.3.3-rc.2. PR #423 merged at
`0df8fc75e0fbe2b2ca79f6299cb66308c7789dee`, adding loose-chapter preservation
and rescan recovery, dotted-title compatibility and discovered-volume alias
exclusions. Optional library-file cleanup remains a separate proposal in #422.

The preparation passed its full local release gate and independent review.
PR #424 merged at `45e8a3fdf6035c72e1365c47c4c74cdb4cbfb748`, with a tree
identical to the tested preparation. Annotated `v1.3.3-rc.2` points at that
exact merge. Release Image run
[38101546771](https://github.com/Kha-kis/manga-arr/actions/runs/38101546771)
published only the full candidate tag. Both platform identities, attestations
and dated fixed High/Critical scans passed; these do not clear the runtime
failure. A separately reviewed correction and a new immutable candidate are
required. See
[RC2 qualification](release-qualification.md#133-rc2-qualification).
Do not reuse RC1's artifact receipts for RC2. Neither `v1.3.3-rc.1` nor its
published image is replaced; 1.3.2 keeps the stable aliases and production
remains on the separately deployed RC1 until an authorized qualified cutover.

## 1.3.3-rc.1 Candidate Status

This patch candidate contains the reviewed Suwayomi directory/source,
volume-file and queue-error fixes, metadata retry/coverage corrections, and
backup-age health correction, plus current-series ComicInfo enrichment for
newly merged Suwayomi volumes. The immutable RC1 tag identifies its published
source. It predates the loose-chapter state correction. 1.3.2 remains the
qualified stable release.

The reviewed release-preparation merge
`fd5dc1d99bce0f65236e4a47e735bd1baa70a723` is tagged `v1.3.3-rc.1`. Publication
and representative amd64 fresh setup, stopped 1.3.2 copy upgrade, matching rollback,
metadata refresh and a new eight-chapter download/import passed. The full local
release gate ran on the reviewed preparation head with an identical tree;
fast/security/image gates also ran on the exact merge revision.

Published index:
`sha256:282a196c7c92fe46bb280fbf01e51cd1630dfa5da168dba143f2bd11eb318fcd`.
Only the full candidate image tag was published; `1.3`, `1`, and `latest` remain
on 1.3.2. Both platform attestations and fixed High/Critical scans passed.
See exact source/platform identities, dated runtime results and limitations in
[candidate qualification](release-qualification.md#133-rc1-qualification).
Candidate evidence is not stable-promotion approval. This documentation update
does not change the immutable tagged source or published image.

## 1.3.2 Stable Status

The 1.3.2 image was published on 2026-10-08 from exact source
`091b2b12ea235eb0d62306bce94b7e5d70746f4c` and annotated tag `v1.3.2`.
It is the current qualified stable release: all local and published-artifact
gates and final independent audit/GO passed. Publication alone is not qualification.
1.3.1's historical qualification remains valid for its unchanged exact image,
not today's moving aliases.

Verified 1.3.2 index:
`sha256:4b8a312888ba116784b89a2479c5ca51dbf9e2fba881d658e368bfd082b4f590`.
`1.3.2`, `1.3`, `1`, and `latest` resolve to that index. Published image
verification, both platform fixed High/Critical scans and SPDX/SLSA checks passed.
Published amd64 fresh/upgrade/rollback, reset, large-archive and decoder checks
passed, as did all 30 published-image NFS cases. Temporary emulated qualification
from the exact published ARM image passed 13 decoder checks, without claiming
native ARM hardware or ARM NFS/HTTP coverage. Final independent audit verified
the full NFS receipts and remaining artifact/runtime evidence without required gaps.

The release includes the merged fixes from PRs #386, #387, #388, #389, #390,
#392, #393, #394, #396, #397, #398, #399, #400, #401, and #402.
See the [1.3.2 changelog](../CHANGELOG.md#132---2026-10-08) and
[qualification status](release-qualification.md#132-stable-qualification).
Local and published-artifact receipts remain separate evidence. Qualification
does not authorize a production deployment.

## Version Policy

- Stable releases use `MAJOR.MINOR.PATCH`, for example `1.2.0`.
- Release candidates use `MAJOR.MINOR.PATCH-rc.N`, for example
  `1.0.0-rc.2`.
- Every Git tag is `v` followed by the exact application version, for example
  `v1.0.0-rc.2`.
- Release and image tags are immutable. Never move or replace a published tag.
- An active repository ruleset blocks updates and deletion for every `v*` tag.
- `latest` points only to the newest stable release, never to a release
  candidate.

The public Compose file follows `latest` for the normal stable channel.
Operators evaluating a candidate or requiring reproducible deployments should
replace `latest` on the `image:` line with the full candidate or stable version.

## Container Tags

The release workflow publishes `ghcr.io/kha-kis/manga-arr` with these tags:

| Release | Image tags |
| --- | --- |
| `1.3.3-rc.7` (published; unqualified) | `1.3.3-rc.7` |
| `1.3.3-rc.6` (published; unqualified) | `1.3.3-rc.6` |
| `1.3.3-rc.5` (published; unqualified) | `1.3.3-rc.5` |
| `1.3.3-rc.4` (published; unqualified) | `1.3.3-rc.4` |
| `1.3.3-rc.3` (published; unqualified) | `1.3.3-rc.3` |
| `1.3.3-rc.2` (published; qualification failed) | `1.3.3-rc.2` |
| `1.3.3-rc.1` (candidate) | `1.3.3-rc.1` |
| `1.3.2` (qualified stable) | `1.3.2`, `1.3`, `1`, `latest` |
| `1.3.1` | `1.3.1`, `1.3`, `1`, `latest` |
| `1.3.0` (superseded security hold) | `1.3.0`, `1.3`, `1`, `latest` |
| `1.3.0-rc.2` | `1.3.0-rc.2` |
| `1.3.0-rc.1` | `1.3.0-rc.1` |
| `1.2.0` | `1.2.0`, `1.2`, `1`, `latest` |
| `1.2.0-rc.10` | `1.2.0-rc.10` |
| `1.2.0-rc.9` | `1.2.0-rc.9` |
| `1.2.0-rc.7` | `1.2.0-rc.7` |
| `1.2.0-rc.6` | `1.2.0-rc.6` |
| `1.2.0-rc.5` | `1.2.0-rc.5` |
| `1.2.0-rc.4` | `1.2.0-rc.4` |
| `1.2.0-rc.3` | `1.2.0-rc.3` |
| `1.2.0-rc.2` | `1.2.0-rc.2` |
| `1.2.0-rc.1` | `1.2.0-rc.1` |
| `1.1.0` | `1.1.0`, `1.1`, `1`, `latest` |
| `1.0.1` | `1.0.1`, `1.0`, `1`, `latest` |
| `1.0.0` | `1.0.0`, `1.0`, `1`, `latest` |
| `1.0.0-rc.2` | `1.0.0-rc.2` |
| `1.0.0-rc.1` | `1.0.0-rc.1` |
| `1.2.3` | `1.2.3`, `1.2`, `1`, `latest` |

The exact image digest recorded by GitHub Container Registry is the strongest
deployment pin. Version tags are intended to remain immutable, but a digest
also protects against registry-side tag changes.

The 1.3.1 image was published and qualified on 2026-10-07. At that time,
`1.3.1`, `1.3`, `1`, and `latest` resolved to
`sha256:556c657abe60518695d799f00a2c38abd43d762d57b257cc6ef649d8f3c45b7e`.
It replaced the blocked 1.3.0 image without modifying that image or tag. Exact
1.3.0, 1.2.0, and release-candidate tags remain unchanged. Historical rows
describe tags emitted at each release, not ownership of moving aliases today.

## Release Checklist

1. Start from a clean `master` synchronized with GitHub.
2. Set `app/VERSION` and add complete release notes to `CHANGELOG.md`.
3. Run `make test-release-safe` locally.
4. Merge the release PR without changing its reviewed head revision.
5. Create a signed or annotated `v<version>` tag on the merge commit.
6. Let the release workflow build, scan, and publish the image from that tag.
7. Verify the published image digest and runtime version.
8. Smoke-test both a fresh install and an upgrade from the previous release.
9. Publish the GitHub release with upgrade notes, known limitations, and the
   image digest.
10. For a stable release, verify that `latest` resolves to the same digest.

The stable-release evidence is tracked in `docs/release-qualification.md`.

Starting with 1.3.1, the release version is a cache input before the Dockerfile's
OS-package update step. A new version refreshes Debian packages even when the
pinned base is unchanged. Code-only builds of the same version can reuse that
layer; commit IDs and timestamps remain late build arguments. A retry of an
unpublished version after new security updates may still need `docker build
--no-cache`. Never rebuild an already published exact-version tag in place.

Release candidates are promoted by creating a new stable release from a tested
commit. An RC tag itself is never renamed or converted into a stable tag.

## Required Release Evidence

- Python, static, route, and isolated browser suites pass.
- Dependency and container vulnerability scans have no unresolved release
  blockers.
- Fresh-install setup, login, logout, and admin recovery pass.
- Database migration and rollback instructions are tested against a copy of
  real-world data.
- Search, grab, download handoff, import, metadata refresh, and backup
  validation receive representative smoke coverage.
- The public Compose file starts without private host overrides.

GitHub-hosted automation is useful evidence, but the local release-safe gate
remains required because it exercises the full isolated browser stack.

## Local-First Automation

Every pull request runs a required fast gate covering Ruff correctness checks,
package and CLI metadata, backup/recovery contracts, deployment consistency,
architecture invariants, and the static confirmation flow. The expensive full
Python and isolated-browser suites remain local-first:

```bash
make release-local
```

That target validates version metadata, runs `make test-release-safe`, audits
pinned Python dependencies, scans Git history for secrets, gates Docker and
Compose configuration with Trivy, builds the production image, verifies its
version, labels, non-root user, and file inventory, then gates the image on
fixed High/Critical vulnerabilities.

The full Test workflow remains available by manual dispatch, and the Security
workflow runs weekly or by manual dispatch. `.github/workflows/release.yml` is
the only tag-triggered workflow. It runs only for an explicit `v*` tag, requires
that tag to exactly match `app/VERSION`, and publishes amd64/arm64 images with
SBOM and provenance attestations. It never publishes `latest` for a release
candidate.

If hosted Actions cannot run, an authenticated maintainer can use the local
fallback after the full gate passes:

```bash
make release-push CONFIRM_RELEASE=1.3.0
```

The confirmation must exactly match `app/VERSION`. The Docker client must
already be logged in to `ghcr.io` with package-write permission. Local
publishing also requires a clean worktree with `v<version>` pointing at `HEAD`;
both publishing paths refuse to replace an existing exact-version image tag.
Their shared preflight uses the existing authenticated Docker Buildx client with
a 60-second timeout. Only exit status 1, empty stdout, and exactly
`ERROR: <full-requested-image-reference>: not found` followed by one newline
permit publication. Existing tags, authentication/network failures, and unknown
or ambiguous Buildx output abort publication. An unfamiliar Buildx diagnostic
format intentionally requires review, not an override or a retry that skips the
guard.

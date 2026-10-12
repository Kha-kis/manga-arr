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

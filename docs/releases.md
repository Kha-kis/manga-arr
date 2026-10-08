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

## 1.3.2 Preparation Status

`app/VERSION` is `1.3.2` on the release-preparation branch. This identifies the
candidate source, not a published or qualified release. 1.3.1 remains the current
published, qualified stable release and retains the moving aliases below.
There is no qualified 1.3.2 tag, image digest, or alias change recorded here.

The candidate includes the merged fixes from PRs #386, #387, #388, #389, #390,
#392, #393, #394, #396, #397, #398, #399, #400, #401, and #402.
See the [1.3.2 changelog](../CHANGELOG.md#132---unreleased) and
[qualification status](release-qualification.md#132-release-preparation).
Final committed release source, immutable image/platform identity, security
scans, the existing six-set NFS ledger, fresh install, copied-config upgrade,
and matching stopped rollback remain release gates. Do not infer these passes
from source suites or prior component/image evidence.

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

The 1.3.1 image was published and qualified on 2026-10-07. `1.3.1`, `1.3`,
`1`, and `latest` resolve to
`sha256:556c657abe60518695d799f00a2c38abd43d762d57b257cc6ef649d8f3c45b7e`.
It replaces the blocked 1.3.0 image without modifying that image or tag. Exact
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

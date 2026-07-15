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

## Version Policy

- Stable releases use `MAJOR.MINOR.PATCH`, for example `1.2.0`.
- Release candidates use `MAJOR.MINOR.PATCH-rc.N`, for example
  `1.0.0-rc.2`.
- Every Git tag is `v` followed by the exact application version, for example
  `v1.0.0-rc.2`.
- Release and image tags are immutable. Never move or replace a published tag.
- `latest` points only to the newest stable release, never to a release
  candidate.

The public Compose file accepts any published image tag through
`MANGARR_VERSION`. Operators evaluating a candidate should pin the full
candidate version instead of using a moving channel.

## Container Tags

The release workflow publishes `ghcr.io/kha-kis/manga-arr` with these tags:

| Release | Image tags |
| --- | --- |
| `1.0.1` | `1.0.1`, `1.0`, `1`, `latest` |
| `1.0.0` | `1.0.0`, `1.0`, `1`, `latest` |
| `1.0.0-rc.2` | `1.0.0-rc.2` |
| `1.0.0-rc.1` | `1.0.0-rc.1` |
| `1.2.3` | `1.2.3`, `1.2`, `1`, `latest` |

The exact image digest recorded by GitHub Container Registry is the strongest
deployment pin. Version tags are intended to remain immutable, but a digest
also protects against registry-side tag changes.

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

Normal pull requests do not automatically start GitHub Actions. The repository's
Test and Security workflows remain manual-only and disabled while hosted-runner
billing is unavailable. Run the same release evidence locally:

```bash
make release-local
```

That target validates version metadata, runs `make test-release-safe`, audits
pinned Python dependencies, scans Git history for secrets, gates Docker and
Compose configuration with Trivy, builds the production image, verifies its
version, labels, non-root user, and file inventory, then gates the image on
fixed High/Critical vulnerabilities.

`.github/workflows/release.yml` is the only tag-triggered workflow. It runs only
for an explicit `v*` tag, requires that tag to exactly match `app/VERSION`, and
publishes amd64/arm64 images with SBOM and provenance attestations. It never
publishes `latest` for a release candidate.

If hosted Actions cannot run, an authenticated maintainer can use the local
fallback after the full gate passes:

```bash
make release-push CONFIRM_RELEASE=1.0.1
```

The confirmation must exactly match `app/VERSION`. The Docker client must
already be logged in to `ghcr.io` with package-write permission. Local
publishing also requires a clean worktree with `v<version>` pointing at `HEAD`;
both publishing paths refuse to replace an existing exact-version image tag.

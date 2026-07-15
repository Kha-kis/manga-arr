# Mangarr — test orchestration.
#
# Two top-level targets:
#   make test          — safe PR gate. Hermetic, fast. Run before every PR.
#   make test-release  — pre-release gate. Adds browser + DB-mutation suites.
#
# Browser and DB-mutation targets require a running container at
# http://127.0.0.1:6789 and currently exercise the operator's live DB.
# Do NOT wire them into CI until a hermetic test DB is in place.

.PHONY: help test test-release test-release-safe \
        test-python test-confirm-flow test-route-sweep \
        test-browser-smoke test-browser-integration test-browser-e2e \
        test-browser-isolated test-browser-isolated-smoke \
        test-browser-isolated-integration test-browser-isolated-e2e \
        test-verify-e2e release-validate security-deps security-secrets \
        security-config security-local image-build image-scan \
        image-verify release-local release-push

PYTHON ?= python3
PYTEST ?= $(PYTHON) -m pytest
VERSION := $(shell tr -d '\n' < app/VERSION)
RELEASE_IMAGE ?= ghcr.io/kha-kis/manga-arr
LOCAL_RELEASE_IMAGE ?= mangarr-release
RELEASE_PLATFORMS ?= linux/amd64,linux/arm64
GIT_SHA := $(shell git rev-parse HEAD)
BUILD_DATE := $(shell date -u +%Y-%m-%dT%H:%M:%SZ)
RELEASE_TAG_ARGS = $(shell $(PYTHON) scripts/release_metadata.py --image "$(RELEASE_IMAGE)" --format docker-args)

help:
	@echo "Hermetic targets (no running app required):"
	@echo "  test-python            Full Python suite (pytest tests/python/)"
	@echo "  test-confirm-flow      Static JS/CSS confirm-flow analysis"
	@echo "  test-route-sweep       Auto-derived FastAPI route render check"
	@echo ""
	@echo "Live-app targets (require container at 127.0.0.1:6789):"
	@echo "  test-browser-smoke         Playwright presence/structure (read-only)"
	@echo "  test-browser-integration   Playwright HTMX/confirm + page sweep (read-only)"
	@echo "  test-browser-e2e           Playwright real DB mutations + revert (LIVE DB)"
	@echo "  test-verify-e2e            DB integrity verifier (read-only against live DB)"
	@echo ""
	@echo "Aggregates:"
	@echo "  test          Safe PR gate: python + confirm-flow + route-sweep"
	@echo "  test-release  Pre-release: test + browser-smoke/integration/e2e + verify-e2e"
	@echo ""
	@echo "Release targets (local-first; no GitHub runner required):"
	@echo "  release-validate  Check SemVer, docs, and v<version> tag identity"
	@echo "  security-local    pip-audit + gitleaks + Trivy config gate"
	@echo "  image-scan        Build, verify, and Trivy-scan the release image"
	@echo "  release-local     Full release-safe, security, and image gate"
	@echo "  release-push      Publish multi-arch tags after explicit confirmation"

# ── Hermetic targets ──────────────────────────────────────────────────────────

test-python:
	$(PYTEST) tests/python/ -q

test-confirm-flow:
	cd app && $(PYTHON) test_confirm_flow.py

test-route-sweep:
	$(PYTEST) tests/python/test_route_sweep.py -v

# ── Live-app targets ──────────────────────────────────────────────────────────

test-browser-smoke:
	cd tests && node browser_smoke.js

test-browser-integration:
	cd tests && node browser_integration.js

# Mutates the operator's live DB; reverts on success. Do NOT run from CI.
test-browser-e2e:
	cd tests && node browser_e2e.js

# Read-only against live DB. Safe to run any time the container is up.
test-verify-e2e:
	docker exec -i mangarr $(PYTHON) - /config/manga_arr.db < app/verify_e2e.py

# ── Isolated browser targets ──────────────────────────────────────────────────
# These boot a separate mangarr-test container against a tmp /config mount
# (port 16789, see docker-compose.test.yml) and tear it down on exit. They
# never touch the live operator DB and ARE safe to run from CI.

test-browser-isolated:
	./tests/run_isolated_browser.sh all

test-browser-isolated-smoke:
	./tests/run_isolated_browser.sh smoke

test-browser-isolated-integration:
	./tests/run_isolated_browser.sh integration

test-browser-isolated-e2e:
	./tests/run_isolated_browser.sh e2e

# ── Aggregates ────────────────────────────────────────────────────────────────

test: test-python test-confirm-flow test-route-sweep

# Safe pre-release: hermetic gate + isolated browser suite.
test-release-safe: test test-browser-isolated

# Pre-release including the live-DB browser suite. Only run manually with
# the operator's container up.
test-release: test test-browser-smoke test-browser-integration test-browser-e2e test-verify-e2e

# ── Public release gates ──────────────────────────────────────────────────────

release-validate:
	$(PYTHON) scripts/release_metadata.py --tag "v$(VERSION)" --image "$(RELEASE_IMAGE)"

security-deps:
	pip-audit -r requirements.txt --strict

security-secrets:
	gitleaks git --no-banner

security-config:
	trivy config --severity HIGH,CRITICAL --exit-code 1 .

security-local: security-deps security-secrets security-config

image-build: release-validate
	docker build \
	  --build-arg MANGARR_VERSION="$(VERSION)" \
	  --build-arg VCS_REF="$(GIT_SHA)" \
	  --build-arg BUILD_DATE="$(BUILD_DATE)" \
	  --tag "$(LOCAL_RELEASE_IMAGE):$(VERSION)" .

image-verify: image-build
	$(PYTHON) scripts/verify_release_image.py \
	  --image "$(LOCAL_RELEASE_IMAGE):$(VERSION)" \
	  --version "$(VERSION)" \
	  --revision "$(GIT_SHA)"

image-scan: image-verify
	trivy image --scanners vuln --ignore-unfixed \
	  --severity HIGH,CRITICAL --exit-code 1 \
	  "$(LOCAL_RELEASE_IMAGE):$(VERSION)"

release-local: release-validate test-release-safe security-local image-scan

# Emergency/local publishing fallback for Actions billing or availability
# failures. The operator must already be authenticated to ghcr.io with
# package-write permission and must type the exact version as confirmation.
release-push: release-local
	@test "$(CONFIRM_RELEASE)" = "$(VERSION)" || \
	  (echo "Set CONFIRM_RELEASE=$(VERSION) to publish" >&2; exit 1)
	@test -z "$$(git status --porcelain)" || \
	  (echo "Refusing to publish from a dirty worktree" >&2; exit 1)
	@git tag --points-at HEAD | grep -Fxq "v$(VERSION)" || \
	  (echo "HEAD must have tag v$(VERSION) before publishing" >&2; exit 1)
	@if docker buildx imagetools inspect "$(RELEASE_IMAGE):$(VERSION)" >/dev/null 2>&1; then \
	  echo "Refusing to replace published image tag: $(RELEASE_IMAGE):$(VERSION)" >&2; \
	  exit 1; \
	fi
	docker buildx build \
	  --platform "$(RELEASE_PLATFORMS)" \
	  --build-arg MANGARR_VERSION="$(VERSION)" \
	  --build-arg VCS_REF="$(GIT_SHA)" \
	  --build-arg BUILD_DATE="$(BUILD_DATE)" \
	  $(RELEASE_TAG_ARGS) --provenance=mode=max --sbom=true --push .

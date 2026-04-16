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
        test-verify-e2e

PYTHON ?= python3
PYTEST ?= $(PYTHON) -m pytest

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

# ── Hermetic targets ──────────────────────────────────────────────────────────

test-python:
	$(PYTEST) tests/python/ -q

test-confirm-flow:
	@if docker ps --format '{{.Names}}' | grep -q '^mangarr$$'; then \
	  docker exec mangarr $(PYTHON) /app/test_confirm_flow.py; \
	else \
	  cd app && $(PYTHON) test_confirm_flow.py; \
	fi

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
	docker exec mangarr $(PYTHON) /app/verify_e2e.py

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

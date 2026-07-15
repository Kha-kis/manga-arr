#!/usr/bin/env bash
# Isolated browser test runner.
#
# Brings up a separate mangarr-test container with a temp /config mount,
# seeds minimum fixtures, runs the requested browser suite(s), then tears
# down. Never touches the live operator container or DB.
#
# Hardened against the class of accident that previously stopped the live
# container: see PRE-FLIGHT SAFETY CHECKS below.
#
# Usage:
#   tests/run_isolated_browser.sh [smoke|integration|e2e|all]
# default: all
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

SUITE="${1:-all}"
# Pinned constants — the safety tests in tests/python/test_runner_safety.py
# assert these exact values. Changes here must be intentional.
BASE_URL="http://127.0.0.1:16789"
PORT="16789"
PRODUCTION_PORT="6789"
PRODUCTION_CONTAINER="mangarr"
PRODUCTION_CONFIG_DIR="$HOME/.config/mangarr"
# Absolute paths so the cleanup trap works regardless of cwd at exit time.
COMPOSE_FILE="$REPO_ROOT/docker-compose.test.yml"
CONTAINER="mangarr-test"
TEST_CONFIG_DIR="$REPO_ROOT/.test-config"
# Distinct compose project so the live `mangarr` container is not classified
# as an orphan of this project. CRITICAL: without this, `compose down`
# would also stop the live container (or, with --remove-orphans, remove it).
PROJECT="mangarr-test"
COMPOSE="docker compose -p $PROJECT -f $COMPOSE_FILE"

# ── PRE-FLIGHT SAFETY CHECKS ─────────────────────────────────────────────────
# These guard against configuration drift. If any of them ever start failing,
# stop and investigate before bypassing — they exist because of a real
# incident in pass 2 where --remove-orphans removed the live container.

# 1. Compose file must exist where we expect it.
if [ ! -f "$COMPOSE_FILE" ]; then
  echo "[isolated-browser] FATAL: compose file not found: $COMPOSE_FILE"
  exit 1
fi

# 2. Container name must be the test name, not the production name.
if [ "$CONTAINER" = "$PRODUCTION_CONTAINER" ]; then
  echo "[isolated-browser] FATAL: refusing to run — CONTAINER='$CONTAINER' would target production"
  exit 1
fi

# 3. Project name must be the test project, not the production directory name.
if [ "$PROJECT" = "manga-arr" ] || [ "$PROJECT" = "mangarr" ]; then
  echo "[isolated-browser] FATAL: refusing to run — PROJECT='$PROJECT' would collide with production"
  exit 1
fi

# 4. Test config dir must NOT resolve to the production config dir.
TEST_CONFIG_REAL="$(realpath -m "$TEST_CONFIG_DIR")"
PROD_CONFIG_REAL="$(realpath -m "$PRODUCTION_CONFIG_DIR" 2>/dev/null || echo "/__nonexistent__")"
if [ "$TEST_CONFIG_REAL" = "$PROD_CONFIG_REAL" ]; then
  echo "[isolated-browser] FATAL: TEST_CONFIG_DIR resolves to production config dir"
  echo "                  test:       $TEST_CONFIG_REAL"
  echo "                  production: $PROD_CONFIG_REAL"
  exit 1
fi

# 5. Test port must NOT be the production port.
if [ "$PORT" = "$PRODUCTION_PORT" ]; then
  echo "[isolated-browser] FATAL: refusing to run — PORT='$PORT' is the production port"
  exit 1
fi

# 6. Compose command must not contain dangerous flags. We can't intercept
#    every future shell expansion, but we can assert the literal string
#    we'll execute is free of the flag that caused the previous incident.
if echo "$COMPOSE" | grep -q -- "--remove-orphans"; then
  echo "[isolated-browser] FATAL: --remove-orphans must not appear in COMPOSE"
  exit 1
fi

cleanup() {
  echo "[isolated-browser] tearing down test container"
  # Explicitly NO --remove-orphans: never touch containers outside this project.
  # Don't swallow errors — a failed teardown leaks resources, surface it.
  $COMPOSE down || echo "[isolated-browser] WARN: 'compose down' returned non-zero"
  # Best-effort: leave the test config dir on disk for post-mortem inspection.
}
trap cleanup EXIT

# Hard fail if the test container name is already in use (it shouldn't,
# since live uses 'mangarr' and test uses 'mangarr-test', but be explicit).
if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "[isolated-browser] '$CONTAINER' already running — refusing to clobber"
  echo "[isolated-browser] run '$COMPOSE down' first"
  exit 1
fi

# Fresh /config dir every run so DB starts empty and seeds are deterministic.
# Prior runs may have left root-owned leftovers from when the container ran
# as root; sudo-fallback to clear those before the user-mode rm -rf so the
# reset is reliable across upgrade boundaries.
if ! rm -rf "$TEST_CONFIG_DIR" 2>/dev/null; then
  sudo rm -rf "$TEST_CONFIG_DIR"
fi
mkdir -p "$TEST_CONFIG_DIR/covers"

# Run the test container as the caller's UID/GID so .test-config (owned by
# us) is writable. Matches docker-compose.test.yml's `user:` directive.
export TEST_UID="$(id -u)"
export TEST_GID="$(id -g)"

echo "[isolated-browser] starting test container on $BASE_URL"
$COMPOSE up -d --build

echo "[isolated-browser] waiting for healthy"
for i in {1..30}; do
  status=$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo "starting")
  if [ "$status" = "healthy" ]; then
    echo "[isolated-browser] healthy after ${i}s"
    break
  fi
  if [ "$i" = "30" ]; then
    echo "[isolated-browser] container did not become healthy in 30s"
    docker logs "$CONTAINER" --tail 50
    exit 1
  fi
  sleep 1
done

echo "[isolated-browser] checking first-run browser authentication"
MANGARR_TEST_BASE="$BASE_URL" node tests/browser_auth_setup.js

# After bringing up, sanity-check the live container is still healthy.
# A failure here means the test compose accidentally stopped/clobbered it.
if docker ps --format '{{.Names}}' | grep -qx "$PRODUCTION_CONTAINER"; then
  prod_status=$(docker inspect -f '{{.State.Health.Status}}' "$PRODUCTION_CONTAINER" 2>/dev/null || echo "unknown")
  if [ "$prod_status" = "unhealthy" ]; then
    echo "[isolated-browser] WARN: production container '$PRODUCTION_CONTAINER' became unhealthy after test bring-up"
  fi
fi

echo "[isolated-browser] seeding fixtures"
./tests/seed_test_db.sh

export MANGARR_TEST_BASE="$BASE_URL"
export MANGARR_TEST_CONTAINER="$CONTAINER"

cd tests
case "$SUITE" in
  smoke)        node browser_smoke.js ;;
  integration)  node browser_integration.js ;;
  e2e)          node browser_e2e.js ;;
  all)
    node browser_smoke.js
    node browser_integration.js
    node browser_e2e.js
    ;;
  *)
    echo "unknown suite: $SUITE (use smoke|integration|e2e|all)"
    exit 2
    ;;
esac

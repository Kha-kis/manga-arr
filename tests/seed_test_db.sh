#!/usr/bin/env bash
# Seed minimum fixtures into the isolated mangarr-test container.
# See tests/seed_test_db.py for what's seeded and why.
set -euo pipefail

CONTAINER="${MANGARR_TEST_CONTAINER:-mangarr-test}"

# The seed script lives in tests/ on the host and isn't copied into the
# image (tests/ is not in the Docker build context). Stream it via stdin.
docker exec -i "$CONTAINER" python3 - <"$(dirname "$0")/seed_test_db.py"

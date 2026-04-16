# Mangarr tests

Three layers of verification — Python unit/regression, hermetic isolated
browser suite, and operator diagnostics — plus an opt-in live-integration
suite that never runs in CI.

## TL;DR

```bash
make test                    # PR gate. Hermetic, ~30s. Run before every push.
make test-browser-isolated   # Pre-release. Spins up isolated container, ~3min.
make test-release-safe       # Both of the above.
```

If you're an operator and want to inspect DB residue without changing anything:

```bash
docker exec mangarr python3 /app/verify_e2e.py    # legacy text report
docker exec mangarr python3 /app/reconcile.py plan # dry-run repair planner
```

Both are read-only.

## Make targets

```
make help                                 # list and describe everything

# Hermetic — no app required
make test-python                          # full Python suite
make test-confirm-flow                    # static JS/CSS confirm-flow analysis
make test-route-sweep                     # auto-derived FastAPI page sweep
make test                                 # all three above (PR gate)

# Isolated browser — spins up mangarr-test container with mock-qbit sidecar
make test-browser-isolated-smoke          # 27 Playwright assertions
make test-browser-isolated-integration    # 19 Playwright assertions
make test-browser-isolated-e2e            # 24 Playwright assertions (real DB mutations)
make test-browser-isolated                # all three above
make test-release-safe                    # make test + make test-browser-isolated

# Live container — read-only, safe to run any time
make test-verify-e2e                      # DB integrity verifier on live DB

# Live container — DESTRUCTIVE: hits operator's running app + mutates live DB
make test-browser-smoke                   # browser_smoke.js against live mangarr
make test-browser-integration             # browser_integration.js against live mangarr
make test-browser-e2e                     # browser_e2e.js against live mangarr (mutates!)
make test-release                         # all live-container variants. Manual only.
```

## What runs in CI

`.github/workflows/test.yml` defines two jobs:

1. **`hermetic`** — `make test`. Runs the Python suite, static confirm-flow,
   and the auto-derived route sweep. ~30s.
2. **`isolated-browser`** — `make test-browser-isolated`. Boots the
   `docker-compose.test.yml` stack (mangarr-test + mock-qbit sidecar) on
   port 16789 against `./.test-config`, then runs all three browser
   suites. ~3min. Tears down on exit. Container logs uploaded on failure.

## What intentionally does NOT run in CI

| Suite | Why excluded |
|---|---|
| `make test-browser-smoke / -integration / -e2e` (live variants) | Hit the operator's running container at `127.0.0.1:6789` and (e2e) mutate the live DB. The isolated equivalents cover the same surface in CI. |
| `make test-verify-e2e` | Reads the live operator DB. Safe to run manually but no value in CI. |
| `tests/python/test_live_integrations.py` | Skipped unless per-provider env flags are set (see below). |

## Live integration tests

`tests/python/test_live_integrations.py` contains opt-in probes for real
upstream services. Each test skips with a clear reason unless its env flag
is set. Never runs in CI.

```bash
PROWLARR_LIVE=1   PROWLARR_URL=http://prowlarr:9696   PROWLARR_API_KEY=xxx \
QBITTORRENT_LIVE=1 QBIT_HOST=http://qbit:8080 QBIT_USER=u QBIT_PASS=p \
SABNZBD_LIVE=1     SAB_HOST=http://sab:8080  SAB_API_KEY=xxx \
MANGADEX_LIVE=1 \
SUWAYOMI_LIVE=1   SUWAYOMI_URL=http://swy:4567 \
  python3 -m pytest tests/python/test_live_integrations.py -v
```

Each probe makes one read-only request (system/status, app/version, ping,
about, queue listing). None of them grab, add, or modify upstream state.

## Diagnostics & reconciliation

Two read-only operator tools live under `app/`:

### `verify_e2e.py` — DB state-machine verifier

```bash
docker exec mangarr python3 /app/verify_e2e.py
```

Walks the live DB and prints a structured report. Exit code is non-zero
when a *critical* finding is present (orphan FK violation, blank api_key);
*warning* findings (stuck-grabbed >2d, ghost-downloaded chapters, stale
import_queue) report but do not fail.

The script is a thin CLI over `verify_e2e.diagnose(db_path)`, which
returns `Finding(code, severity, message, count, detail)` records that
tests assert against. Connection is opened in `mode=ro` URI mode.

**Severity legend**:
- `info` — counts and totals only, no action needed
- `warning` — operator-actionable, doesn't fail the verifier
- `critical` — fails the verifier; investigate before continuing

### `reconcile.py` — dry-run repair planner

```bash
docker exec mangarr python3 /app/reconcile.py report   # same as verify_e2e
docker exec mangarr python3 /app/reconcile.py plan     # proposed actions
```

`reconcile.py plan` walks every finding `verify_e2e` would surface and
proposes a repair action with explicit risk level. **Every action is dry
run — no rows are modified.** Each action carries:

- `action`: short verb (`reset_to_wanted`, `revert_to_failed_for_retry`,
  `revert_ghost_chapter_to_wanted`, `manual_review_orphan_*`)
- `target`: `(table, id)` of the affected row
- `risk`: `low` / `medium` / `high`
- `would_mutate`: `{column: new_value, ...}` showing exactly what an apply
  step *would* set. Empty means "review only — no mutation proposed".
- `requires_manual_review`: `True` when the action is not safe to apply
  even with an `--apply` flag (operator must inspect first). Ghost
  chapters, partial imports, and orphan rows always carry this flag.

There is no `--apply` mode in this version. Operators read the plan,
decide per row, and run targeted SQL or use the web UI to act on what
they confirm.

## Test files

```
tests/
├── browser_e2e.js                        24 Playwright assertions, real DB mutations
├── browser_integration.js                19 Playwright HTMX/confirm flow assertions
├── browser_smoke.js                      27 Playwright presence/structure assertions
├── mock_qbit.py                          stdlib HTTP server stand-in for qBittorrent
├── seed_test_db.{py,sh}                  fixtures the isolated browser suite needs
├── run_isolated_browser.sh               isolated test runner (safety-checked)
└── python/
    ├── conftest.py                       redirects /config and sqlite paths to tmp
    ├── test_api_key_middleware.py        12 tests
    ├── test_background_tasks.py          7 tests
    ├── test_crud_roundtrip.py            4 tests
    ├── test_csrf_cookie.py               14 tests
    ├── test_docs_consistency.py          6 tests
    ├── test_fstring_input_shape.py       16 tests
    ├── test_handoff_sab_nzbget_blackhole.py    10 tests (mocked)
    ├── test_import_atomicity.py          17 tests
    ├── test_import_concurrency.py        9 tests
    ├── test_indexer_dlclient_secret_migration.py    23 tests
    ├── test_init_db.py                   3 tests
    ├── test_live_integrations.py         5 opt-in probes
    ├── test_log_event.py                 6 tests
    ├── test_mangadex_adapter.py          14 tests (mocked httpx)
    ├── test_notification_secret_migration.py    33 tests
    ├── test_order_by.py                  14 tests
    ├── test_pipeline_mocked.py           5 tests (search → grab → qBit handoff)
    ├── test_reconcile.py                 18 tests (dry-run repair planner)
    ├── test_regex_safety.py              21 tests
    ├── test_route_sweep.py               10 tests (auto-derived page sweep)
    ├── test_runner_safety.py             12 static guards on the isolated runner
    ├── test_scheduler_rss.py             6 tests (rss_loop tick)
    ├── test_secret_cipher.py             17 tests
    ├── test_security.py                  10 tests
    ├── test_settings_secret_migration.py 17 tests
    ├── test_silent_except_logging.py     7 tests
    ├── test_ssrf.py                      30 tests
    ├── test_state_diagnostics.py         12 tests (verify_e2e library)
    ├── test_status_loop.py               6 tests (status_loop tick + auto-reset)
    ├── test_suwayomi_adapter.py          22 tests (mocked GraphQL)
    └── test_suwayomi_filesystem.py       22 tests (tmpdir + cbz fixtures)
```

## Adding a new test

| If you're testing… | Put it in… |
|---|---|
| A pure helper or DB query | `tests/python/test_<topic>.py` |
| HTTP behaviour at the route layer | use `TestClient`, see `test_crud_roundtrip.py` |
| External integration (Prowlarr/qBit/etc.) | mock httpx, see `test_pipeline_mocked.py` |
| New top-level operator page | the auto-derived sweep in `test_route_sweep.py` covers it for free |
| New background loop | follow the pattern in `test_scheduler_rss.py` and `test_status_loop.py` |
| Live integration | `test_live_integrations.py`, gated by an env flag, **never** unconditional |

If you're tempted to add a test that needs the live container or mutates
the live DB, instead add the seed/sidecar to `docker-compose.test.yml`
and write the test against `mangarr-test` on port 16789.

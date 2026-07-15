# Mangarr tests

Three layers of verification — Python unit/regression, hermetic isolated
browser suite, and operator diagnostics — plus an opt-in live-integration
suite that never runs in CI.

## TL;DR

```bash
make test                    # PR gate. Hermetic, ~2.5min. Run before every push.
make test-fast               # Short required GitHub PR check.
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
make lint                                 # Ruff correctness + focused format checks
make test-fast                            # short PR contract/invariant gate
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

`.github/workflows/pr-fast.yml` runs `make test-fast` automatically for every
pull request and is the required branch-protection check. It targets the
minimum supported Python 3.11 and validates that the `mangarr` console command
can be installed from project metadata.

`.github/workflows/test.yml` remains manually dispatchable and defines two
full-suite jobs:

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

## Test files — by category

77 Python test files (920 individual cases). The alphabetical view below previously listed ~30; the categorized map is the better navigation tool now that the suite is larger.

### End-to-end & route integration

Real HTTP → router → DB path against a test-isolated database. `TestClient` + CSRF cookie + header pair, assert via direct `sqlite3` queries.

| File | Covers |
|---|---|
| `test_e2e_grab_to_library.py` | Search → grab → seen dedup (URL + GUID) → import → library |
| `test_route_destructive_ops.py` | Series delete + blocklist mutations (cascade correctness) |
| `test_route_state_changes.py` | Volume actions, chapter map editor, history mutations, queue actions, tags, import-list CRUD |
| `test_route_profile_crud.py` | Quality / delay / release / language / custom-format / remote-path-mappings CRUD |
| `test_route_backup_and_import_queue.py` | Backup zip integrity + import-queue actions (skip / dismiss / retry / clear-old) |
| `test_route_sweep.py` | Auto-renders every parameter-free GET page |
| `test_metadata_health_panel.py` | Health-panel route round-trip |
| `test_reconcile.py` / `test_reconcile_ui.py` / `test_reconcile_refresh_then_preview.py` | Metadata-reconcile flow |
| `test_crud_roundtrip.py` | Indexer + download-client + connection CRUD |
| `test_series_patch_endpoint.py` / `test_series_patch_lock_handling.py` | `PATCH /api/series/{id}` + concurrency |
| `test_editor_stub_reconciliation.py` | Series editor recreates missing volume stubs |
| `test_root_folder_required_at_creation.py` | Series-add must resolve a root_folder_id |

### Pipeline, jobs, schedulers

Background loops and async tasks.

| File | Covers |
|---|---|
| `test_grab_timeout_wrap.py` | `grab_item` releases the in-flight URL on timeout |
| `test_check_download_status_single_flight.py` | Status-loop single-flight lock |
| `test_status_loop.py` | Status loop top-level behavior |
| `test_scheduler_rss.py` | RSS poll loop + idempotency |
| `test_circuit_breaker_persistence.py` | Download-client CB survives restart |
| `test_indexer_backoff.py` | Indexer error backoff |
| `test_mangadex_backfill_backoff.py` | MangaDex 429 / network backoff |
| `test_pipeline_mocked.py` | Older mocked pipeline (prefer `test_e2e_*` for new work) |
| `test_background_tasks.py` | Misc background-task helpers |
| `test_queue_upstream_timeout.py` | qBit/SAB upstream timeout on `/queue` |
| `test_status_cache.py` | Queue render cache invalidation |
| `test_stuck_state_cleanup.py` | Auto-reset of stuck `grabbed` volumes + import queue |

### Import pipeline (file staging)

| File | Covers |
|---|---|
| `test_import_atomicity.py` | Two-phase staged import (stage / commit_all / rollback) |
| `test_import_concurrency.py` | Bounded import semaphore + atomic claim |
| `test_import_mapping.py` | Filename → volume parser + mapping decisions |
| `test_execute_import_event_loop.py` | `_execute_import` event-loop interaction |
| `test_torrent_save_path_split.py` | Split-vs-shared download client `save_path` |
| `test_handoff_sab_nzbget_blackhole.py` | Non-qBit adapters (SAB, NZBGet, blackhole) |

### Parsers & helpers

| File | Covers |
|---|---|
| `test_release_mapping_parser.py` | Release-title → metadata parser |
| `test_chapter_range.py` | Chapter-range expansion / contraction |
| `test_chapter_key_candidates.py` | Chapter-number normalization |
| `test_vol_num_to_search.py` | `vol_num_to_search` helper (PR #102) |
| `test_cvm_trim_out_of_range.py` | Chapter-volume-map trimming |
| `test_regex_safety.py` | Catastrophic backtracking guards |
| `test_fstring_input_shape.py` | f-string parser tolerance |

### Schema, DB, and migrations

| File | Covers |
|---|---|
| `test_init_db.py` | First-boot DB initialization |
| `test_db_connection_unified.py` | All DB writes go through `get_db()` |
| `test_db_busy_timeout.py` | SQLite `BUSY_TIMEOUT` set on every connection |
| `test_schema_fk_migration.py` | FK constraint migration (events / blocklist / seen / pending_releases) |
| `test_migration_drift_guard.py` | Drift guard catches missing columns in rebuild DDL |
| `test_root_folder_bootstrap.py` | Root-folder migration |
| `test_indexer_dlclient_secret_migration.py` | Encrypt-at-rest for indexer + DL-client secrets |
| `test_notification_secret_migration.py` | Encrypt-at-rest for notification secrets |
| `test_settings_secret_migration.py` | Encrypt-at-rest for settings keys |
| `test_settings_validator.py` | Settings JSON validator |

### Architecture & invariants

| File | Covers |
|---|---|
| `test_hard_invariants.py` | Tripwires for the silent-correctness invariants in CLAUDE.md |
| `test_main_py_split_invariants.py` | `main.py` line ceiling + extracted-module import boundaries |
| `test_order_by.py` | `build_order_by` allowlist (SQL-injection guard) |
| `test_static_assets_provenance.py` | All static assets are committed |
| `test_docs_consistency.py` | Docstrings stay aligned with route shapes |

### Security

| File | Covers |
|---|---|
| `test_security.py` | General security guards |
| `test_secret_cipher.py` | Fernet key bootstrap + envelope encryption |
| `test_csrf_cookie.py` | CSRF middleware + `/api/` bypass |
| `test_api_key_middleware.py` | API-key auth |
| `test_ssrf.py` | SSRF guards on user-supplied URLs |
| `test_runner_safety.py` | Subprocess / runner safety |
| `test_silent_except_logging.py` | `except Exception: pass` is logged |
| `test_observability_events.py` | Grab-rejection / failure events emitted |
| `test_log_event.py` | `log_event` helper behavior |

### Adapters (external services)

| File | Covers |
|---|---|
| `test_mangadex_adapter.py` | MangaDex API client |
| `test_suwayomi_adapter.py` / `test_suwayomi_jobs.py` / `test_suwayomi_job_retry.py` / `test_suwayomi_filesystem.py` | Suwayomi DDL adapter |
| `test_live_integrations.py` | Live-network smoke tests (skipped by default) |

### Library / health / wanted

| File | Covers |
|---|---|
| `test_wanted_coverage.py` | `/wanted` and `/cutoff-unmet` data shape |
| `test_health_classifier_surfaces_blockers.py` | Health classifier surfaces real blockers |
| `test_health_performance.py` | Health classifier under realistic library size |
| `test_metadata_readiness.py` | Series metadata-readiness signals |
| `test_phantom_stub_detection.py` | Phantom volume-stub detection |
| `test_populate_chapters_relinks_unlinked.py` | `populate_chapters` re-links orphans |
| `test_map_drift_reconcile.py` | MangaDex chapter-map drift detection |
| `test_state_diagnostics.py` | `/state` diagnostic page |
| `test_patch_total_volumes_validation.py` | `total_volumes` PATCH validates non-negative + cascades |

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

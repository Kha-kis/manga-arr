# Mangarr App Audit — Remediation Plan

Generated from the 2026-04-21 whole-app audit. Seven PRs staged for value + risk, each reviewable under 300 LOC plus tests.

## Findings covered

| # | Finding | PR |
|---|---------|----|
| 1 | Silent grab/metadata/import failures reach neither event log nor UI | PR 1 |
| 2 | No retry/backoff on indexer 429s, MangaDex backfill, Suwayomi jobs | PR 2 |
| 3 | Stuck-state cleanup only runs at startup (grabbed volumes, pending_releases, queue rows) | PR 3 |
| 4 | Missing FK constraints on events / blocklist / seen / pending_releases | PR 5 |
| 5 | No CHECK constraint on volumes.status / chapters.status enums | PR 5 |
| 6 | Circuit breaker state is in-memory, lost on restart | PR 4 |
| 7 | `_GRABBING_URLS` lacks outer timeout | PR 4 |
| 8 | `main.py` is 8436 lines | PR 7 |
| 9 | `CONFIG` dict loaded without schema validation | PR 7 |
| 10 | Two independent refresh loops with no coordination | PR 7 |
| 11 | Phantom extra mainline stubs not surfaced by classifier | PR 6 |
| 12 | Series-editor endpoint clobbers unsubmitted fields | PR 4 |

## Execution order

```
PR 1 (observability)       ← start here, lowest risk
PR 4 (small fixes)         ← CB persistence + grab timeout + partial-patch editor
PR 6 (phantom stubs)       ← tiny classifier change
PR 2 (retry + backoff)     ← depends on PR 1's log_event wiring
PR 3 (stuck-state sweep)   ← depends on PR 1 for event logging
PR 5 (schema hardening)    ← after stuck-state sweep has purged orphans; needs DB backup
PR 7 (architectural)       ← last; large refactor wants a quiet moment
```

## PR 1 — Observability hardening
**Findings:** 1

Replace `print()` with `log_event()` at every silent-failure site:
- `grab_item` rejection branches (`main.py:6647-6821`) — emit `rejected_release` events with reason
- `fetch_mangadex_id`, `fetch_chapter_volume_map`, `fetch_kitsu_chapter_map` — emit `metadata_fetch_failed` events with source + reason (`main.py:5822-5963`)
- `import_lists.py:238-250` task orchestration — wrap in try/except + `log_event('error')`

**Risk:** Low. Additive only.
**LOC:** ~200 + ~150 tests.

## PR 4 — Small targeted fixes
**Findings:** 6, 7, 12

- **CB persistence:** `client_breaker_state` table; replace module-level `_circuit` dict (`download_clients.py:28-57`)
- **Grab timeout:** `asyncio.wait_for(grab_url, timeout=45)` in `grab_item` (`main.py:6624-6842`)
- **Editor partial-patch:** new `PATCH /api/series/{id}` that only writes submitted fields

**Risk:** Low-medium.
**LOC:** ~150 + ~150 tests.

## PR 6 — Phantom-stub detection
**Findings:** 11

- New `_BLOCKER_EXTRA_MAINLINE_STUBS` in `reconcile_map.py`
- `_health_state` surfaces it; `recommended_next_step` text explains resolution path

**Risk:** Very low.
**LOC:** ~40 + ~30 tests.

## PR 2 — Retry and backoff
**Findings:** 2

- `indexer_backoff` table; indexers check deadline before each request; respect `Retry-After`
- `_backfill_metadata_loop` honours same backoff for MangaDex 429s
- Suwayomi job processing: 3-attempt retry with exponential backoff before terminal error

**Risk:** Medium. Retry loops need solid tests against mocked 429s.
**LOC:** ~300 + ~200 tests.

## PR 3 — Stuck-state reconciler
**Findings:** 3

New hourly `cleanup_stuck_state` background task:
- Volumes `grabbed` with no `download_id` for >6h → reset to `wanted`
- `pending_releases` whose series is unmonitored/deleted → delete
- `import_queue` rows in `pending`/`partial` >30 days → `failed`, reset volumes
- `queue.dismiss_pending` cascades to matching `pending_releases` row

**Risk:** Medium-high. Wrong filter could delete user data. Mitigations: explicit WHEREs, SELECT preview on first run, row cap.
**LOC:** ~200 + ~250 tests.

## PR 5 — Defensive schema
**Findings:** 4, 5

SQLite rebuild pattern for 6 tables:
- `events`, `blocklist`, `seen`, `pending_releases` gain `REFERENCES series(id) ON DELETE CASCADE`
- `volumes.status` and `chapters.status` gain `CHECK(status IN (...))`
- Orphan-preview on startup before migration commits
- Central `VOLUME_STATUSES`, `CHAPTER_STATUSES` constants in `shared.py`

**Risk:** **High.** Schema migration on a ~1.4 GB production DB. Test on backup copy first.
**LOC:** ~200 + ~200 tests.

## PR 7 — Architectural cleanup
**Findings:** 8, 9, 10

Sequential commits, each a pure move / additive validator:
1. Extract `schema.py` *(deferred — see below)*
2. Extract `metadata.py` *(deferred)*
3. Extract `grab.py` *(deferred)*
4. Extract `pipeline.py` *(deferred)*
5. Add `SETTINGS_VALIDATORS` + validating `load_config` ✅
6. Consolidate refresh loops *(deferred — the 20s vs 300s cadence difference reflects genuinely different responsibilities; forcing a merge would lose separation)*

**Risk:** Medium-high (surface area). Each commit must leave the suite green.
**LOC:** ~200 added (validator). File-split deferred.

### Why the file split is deferred

The four-module extraction (1500+ lines moved) is a mechanical refactor,
but a mechanical refactor of this size ships a lot of surface area in
one PR. Every cross-module import can introduce a subtle circular-import
or test-path regression that only shows up under specific run orders.
Doing it responsibly requires attention that's easier to give when it's
the only thing landing in a PR — not bundled with behavioural changes.

Split as its own follow-up PR series (one module per PR), each commit
proven green before the next starts. The validator + loop coverage from
this PR do the high-signal observability work without requiring the
file split first.

## Known false positives from the audit

- The CSRF middleware's `except Exception: pass` at `main.py:8160` is **not** a bypass. If the parse raises, `valid` stays `False` and line 8173 returns 403. Fail-closed default.

# Metadata Retry Policy

Automatic provider retries use the same update strategy as scheduled metadata
refreshes and the core refresh phase of startup backfill:

- `once` is manual-only. Automatic retry selection excludes it even when a
  series is initially pending or an optional provider has failed.
- `throttled` permits automatic retries only when the last successful core
  metadata refresh is at least seven days old. Without a successful refresh,
  retries remain eligible after the provider backoff expires. An invalid stored
  refresh timestamp does not prevent recovery.
- `always` and an unset strategy retain the existing automatic retry behavior.

Only the strategy predicate is shared. Startup core backfill retains its
existing missing-ID/map criteria, recent MangaUpdates no-match cache, and
deleted-series exclusion; it can still select unmonitored series. Automatic
retries continue to require monitoring. The separate startup MangaDex
manifest-observation phase retains its own selection policy and is not
suppressed by `once` or `throttled`.

These rules apply to automatic selection, not to the refresh service itself.
Creation and explicit manual refresh can still call the service for any update
strategy. A non-forced refresh honors the chapter-map source's future retry
deadline, including when another provider is due. A forced refresh can bypass
that deadline. Skipping a deferred map leaves its source record untouched.
When that deferred source is failed or degraded, the refresh result retains
its source status and a warning, keeping successful-core aggregate metadata
degraded. Normal refresh persists that aggregate for raw API `metadataStatus`;
preview does not write it. A healthy or absent deferred source does not create
a failure warning.

An exception escaping chapter-map refresh is persisted as a failed attempt with
a retry deadline. A nonempty cached map, or a prior successful source refresh,
keeps the persisted source degraded rather than failed. The exception handler
does not replace maps, counts, selected provenance, or manual field locks.
Cancellation is propagated, not recorded as a provider failure; existing
startup recovery handles interrupted attempts.

This policy does not change provider response classification, schemas, provider
identity selection, or candidate priority. The regression coverage is in
`tests/python/test_metadata_retry_policy.py`.
Startup selection and deferred aggregates are covered by
`tests/python/test_metadata_startup_and_deferred_policy.py`.

# Metadata Retry Policy

Automatic provider retries use the same update strategy as scheduled metadata
refreshes:

- `once` is manual-only. Automatic retry selection excludes it even when a
  series is initially pending or an optional provider has failed.
- `throttled` permits automatic retries only when the last successful core
  metadata refresh is at least seven days old. Without a successful refresh,
  retries remain eligible after the provider backoff expires. An invalid stored
  refresh timestamp does not prevent recovery.
- `always` and an unset strategy retain the existing automatic retry behavior.

These rules apply to automatic selection, not to the refresh service itself.
Creation and explicit manual refresh can still call the service for any update
strategy. A non-forced refresh honors the chapter-map source's future retry
deadline, including when another provider is due. A forced refresh can bypass
that deadline. Skipping a deferred map leaves its source record untouched.

An exception escaping chapter-map refresh is persisted as a failed attempt with
a retry deadline. A nonempty cached map, or a prior successful source refresh,
keeps the persisted source degraded rather than failed. The exception handler
does not replace maps, counts, selected provenance, or manual field locks.
Cancellation is propagated, not recorded as a provider failure; existing
startup recovery handles interrupted attempts.

This policy does not change provider response classification, schemas, provider
identity selection, or candidate priority. The regression coverage is in
`tests/python/test_metadata_retry_policy.py`.

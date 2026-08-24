# Metadata Identity and Field Ownership Audit

This audit defines the metadata correctness contracts used for the Mangarr 1.3
milestone. It covers identity selection, persisted field ownership, candidates,
locks, refresh state, and existing-library matching as implemented in 1.2.0.

## Current invariants

### Identity selection

- A stored AniList ID is authoritative for AniList refreshes. Refresh uses an
  exact ID lookup and never replaces it with a title-search result.
- Without an AniList ID, refresh searches by the persisted series title and
  requires a normalized word F1 score of at least `0.85` before assigning an
  AniList identity.
- MangaDex matching prefers external AniList, MAL, or MangaUpdates links and
  otherwise requires a confident full-title match. Shortened-title matches do
  not silently establish an identity without an external-ID link.
- Existing-library matching only proposes candidates. Adoption requires an
  explicit operator choice and persists the chosen provider IDs and metadata.
- Provider IDs already stored on a series must not be replaced by an unrelated
  same-title search result.

### Field ownership and candidates

- The `series` row is the source of truth for the value currently used by the
  application. `series_metadata_fields` records the selected source and lock;
  `series_metadata_candidates` records provider observations.
- Recording a candidate does not itself grant ownership or mutate the persisted
  series value.
- Manual edits to title, volume count, chapter count, and chapter-volume map
  record manual provenance and lock the affected fields.
- A locked field continues to collect provider candidates but provider refresh
  cannot apply those candidates until the operator unlocks the field.
- Core AniList refresh records title as a candidate but does not automatically
  replace the persisted title.
- Applying a candidate updates the persisted value and its selected provenance
  in one transaction.

### Counts and local observations

- Manual, Google Books, and Wikipedia volume counts are protected from routine
  MangaUpdates replacement.
- Downloaded local volumes and chapters are lower bounds. Provider refresh
  cannot reduce a count below observed downloaded content.
- Routine AniList refresh is monotonic for counts. A lower candidate requires
  explicit operator confirmation through candidate application.
- Volume stub creation follows accepted count increases. Explicitly confirmed
  decreases retain downloaded rows and only remove safe wanted stubs in the
  edit workflow.

### Refresh and failure behavior

- Provider I/O occurs without an open SQLite connection. Refresh state and
  provider results are written in short transactions.
- One in-process lock serializes refreshes for a given series.
- A failed core refresh does not advance `last_metadata_refresh` and does not
  clear previously persisted metadata.
- Optional-provider, chapter-map, and cover failures preserve usable cached or
  local data and are reported as failed or degraded source state.
- Preview refresh records candidates but does not apply series values, aliases,
  tags, manifests, or covers.

## Observed disagreement cases

### Selected for this PR: stored MangaUpdates identity is not used for selection

`fetch_mu_metadata()` searches by title and chooses the highest title score even
when `series.mu_id` is already populated. If two MangaUpdates works share a
title, the first result can supply the volume count while the persisted MU ID
continues to identify a different work. The row, selected count provenance, and
provider identity can therefore disagree.

The narrow correction is to select the search result whose `mu_id` equals the
stored ID. If that identity is absent from the search response, refresh must not
borrow metadata from another candidate.

### Follow-up: AniList title-only refresh does not reject tied identities

When no AniList ID is stored, `_resolve_anilist_record()` checks only the best
title score and has no ambiguity margin. Distinct works with the same normalized
title can both score `1.0`; result ordering then decides which identity is
persisted. This is independent of the stored-MU-ID defect and needs a separate
policy for ID, year, alias, and score tie-breaking.

### Follow-up: initial title ownership is not initialized consistently

Manual edit paths explicitly lock title ownership, but series creation and
existing-library adoption insert the title after startup provenance backfill has
already run. Until a later backfill or manual edit, title can appear as `legacy`
with no selected row even when it was intentionally supplied by the operator.
An AniList title candidate can consequently appear as a safe pending change.
Creation-path ownership needs a separate decision because search additions,
API additions, and folder adoption do not all imply the same title owner.

### Follow-up: equal persisted and candidate values can hide source drift

Candidate application is driven by value differences. If the persisted value
equals a recommended candidate but `selected_source` is stale or missing,
`pending` is false and safe apply does not reconcile the provenance source.
The application value is correct, but the ownership display can remain wrong.

### Follow-up: existing-library confidence does not express ambiguity

Unmapped-folder matching scores the canonical candidate title and can return
multiple candidates with the same confidence. This is currently data-safe
because the operator must select a candidate, but the response does not label a
tie or incorporate aliases, year, or external IDs into confidence. This is a UX
and matching-policy follow-up, not part of the selected identity fix.

### Follow-up: candidate confidence can overstate fuzzy AniList resolution

After a title-only AniList resolution passes the `0.85` threshold, all fields
are recorded with confidence `1.0`. The persisted identity may be acceptable,
but the candidate record does not preserve the actual title-match confidence.
Changing this requires carrying resolution evidence through the service and is
separate from identity anchoring.

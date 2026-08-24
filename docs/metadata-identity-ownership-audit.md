# Metadata Identity and Field Ownership Audit

This audit defines the metadata correctness contracts used for the Mangarr 1.3
milestone. It covers identity selection, persisted field ownership, candidates,
locks, refresh state, and existing-library matching as implemented in 1.2.0.

## Current invariants

### Identity selection

- A stored AniList ID is authoritative for AniList refreshes. Refresh uses an
  exact ID lookup and never replaces it with a title-search result.
- Without an AniList ID, a stored MAL ID anchors the result only when exactly
  one distinct AniList candidate has that MAL ID. Conflicting or missing MAL
  evidence fails closed.
- Without a stored provider identity, refresh searches by the persisted series
  title. It assigns an AniList identity only when exactly one distinct result
  reaches the existing normalized word F1 acceptance threshold of `0.85`.
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

### Resolved: AniList title-only identity ambiguity

AniList identity evidence now has explicit precedence: stored AniList ID,
unique exact stored-MAL-ID match, then a unique accepted English/romaji title
match. If multiple distinct candidates reach the existing `0.85` title
acceptance threshold, result order and score rank do not break the tie. Refresh
records an `identity_ambiguous` source failure without applying fields or
recording candidates from the ambiguous AniList results. Cached AniList metadata
remains available; independent MangaUpdates, MangaDex, and cover layers continue
under their existing refresh policies.

No new ambiguity margin was introduced. Publication year can be absent, shared,
or represent a different publication boundary, so it does not establish
identity. AniList format describes a media category and does not map safely to
Mangarr edition types. Search results do not currently include synonyms, and
aliases therefore cannot disambiguate candidates. These signals remain
deferred until their semantics and operator-facing behavior are designed.

### Resolved: manual-import auto-add identity safety

Manual-import automatic creation applies the same pure AniList candidate
resolver as routine metadata refresh. With no persisted series row available,
there is no stored provider identity to anchor the search: exactly one distinct
AniList ID must satisfy the existing English/romaji title F1 threshold of
`0.85`. Duplicate search rows for that ID count as one identity. Multiple
qualifying identities, no qualifying identity, or a result without an identity
fails closed; provider ordering is never a tie-breaker.

MangaUpdates fallback follows the existing background-enrichment title F1
threshold of `0.7`, but creation is more conservative than ranking alone:
exactly one distinct qualifying MU ID is required. A successful fallback
persists that MU ID with the new series so later refreshes remain anchored.
Routine MangaUpdates enrichment keeps its existing stored-ID behavior; this
creation contract does not change provider precedence or refresh policy.

After candidate resolution, an existing standard-edition series is reused only
by the selected provider's exact ID. Manual-import auto-add has no edition
evidence and creates standard editions, so an exact-ID omnibus, deluxe, or
other non-standard row is not reused. Files in the detected group are bound
directly to the resolved standard series, so exact-ID reuse also works when the
local filename and persisted title differ.

If no exact standard-edition identity exists but an active series already has
the selected provider title, creation fails closed. Auto-import cannot safely
reuse the different identity or invent a second filesystem destination for the
same title without operator input.

Ambiguous or unresolved groups remain unmatched for operator handling. They do
not create a series, provider IDs, title provenance or candidates, volume
stubs, or a metadata-refresh task, and their source files are not moved,
linked, copied, or removed.

### Resolved: initial title ownership by creation path

Every production path initializes title selection and its initial candidate in
the same transaction as the new series row. Ownership follows the origin of the
persisted title rather than treating every submitted string as manual:

| Creation path | Title origin | Selected source | Locked |
| --- | --- | --- | --- |
| Browser search/add with AniList result | Selected AniList result | `anilist` | No |
| Browser search/add with MangaUpdates fallback | Selected MangaUpdates result | `mangaupdates` | No |
| Browser add without a provider identity | Explicit submitted title | `manual` | Yes |
| API series creation | API caller payload | `api` | No |
| Existing-library folder adoption without `title` | Local folder name | `local` | Yes |
| Existing-library folder adoption with `title` | Explicit adoption title | `manual` | Yes |
| AniList import list | Automated AniList list entry | `anilist` | No |
| MyAnimeList import list | Automated MAL list entry | `myanimelist` | No |
| Custom RSS import list | Automated external feed entry | `custom_rss` | No |
| Manual-import auto-add | Unique accepted AniList or MangaUpdates result | Selected provider | No |

Local and explicit adoption titles are locked because the persisted title names
an existing library folder or an operator choice; `metadataTitle` remains the
search pattern and does not take ownership of the library title. API and
automated paths are deliberately not classified as manual. Their origin remains
visible, and unlocked provider/API selections can participate in normal
candidate review. Legacy rows remain compatible with startup provenance
backfill.

### Resolved: unlocking a locally owned title relinquishes local priority

A locally adopted title starts as `local` and locked. While locked, its local
candidate remains current and recommended; differing provider titles remain
visible as conflicts but cannot become pending or change the persisted title.

Unlocking is an ownership decision, not a value change. The persisted title and
`selected_source` remain unchanged, and the historical local candidate remains
visible. For an unlocked `title` only, local candidates are treated like
unlocked manual candidates: they are demoted below provider candidates and
excluded from conflict calculation. One credible provider title can therefore
become recommended without conflicting solely with the relinquished local
value. Differing provider candidates still conflict with each other and require
explicit review.

Accepting a provider candidate updates the persisted title and selected source
through the existing candidate-application path. Relocking preserves that
value and source and prevents later candidates from becoming pending. Relocking
also restores ordinary locked candidate ranking and conflict participation, so
the historical local candidate can rank first again without being applied over
the protected current value. This rule does not change global `local` priority;
local volume and chapter observations retain their existing recommendation and
conflict behavior.

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

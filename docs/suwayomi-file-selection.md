# Suwayomi Chapter File Selection

Suwayomi imports recognize `Ch.N`, `Ch. N`, `Chapter N`, and `# N`, with
an optional scanlator prefix and `Vol.N` label. Chapter numbers are compared
exactly: chapter 17 is not 17.5 or 170, and 17.5 is not 17.55. A narrow
fallback accepts `<scanlator>_<one word> <number>.cbz`, including `Mission`,
`quest`, and `Chime`, only when the number ends the chapter title.
Both explicit labels and this fallback require a single scanlator-prefix
segment. Extra underscores could instead be chapter-title text and are not
consumed to discover a secondary chapter label. Malformed first identities and
unsupported multi-underscore prefixes fail closed rather than selecting a
number from later title text.

Duplicate variants are selected deterministically. Explicit chapter labels
rank before the single-word fallback. Within that identity class, an
undecorated chapter title ranks before a title with extra text, regardless of
scanlator name length. Remaining ties use suffix length, filename length,
case-insensitive filename, then the original filename. This policy does not
claim scanlator quality or language preference. Only regular CBZ files are
selected; directories and chapter-file symlinks are excluded.

## Volume Jobs

The existing job-completion query requests `chapterNumber` together with
`id` and `isDownloaded`. Only the stored job chapter IDs supply the chapter
numbers used for assembly. Volume filename labels and the series metadata
chapter map do not add chapters to that set. Each logical chapter number is
selected once, in numeric order, in both merged and individual-file modes.

All required files must be selected before any library output is created,
copied, or merged. A missing file, unknown/invalid chapter number, or empty
chapter set leaves the job errored rather than marking a partial volume
downloaded. A queued chapter ID missing from the live feed remains incomplete
under the existing polling/startup-recovery rules. Stored IDs, progress
accounting, retry controls, provider identity, monitoring, and metadata
ownership are not rewritten by file selection.

## Split Chapters

There is no automatic `45.1`/`45.2` to `45` fallback. Neither a filename nor
the current source chapter records prove that chapter 45 contains both parts,
that every part belongs to the same volume, or that a decimal denotes a part
rather than an extra chapter. Even a job containing both 45 and 45.1 is not
proof of equivalent content. Missing exact parts therefore fail closed.

A job whose stored source IDs resolve to whole chapter 45 can import the exact
chapter 45 file, even when the metadata map lists 45.1 and 45.2. That is exact
source identity selection, not inferred split coverage. Supporting other
split/whole mappings requires explicit content-equivalence evidence and a
separately reviewed policy; this change introduces no schema or provider calls
to guess it.

This does not resolve the split-map-only grab case in issue #384. The existing
`suwayomi_grab` selector requires an exact chapter-map number or a source
chapter name identifying the requested volume. If the map contains only 45.1
and 45.2 but the source offers an unlabelled chapter 45, it returns false and
creates no job. A manually queued whole-chapter job tests assembly only, not
that selection path. The regression suite also exercises real grab and polling:
an explicit source `Vol.1 Chapter 45` name can queue/import chapter 45 under the
existing volume-name rule, whereas an unlabelled whole chapter, a different
volume label, and a map splitting parts across volumes cannot establish the
requested membership. No map entries or membership/equivalence rules are
changed here; the selector's completeness check is described below.

### Operator Remediation

When the operator has verified that the chosen source's whole chapters cover
the intended volumes, the existing series Edit form's **Chapter-to-Volume Map**
textarea (`chapter_map_text`) can replace the split metadata map. It accepts
integer chapter numbers, comma-separated lists, and integer ranges. Each
non-empty line represents the next volume, starting at volume 1; blank lines
do not reserve a volume number. Replace the entire map with the expected
source chapter set, retaining the verified assignments for other volumes.
For example, `45` on the first line maps source chapter 45 to volume 1; `43, 44`
on that line maps both whole chapters to volume 1. Do not use these examples
unless they match the actual source content and intended volume membership.

Saving uses the existing `POST /series/{id}/edit` route, replaces
`series.chapter_vol_map`, and records manual map ownership and a locked manual
metadata selection. The regression submits `chapter_map_text=45` through that
actual route after split-map refusal, then verifies real grab and polling,
exact whole-45 CBZ output in both merge/copy modes, and retained manual source,
provider IDs, monitoring, and preexisting local metadata. It does not rely on
SQL insertion of the remedial map. No new UI, endpoint, or automatic inference
is introduced.

Adding 45 while retaining unavailable 45.1/45.2 is not remediation: the
complete expected set is still unavailable, and grab remains unsuccessful.
Changing this series-wide map also affects consumers other than Suwayomi;
review the whole map before replacing it. The existing edit handler can seed
chapter stubs but does not remove old split stubs or repair historical imports.

The separate **Chapter Map** override editor is not this textarea. Its existing
`POST /series/{id}/chapter-map` route writes `series_chapter_overrides`, not
`series.chapter_vol_map`. The regression posts a `45 -> 1` override and verifies
that it alone does not enable Suwayomi grab from the split-only map. Use the
series Edit map replacement for this remediation, not that override table.
These tests exercise HTTP handlers and import processing, not browser rendering.

Mapped selection now requires the complete expected number set in the live
feed, for both the series map and the cached MangaDex fallback. For example,
`43: 1, 44.1: 1, 44.2: 1` cannot queue only 43 when the source offers 43 and
whole 44. Unknown source numbers cannot bypass that guard via a less complete
cached mapping. The grab remains unsuccessful and no volume output is created.
This guard prevents new subset jobs; it does not infer coverage or retroactively
rewrite previously stored job IDs. Historical subset jobs require operator
review because their original intended chapter set is not stored separately.

The legacy direct volume-import helper can still select volume-named files
when no explicit job chapter set is supplied. Production volume job processing
always supplies the exact set and never uses that legacy path to bypass a
missing or unknown chapter.

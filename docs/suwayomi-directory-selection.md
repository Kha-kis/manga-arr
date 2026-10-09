# Suwayomi Directory Selection

Completed chapter and volume jobs resolve the source from their persisted
`suwayomi_manga_id`, then choose a download directory only within that source.
The strongest matching title tier must contain one eligible directory.
Filesystem order does not break ties. Missing source evidence, a missing source
folder, or an ambiguous title leaves the job errored rather than importing from
an unrelated or stale source folder.

## Directory Inventory

The configured Suwayomi client's `download_path` is the trusted download-root
anchor. The chooser inventories only this structure:

```text
<download_path>/mangas/<source-folder>/<manga-folder>/
```

`mangas`, source-folder, and manga-folder child symlinks are excluded. Source
and manga entries must be real directories and their resolved parents must
match the expected inventory level. A symlink supplied as the configured
download-root anchor remains supported. Files, nested descendants, and
out-of-root targets cannot substitute for a direct-child manga directory.
An enumeration error refuses selection rather than using a partial inventory.
Production jobs restrict this inventory to the exact sanitized source-folder
basename returned for the queued manga. No normalized or alias source-folder
search is performed, and an absent folder does not enable cross-source lookup.

Titles are labels, not paths. No raw title is joined onto a source folder.
Every returned path comes from the directory inventory, including when a
title contains absolute-path syntax, slashes, or traversal components.
Such punctuation can still match its safe, single-component sanitized name.
This is not an atomic defense against a hostile actor swapping directories
after inspection; it does not introduce file-descriptor-pinned source reads.

## Matching Precedence

The existing import callers supply the live Suwayomi manga title first and
the Mangarr series title as an alias. Legacy callers can omit the live title.
The chooser evaluates these tiers within the job's resolved source folder.
Legacy direct helpers without source evidence retain a unique-only inventory
across all eligible source folders; production jobs never use that fallback:

1. Exact basename derived from the first title argument, when nonempty, using Suwayomi
   SafePath rules. The first argument retains its position: an empty live
   title does not promote the metadata alias into this stronger tier.
2. Exact safe aliases: sanitized basenames from all supplied titles, together
   with raw single-component legacy labels. Raw labels with control
   characters, slashes, backslashes, or the components `.` and `..` are not
   aliases. Metadata aliases are considered together, not in argument order.
3. Nonempty normalized equality. The existing normalization lowercases,
   replaces characters outside ASCII letters/digits with spaces, collapses
   whitespace, and trims it. Only equal normalized names qualify; substring
   containment is not evidence of directory identity.

A populated tier with one distinct directory wins. A populated tier with
multiple directories returns no match immediately; a lower tier cannot
repair the ambiguity. Repeated aliases do not duplicate a candidate.
If every tier is empty, the import also refuses selection.

For example, a live `Source:Title` selects a unique `Source_Title` directory
before a different directory matching the metadata title. With source evidence,
an identically titled directory in another source is not a candidate. Without
source evidence, two source folders containing that exact `Source_Title`
directory remain ambiguous, even if only one contains a requested chapter file
or another metadata alias is unique.
Chapter availability, cached library output, folder sorting, and source-name
preferences are not directory tie-breakers.

## Sanitization And Source Evidence

The directory basename follows the pinned upstream
[SafePath implementation](https://github.com/Suwayomi/Suwayomi-Server/blob/v2.3.2243/AndroidCompat/src/main/java/xyz/nulldev/androidcompat/util/SafePath.kt):
trim leading/trailing ASCII dots and spaces, substitute `(invalid)` for an
empty result, replace FAT-invalid characters with underscores, and truncate
to at most 240 UTF-8 bytes without splitting a code point. This also satisfies
the upstream 240 UTF-16-unit bound. Unencodable Unicode is not a basename.
Unicode titles remain supported by exact matching even when their ASCII
normalization is empty. Sanitization and truncation can collide; a collision
present in multiple candidate directories is refused, not sorted away.

Upstream [DirName](https://github.com/Suwayomi/Suwayomi-Server/blob/v2.3.2243/server/src/main/kotlin/suwayomi/tachidesk/manga/impl/util/DirName.kt)
uses `SafePath(source.toString())` for the source-folder component.
[SourceType](https://github.com/Suwayomi/Suwayomi-Server/blob/v2.3.2243/server/src/main/kotlin/suwayomi/tachidesk/graphql/types/SourceType.kt)
exposes that same `source.toString()` as `displayName`. The existing exact
`manga(id: $mid)` query now requests `source { displayName }` alongside the
title and chapters, where `$mid` is the job's persisted manga ID. No additional
provider request is needed. The current series linkage does not choose or
override this source, including after a relink.

Stored linkage names may be aliases, and stored languages may be defaults.
Neither is used to reconstruct the source-folder component. A missing, null,
non-string, or blank display name refuses production import, as does a missing
source folder. The existing import error/retry flow remains in place;
no source download, job identity, or linkage is rewritten.

This contract isolates stale folders from a different source, but the upstream
layout still uses names rather than IDs. Two identities that share a single
sanitized source/manga path cannot be distinguished from folder evidence alone.
Source rename/migration recovery, support for a different upstream directory
layout, and descriptor-pinned reads remain separate follow-ups. Mangarr does
not guess a replacement source or rename existing folders.

## Import Behavior And Compatibility

Production polling and legacy direct import helpers use the same title chooser,
with the source boundary required for production jobs.
Refusal occurs before chapter-file selection, copy, merge, or destination-cache
reuse. Existing polling then records an import error without completing the
job or marking the chapter/volume downloaded. A failed job remains available
for the existing operator retry workflow; no job IDs or source downloads are
rewritten.

Unique exact, sanitized, safe legacy-alias, case-only, and punctuation-equivalent
matches remain supported. Substring-only guesses, ambiguous matching tiers,
and child directory symlinks are now refused. No directories are renamed;
configuration, schema, source linkages, metadata, monitoring, chapter maps,
and chapter/volume filename parsing are unchanged. The chapter basename policy
from PR407 remains unchanged.

`tests/python/test_suwayomi_directory_selection.py` covers reversed inventory
order, matching precedence, collisions, Unicode and byte truncation, path
components, symlinks, and enumeration failure. Real chapter/volume jobs exercise
both merged and individual-file volume modes, with new and cached outputs,
and verify refusal preserves source files, library files, and domain rows.

`tests/python/test_suwayomi_source_identity.py` exercises completed jobs with
stale same-title folders and a current linkage different from the queued manga.
Chapter, merged-volume, and individual-file volume imports must read the queued
source's pages. Missing or malformed source evidence, a missing source/title,
the wrong language label, and source/manga child symlinks refuse import without
changing library rows or retained files. Source display labels and manga titles
use the same upstream sanitization; legacy direct-helper compatibility remains
covered separately.

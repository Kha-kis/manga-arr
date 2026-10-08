# Suwayomi Directory Selection

Completed chapter and volume imports choose a download directory only when
the strongest matching tier contains one eligible directory. Filesystem order
does not break ties. An ambiguous directory leaves the job errored rather than
importing a file from the first source folder encountered.

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

Titles are labels, not paths. No raw title is joined onto a source folder.
Every returned path comes from the directory inventory, including when a
title contains absolute-path syntax, slashes, or traversal components.
Such punctuation can still match its safe, single-component sanitized name.
This is not an atomic defense against a hostile actor swapping directories
after inspection; it does not introduce file-descriptor-pinned source reads.

## Matching Precedence

The existing import callers supply the live Suwayomi manga title first and
the Mangarr series title as an alias. Legacy callers can omit the live title.
The chooser evaluates these tiers across all eligible source folders:

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
before a different directory matching the metadata title. Two source folders
containing that exact `Source_Title` directory are ambiguous, even if only
one contains a requested chapter file or another metadata alias is unique.
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
uses `SafePath(source.toString())` for the source-folder component. Current
job polling supplies a manga ID and title but no authoritative source-folder
descriptor. Stored linkage names may be aliases, and stored languages may
be defaults. This change does not reconstruct a source folder from those
values, invent an API field, or add a provider request.

Global uniqueness removes the observed multi-directory ambiguity; it does
not prove that a sole matching folder belongs to the queued manga/source ID.
Two provider identities that share a single sanitized directory cannot be
distinguished with the current folder evidence. Automatic disambiguation
requires a separately reviewed source/path identity contract.

## Import Behavior And Compatibility

Production polling and legacy direct import helpers use the same chooser.
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

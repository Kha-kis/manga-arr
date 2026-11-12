# Archive Runtime

New images use the official upstream 7-Zip 26.06 standalone Linux decoder for
amd64 and arm64. The Debian trixie 7zip/7zip-rar packages are not installed.
This replaces the affected archive tools identified in
[issue #518](https://github.com/Kha-kis/manga-arr/issues/518); source changes alone
do not qualify or publish a replacement release.

## Build And Identity

`scripts/install_7zip.py` pins each official release archive and its SHA256.
The build checks the archive before parsing it and verifies the selected
static executable's ELF architecture. Unknown platforms, altered downloads,
unsafe archive members and unexpected output paths fail the build.
Downloads use a 30-second socket timeout and a cooperative 120-second deadline;
the deadline is not a hard wall-clock cap across blocking reads and redirects.

The static `7zzs` executable is installed as `/usr/bin/7zz`, with a `/usr/bin/7z`
compatibility alias. RAR decoding is built in; no separate `Rar.so` plugin is
required. Application extraction routes and the Python `rarfile` dependency
are unchanged.

The image retains upstream `License.txt` and `readme.txt` under
`/usr/share/doc/7zip/`, alongside build provenance and an explicit
`7zip.cdx.json` component inventory. The legacy
`/usr/share/doc/7zip-rar/copyright` location contains upstream license information
for compatibility; it does not indicate an installed Debian package. The
application's AGPL label does not relicense this dependency. The upstream unRAR
restriction remains in force; the decoder is not permission to create a
RAR-compatible encoder.

## Verification And Limits

Focused installer tests cover download/hash/member/architecture refusal and
valid installation. The explicit image gate in `tests/archive_release/` checks
real compressed single- and multipart RAR bytes, refusal when a required part
is missing, ZIP/7z controls, CBR conversion and original-file preservation.
Only installation identity and license assertions change with this backend;
the payload fixtures and decoding assertions remain unchanged.

A passing version check is not a vulnerability scan. Final release evidence
must identify the actual per-platform binary, preserve scanner findings, and
verify supported import flows. Downloading or parsing an arm64 binary on amd64
is not native arm64 execution. No exploit reproduction is needed to establish
that the old package version is affected.

## Upstream References

- [Official 26.06 release and archive digests](https://github.com/ip7z/7zip/releases/tag/26.06)
- [Official distribution license](https://7-zip.org/license.txt)
- [Debian CVE-2026-48095 status](https://security-tracker.debian.org/tracker/CVE-2026-48095)
- [Debian CVE-2026-14266 status](https://security-tracker.debian.org/tracker/CVE-2026-14266)

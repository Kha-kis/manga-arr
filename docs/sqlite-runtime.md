# Private SQLite Runtime

The Docker image builds canonical upstream SQLite 3.54.1 in a separate stage
using the same literal digest-pinned official Python 3.14-slim base as runtime.
This is a Mangarr-maintained upstream build, not a Debian-supported package.
Mangarr maintainers own upstream advisory monitoring, source-pin review and
rebuilds. No Python adapter, application WAL/locking policy or live host library
is replaced.

## Source And Build Contract

`scripts/sqlite_runtime.py` pins the official versioned source ZIP URL,
publisher's SHA3-256 and complete `SQLITE_SOURCE_ID`. Each build downloads its
own bounded archive without redirects, checks the ZIP digest, version, Fossil
manifest identity and every manifest file (SHA1 or SHA3 as declared), and refuses
unlisted/missing/duplicate/special/traversal members before source execution.
Only upstream's documented final `# Remove this line` manifest comment may be
omitted for the manifest identity calculation. This is hash-authenticated source
against reviewed publication pins, not a detached-signature verification claim.
Legacy SHA1 Fossil cards are checked for consistency only inside the separately
SHA3-authenticated archive; SHA1 is not the independent trust anchor.
Canonical input bytes are checked again after generation. No acceptance uses
the exit status of upstream `src-verify -x` (which returns0 even on changes).

The canonical build generates Lemon's parser and the amalgamation. Both the
configure `--enable-update-limit` switch and the complete `OPTS` definitions
reach parser generation as well as library compilation. `--soname=legacy`
requests CPython's existing `libsqlite3.so.0` dependency. No prebuilt
amalgamation, sid packages, `--all` feature bundle or shell-only feature flags
are used. Build tools, headers, source and Tcl remain outside the final image.
The configured update-limit parser can change the generated amalgamation hash
from the default published amalgamation; provenance records the actual generated
bytes and never invents byte-equivalence to that different configuration.

`runtime/sqlite-baseline.json` preserves the public actual Native11 **amd64
official-base** observation: SQLite 3.46.1, Debian 3.46.1-7+deb13u1, 60 options
and 17 CPython API features. It is a compatibility target, NOT an ARM observation
or fixed-runtime qualification. Future OS upgrades can produce a different
retained Debian package; actual packages are recorded, never inferred as u2.

All 59 baseline non-compiler option strings must still match exactly. Only the
exact pinned3.54.1 version and full source ID additionally require
`MAX_SCHEMA=10000000`, once with that exact value; other source bindings refuse.
The baseline JSON and17 API-feature classifications remain unchanged. This
upstream-default schema-object ceiling was introduced in3.54.0 and did not exist
as a limit category in baseline3.46.1; it is an explicit new behavior, not an
assertion that the old engine already had the same ceiling. No build define is
added for it, and no prefix allowance, option normalization or exception applies.
The build requests
the vendor feature/default/limit definitions explicitly; configure/compiler
detect atomic intrinsics, pthread mutexes, system malloc and isnan. A changed
compiler's real identity is recorded instead of forcing the old gcc-14.2.0
string. In particular:

| Boundary | Preserved choice |
| --- | --- |
| Threading, persistence | THREADSAFE=1, synchronous=2, WAL synchronous=2, auto-checkpoint=1000, TEMP_STORE=1 |
| Limits | Function arguments=127, variables=250000, all other observed limits retained |
| Search/API | FTS3/4/5, tokenizer/parentheses, RTree, JSON, math, column metadata, dbpage/dbstat/stmt |
| Native APIs | Session/preupdate/unlock, backup, serialize/deserialize, load-extension API |
| Legacy compatibility | Rowid-in-view, URI, secure-delete, LIKE/blob, soundex, file format4 and page-size choices retained |

Some names are historical rather than new features: file-format4 remains the
current format; `ENABLE_LOAD_EXTENSION` does not recreate CPython header-time
APIs; `TEMP_STORE=1` is a compile default while SQL `PRAGMA temp_store` initially
reports0 (use that default). In-memory databases deliberately report
auto_vacuum0 even with DEFAULT_AUTOVACUUM=1; fresh disk databases retain1.
MAX_DEFAULT_PAGE_SIZE=32768 remains relevant to device-dependent page selection.
No obsolete-option removal or option drift is silently waived. Unexpected
changes stop the build and require a specific semantic reconciliation.
Compile-option refusal includes structured JSON with complete missing/unexpected
sets, duplicate occurrence counts and compiler values/count. Diagnostics accept
at most256 options of1-256 printable ASCII characters each; malformed or
oversized data gets a fixed non-echoing refusal, never a truncated policy check.
No option normalization, exception or build-flag adjustment follows automatically.

Upstream 3.54.1's configure math switch also enables percentile functions.
To preserve the vendor set, the recipe disables that bundle, explicitly defines
SQLITE_ENABLE_MATH_FUNCTIONS and links libm. Carray/geopoly/stat4 and other
unrequested extensions are not enabled. DQS is not changed to the CLI shell's
DQS=0 setting.

## Loader And Provenance

Only `/opt/mangarr/sqlite/3.54.1/lib/libsqlite3.so.0` is copied as the private
library. Root-owned `/etc/ld.so.conf.d/00-mangarr-sqlite.conf` plus `ldconfig`
select it image-locally. No LD_PRELOAD/LD_LIBRARY_PATH or Debian-owned library
overwrite is used. The final root and final application UID each run the probe;
wrong mappings or unresolved ABI dependencies fail the image build.
Configure's supported `--disable-rpath` prevents upstream's default install-prefix
search path from entering the library link. Both the library and unchanged
CPython extension must still have no RPATH/RUNPATH; the ELF refusal is retained.

Root-owned `provenance.json` binds official base reference, archive SHA3 and
computed SHA256, canonical identity and all source-file hashes, generated
parser/amalgamation/configuration hashes, exact flags, actual compiler/packages,
platform, library SONAME/path/hash/ELF/export/version data, CPython extension
hash/imports/ELF and observed library options. It also hashes the probe and
baseline input. It is build provenance, not a signature or release attestation.
The final actual OS package set and immutable image/index/child digests still
need release evidence; same base pins do not guarantee synchronized apt mirrors.
Link hardening is a configure-time `LDFLAGS` assignment, which the pinned build
consumes through `LDFLAGS.configure`, not an unused make-time `LDFLAGS` value.
The builder requires actual GNU_RELRO program headers and BIND_NOW/NOW dynamic
flags and records those program headers. Offline dispatch/ELF-text models do not
prove that a real built library has these properties.

The Debian SQLite copy is retained intact and remains in SBOM/scanner scope.
Its findings must remain visible. Do not blanket-suppress Debian findings just
because the application now maps a source-built library.

## Small Artifact Interfaces

In a future admitted built image (not the host checkout):

```sh
python -B /opt/mangarr/sqlite/3.54.1/sqlite_runtime.py verify
python -B /opt/mangarr/sqlite/3.54.1/sqlite_runtime.py qualify-wal --workspace /owned-disposable/new-child
```

The first command uses unchanged CPython sqlite3, verifies root-owned artifact
hashes, actual ELF machine, exclusive `/proc/self/maps` library path, SQL source
ID, all compile options/APIs, defaults/limits and parser/FTS/RTree/JSON/math/
backup/serialization behavior. Output is this probe process's observation,
**not** the application's PID1/server mappings or SQL observation.
It also probes schema-object limits on new private `:memory:` connections using
the existing `getlimit`/`setlimit` API and integer category13, pinned by
`src/sqlite.h.in:4467`; the older-header Python adapter need not export a new
module constant. The default must be10million. A local test cap of5 permits the
upstream table-with-UNIQUE, named-index and view fixture, then refuses its
sixth-object trigger with exact SQLITE_ABORT/`too many schema objects`.
The pinned callback path returns SQLITE_ABORT through sqlite3_exec; upstream's
test result1 is Tcl catch status, not the SQLite primary error code. SQLITE_ERROR
is not an alternative accepted result for this fixture.
The cap is restored in `finally`, connections close, and a fresh connection must
still report10million. No application connection, global default or data model
is changed. Offline fake-API controls do not prove real shipped-library behavior;
the next actual admitted image build must pass this probe.
On overflow mismatch, the strict code/message predicate refuses and appends structured
JSON with actual `sqlite_errorcode`, `sqlite_errorname` and message from this
fixed private memory fixture only. Diagnostic bounds are integer code1-2147483647,
name1-64 and message1-512 printable ASCII characters. Malformed/unavailable
details produce only a fixed classification, without value echo or truncation.
Diagnostics do not learn an accepted error code or waive the existing assertion.

The second first requires that same runtime admission, then exclusively creates
a new disposable directory. It runs bounded separate writer/checkpointer
processes against the mapped shipped library, a deliberately unclean process
exit with committed and uncommitted rows, reopening, integrity and truncating
checkpoint assertions. It joins its own children, terminates/kills only those
children on failure, leaves evidence/data for the caller's lifecycle, and never
changes application defaults. It does not accept an existing workspace.
An incomplete worker reap is a refusal, not successful cleanup; an outer native
gate must still reconcile the complete CLI process group, including Python's
multiprocessing support process. No such processes are started by offline tests.
Both initial and crash-worker batches enroll each pending handle before `start()`.
Cleanup closes genuinely unstarted handles separately; a failed attempted start
without published child identity is never signalled or reported reaped. Every
identified child receives bounded best-effort reconciliation even if another
child's join/terminate/kill/close raises. Any cleanup error or incomplete terminal
proof refuses completion and retains the outer exact process-group fence.

This bounded public-API exercise is **not** a deterministic reproduction of the
upstream WAL-reset race, power-loss simulation, NAS qualification or application
test. Clean stress cannot prove absence of that race. Provenance includes the
exact shipped `src/wal.c` source hash and the entire source/test file manifest
for the separately reviewed upstream fix-ancestry/regression binding.
An upstream debug Tcl testfixture can compile a different SQLite; its pass alone
must never be labeled shipped-binary coverage. No test overlay, pytest install,
generic launcher or native ARM workflow is added to the image.

## Required Release Gates

- Fresh actual build, loader/ELF/import/export/package/provenance checks and
  source-bound upstream WAL fix ancestry plus the actual regression. Distinguish
  debug-instrumented source tests from actual shipped-library public-API stress.
- On native amd64 AND arm64, actual artifact writer/checkpoint/unclean-exit/
  recovery/integrity behavior and immutable per-platform image subjects. The
  retained amd64 base observation supplies no ARM inventory or runtime credit.
- Actual application startup/migrations/search, committed-WAL backup/restore,
  upgrade and exact prior-image plus matching pristine-backup rollback on owned
  disposable data. Preserve original entrypoint/nonroot/API behavior and observe
  actual server mappings separately from this probe.
- Full existing source/release gates, both-copy SBOM/scans, exact immutable image
  identity and independent review before publication. Old-image rollback also
  restores the old image's unresolved WAL-reset exposure.

No native build, fixed runtime, WAL protection, application, ARM or NAS pass is
claimed by offline unit tests or source hash verification.
The existing fast gate includes the offline build-contract and final-base helper
tests; its later Docker/Compose step is not a substitute for artifact qualification.

Primary references: [3.54.1 release identity](https://www.sqlite.org/releaselog/3_54_1.html),
[canonical source and authenticity](https://www.sqlite.org/src/doc/release/README.md),
[compile options](https://www.sqlite.org/compile.html),
[WAL-reset advisory](https://www.sqlite.org/wal.html#walresetbug).

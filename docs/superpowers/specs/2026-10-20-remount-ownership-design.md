# Remount-Safe Ownership Proofs And Legacy Recovery

Status: **maintainer-approved for implementation planning on 2026-10-20**.
Approval authorizes the implementation plan, not implementation, migration,
recovery operations, release or deployment.

Source reviewed: `c4d6b039656b3051b4b76725d2aa2138a4d50ed4` (PR #479).
Issue: [#474](https://github.com/Kha-kis/manga-arr/issues/474).

## Outcome And Boundaries

Mangarr must continue an owned operation after a qualified client-only remount
changes media `st_dev`, without confusing another object or storage incarnation
with the original. Already-drifted legacy operations need an explicit recovery
path; permanent refusal alone does not resolve the reported defect.

The maintainer requested versioned, per-object proofs plus reviewed legacy
recovery. This proposal preserves strict live checks, controlled birth, journal
barriers and no-clobber behavior. It does not relax global fingerprint equality,
rewrite historical markers, or treat an inode, matching content or mount path
as sufficient ownership evidence.

PR #477 preserves allocation diagnostics. PR #479 closes the disposed chapter
file pin before carrier removal. Both remain separate corrections; neither
establishes remount continuity or authorizes historical directory cleanup.

Non-goals: provider/metadata work, generic storage migration, automatic snapshot
recovery, remote mount management, additional capabilities, host policy changes,
global garbage collection, or HTTP API changes. This is not one large product
patch: implementation must be split into independently reviewable safety steps.

## Current Behavior And Reported Case

`private_file_claim.py` persists namespace v1/v2 parent/root device and inode,
marker full fingerprint, and a token-bound payload. Namespace v2 additionally
records historical exclusive-parent controlled birth. Carrier v1 independently
records its directory, marker, artifact and optional restore link receipt.
`FullFileFingerprint` includes device, inode, size, nanosecond mtime and SHA-256.

`NamespaceHandle.verify()` compares persisted device identity with both held
descriptors and current names. A fresh process or guard cannot repair that
persisted mismatch. Adapting only this check leaves independent carrier,
artifact, link, pack and queue-origin comparisons unable to authorize replay.

The reporter describes Synology DSM 7.4.1/Btrfs, NFSv4.1/TCP hard mounts,
`sec=sys`, and stable inodes but different devices across client nodes. Returning
to the original node restores its device value. Their prepared operation and
original evidence are preserved. These are reporter observations, not our
independent Synology qualification. The actual operation inventory must be
reviewed before prescribing recovery; the report alone does not prove no stage
or carrier exists.

## Considered Approaches

| Approach | Decision |
| --- | --- |
| Ignore persisted device, compare inode/token/hash | Reject: copied markers, snapshots and independent inode namespaces can agree; changes deletion authority. |
| Provision only an idle replacement namespace | Useful recovery component, but insufficient for active operations and future remounts. |
| Versioned per-object native identity, qualified storage contract, explicit legacy recovery | Recommended: preserves old evidence and distinguishes object identity, content and authority. Requires platform qualification and coordinated consumer support. |

## Trust Contract

The existing local DB/configuration and its live `FileMutationGuard` remain the
authority for cooperating Mangarr actors. Independent DBs, hostile same-UID
tampering and malicious storage administrators are outside this boundary.
The guard is not a distributed NAS lock. Never relax its local device/inode,
process, thread, descriptor or sidecar checks to accommodate media remounts.

An enrolled storage authority identifies an administrator-declared export and
storage incarnation under a qualified encoder profile. Its local identifier
is a scope, not a cryptographic identity or a NAS UUID inferred from `statfs`.
Server address, export path, mount ID and `f_fsid` are not sufficient proofs.

Automatic continuity is supported only while that declared storage incarnation
and qualified server/export/encoder contract remain unchanged. Replacement,
snapshot restore, file-level restore, export reconfiguration or an unqualified
server transition requires explicit recovery. Observable mismatches refuse.
An identity-preserving clone/time rollback may be indistinguishable through
client handles; the contract must require the operator to suspend mutation
before such storage administration and requalify/recover afterward. Do not claim
that this design detects an indistinguishable restore or authenticates a
`sec=sys` server. If this contract cannot be accepted for a deployment, retain
exact legacy checks and report remount continuity as unsupported there.

## Proposed Proof Model

Use a new, strictly versioned identity envelope alongside the original serialized
proof. The old proof bytes and their meaning remain immutable. The envelope
binds their exact digest, operation/domain/file/purpose, canonical object role
and path relationship, storage authority/incarnation, encoder profile, native
handle type, exact length and complete opaque bytes. References must not be
resolved by path alone or by a loose device-to-device mapping.

The native backend uses descriptor-bound `name_to_handle_at` comparison. It does
not open objects by handle, require `CAP_DAC_READ_SEARCH`, or request privilege
or seccomp/AppArmor changes. Keep this behind a small internal backend with
strict parsers and bounded buffers. Missing symbols/export support, permission
denial, incompatible ABI, oversized handles, malformed lengths and exhausted
size negotiation are explicit unsupported/error outcomes. Never truncate or
fall back to inode-only identity, generic FID encoding or weaker flags.

API success is not encoder qualification. The profile must identify the native
filesystem/exporter behavior tested, handle limits, supported client/server
versions and transition boundaries. Linux NFS `f_fsid` can be zero; Btrfs's
subvolume-aware handle is stronger than inode equality but not universal
storage-lineage authentication. Qualification must test the deployed container
security context rather than infer permission from upstream defaults.

Persist identity evidence in a local, append-only proof-set ledger, linked to
the exact existing journal/registry serialization and revision. A proof set has
an enrollment origin (controlled birth, exact legacy enrollment, or explicit
operator recovery), complete role manifest, profile/authority and immutable
evidence. Separate committed activation events select a complete proof set;
an incomplete intent never grants authority. Schema names and wire keys belong
in the implementation plan, but these activation and immutability contracts do
not. Do not insert native fields into strict legacy JSON or make every old
`FullFileFingerprint` silently remount-aware.

For an enrolled object, cross-session authorization requires native identity
equality within its authority/profile and all applicable non-device legacy
constraints: token/binding, role, path relationship, kind, UID/mode/link policy,
size, mtime, digest and phase. Historical device/inode fields stay available as
evidence, not rewritten to the current session. Current held-FD/name comparisons
remain exact using current device/inode throughout each action. Native identity
must be captured/checked around content reads and mutation boundaries; it never
substitutes for a content fingerprint or transaction intent.

An unqualified or incompletely enrolled operation continues using unchanged
legacy exact equality. No mixed proof set can authorize only the convenient
parts of a destructive action. Distinguish unsupported capability, identity
mismatch, content mismatch, invalid proof and incomplete recovery in diagnostics.

## Required Consumer Coverage

| Surface | Proofs that must remain independently authoritative |
| --- | --- |
| Namespace registry and purge | Parent, namespace root, owner marker, creation boundary; exact registry snapshot/CAS used by purge. |
| File carriers | Directory, marker, captured artifact, origin relationship, capture/restore receipts and allocated-to-discarded phase barriers. |
| Rescan enrichment | Source, stage, captured source, publication and acknowledgement receipts; active-operation reconciliation fence. |
| Download/import publication | Prepared source/final/stage, final/source carriers, public link receipts, publication plans stored in queue metadata. |
| Volume deletion | Requested file proof, capture carrier, deletion journal and restoration/cleanup authority. |
| Generated packs | Directory proofs, full file inventory, placement carriers, reservations, retained ownership, tombstones and queue-source origin receipts. |

The implementation must inventory exact columns and embedded JSON consumers
before enabling a profile. Terminal rows still count when retained cleanup or
queue origins can dereference them. Unknown versions, duplicate JSON keys,
malformed references, nested mount crossings or unbounded inventory refuse
activation. A root handle never enrolls all descendants implicitly.

## Enrollment And Crash Consistency

For new objects, retain today's controlled-birth prerequisites. Record a durable
creation intent under the local guard; create using pinned descriptors and
no-clobber operations; capture native and content evidence for every role;
fsync the required object/parent; then commit a complete proof set and activate
it with an exact journal/registry CAS. Newly created files acquire their own
proofs at the existing action barrier before subsequent destructive authority.

For pre-drift legacy objects, enrollment is permitted only while the complete
applicable old proof verifies exactly. Retain v1/v2 birth history unchanged;
enrollment is not a retroactive v2 exclusive-parent receipt. Active-operation
enrollment requires the entire referenced set and phase-specific present/absent
objects, not just currently existing paths. If this cannot be bounded and
proven, leave that operation legacy and require explicit recovery after drift.

All filesystem work occurs outside SQLite write transactions. Hold the existing
mutation guard across the operation, use short intent/activation transactions,
and revalidate descriptors and exact row revisions around each commit. Partial
enrollment is inactive. A crash after intent or fsync must retain evidence and
fences; replay may finish only an exactly proven intended activation. No blind
retry, cleanup-on-exception deletion or startup enrollment of lookalikes.

Each subsequent journal phase transition must bind its new serialization to a
new immutable ledger revision in the same short activation transaction. Carry
forward an object's proof only after revalidation; enroll newly created objects
at their own action barrier. A previous phase's digest is not a wildcard for
later journal contents. Commit uncertainty requires reloading durable authority,
as current settlement does, rather than trusting an in-memory receipt.

Old application versions cannot understand new activation/recovery authority.
Require a minimum-writer schema gate before enabling it; reject an older writer
against that database rather than allowing legacy replay. Matching pre-upgrade
DB/config/key backup plus old image remains the downgrade path. Do not migrate
a newer DB backward or restore its DB independently of media recovery state.

## Already-Drifted Legacy Recovery

No historical native handles exist for these operations. Reading handles now
cannot prove past continuity. Recovery is a new, explicit operator authority,
not automatic revalidation and not a way to make old receipts pass.

Provide a local administrative workflow, not an automatic web/startup repair:

1. **Inspect:** under the same exclusive guard, generate a private, bounded,
   read-only inventory across every domain referencing the affected namespace.
   Include exact journal/registry bytes and revisions, phases, diagnostics,
   canonical paths, marker/artifact presence, current identities and full
   content evidence. Never expose private paths or tokens in public issue logs.
2. **Review:** produce an immutable plan bound to that inventory digest and
   storage declaration. Identify each retained object, proposed disposition,
   affected series and remaining uncertainty. The operator must identify the
   intended original storage/incarnation and select explicit actions. Unknown
   children, incomplete references or unreadable evidence block that scope.
3. **Apply:** require the exact reviewed plan digest, stopped background actors,
   the same local guard and a matching backup/recovery receipt. Re-inventory;
   any changed row/object/phase invalidates the plan. Record durable intent
   before filesystem or journal changes and a committed completion event after
   all required durability checks. Restart resumes only that approved intent.
4. **Verify:** check retained originals, new journal authority, references,
   observations and fence state. Retain the original evidence, plan and result.
   Clear only the resolved operation's fence after completion; never clear
   unrelated blockers or mark an archive downloaded merely because recovery
   was acknowledged.

The recovery state machine is separate from legacy replay. The following
dispositions define what may become authoritative; each requires the complete
reviewed inventory and fresh per-object recovery proofs, not just a phase label:

| Observed state | Public/private disposition | DB result and fence release |
| --- | --- | --- |
| Prepared; no mutation proven | Keep original; no capture/publication/undisposed stage may be inferred absent solely from a missing row. | Supersede/cancel attempt, no downloaded-state or metadata change. Release only after complete no-mutation inventory and supersession activate atomically. |
| Original captured; publication not committed | Restore through a no-clobber action when destination is absent; retain original/stage until restoration has its own durable recovery receipt. | Record recovered rollback only after restoration and required durability; preserve original historical rows and unrelated observations. |
| Acknowledged publication, DB not committed | Operator selects resumed commit only with all original commit preconditions/CAS intact, or reviewed rollback. Keep originals until that choice completes. | A new recovery receipt may authorize the selected commit, not invent a historical acknowledgement; any stale series/volume context requires a new plan. |
| Unacknowledged public/restore link, including same-inode occupied destination | Never adopt it through EEXIST or fabricate the missing old receipt. Offer explicit keep-as-current-file selection, or stop for a separately reviewed removal/restore plan. Preserve private originals and the occupied public path. | Keep-as-current supersedes the interrupted attempt, then performs an explicit current-file observation/adoption with fresh evidence. It does not label old publication/rollback successful. Fence remains until the selected disposition and every retained artifact's non-replayable retention record are durable. |
| DB-committed publication and matching current publication under reviewed recovery authority | Retain public publication; authorize only individually listed private cleanup or explicit retained-artifact disposition. Do not restore the old source over it. | Preserve committed volume/library state. Complete only after publication and DB consistency checks, cleanup/retention receipts and exact CAS; unknown leftovers keep the fence. |
| Missing/changed committed publication, conflicting objects or uncertain lineage | No automatic settlement or destructive cleanup. Inventory is retained for an explicit restore/adoption plan; no blanket 'complete' or 'rolled back'. | Remain blocked until a new plan explicitly reconciles the current file and DB observation without rewriting historical success or unrelated local observations. |

An acknowledged object that fails its full proof is not downgraded into the
unacknowledged-link shortcut. For generated packs or deletion, use the matching
domain's commit/restore prerequisites and reference inventory, not rescan phase
names. A retained artifact's disposition names its exact bytes, private path,
owner and future handling; retention never schedules automatic deletion.

Supported dispositions must be explicit, not a generic 'trust this directory':

- **Prepared, no mutation proven:** when the complete inventory and journal
  establish no capture/publication and no owned stage requiring disposition,
  supersede/cancel that attempt without mutating its original archive. Record
  the cancellation authority; a new attempt requires a proven new allocation
  epoch and a newly verified current source, not replay of the old attempt.
- **Known retained artifacts:** the plan may establish new authority for each
  individually reviewed original/stage, preserving historical records. Select
  restoration or resumed publication through the existing phase-aware,
  no-clobber machinery. Fresh recovery proofs authorize only the plan's listed
  actions; they must not make the old journal generally valid. Conflict,
  missing prerequisite or an occupied destination (including same-inode
  EEXIST) retains the artifact and fence for a revised plan.
- **Uncertain lineage or contents:** preserve everything and refuse mutation.
  Offer a complete inventory for manual restoration/reprovisioning and another
  reviewed plan; do not acknowledge success, erase a journal or delete evidence.

Where a new epoch is needed, retain the old namespace at its original path and
make it ineligible for new allocations. Provision a distinct empty namespace
under a proven app-controlled allocation parent, with explicit epoch references
for all new operations. Do not chmod/chown shared Arr directories, adopt unknown
children, rename an unproven old root, or silently bypass controlled birth.

The current carrier protocol requires origin and namespace to share a parent.
Keep that rule unchanged for legacy proofs. For a new epoch whose current
shared origin parent cannot supply controlled birth, the new protocol explicitly
supports a separately proven, operator-provisioned private allocation parent on
the SAME filesystem and storage authority as the origin. Its carrier binding
must name both the origin-parent proof and allocation-parent/epoch proof, not
infer either from the other. Provision the dedicated 0700 allocation parent
with a reviewed controlled-birth receipt; then create its empty namespace under
that exclusive boundary. This changes geometry, not permission to capture an
unowned public source.

Capture must verify the pinned source-parent FD against the independently
bound origin parent and exact source name; carrier allocation verifies the
separate allocation parent. Restore/publication uses the explicitly bound
destination parent, never implicitly `namespace.parent_fd`. Keep current
same-filesystem device checks plus qualified authority/profile checks before
rename/link, and revalidate both held-FD/name relationships at every boundary.
No copy-and-delete fallback across devices/exports is permitted. All consumers
in the coverage table must support this new binding before such an epoch can
activate. Geometry substitution, mismatched origin parent, cross-filesystem
placement and replacement of either parent are mandatory refusal tests.

The canonical epoch path and both parent bindings must be versioned and stored;
neither the legacy fixed `_ROOT` path nor the registry's single parent-path key
can silently select a new layout. Old journals keep their original root. Exact
schema/path layout is a plan detail, but preserving explicit source geometry
and same-filesystem mutation is an activation prerequisite, not an optional
follow-up after shared-parent recovery ships.

Superseding evidence must be checked by every consumer, including terminal GC,
purge and queue origins, before releasing a fence or switching epochs. Retained
artifacts are never future GC authority merely because a recovery completed.
Recovery rollback requires the matching DB/config and media disposition record;
it is not implemented by deleting the new ledger event.

## Qualification And Acceptance

Tests are written first. Keep simulated device-drift tests distinct from native
filesystem results. The minimum acceptance matrix is:

| Area | Required evidence |
| --- | --- |
| Existing semantics | Legacy exact same-device behavior; all refusal, hostile-other-UID, controlled-birth, local guard and PR #479 lifecycle tests unchanged. |
| Continuity | Fresh process, client-only remount and cross-node transition with demonstrated changed device and stable enrolled identities for every proof role; same-node restart. |
| Substitution | Different export/subvolume, sibling Btrfs snapshot, copied marker/inventory, same-content replacement, inode reuse, symlink and held-FD/name replacement reject. |
| Freshness | Same native handle with changed bytes/mtime/size, owner/mode/link changes, wrong binding/role or phase reject. |
| Backend | Missing syscall/symbol, denied call, generic fallback, unsupported exporter, oversized/invalid handle, no-growth EOVERFLOW and incompatible profile reject safely. |
| Protocol | Every capture/restore/discard/publication barrier, all pack/reference domains, partial enrollment, stale CAS and concurrent actor. Fault/crash after each new intent/fsync/activation/recovery step. |
| Legacy recovery | Real prepared-without-stage case; captured original; staged/published/db-committed cases; occupied destination; unknown artifacts; changed review inventory; restart mid-apply; retained evidence and per-operation fences. |
| Compatibility | Fresh DB, upgrade, mixed legacy/enrolled objects, unsupported profile, older-writer refusal and matching-backup rollback. No independent DB-only rollback claim. |

The target qualification is the reporter's NFSv4.1/TCP hard/sec=sys
Synology/Btrfs profile, plus supported local-storage behavior and release ARM64
coverage. Capture exact client kernel/container/server versions, exporter,
mount options and security settings. Native server restart and export changes
must demonstrate the stated boundary, not be assumed equivalent to a client
remount. No qualified native profile exists merely because this spec is approved.

Our disposable Ganesha fixture previously hit raw-control EPERM before Mangarr
cases. A later logging-only attempt stopped before its client ran. Neither
qualifies native NFS, hard mounts or Synology. Resolve or replace that fixture
through a separately reviewed probe; never add privileges just to obtain green
results. Contributor testing must use a reviewed exact build and disposable
data first; request no experiments on the preserved production operation yet.

Each behavioral PR requires focused tests, actual Python LSP review, `make test`
and `make test-release-safe`, plus independent safety review. The new immutable
candidate requires its own complete source/security/image, install,
upgrade/matching rollback, download-import/recovery, platform and soak evidence.
No RC10 qualification transfers. Missing platform coverage remains visible and
requires coverage or a maintainer-reviewed support/release decision, not a
silent waiver.

## Proposed Review Boundaries

1. Backend/strict serialization and disabled enrollment ledger; no changed
   legacy authorization or automatically enabled deployment profile.
2. Controlled enrollment and complete consumer integration, phase/rollback
   tests and minimum-writer gate. Do not enable automatic continuity until the
   whole proof chain passes qualification.
3. Administrative legacy inventory/review/apply, explicit epoch provisioning
   and resumable recovery, independently tested before asking the reporter to
   use it. Keep read-only inspection separable from mutation authority.
4. Qualified profile enablement and exact-build contributor procedure, then a
   fresh candidate and release qualification.

These are design boundaries, not an approved execution plan. The implementation
plan must size them honestly; if a boundary cannot remain reviewable, split it
without shipping partially supported authorization as a complete #474 fix.

## Approval Boundary

The maintainer approved this written specification on 2026-10-20, including the
automatic-continuity trust contract, append-only enrollment model, full consumer
coverage and explicit legacy recovery dispositions. This permits writing the
detailed implementation plan, not product implementation or use of the recovery
workflow. Storage-profile qualification and the concrete reporter's operation
plan remain separately evidenced decisions.

## Primary References

- [Linux handle API](https://man7.org/linux/man-pages/man2/open_by_handle_at.2.html)
- [Linux 6.12 NFS native encoder](https://github.com/torvalds/linux/blob/v6.12/fs/nfs/export.c)
- [Linux 6.12 exportfs fallback](https://github.com/torvalds/linux/blob/v6.12/fs/exportfs/expfs.c)
- [Linux 6.12 Btrfs native encoder](https://github.com/torvalds/linux/blob/v6.12/fs/btrfs/export.c)
- [NFSv4.1 filehandle contracts](https://www.rfc-editor.org/rfc/rfc8881.html#section-4)
- [Btrfs subvolume inode namespaces](https://btrfs.readthedocs.io/en/latest/btrfs-subvolume.html#inode-numbers)

These contracts motivate the design; they are not results from the reporter's
server or a substitute for the acceptance matrix.

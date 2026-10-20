# Remount-Safe Ownership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve #474 with qualified per-object remount continuity and explicit, crash-safe recovery for already-drifted operations, then qualify and release 1.3.3 without weakening ownership or recovery fences.

**Architecture:** Preserve legacy proof bytes and exact comparison behavior. Add a native-identity backend, an append-only local proof ledger and explicit per-owner contexts; integrate every existing consumer before enabling continuity. A separate offline administrative workflow authorizes named legacy recovery actions, never inferred historical ownership.

**Tech Stack:** Existing Python 3.11-3.14 support, Linux/glibc via standard-library `ctypes`, SQLite, existing FastAPI lifespan/operator CLI, pytest and current Docker/browser/release tooling. No new dependencies or provider/API changes.

**Spec:** [Approved ownership/remount design](../specs/2026-10-20-remount-ownership-design.md), approved after commit `ccd82bfeb6b9f6b098f5a878fab47748110fc4c8`.

## Global Constraints

- "Never relax its local device/inode, process, thread, descriptor or sidecar checks to accommodate media remounts."
- "The old proof bytes and their meaning remain immutable."
- "No mixed proof set can authorize only the convenient parts of a destructive action."
- "All filesystem work occurs outside SQLite write transactions."
- "No copy-and-delete fallback across devices/exports is permitted."
- "No RC10 qualification transfers."
- Linux-specific identity is optional; unsupported deployments retain exact legacy behavior, not weaker fallback. The reported NAS profile must be qualified before claiming its fix.
- Python implementation and review use PythonPro workers with actual LSP navigation/diagnostics. Preserve Kha-kis noreply identity; do not change the main checkout or shared services.
- Tests first; every behavior PR runs focused tests, `make test`, `make test-release-safe`, independent review and required CI. No merge/qualification claim from test doubles alone.
- This plan is proposed execution, not permission to operate on the preserved reporter operation or production. Record exact heads, trees, RED/GREEN receipts and cleanup ownership per PR.

## Review Focus

1. An old writer against an enrolled DB must refuse before mutation; an already-running pre-upgrade writer must be stopped during upgrade (Tasks 2, 3).
2. A superseded prepared row must not block the next attempt through a partial UNIQUE index, yet unresolved neighbors must stay fenced (Tasks 5, 9).
3. A pack's retained terminal queue-origin proof can remain authoritative after its reservation changes; mismatched or incomplete chains must refuse (Tasks 4, 7).
4. A public hardlink created before its acknowledgement is not ownership proof, even with the same inode/content (Task 9).
5. A namespace in a new dedicated parent must not redirect capture/publication to that parent or silently cross exports (Tasks 5, 8).

## Delivery And Approval Gates

Tasks are ordered review boundaries, not one large PR. Tasks 1-4 may merge as disabled foundations. Tasks 5-7 integrate one domain at a time, with production continuity still disabled. Task 8 adds new-epoch geometry. Task 9 supplies explicit recovery; Task 10 proves and enables a qualified profile. Tasks 11-12 ship and qualify a new artifact. Split an oversized task further rather than merging partially enabled authorization.

No native profile is enabled initially. Test injection uses pytest monkeypatching of an internal registry, not an environment/config escape hatch. Unsupported old operations stay legacy. Do not close #474 merely because disabled foundations or local simulations pass.

## Files And Current Proof Inventory

New modules have distinct responsibilities:

| File | Responsibility |
| --- | --- |
| `app/file_identity.py` | Strict native-handle/profile/role types, bounded syscall adapter and live object comparison. |
| `app/file_identity_store.py` | SQL-only immutable ledger, owner snapshots, activation CAS and durable readback. |
| `app/file_identity_consumers.py` | Allowlisted domain adapters and complete reference/role inventory. No mutation authority from parsing alone. |
| `app/file_identity_claims.py` | Enrolled namespace/carrier contexts and explicit epoch/origin geometry; delegates unchanged legacy policy. |
| `app/file_identity_recovery.py` | Inventory/plan validation, phase-specific recovery state machine and private recovery receipts. |
| `app/file_identity_cli.py` | Ownership subcommands registered in existing `app/cli.py`; safe private report I/O. |
| `app/maintenance_lock.py` | Shared application lifetime / exclusive recovery exclusion, separate from the existing mutation lock. |
| `tests/python/file_identity_helpers.py` | Disposable fixture-only profiles, independent native-identity double and media-device drift proxy. |

Modify only the relevant functions in existing modules; no monolith extraction. Source baseline is PR479 merge `c4d6b039656b3051b4b76725d2aa2138a4d50ed4`. The following is the minimum static adapter inventory:

| Domain | Persisted inputs and consumers |
| --- | --- |
| namespace | `file_claim_namespaces(parent_path, ownership_json)`; `private_file_claim.ensure_namespace/open_namespace`; purge namespace snapshots in `routers/series_.py`. NULL ownership is unresolved intent. |
| rescan | `rescan_file_operations`: token, paths, expected volume/context, `fingerprints_json`, `carriers_json`, `publication_receipt_json`, state; `_store/_allocate/_open/_commit/_settle` and replay/fence selectors in `rescan_file_recovery.py`. |
| publication | `import_publications`: owner/state/staging/destination/queue and series snapshots; `import_publication_files`: source/staged/prepared-final fingerprint columns, claim paths/carrier JSON, publish/cleanup states. Nested queue snapshot keys `_file_publication`, `_publication_staging`, `_pack_source_origins`; initialization, staging, compensation, commit and settlement in `import_publication.py`/`import_staging.py`. |
| deletion | `volume_file_deletions`: exact target fingerprint, paths/state and `claim_carrier_json`; preparation, capture, restore, completion and replay in `volume_file_deletion.py`. |
| pack reservation | `import_pack_cleanup_reservations`: identity/client/protocol/owner/queue/publication/path/purpose/expiry plus `directory_ownership_json`; strict `PackOwnership` v1-v3, directory claims, inventories, placement carrier and queue receipt. |
| pack tombstone | `import_pack_cleanup_tombstones`: composite download/queue/publication/path identity and `carrier_json` containing `PackDirectoryClaim`; cleanup/restart in `import_pack_cleanup.py`. |
| queue source | `import_queue`/`import_queue_files` plus frozen publication `queue_snapshot_json._pack_source_origins`; `private_pack_source.py`, publication and staging admission/delegation checks. |

Read all states, including retained/terminal references, when building closure. Also snapshot relevant `series`/`volumes`, queue lease and `chapter_quarantine` blockers for CAS/eligibility. Do not enroll or clear quarantine's independent proof/fences in this change. Unknown references/versions fail inventory rather than being ignored.

## Shared Data Contracts

All new JSON uses version 1, duplicate-key rejection, exact keys, canonical compact UTF-8 JSON and SHA-256; no NaN/Infinity or bool-as-int. Preserve old embedded JSON as its exact string, not a parsed/re-encoded substitute. These are internal interfaces, not public API additions.

```text
file_identity.py:
  NativeHandle(handle_type: int, handle_bytes: bytes)  # signed int32 type, 1..128 bytes
    .to_value() -> dict[str, object]
    .from_value(value: object) -> NativeHandle
    .from_json(encoded: str) -> NativeHandle  # duplicate-key rejecting decode
  StorageProfile(profile_id: str, revision: int, qualification_sha256: str,
                 client_contract: dict[str, object], server_contract: dict[str, object])
  OwnerRef(domain: str, key: tuple[str | int, ...])  # domain allowlist above
  RoleKey(file_id: int | None, purpose: str, kind: str, relative: str | None)
  FileIdentityError; IdentityUnavailable; IdentityMismatch; InvalidIdentity
  capture_native(fd: int) -> NativeHandle
  validate_profile(profile: StorageProfile) -> None

file_identity_store.py:
  OwnerSnapshot(owner: OwnerRef, rows_json: str, sha256: str)
  ObjectProof(role: RoleKey, canonical_path: str, parent_role: RoleKey | None,
              native: NativeHandle, constraints_json: str)
  ProofSet(set_id: str, owner: OwnerRef, snapshot: OwnerSnapshot,
           authority_id: str, epoch_id: str, origin: str,
           roles: tuple[ObjectProof, ...], manifest_json: str)
  ActiveProofSet(proof: ProofSet, event_id: int)  # obtained only by validated readback
  prepare_set(db: sqlite3.Connection, proof: ProofSet, expected_event_id: int | None) -> int
  assert_snapshot(db: sqlite3.Connection, expected: OwnerSnapshot) -> None
  activate_set(db: sqlite3.Connection, proof: ProofSet,
               expected: OwnerSnapshot, expected_event_id: int) -> int
  read_active(db: sqlite3.Connection, owner: OwnerRef) -> ActiveProofSet | None

file_identity_consumers.py:
  InventoryBudget(max_rows: int, max_objects: int, max_hash_bytes: int)
  RoleSpec(role: RoleKey, path: str, parent_role: RoleKey | None,
           legacy_json: str | None, presence: str, policy: str)
  ReferenceClosure(snapshots: tuple[OwnerSnapshot, ...], roles: tuple[RoleSpec, ...])
  snapshot_owner(db: sqlite3.Connection, owner: OwnerRef) -> OwnerSnapshot
  collect_references(db: sqlite3.Connection, parent: str,
                     budget: InventoryBudget) -> ReferenceClosure
  roles_for(snapshot: OwnerSnapshot) -> tuple[RoleSpec, ...]

file_identity_recovery.py:
  ObservedRole(spec: RoleSpec, native: NativeHandle | None,
               constraints_json: str, outcome: str)
  OwnershipInventory(version: int, parent: str, closure: ReferenceClosure,
                     observed: tuple[ObservedRole, ...], unresolved: tuple[str, ...],
                     budget: InventoryBudget, sha256: str)
  RecoveryDecision(owner: OwnerRef, action: str, roles: tuple[RoleKey, ...],
                   destination: str | None, retained_roles: tuple[RoleKey, ...])
  RecoveryPlan(version: int, inventory: OwnershipInventory,
               decisions: tuple[RecoveryDecision, ...], declaration_json: str,
               backup_receipt_json: str, epoch_bindings_json: str, sha256: str)
  RecoveryResult(plan_sha256: str, event_ids: tuple[int, ...],
                 dispositions: tuple[RecoveryDecision, ...],
                 retained: tuple[ObservedRole, ...], unresolved: tuple[str, ...])

file_identity_claims.py:
  IdentityContext(active: ActiveProofSet, guard: FileMutationGuard)
  VerifiedRole(fd: int, parent_fd: int, name: str, role: RoleKey)  # borrowed FDs
  resolve_context(db: sqlite3.Connection, guard: FileMutationGuard,
                  owner: OwnerRef) -> IdentityContext | None
  verify_role(context: IdentityContext, role: RoleKey,
              fd: int, parent_fd: int, name: str) -> VerifiedRole
  verify_live(role: VerifiedRole, context: IdentityContext) -> None
```

`NativeHandle` wire keys are exactly `version`, `backend`, `handle_type`, `handle_size`, `handle_hex`: version 1, backend `linux-exportfs`, size equal to the decoded bytes, lowercase hexadecimal. Owner domains are the inventory table's explicit domains; keys use canonical JSON arrays of the existing primary-key components. Strict role enums distinguish regular files, directories and markers; policy parsers enforce the old per-role rules. `constraints_json` binds legacy proof digest/binding, file kind, applicable UID/mode/link rules, and size/mtime/SHA-256 where the old role requires them. Directory policies do not freeze mutable directory size/mtime/link count unless existing semantics require it. Missing/pending roles carry explicit phase-specific absence evidence in the manifest, never a fabricated handle. Every manifest names its exact dependency snapshots; activation rejects incomplete closure.

Profile client-contract keys are `platforms` (records with exactly `arch`, `kernel_release`, `libc_version`, `encoder_revision`) and `mount` (exactly `filesystem`, `nfs_version`, `transport`, `security`, `hard`). The target candidate uses NFS version `4.1`, transport `tcp`, security `sys`, hard `true`; exact platform/server values come from native evidence, not guessed compatibility ranges. Server-contract keys are `vendor`, `version`, `filesystem`, `exporter`, `handle_contract`, `allowed_transitions` and `recovery_transitions`; the declaration supplies trusted administrator assertions where the client cannot measure them. Validation requires equality with an independently qualified, source-registered version/digest, not just syntactically valid operator JSON. `capture_native` may return a raw observation for qualification; only a complete validated active proof set can grant application authority.

Backend errors remain internal. Claim entry points translate them into existing `PrivateClaimError`/domain blocked outcomes with the original categorized cause retained; test this boundary so a new exception does not bypass existing retention/fence handling. `verify_role` itself raises the typed identity error for its direct tests. Ownership CLI catches these errors in its own dispatch branch and emits only bounded sanitized outcome codes, not raw exception paths/tokens; existing unrelated CLI behavior stays unchanged.

Inventory digests cover the canonical payload excluding its own `sha256`; plan digests cover the complete embedded inventory, decisions, declarations, backup receipt and epoch bindings, likewise excluding their own digest. Event timestamps are not an authorization freshness shortcut. Current object/row equality and exact predecessor CAS decide whether a plan is stale. JSON dictionaries shown in profile/constraint interfaces require exact per-version parsers; they are not extension bags that silently accept new fields.

For a phase transition, capture/revalidate required FD/native/content evidence outside the SQL writer. Inside one short writer: `assert_snapshot` checks the pre-transition domain rows, execute the existing domain-specific SQL/CAS, obtain the post-write `snapshot_owner`, bind its exact serialization to the already observed role evidence, then `prepare_set` and `activate_set` with that post-write snapshot and the prepared event ID. Both ledger events and domain changes commit together; `activate_set` itself never writes arbitrary business rows. A prior durable action/creation intent must already precede any filesystem mutation. Activation cannot bless a filesystem action that lacked that intent. Pure serialization/hashing of already captured metadata is allowed inside this writer; stat/open/hash-file/fsync are not. If preparation must span transactions, its event grants no authority and activation still rechecks the exact post-write snapshot and predecessor under the final writer. Test both commit-success and commit-uncertain paths by reloading, never by trusting in-memory state.

Add nullable keyword-only `identity: IdentityContext | None = None` to relevant claim open/allocate functions. Default `None` executes the original legacy path. An enrolled context must verify the entire active set and cannot fall back to that path after mismatch. Keep `FullFileFingerprint` dataclass equality unchanged.

## Verification Harness For Every Behavior Task

Use the existing venv and test architecture; do not install a new toolchain. At task start create a uniquely owned 0700 scratch on `/tmp`, set `umask 0022`, `PYTHONDONTWRITEBYTECODE=1`, `PATH=/opt/manga-arr/.venv/bin:/home/khak1s/.local/bin:$PATH`, private `TMPDIR`/pytest `--basetemp`, and record source head/hash. Examples below use `$QA_TMP` and `$QA_STATE` set to that task's scratch/private receipt directory. Delete payloads only after processes and mounts are terminal/absent; retain compact logs.

Focused command prefix: `/opt/manga-arr/.venv/bin/python -B -m pytest -p no:cacheprovider --basetemp "$QA_TMP/pytest"`. Run from the isolated repo root, append each named test as `tests/python/NAME.py`, then the stated options. For example, Task 1 RED is `/opt/manga-arr/.venv/bin/python -B -m pytest -p no:cacheprovider --basetemp "$QA_TMP/pytest" tests/python/test_file_identity_backend.py -q`. Use a fresh basetemp per run. New tests belong under `tests/python/`, so repository defaults include them. File globs below are shell-expanded under `tests/python/` only.

At each task's GREEN step run its focused selection, actual BasedPyright LSP and changed-file Ruff correctness/format. Compare existing typing debt before/after; do not claim global zero errors. Before each behavioral PR merge run `make test`, then a fresh `make test-release-safe`, independent safety review and required Fast CI. Stage only owned paths; commit with `Kha-kis <27432909+Kha-kis@users.noreply.github.com>`. Failure artifacts are evidence, not permission for blind retry.

### Task 1: Bounded Native Identity And Strict Types (Disabled)

**Files:** Create `app/file_identity.py`, `tests/python/test_file_identity_backend.py`, `tests/python/file_identity_helpers.py`.
**Interfaces:** Produces the `file_identity.py` types/functions above. No application caller, qualified registry entry or DB change.

- [ ] Write RED tests `test_capture_uses_borrowed_fd_empty_path_without_follow`, `test_capture_preserves_all_handle_bytes`, `test_no_handle_open_or_privilege_request`, and `test_strict_native_json_rejects_aliases`. Assertions: `AT_EMPTY_PATH=0x1000`, no FID/connectable/follow flags, caller FD stays open, exact type/bytes round-trip; duplicate/extra keys, bool type, zero/odd/oversized hex and version !=1 refuse.
  Assertion excerpt: `assert NativeHandle.from_value(NativeHandle(1, b"\x00\xff").to_value()) == NativeHandle(1, b"\x00\xff")`; the duplicate-key path must raise `InvalidIdentity`, never return a handle.
- [ ] Run `test_file_identity_backend.py -q`; retain missing-feature failures. Add parametrized ENOSYS/EOPNOTSUPP/EPERM/EACCES/EINVAL, unchanged-length/oversized EOVERFLOW and libc-symbol absence cases; none returns identity.
- [ ] Implement a fixed 128-byte bound with `ctypes.CDLL(None, use_errno=True)`, explicit ABI signatures, int32 mount-ID output ignored as durable identity, errno captured immediately. No unbounded retry or generic FID fallback. Registry starts empty; profile validation cannot elevate candidate evidence into qualification.
- [ ] Run GREEN focused tests and harness checks. The fixture double gives independent opaque object IDs across copies/replacements and a module-local media-device proxy; never patches the real local guard/SQLite identity. Label every such test simulated.
- [ ] Commit `feat: add bounded native identity primitives` and review as a disabled foundation.

### Task 2: Immutable Ledger, Compatibility Gate And Safe Supersession Columns

**Files:** Create `app/file_identity_store.py`, `tests/python/test_file_identity_store.py`, `tests/python/test_file_identity_schema.py`; modify `app/schema.py` only in initialization/version migration paths.
**Interfaces:** Produces `OwnerSnapshot`, `ObjectProof`, `ProofSet`, `ActiveProofSet`, store functions above. `prepare_set/activate_set` are SQL-only and never call `guard.verify()` or any filesystem function inside a writer.

- [ ] Write RED fresh/v5-upgrade/idempotent-v6 tests preserving byte-identical old JSON; SQL UPDATE/DELETE against immutable rows must fail. Add `test_v5_binary_rejects_v6_before_any_write` using the frozen v5 validator source in an isolated subprocess (no installed/live DB).
  Assertions: `assert after_legacy_json == before_legacy_json`, `assert schema_version == 6`, and `with pytest.raises(sqlite3.IntegrityError): ...` around immutable-row UPDATE/DELETE. Compare all fixture rows before/after rejected old-writer startup.
- [ ] Run `test_file_identity_schema.py test_file_identity_store.py -q`; retain failures for absent tables/gate, duplicate activation, stale row/event CAS and missing roles. Assert rollback leaves no active set after commit failure.
- [ ] Add schema version **6** (if still unused; stop/rebase the plan if another migration claims it). Ensure the existing v5 early return cannot skip v6 and the idempotent v5 migration never resets v6 to 5. Create these local tables with foreign keys/indexes and immutable-row UPDATE/DELETE refusal triggers:

  | Table | Required fields/constraints |
  | --- | --- |
  | `file_identity_authorities` | 32-hex ID, immutable incarnation token, profile JSON, declaration JSON and qualification digest; unique ID. Declaration is operator scope, not remote authentication. |
  | `file_identity_epochs` | 32-hex epoch ID, authority ID, origin/allocation/root paths, exact controlled-birth JSON; immutable. An epoch row alone is not active. |
  | `file_identity_sets` | 32-hex set ID, owner domain/key JSON, prior set ID, snapshot JSON/digest, origin enum `birth/exact_legacy/recovery`, epoch ID, complete manifest JSON/digest; immutable. |
  | `file_identity_events` | integer ID, owner domain/key JSON, monotonically increasing owner revision, predecessor event ID, set ID, kind `prepared/activated/recovery_intent/recovery_step/superseded/retained/retired`, strict payload JSON. UNIQUE(owner domain,key,revision); immutable; validate expected predecessor under writer. |

  Add nullable `recovery_event_id` references to rescan operations and volume-deletion journals. Rebuild only their active-volume UNIQUE/series/keyset indexes with `recovery_event_id IS NULL`; original rows all remain NULL. Non-NULL requires a matching committed supersession event validated by application adapters AND SQL INSERT/UPDATE triggers matching event domain/key/kind, not an arbitrary integer bypass. Test pointing at a valid but foreign event as well as a nonexistent one. No legacy JSON rewrite or automatic enrollment in migration.
- [ ] Implement exact owner snapshot/event CAS and durable active-set readback. Add adversarial tests for wrong domain/key, missing/foreign event, conflicting activations, malformed JSON, and attempts to delete a referenced event. New schema refusal protects stopped/restarted v5 binaries; it does not stop an already-running old process, so upgrade requires stopping it first.
- [ ] Run GREEN plus `test_private_file_claim_schema.py test_rescan_recovery_schema.py test_import_pack_ownership_schema.py`, then full task gates; commit `feat: persist immutable file identity evidence`.

### Task 3: Offline Recovery Exclusion

**Files:** Create `app/maintenance_lock.py`, `tests/python/test_file_identity_maintenance.py`; modify only `app/main.py` lifespan entry/exit.
**Interfaces:** `service_maintenance_guard(db_path: str, *, exclusive: bool) -> ContextManager[None]`; application lifespan uses shared mode, mutating ownership CLI uses exclusive nonblocking mode before taking `file_mutation_guard`.

- [ ] Write RED real subprocess tests: multiple service shared holders coexist; recovery fails while any service holder exists; service fails while recovery holds exclusive; process death releases exclusion but never unlinks the entry. Swapped/symlink/DB-alias/foreign/wrong-mode sidecars refuse without opening the DB inode.
  Assertions: blocked attempts report busy without DB/FS mutation, `assert lock_path.stat().st_size == 0`, and `assert lock_path.stat().st_ino == original_inode` after holder exit and reacquisition.
- [ ] Run `test_file_identity_maintenance.py -q`; retain failures. Existing backup/admin behavior is not automatically changed.
- [ ] Implement separate persistent `.<DB basename>.ownership-maintenance.lock`, 0600/current UID/empty/single-link, local parent validation, CLOEXEC, no explicit unlock/recreate. Reuse existing validation patterns without changing the existing nonreentrant mutation guard. Acquire service shared hold before schema/startup replay; release after all background shutdown. Recovery uses LOCK_EX|LOCK_NB and fails on busy. Do not use PID files, namespace-limited process scans or `--service-stopped` as proof.
- [ ] Run GREEN plus `test_file_mutation_lock*.py` and startup/runtime tests. Test lock ordering (maintenance then mutation then SQL) in two processes and cancellation/shutdown. Old writers remain an offline-upgrade prerequisite as Task 2 states.
- [ ] Run full task gates; commit `feat: exclude live service during ownership recovery`.

### Task 4: Complete Read-Only Reference Inventory

**Files:** Create `app/file_identity_consumers.py`, `app/file_identity_recovery.py` (inventory types/functions only), `app/file_identity_cli.py` (inspect/plan only), `tests/python/test_file_identity_inventory.py`, `tests/python/test_file_identity_cli.py`; modify `app/cli.py` parser/dispatch only.
**Interfaces:** Produces reference functions above plus `inspect_ownership(guard: FileMutationGuard, parent: str, budget: InventoryBudget) -> OwnershipInventory`; `OwnershipInventory` contains version, closure, observed roles, unresolved entries, limits and digest. `build_recovery_plan(inventory: OwnershipInventory, decisions_json: str, declaration_json: str, backup_receipt_json: str) -> RecoveryPlan` is pure and performs no filesystem/DB mutation.

- [ ] Write RED representative real-row fixtures for every inventory domain, including terminal tombstones, nested `_pack_source_origins`, inconsistent duplicate publication carrier copies, cross-series references and NULL namespace intent. Assert complete closure/digest and refusal on unknown/malformed/unbounded data; do not infer absence from a missing carrier column.
  Assertions: `assert inventory.unresolved == ()` only for complete fixtures; malformed/over-budget fixtures satisfy `assert inventory.unresolved` and are rejected by `build_recovery_plan(...)`. `assert after_db_rows == before_db_rows` and `assert after_media_bytes == before_media_bytes` hold in both cases.
- [ ] Run `test_file_identity_inventory.py test_file_identity_cli.py -q`; compare all DB rows and media bytes before/after inspect/plan. Creating the existing local guard sidecar and exclusively creating the requested private report are the only documented control-file effects.
- [ ] Implement the static adapter list in this plan, not ad hoc recursive JSON discovery or dynamic SQL from input. Keyset-page rows, strictly parse supported versions/roles, enforce no-follow traversal and mount boundaries, hash regular files in 1 MiB chunks with before/after FD/name checks. Default budgets: 1,000 scanned reference rows, 10,000 objects, 64 GiB total content; overflow yields incomplete/refused output, never a partial success. Operators may explicitly raise numeric budgets for a fresh inventory; no automatic retry. Bound report/plan JSON to 16 MiB; do not claim a deadline can interrupt blocked hard-mount I/O.
- [ ] Register `mangarr ownership inspect --parent PATH --output FILE` and `ownership plan --inventory FILE --decisions FILE --storage-declaration FILE --backup-receipt FILE --output FILE`. Private files are current-UID 0600 regular single-link, no-follow, new outputs O_EXCL; stdout prints only digest/count/outcome, no paths/tokens/content. Plan actions are `cancel-unmutated`, `restore-original`, `resume-commit`, `keep-current`, `cleanup-committed`, `retain-blocked`, plus explicit new-epoch provisioning. Refuse unspecified object dispositions.
- [ ] Run GREEN plus `test_operator_cli.py test_private_pack_source.py test_purge_namespace_preservation.py`; full task gates; commit `feat: inventory ownership recovery without mutation`.

### Task 5: Complete Native Claim Context And Rescan Enrollment

**Files:** Create `app/file_identity_claims.py`, `tests/python/test_file_identity_claims.py`, `tests/python/test_file_identity_rescan.py`; modify scoped claim open/verify/action functions in `private_file_claim.py`, and rescan `_store/_allocate/_open/_commit/_settle`/fence/replay functions in `rescan_file_recovery.py`.
**Interfaces:** Produces context/role interfaces above; `enroll_exact_legacy(guard: FileMutationGuard, closure: ReferenceClosure, authority_id: str) -> tuple[int, ...]` in `file_identity_claims.py`. Caller must already hold maintenance exclusive mode for administrative enrollment. New controlled births enroll through the journal action barriers; no startup scanning/adoption.

- [ ] First demonstrate the real gap against unchanged legacy helpers: after creating a real namespace/carrier/journal, shift only simulated media device values and expect a complete enrolled rescan reopen; baseline must fail `private namespace parent identity changed`, not merely a missing import. Also assert the same legacy operation without enrollment still refuses after the fix.
- [ ] Add RED tests for each role changing independently: parent/root/marker/carrier/artifact/public receipt; same content with different native identity; stable native identity with changed digest/mtime/owner/mode; mixed enrolled/legacy roles; wrong binding and closed/transferred guard. Tests preserve old registry/marker/journal proof strings verbatim.
  Assertions after a successful simulated enrolled replay: `assert original_proof_json == retained_proof_json` and `assert reopened_native == enrolled_native`. For each independently changed role, `with pytest.raises(IdentityMismatch): ...` and `assert retained_artifact_bytes == original_artifact_bytes`; do not accept a result merely because its role has the same digest.
- [ ] Implement complete-set resolution and non-device legacy constraints only after exact profile/native-role verification. Preserve strict current-session device/inode name/FD checks. Refactor only journal SQL persistence as needed so next phase plus new proof-set/event revision commit atomically after FS evidence is captured outside the writer. Re-read durable authority after uncertain commit; never treat a prepared set as active.
- [ ] Integrate rescan reciprocal fences/replay and the new nullable supersession column. `active_for_series/active_for_path`, target preparation, replay keysets and competing selectors must exclude only a valid committed supersession, never an arbitrary flag. An unresolved journal and every unrelated namespace still fence. Test that a recovered/superseded prepared row permits a new operation despite the UNIQUE index, while retaining original proof bytes and state as historical evidence.
  The SQL fence callers also include `import_lease.py`, purge preparation in `routers/series_.py` and publication's reciprocal fence. Update their rescan/deletion predicates when each domain's supersession support lands; do not require changing HTTP action semantics. Until a domain's completed recovery event can be validated, its old active row still blocks.
- [ ] Run GREEN new tests plus `test_private_file_claim.py test_private_claim_creation_boundary.py test_rescan_recovery_protocol.py test_rescan_recovery_crash_gaps.py test_rescan_recovery_fences.py test_rescan_nfs_recovery.py test_chapter_rollback_descriptor_lifecycle.py`; full gates; commit `feat: enroll complete rescan ownership contexts`. Production registry remains empty.

### Task 6: Publication And Deletion Integration

**Files:** Modify scoped functions in `app/import_publication.py`, `app/import_staging.py`, `app/volume_file_deletion.py`, `app/import_lease.py`; create `tests/python/test_file_identity_publication.py`, `tests/python/test_file_identity_deletion.py`.
**Interfaces:** Consume `IdentityContext`, store activation, domain adapters. Existing public signatures/HTTP endpoints remain compatible; internal context is keyword-only and propagated explicitly.

- [ ] Write RED operation-level tests for new birth, exact pre-drift enrollment and device drift across publication staging/prepared/publishing/published/db_committed/cleaning and deletion active/completed. Include final/source carrier columns disagreeing with queue snapshot metadata and blocked leases; no source is admitted through only one copy of a receipt.
  Assertions: `assert public_bytes == committed_bytes` after db-committed settlement; `assert source_bytes == original_bytes` after acknowledged rollback; incomplete/mismatched receipt fixtures satisfy `assert journal_is_still_blocking` and retain both original and stage.
- [ ] Run new tests; retain baseline failures. Exercise no-clobber restore with occupied destination, including same-inode EEXIST; missing acknowledgement stays blocked with both copies retained.
- [ ] Wire independent source/stage/final/link role proofs and atomic per-phase ledger revisions into existing actions, compensation, DB commit, cleanup and startup replay. Keep full source/volume/queue-owner/grab-claim CAS requirements and current side-effect idempotency. Update all reciprocal fence/queue lease selectors for valid supersession; do not clear a lease merely because a proof mismatch occurred.
  Enumerate every direct journal-state query with `rg -n 'rescan_file_operations|volume_file_deletions|import_publications' app` and classify it as mutation/eligibility, replay, or reporting. Include narrow affected selectors in `import_execute.py`, `import_discovery.py`, `import_commit.py`, `tasks.py` and `routers/{series_,queue_,import_,history_,system}.py` where they would replay or reserve superseded work. Reporting must distinguish the existing state's historical meaning from a resolved recovery diagnostic without changing response shape; unrelated read-only counts need no rewrite. Add a static reference-coverage regression so a new mutation selector cannot silently bypass the shared semantics.
- [ ] Run GREEN plus `test_publication_private_recovery_391.py test_publication_nfs_file_claims_391.py test_publication_stage_carrier_391.py test_import_publication_journal.py test_volume_file_deletion_journal.py test_private_file_deletion.py`. Verify committed publication survives with original only privately retained until authorized cleanup; deletion supersession releases only its own active-volume index.
- [ ] Run full task gates; commit `feat: preserve enrolled publication and deletion ownership`.

### Task 7: Pack, Queue-Origin And Purge Integration

**Files:** Modify scoped proof/admission/replay functions in `app/private_pack_claim.py`, `app/private_pack_source.py`, `app/import_pack_cleanup.py`, staging generated-source admission in `app/import_staging.py`, and only namespace snapshot/protection helpers in `app/routers/series_.py`; create `tests/python/test_file_identity_packs.py`, `tests/python/test_file_identity_purge.py`.
**Interfaces:** Same complete-set/role/store interfaces; no global `DirectoryProof`, inventory or fingerprint equality relaxation. All legacy v1-v3 pack serializers remain unchanged.

- [ ] Write RED tests covering reservation building/attaching/ready/queued/attached/cleaning/retained, placement and cleanup directory carriers, tombstones and frozen generated-file origins. Deliberately remove or mismatch one nested proof/dependency; assert no partial cleanup or source delegation.
  Assertions: `assert retained_pack_bytes == before_pack_bytes` and `assert admitted_file_count == 0` for incomplete chains; valid chains satisfy `assert admitted_native == frozen_native` for every file. Purge refusal preserves both old and new epoch marker bytes.
- [ ] Run new tests and retain failures. Include terminal retained records, independent nested-mount child, same-inode different-subvolume double, and old/new epochs both referenced by different series.
- [ ] Thread enrolled contexts through per-file inventory verification, directory capture/restoration, queue freeze/admission/delegation and tombstone GC. Persist new revisions atomically with reservation/tombstone/queue-snapshot updates. Inventory every file; root/marker handles cannot authorize unknown descendants. Exact CAS and download-client identity remain unchanged.
- [ ] Extend purge snapshots to include all relevant epoch IDs/active-set events and their protected roots, then revalidate outside SQL. Protect old/unresolved/retained namespaces and private allocation parents; never recursively delete them as ordinary library shells. Test foreign epoch, stale purge snapshot and supersession without a matching event refusing.
- [ ] Run GREEN plus `test_private_pack_proof.py test_private_pack_source.py test_import_pack_private_placement.py test_import_pack_nfs_lifecycle.py test_import_pack_lifecycle_authority.py test_publication_generated_source_consumer_391.py test_purge_namespace_preservation.py`; full task gates; commit `feat: verify enrolled pack and purge proof chains`.

### Task 8: Explicit New-Epoch Geometry

**Files:** Extend `app/file_identity_claims.py`, epoch parsers/store and named consumer adapters from Tasks 5-7; create `tests/python/test_file_identity_epochs.py`.
**Interfaces:** `EpochBinding(epoch_id: str, origin_parent: str, allocation_parent: str, root_path: str, authority_id: str)`; `provision_epoch(guard: FileMutationGuard, approved_plan: RecoveryPlan) -> EpochBinding`. Only an approved apply intent may call provisioning; tests construct a validated fixture plan, not an unrestricted path/boolean override.

- [ ] Write RED same-parent legacy and new two-parent tests. New path layout: `<origin_parent>/.mangarr-epoch-<32hex epoch_id>/.mangarr-claims`; allocation parent is the newly provisioned 0700 epoch directory. Keep original `.mangarr-claims` and old registry proof byte-identical. Existing epoch-parent collision, symlink, wrong owner/ACL policy or unclassified children refuse.
  Assertions: `assert restored_path.parent == origin_parent`, `assert restored_path.parent != allocation_parent`, `assert old_root_marker == before_old_root_marker`; substituted/cross-device fixtures raise and leave the original bytes in place.
- [ ] Run `test_file_identity_epochs.py -q`; prove existing same-parent allocation/capture checks reject this geometry before correction. Add destination-parent substitution and EXDEV cases; no copy/delete fallback is allowed.
- [ ] Under reviewed local intent, pinned origin FD and no-clobber creation, provision/verify/fsync the fresh allocation parent and record its explicit controlled birth; create the private namespace under its exclusive boundary. Store both parent proofs, root and epoch paths. New-version contexts bind actual origin FD/name separately from allocation FD and bind each publication/restore destination explicitly. Do not change legacy fixed-root parsing or chmod/chown shared paths.
- [ ] Test every consumer uses the bound destination, never implicitly `namespace.parent_fd`; current devices and qualified authority/profile must agree for mutation. Inject crash/commit failure at parent creation, marker write, fsync and activation; old root remains protected and incomplete new epoch cannot allocate. Reusing an exact prepared plan may finish only its own proven birth, never adopt a collided directory.
- [ ] Run GREEN plus Tasks 5-7 focused selections and full gates; commit `feat: bind recovery epochs to proven origin parents`.

### Task 9: Reviewed, Resumable Legacy Recovery

**Files:** Extend `app/file_identity_recovery.py`, `app/file_identity_cli.py`, relevant domain settlement/fence adapters; create `tests/python/test_file_identity_recovery.py`, `tests/python/test_file_identity_recovery_crashes.py`; update `tests/python/test_file_identity_cli.py`, `docs/deployment.md` and `docs/suwayomi-file-selection.md`.
**Interfaces:** `apply_recovery(guard: FileMutationGuard, plan: RecoveryPlan, confirmed_sha256: str, backup_receipt_json: str) -> RecoveryResult`; `verify_recovery(guard: FileMutationGuard, plan_sha256: str) -> RecoveryResult`. Result includes plan digest, event IDs, per-owner disposition, retained-object list and unresolved blockers, never secret data in console output.

- [ ] Write RED tests for all six rows of the approved spec's disposition table. Use representative real rescan, publication, deletion and pack journals rather than testing a generic action dispatcher alone. Include the reported prepared-before-allocation failure with preserved original, plus a deliberately orphaned stage that forbids the no-mutation disposition.
  Assertions after cancellation: `assert result.unresolved == ()`, `assert not active_for_series(db, recovered_series_id)` and `assert active_for_series(db, unresolved_neighbor_id)`; original file and historical receipt bytes remain unchanged. For same-inode unacknowledged links, `assert historical_link_receipt is None` before and after explicit keep-current recovery; no old publication-success event is emitted.
- [ ] Run new tests; retain failures. Assert `keep-current` never turns same-inode EEXIST into an old link receipt; cleanup-committed never rolls back the public file or alters its committed volume state. Changed/missing acknowledged publication cannot use the unacknowledged-link path.
- [ ] Implement `ownership authority register`, `ownership enroll`, `ownership apply --plan FILE --confirm-sha256 HEX --backup-receipt FILE`, and `ownership verify --plan-sha256 HEX`. Mutating commands require maintenance exclusive then mutation guard, validated v6 schema (never auto-upgrade through CLI), an allowed qualified profile and exact current inventory/row CAS. Registration stores a declaration, not a claim that arbitrary server identity is verified. `enroll` accepts only complete exact pre-drift evidence. No HTTP actions.
- [ ] Before apply, validate a self-contained stopped DB/config/key backup using existing backup helpers; compare its relevant logical owner snapshots with the plan. The private receipt binds archive digest, source image identity, scope and media snapshot/retention disposition. A DB-only backup is not full rollback authority. Never print keys or request a live media/config copy as an incidental test fixture.
- [ ] Execute only named dispositions with new recovery intent/step events around existing no-clobber filesystem primitives. Missing historical receipts are never synthesized. For keep-current, supersede the interrupted attempt and perform explicit fresh current-file observation using existing safe local observation rules; preserve manual metadata/provider IDs/count protections. For committed cleanup, retain public state and authorize only enumerated private bytes. Retained artifacts get non-replayable records and no automatic GC schedule.
- [ ] Atomically commit final supersession events plus validated `recovery_event_id` selectors/CAS and ordinary domain completion/release effects. Original proof/marker/snapshot bytes remain in immutable history. Ephemeral pack reservations may retire only through their existing exact disposition/identity protocol after archival in the ledger; do not simply free a primary key while old queue/tombstone references remain. New publication attempts use a new queue identity, never overwrite the old UNIQUE queue binding.
- [ ] Inject process kill or fsync/SQL exceptions before/after every new intent, action, acknowledgement and final CAS. Restart requires the same explicitly confirmed plan digest and resumes only proven steps; ordinary startup replay sees pending recovery and stays fenced. Changed decisions require a new plan; same plan applied twice is idempotent and never relinks/deletes again. Unknown children, changed review inventory, failed backup validation and neighbor journals remain blocked.
- [ ] Run GREEN with real subprocess crash tests, CLI privacy/permission tests and all Tasks 5-8 selections; full gates and independent safety review. Commit `feat: recover reviewed legacy ownership operations`. Document distinct outcomes and rollback requirements; do not ask the reporter to use it yet.

### Task 10: Native Qualification And Profile Enablement

**Files:** Create `tests/native_file_identity_qualification.py` and its unit tests `tests/python/test_file_identity_qualification_runner.py`; add exact profile fixture/support entries to `app/file_identity.py` only after qualification; update `docs/release-qualification.md`, deployment support notes and the #474 contributor procedure.
**Interfaces:** Qualification runner consumes explicit owned paths/profile candidate, produces a private versioned manifest of exact source/image/platform/security/mount/server identity and per-case receipts. It never provisions host mounts or changes privilege by itself. Candidate observations cannot activate a runtime profile.

- [ ] Write RED runner tests refusing missing ownership labels, unverified mount/peer, unknown object counts, changed source/image, partial results and teardown uncertainty. Unit simulations are not native passes. Mark each acceptance case required/passed/failed/unavailable; no skip-to-pass coercion.
  Assertions: an unavailable native case satisfies `assert result["outcome"] == "unavailable"` and `assert not result["qualified"]`; changing any source/image/profile digest invalidates the qualification manifest and profile activation.
- [ ] Resolve the raw-control EPERM lab blocker through a separately reviewed diagnostic probe or select a representative external target. The retired logging-only attempt does not authorize another run. Specify ownership, isolation, security, payload/log/time bounds and ordered cleanup before any new Docker/NFS operation; no added caps or reused unrelated lab resources.
- [ ] Execute native per-role tests across close/reopen, fresh process, demonstrated changed-device remount and cross-node transitions; qualify hard NFSv4.1/TCP/sec=sys Synology/Btrfs with exact versions. Test substitution/snapshot/copy/replacement, filesystem freshness, native/denied/oversized API behavior, unknown/export/server changes and full carrier/pack/publication recovery. Distinguish supported same-incarnation transitions from recovery-required ones. Record identity-preserving restore indistinguishability and operator contract, not a fictitious detection test.
- [ ] Obtain native ARM64 and representative NAS execution targets or an explicit reviewed release/support policy decision. Do not use emulation/Ganesha/amd64/report comments as substitutes. Keep production profile disabled and #474 unresolved if the reported target remains unqualified; a support-policy change needs maintainer review, not worker discretion.
- [ ] Add only qualified versioned profile contracts/digests, with explicit operator authority enrollment and no auto-detection opt-in. Run full gates against that exact enablement head, independent review and CI; give the contributor an exact immutable test build and disposable-data procedure first. Their preserved operation requires its own inspect/review/backup/apply decision after those results.
- [ ] Update source-only versus shipped status, preserve all evidence and clean exact owned QA resources; commit `feat: enable qualified remount identity profile` only when the above claims are proven.

### Task 11: New Immutable Release Candidate And Fresh Qualification

**Files:** Release preparation only in `app/VERSION`, `CHANGELOG.md`, `README.md`, `docs/releases.md`, `docs/release-qualification.md`; use existing Makefile/release automation without billing or branch-protection bypass.
**Interfaces:** Consumes reviewed source plus closed #474 acceptance evidence, #467 triage and the normal repository release contract. Produces a new candidate from its exact reviewed merge; never modifies RC10.

- [ ] Recheck GitHub/issues/CI. If #467 recurs, retain diagnostics and investigate before retry; it is not currently a claimed auth fix. Update source/shipped docs to include #476/#477/#479 and later reviewed fixes. Choose an unused next RC only after checking current tags; do not preassign or overwrite a number in this plan.
- [ ] Commit the preparation as `chore: prepare next immutable 1.3.3 candidate`. On that exact preparation source run `make release-local`; preserve complete `make test`/`make test-release-safe`, dependency/secret/config security and exact-image fixed HIGH/CRITICAL scan receipts, including tool/database coverage limits. Review/merge with unchanged tested tree, then verify the merge identity before tag/publication.
- [ ] Publish a new annotated immutable candidate with existing `make release-push` confirmation workflow. Verify both platform manifests, revision/version/runtime user, SBOM and provenance; prove existing immutable tags and stable/latest aliases unchanged. Candidate is non-latest/unqualified until runtime gates pass.
- [ ] Run fresh published-artifact empty public-Compose install/setup/auth/health/DB checks; realistic stopped stable-1.3.2 DB/config/key upgrade, provider/client credential/identity/title/count/path preservation and matching pristine-backup rollback. Never downgrade the migrated DB or claim cached/substituted environments prove default online installation.
- [ ] Run new real automatic search/grab/download/import/rescan, exact source/client ownership/content checks, recovery/startup replay, default-off chapter preservation and opt-in quarantine/restore negative/positive cases. Add installed-artifact #474 native/legacy recovery cases under qualified storage; all observations/provider cache protections remain intact.
- [ ] Complete a NEW uninterrupted natural soak of at least **87,600 seconds**, with two natural daily metadata polls at least **86,400 seconds** apart. No forced polls, elapsed transfer, missing observer credit or borrowed RC9/RC10 evidence. Then independently audit every release acceptance item, including platform coverage and cleanup, before recommending stable.

### Task 12: Stable Publication And Production Cutover

**Files:** Release/version documentation and compact private qualification/cutover receipts only; production change is Mangarr image reference only unless a separately reviewed migration procedure requires otherwise.
**Interfaces:** Consumes independently accepted candidate, stable source/artifact identity and a fresh matching production rollback pair. Produces verified published 1.3.3 and deployed smoke evidence.

- [ ] Review final GO against all original requirements. Prepare a new stable commit with `app/VERSION=1.3.3` and matching release documents only; any runtime/schema/dependency/build change after candidate qualification returns to Task 11 and a new RC. Never retag an RC image as stable. Run `make release-local` on the reviewed preparation and exact merge per the repository promotion contract; current scans must cover the exact stable source/artifact. Stable identity/aliases/release notes must accurately describe schema-v6 rollback, storage support and manual recovery, without claiming universal remount support.
- [ ] Publish the new stable artifact only after exact-commit gates and explicit final GO. Verify both manifests, SBOM/provenance, version/revision, existing immutable tags and stable alias identities. Before publishing the GitHub stable release, smoke that exact new digest on fresh install and separate upgrade/rollback copies, including #474 recovery and downloader connectivity. Candidate soak remains candidate evidence, not a claimed run against the new stable digest. A failed stable artifact is held, not replaced in place; no production cutover until its own gates pass. Missing-coverage support policy is never silently waived.
- [ ] Before production change take a fresh consistent matching DB/config/key and media-disposition backup, record original image/digest and recovery validation. This is a protected recovery artifact, not disposable QA; use the host backup-retention runbook. Never use an old RC5 backup against later media as an assumed rollback pair.
- [ ] Update only Mangarr through its existing deployment configuration, preserve shared SAB/qBittorrent/Suwayomi/mappings/settings, and verify actual deployed image/version/schema/health/credentials. Smoke auth, library/metadata/cache, queue and a bounded agreed real download-import/recovery workflow without claiming another client's work. On failure stop and use the reviewed matching recovery path, not DB-only downgrade.
- [ ] Comment on affected issues with exact shipped version/digest, user-visible behavior, limits and verification. Confirm the contributor's preserved-operation outcome separately; source merges alone do not resolve it. Close only issues actually resolved.
- [ ] Retire exact owned temporary containers/volumes/networks/images/worktrees/branches after reference/mount checks, retaining compact receipts and the required production recovery pair. No global prune, forced unmount or unrelated artifact deletion. Mark the native release goal complete only after verifying all requested deliverables against current authoritative state.

## Plan Self-Review And Handoff

- Spec coverage: trust/native identity -> Tasks 1/10; immutable proof sets/migrations -> 2; quiescence -> 3; complete references/private review -> 4; enrollment/revision safety -> 5-7; geometry -> 8; every legacy disposition/crash window -> 9; platform/profile boundaries -> 10; full immutable candidate/soak -> 11; stable/backup/production/contributor cleanup -> 12.
- Type consistency: the shared contracts above are the single interface source. Store functions are SQL-only; borrowed descriptor lifetimes remain with their existing owners. No task may replace `IdentityContext` with a permissive boolean or infer recovery authority from a parsed plan.
- Review Focus is exercised by the named schema/maintenance/index/pack-origin/unacknowledged-link/geometry tests. Full gate claims always identify their exact source/artifact scope.
- This document contains no implementation or executed verification result. Source/security/platform prerequisites remain unresolved until their tasks produce evidence. Task 10's missing native targets cannot be converted into a worker-selected weaker profile to complete the goal.

**Requested execution method:** retain PythonPro/LSP implementation workers with independent safety review per PR, coordinated in this thread. The maintainer must review this plan before implementation; preserving the chosen worker method does not turn plan approval into permission for production/reporter recovery operations without their exact reviewed procedures.

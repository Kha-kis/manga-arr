"""Complete, descriptor-bound claim contexts. No source profile is enabled here.

SQL captures and activation are separate from filesystem verification. Exact
legacy enrollment is an offline action, not adoption after device drift.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field
import hashlib
import os
import secrets
import sqlite3
import stat
from typing import cast

import file_identity as native
from file_identity import (
    IdentityMismatch,
    IdentityUnavailable,
    InvalidIdentity,
    OwnerRef,
    RoleKey,
)
from file_identity_consumers import (
    InventoryBudget,
    ReferenceClosure,
    RoleSpec,
    claim_roles_for,
    _capture_references,
    _collect_references,
)
import file_identity_store as store
from file_identity_store import ActiveProofSet, ObjectProof, OwnerSnapshot, ProofSet
from file_mutation_lock import FileMutationGuard, FileMutationLockError
import private_file_claim as claims

_media = os
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_MAX_HASH_BYTES = 64 * 1024**3
_MAX_WIRE_BYTES = 16 * 1024**2


@dataclass(slots=True)
class _CaptureBudget:
    hash_bytes: int = 0
    wire_bytes: int = 0


def _encode(value: object) -> str:
    return store._encode(value)


def _digest(encoded: str) -> str:
    return hashlib.sha256(encoded.encode()).hexdigest()


def _document(encoded: str) -> dict[str, object]:
    value = store._load(encoded)
    if type(value) is not dict:
        raise InvalidIdentity("claim evidence is not an object")
    return cast(dict[str, object], value)


def _row(snapshot: OwnerSnapshot) -> dict[str, object]:
    return cast(
        dict[str, object],
        cast(dict[str, object], _document(snapshot.rows_json)["root"])["row"],
    )


@dataclass(frozen=True, slots=True)
class IdentityContext:
    active: ActiveProofSet
    guard: FileMutationGuard
    _dependencies: tuple[IdentityContext, ...] = field(
        default=(), init=False, repr=False
    )

    def __post_init__(self) -> None:
        _verify_guard(self.guard)
        _validate_manifest(self.active)

    def verify_path(self, path: str, fd: int | None = None) -> None:
        if fd is None:
            with _opened(path) as (held, parent, name):
                verify_role(self, role_at(self, path), held, parent, name)
        else:
            verify_path_fd(self, path, fd)

    def saved_fingerprint(self, path: str) -> claims.FullFileFingerprint | None:
        for spec in claim_roles_for(self.active.proof.snapshot):
            if spec.path == path and any(
                o.role == spec.role for o in self.active.proof.roles
            ):
                value = _legacy_fingerprint(spec, self.active.proof.snapshot)
                return value if isinstance(value, claims.FullFileFingerprint) else None
        return None

    def assert_allocation_supported(self) -> None:
        if _document(self.active.proof.manifest_json)["version"] == 2:
            raise IdentityUnavailable(
                "new epoch allocation requires completed consumer qualification"
            )


@dataclass(frozen=True, slots=True)
class VerifiedRole:
    fd: int
    parent_fd: int
    name: str
    role: RoleKey
    _context: IdentityContext | None = field(default=None, init=False, repr=False)
    _session: tuple[int, ...] = field(default=(), init=False, repr=False)


def _session(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_nlink,
    )


def _verify_guard(guard: FileMutationGuard) -> None:
    try:
        guard.verify()
    except FileMutationLockError as exc:
        raise IdentityMismatch(
            "claim mutation guard is closed, transferred, or changed"
        ) from exc


def _policy(guard: FileMutationGuard, spec: RoleSpec, info: os.stat_result) -> None:
    if (info.st_dev, info.st_ino) in {
        (guard._dev, guard._inode),
        (guard._lock_dev, guard._lock_inode),
    }:
        raise IdentityMismatch("claim object aliases local SQLite/control state")
    if spec.role.kind == "directory":
        if not stat.S_ISDIR(info.st_mode):
            raise IdentityMismatch("claim directory kind differs")
        if spec.policy == "private_directory":
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise IdentityMismatch("claim private directory ownership differs")
        if spec.policy == "exclusive_parent" and (
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise IdentityMismatch("claim exclusive parent ownership differs")
    elif not stat.S_ISREG(info.st_mode):
        raise IdentityMismatch("claim file kind differs")
    if spec.policy == "marker" and (
        info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise IdentityMismatch("claim marker ownership/link policy differs")


@contextmanager
def _opened(path: str) -> Generator[tuple[int, int, str], None, None]:
    """Pin every path component without following symlinks; retain current names."""
    if not os.path.isabs(path) or os.path.normpath(path) != path or path == "/":
        raise InvalidIdentity("invalid claim object path")
    with ExitStack() as stack:
        parent = _media.open("/", _DIR)
        stack.callback(_media.close, parent)
        chain: list[tuple[int, int, str]] = []
        parts = path.lstrip("/").split("/")
        for name in parts[:-1]:
            fd = _media.open(name, _DIR, dir_fd=parent)
            stack.callback(_media.close, fd)
            chain.append((fd, parent, name))
            parent = fd
        fd = _media.open(
            parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=parent,
        )
        stack.callback(_media.close, fd)
        chain.append((fd, parent, parts[-1]))
        for held, directory, name in chain:
            _current_name(held, directory, name)
        yield fd, parent, parts[-1]
        for held, directory, name in chain:
            _current_name(held, directory, name)


def _current_name(fd: int, parent_fd: int, name: str) -> os.stat_result:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise InvalidIdentity("claim role requires one current child name")
    try:
        held = _media.fstat(fd)
        named = _media.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise IdentityMismatch("current claim FD/name is unavailable") from exc
    if _session(held) != _session(named):
        raise IdentityMismatch("current claim FD/name changed")
    return held


def _fingerprint(
    fd: int, parent: int, name: str, info: os.stat_result
) -> claims.FullFileFingerprint:
    if not stat.S_ISREG(info.st_mode):
        raise IdentityMismatch("claim object is not a regular file")
    if not 0 <= info.st_size <= _MAX_HASH_BYTES:
        raise InvalidIdentity("claim content exceeds the fixed hash budget")
    digest = hashlib.sha256()
    offset = 0
    while offset < info.st_size:
        chunk = _media.pread(fd, min(1024 * 1024, info.st_size - offset), offset)
        if not chunk:
            raise IdentityMismatch("claim object shortened while hashing")
        digest.update(chunk)
        offset += len(chunk)
    if _session(_current_name(fd, parent, name)) != _session(info):
        raise IdentityMismatch("claim object changed while hashing")
    return claims.FullFileFingerprint(
        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, digest.hexdigest()
    )


def _object(context: IdentityContext, role: RoleKey) -> ObjectProof:
    matches = [o for o in context.active.proof.roles if o.role == role]
    if len(matches) != 1:
        raise IdentityMismatch("role lacks complete active identity evidence")
    return matches[0]


def verify_role(
    context: IdentityContext, role: RoleKey, fd: int, parent_fd: int, name: str
) -> VerifiedRole:
    _verify_guard(context.guard)
    obj = _object(context, role)
    info = _current_name(fd, parent_fd, name)
    if (
        os.path.join(os.readlink(f"/proc/self/fd/{parent_fd}"), name)
        != obj.canonical_path
    ):
        raise IdentityMismatch("claim role path/parent binding differs")
    if obj.parent_role is not None:
        parent = _object(context, obj.parent_role)
        if (
            native.capture_native(parent_fd) != parent.native
            or info.st_dev != _media.fstat(parent_fd).st_dev
        ):
            raise IdentityMismatch("claim parent identity or mount boundary differs")
    before = native.capture_native(fd)
    if before != obj.native:
        raise IdentityMismatch("claim native identity differs")
    spec = next(
        s for s in claim_roles_for(context.active.proof.snapshot) if s.role == role
    )
    _policy(context.guard, spec, info)
    fields = cast(dict[str, object], _document(obj.constraints_json)["non_device"])
    values: dict[str, object] = {
        "inode": info.st_ino,
        "uid": info.st_uid,
        "mode": stat.S_IMODE(info.st_mode),
        "nlink": info.st_nlink,
    }
    if role.kind == "directory":
        if not stat.S_ISDIR(info.st_mode):
            raise IdentityMismatch("claim directory kind changed")
    else:
        fingerprint = _fingerprint(fd, parent_fd, name, info)
        values.update(
            size=fingerprint.size,
            mtime_ns=fingerprint.mtime_ns,
            sha256=fingerprint.sha256,
        )
    if any(
        value is not None and values.get(key) != value for key, value in fields.items()
    ):
        raise IdentityMismatch("claim non-device content/ownership constraints differ")
    if native.capture_native(fd) != before or _session(
        _current_name(fd, parent_fd, name)
    ) != _session(info):
        raise IdentityMismatch("claim changed across content verification")
    _verify_guard(context.guard)
    result = VerifiedRole(fd, parent_fd, name, role)
    object.__setattr__(result, "_context", context)
    object.__setattr__(result, "_session", _session(info))
    return result


def verify_live(role: VerifiedRole, context: IdentityContext) -> None:
    if role._context is not context:
        raise IdentityMismatch("verified claim role borrowed another context")
    if _session(_current_name(role.fd, role.parent_fd, role.name)) != role._session:
        raise IdentityMismatch("verified claim role changed in this session")
    verify_role(context, role.role, role.fd, role.parent_fd, role.name)


def _validate_manifest(active: ActiveProofSet) -> None:
    proof = active.proof
    specs = claim_roles_for(proof.snapshot)
    manifest = _document(proof.manifest_json)
    provisioned = _document(proof.snapshot.rows_json)["version"] == 2
    expected = [asdict(s.role) for s in specs]
    if manifest["required_roles"] != expected:
        raise IdentityMismatch("claim activation lacks the complete consumer role set")
    by_role = {o.role: o for o in proof.roles}
    absent = {
        store._role_from(cast(dict[str, object], a)["role"]): cast(dict[str, object], a)
        for a in cast(list[object], manifest["absent_roles"])
    }
    for spec in specs:
        obj = by_role.get(spec.role)
        if obj is None:
            item = absent.get(spec.role)
            parent_role = (
                None
                if item is None or item["parent_role"] is None
                else store._role_from(item["parent_role"])
            )
            if (
                item is None
                or item["canonical_path"] != spec.path
                or parent_role != spec.parent_role
            ):
                raise IdentityMismatch("claim role absence/path differs")
            continue
        fields = _document(obj.constraints_json)
        if provisioned:
            # Store validation binds the exact birth2 objects, intent, and
            # protected dependencies. It does not grant allocation capability.
            if (
                obj.canonical_path != spec.path
                or obj.parent_role != spec.parent_role
                or fields["policy"] != spec.policy
            ):
                raise IdentityMismatch("provisioned claim role binding differs")
            continue
        if type(fields["legacy"]) is not dict:
            raise IdentityMismatch("claim role lacks legacy evidence")
        legacy = cast(dict[str, object], fields["legacy"])
        binding = cast(dict[str, object], fields["binding"])
        if (
            obj.canonical_path != spec.path
            or obj.parent_role != spec.parent_role
            or fields["policy"] != spec.policy
            or legacy["json"] != spec.legacy_json
            or binding["operation_key"] != _binding_for(spec, proof.snapshot)[0]
            or binding["owner_token"] != _binding_for(spec, proof.snapshot)[1]
        ):
            raise IdentityMismatch("claim role legacy/binding differs")
        if spec.policy == "file" and any(
            cast(dict[str, object], fields["non_device"])[k] is None
            for k in ("uid", "mode", "nlink")
        ):
            raise IdentityMismatch("claim file ownership constraints are incomplete")


def resolve_context(
    db: sqlite3.Connection, guard: FileMutationGuard, owner: OwnerRef
) -> IdentityContext | None:
    if db.in_transaction:
        raise IdentityUnavailable("claim context requires ended SQL transaction")
    _verify_guard(guard)
    assert_database(db, guard)
    active = store.read_active(db, owner)
    if active is None:
        return None
    if owner.domain not in {"namespace", "rescan"}:
        raise IdentityUnavailable("claim consumer domain is not integrated")
    _validate_manifest(active)
    dependencies: list[IdentityContext] = []
    if owner.domain == "rescan":
        parent = os.path.dirname(cast(str, _row(active.proof.snapshot)["source_path"]))
        namespace = resolve_context(db, guard, OwnerRef("namespace", (parent,)))
        if namespace is None:
            raise IdentityMismatch("enrolled rescan has an unenrolled namespace")
        bound = store._dependencies(_document(active.proof.manifest_json), owner)
        if bound != [namespace.active.proof.snapshot]:
            raise IdentityMismatch("rescan namespace dependency is incomplete or stale")
        if (
            namespace.active.proof.authority_id != active.proof.authority_id
            or namespace.active.proof.epoch_id != active.proof.epoch_id
        ):
            raise IdentityMismatch("rescan namespace authority differs")
        dependencies.append(namespace)
    result = IdentityContext(active, guard)
    object.__setattr__(result, "_dependencies", tuple(dependencies))
    return result


def assert_database(db: sqlite3.Connection, guard: FileMutationGuard) -> None:
    """SQL-only binding check, also usable inside the short writer transaction."""
    databases = db.execute("PRAGMA database_list").fetchall()
    main = [row for row in databases if row[1] == "main"]
    if (
        len(main) != 1
        or not main[0][2]
        or os.path.abspath(main[0][2]) != guard._db_path
    ):
        raise IdentityMismatch("claim context borrowed another SQLite database")


def verify_complete(context: IdentityContext) -> None:
    """Verify every present role and intended absence before granting any part."""
    _verify_guard(context.guard)
    for dependency in context._dependencies:
        verify_complete(dependency)
    sizes = [
        cast(dict[str, object], _document(o.constraints_json)["non_device"])["size"]
        for o in context.active.proof.roles
    ]
    if sum(cast(int, size) for size in sizes if size is not None) > _MAX_HASH_BYTES:
        raise InvalidIdentity("complete claim set exceeds the fixed hash budget")
    for obj in context.active.proof.roles:
        try:
            with _opened(obj.canonical_path) as (fd, parent, name):
                verify_role(context, obj.role, fd, parent, name)
        except OSError as exc:
            raise IdentityMismatch("complete claim role is missing or unsafe") from exc
    manifest = _document(context.active.proof.manifest_json)
    for value in cast(list[object], manifest["absent_roles"]):
        absent = cast(dict[str, object], value)
        path = cast(str, absent["canonical_path"])
        try:
            with _opened(path):
                raise IdentityMismatch("claim intended absence is occupied")
        except FileNotFoundError:
            pass
    _verify_guard(context.guard)


def role_at(context: IdentityContext, path: str) -> RoleKey:
    roles = [o.role for o in context.active.proof.roles if o.canonical_path == path]
    if len(roles) != 1:
        raise IdentityMismatch("claim path lacks an unambiguous enrolled role")
    return roles[0]


def verify_path_fd(context: IdentityContext, path: str, fd: int) -> None:
    with _opened(path) as (current, parent, name):
        if _session(_media.fstat(current)) != _session(_media.fstat(fd)):
            raise IdentityMismatch("borrowed claim FD differs from its current name")
        verify_role(context, role_at(context, path), fd, parent, name)


def _legacy_fingerprint(
    spec: RoleSpec, snapshot: OwnerSnapshot
) -> claims.FullFileFingerprint | tuple[int, int] | None:
    row = _row(snapshot)
    purpose = spec.role.purpose
    if snapshot.owner.domain == "namespace":
        proof = claims._NamespaceProof.from_json(cast(str, row["ownership_json"]))
        if purpose == "namespace_parent":
            return proof.parent_dev, proof.parent_ino
        if purpose == "namespace_root":
            return proof.root_dev, proof.root_ino
        return proof.marker_fingerprint
    if purpose == "operation_parent":
        return None
    fingerprints = _document(cast(str, row["fingerprints_json"]))
    if purpose in {"source", "destination"}:
        key = "source" if purpose == "source" else "publication"
        if (
            row["source_path"] == row["destination_path"]
            and row["publication_receipt_json"] is not None
        ):
            source = _document(cast(str, row["carriers_json"]))["source"]
            if (
                source is None
                or cast(dict[str, object], source)["restore_receipt"] is None
            ):
                key = "publication"
        value = fingerprints[key]
        return (
            claims.FullFileFingerprint.from_value(value) if value is not None else None
        )
    carrier_purpose, suffix = purpose.rsplit("_", 1)
    carriers = _document(cast(str, row["carriers_json"]))
    record = claims.CarrierRecord.from_json(_encode(carriers[carrier_purpose]))
    if suffix == "carrier":
        return record.dir_dev, record.dir_ino
    if suffix == "marker":
        return record.marker_fingerprint
    key = (
        "source"
        if carrier_purpose == "source"
        else "publication"
        if carrier_purpose == "rollback"
        else carrier_purpose
    )
    value = fingerprints[key]
    return claims.FullFileFingerprint.from_value(value) if value is not None else None


def _constraints(
    spec: RoleSpec,
    snapshot: OwnerSnapshot,
    info: os.stat_result,
    fingerprint: claims.FullFileFingerprint | None,
) -> str:
    fields: dict[str, object] = {
        key: None
        for key in ("inode", "size", "mtime_ns", "sha256", "uid", "mode", "nlink")
    }
    fields["inode"] = info.st_ino
    if fingerprint is not None:
        fields.update(
            size=fingerprint.size,
            mtime_ns=fingerprint.mtime_ns,
            sha256=fingerprint.sha256,
        )
    if spec.policy in {"file", "marker", "private_directory", "exclusive_parent"}:
        fields.update(uid=info.st_uid, mode=stat.S_IMODE(info.st_mode))
    if spec.policy in {"file", "marker"}:
        fields["nlink"] = info.st_nlink
    legacy = spec.legacy_json
    if legacy is None:
        raise IdentityMismatch("exact enrollment lacks legacy evidence")
    operation_key, owner_token = _binding_for(spec, snapshot)
    return _encode(
        {
            "version": 1,
            "legacy": {"kind": "journal", "json": legacy, "sha256": _digest(legacy)},
            "binding": {
                "owner": store._owner_value(snapshot.owner),
                "role": asdict(spec.role),
                "operation_key": operation_key,
                "owner_token": owner_token,
            },
            "policy": spec.policy,
            "non_device": fields,
        }
    )


def _binding_for(spec: RoleSpec, snapshot: OwnerSnapshot) -> tuple[str, str]:
    row = _row(snapshot)
    if snapshot.owner.domain == "namespace":
        proof = claims._NamespaceProof.from_json(cast(str, row["ownership_json"]))
        return str(snapshot.owner.key[0]), proof.owner_token
    operation_key = cast(str, row["operation_token"])
    purpose = spec.role.purpose
    if purpose in {"operation_parent", "source", "destination"}:
        return operation_key, operation_key
    key = purpose.rsplit("_", 1)[0]
    record = claims.CarrierRecord.from_json(
        _encode(_document(cast(str, row["carriers_json"]))[key])
    )
    return operation_key, record.owner_token


def _discard_link_removed(
    prior: dict[str, ObjectProof],
    before: dict[str, object],
    current: dict[str, object],
    matched: ObjectProof,
    purpose: str,
    absent: set[str],
) -> bool:
    siblings = {
        "source": ("source", "publication"),
        "destination": ("publication",),
        "publication_artifact": ("rollback",),
    }.get(purpose, ())
    if not siblings:
        return False
    old_carriers = _document(cast(str, before["carriers_json"]))
    carriers = _document(cast(str, current["carriers_json"]))
    for key in siblings:
        artifact = prior.get(key + "_artifact")
        if (
            artifact is None
            or artifact.native != matched.native
            or key + "_artifact" not in absent
        ):
            continue
        old_record = cast(dict[str, object] | None, old_carriers[key])
        record = cast(dict[str, object] | None, carriers[key])
        if (
            old_record is None
            or record is None
            or old_record["phase"] != "discarding"
            or record["phase"] not in {"discarding", "discarded"}
        ):
            continue
        if key == "source":
            if (
                old_record["restore_receipt"] is not None
                and record["restore_receipt"] == old_record["restore_receipt"]
            ):
                return True
        elif (
            before["publication_receipt_json"] is not None
            and current["publication_receipt_json"]
            == before["publication_receipt_json"]
        ):
            return True
    return False


def _transition(
    previous: IdentityContext,
    spec: RoleSpec,
    snapshot: OwnerSnapshot,
    captured: native.NativeHandle,
    constraints: str,
    absent: set[str],
) -> None:
    prior = {o.role.purpose: o for o in previous.active.proof.roles}
    old = prior.get(spec.role.purpose)
    current = _row(snapshot)
    before = _row(previous.active.proof.snapshot)
    purpose = spec.role.purpose
    candidates: list[ObjectProof | None] = [old] if old is not None else []
    # Only durable journal actions can move a previously captured object. These
    # are native object references, not path/content-based identity inference.
    if purpose == "source_artifact":
        candidates.append(prior.get("source"))
    elif purpose == "rollback_artifact":
        candidates.append(prior.get("destination") or prior.get("source"))
    elif purpose in {"destination", "source"}:
        if current["publication_receipt_json"] is not None:
            candidates.append(prior.get("publication_artifact"))
        if purpose == "source":
            candidates.append(prior.get("source_artifact"))
    matched = next(
        (o for o in candidates if o is not None and o.native == captured), None
    )
    if matched is not None:
        old_fields = cast(
            dict[str, object], _document(matched.constraints_json)["non_device"]
        )
        new_fields = cast(dict[str, object], _document(constraints)["non_device"])
        removed_link = _discard_link_removed(
            prior, before, current, matched, purpose, absent
        )
        if removed_link and new_fields["nlink"] != cast(int, old_fields["nlink"]) - 1:
            raise IdentityMismatch("claim discard changed enrolled link relationship")
        for key, value in old_fields.items():
            if value is None or new_fields[key] == value:
                continue
            if key == "nlink" and purpose in {"source", "source_artifact"}:
                record = _document(cast(str, current["carriers_json"]))["source"]
                old_record = _document(cast(str, before["carriers_json"]))["source"]
                if (
                    record is not None
                    and old_record is not None
                    and cast(dict[str, object], old_record)["phase"] == "restoring"
                    and cast(dict[str, object], old_record)["restore_receipt"] is None
                    and cast(dict[str, object], record)["phase"] == "restored"
                    and cast(dict[str, object], record)["restore_receipt"] is not None
                    and new_fields[key] == cast(int, value) + 1
                ):
                    continue
            if key == "nlink" and removed_link:
                continue
            if key == "nlink" and purpose in {
                "source",
                "destination",
                "publication_artifact",
            }:
                if (
                    matched.role.purpose == "publication_artifact"
                    and before["publication_receipt_json"] is None
                    and current["publication_receipt_json"] is not None
                    and new_fields[key] == cast(int, value) + 1
                ):
                    continue
            raise IdentityMismatch(
                "claim phase changed enrolled non-device constraints"
            )
        return
    if old is not None:
        raise IdentityMismatch("claim phase replaced an enrolled native object")
    if "_" in purpose:
        key, suffix = purpose.rsplit("_", 1)
        carriers = _document(cast(str, current["carriers_json"]))
        prior_carriers = _document(cast(str, before["carriers_json"]))
        record = carriers.get(key)
        if record is not None:
            phase = cast(dict[str, object], record)["phase"]
            if (
                suffix in {"carrier", "marker"}
                and prior_carriers.get(key) is None
                and phase == "allocated"
            ):
                return
            fingerprints = _document(cast(str, before["fingerprints_json"]))
            if (
                suffix == "artifact"
                and key in {"stage", "publication"}
                and fingerprints[key] is None
                and phase == "allocated"
            ):
                return
    raise IdentityMismatch(
        "claim phase lacks a controlled creation or native movement barrier"
    )


def _absent_allowed(spec: RoleSpec, snapshot: OwnerSnapshot) -> bool:
    if snapshot.owner.domain != "rescan":
        return False
    row = _row(snapshot)
    carriers = _document(cast(str, row["carriers_json"]))
    purpose = spec.role.purpose
    if purpose == "destination" or (
        purpose == "source"
        and row["source_path"] == row["destination_path"]
        and row["publication_receipt_json"] is not None
    ):
        if row["publication_receipt_json"] is None:
            return True
        rollback = carriers["rollback"]
        return rollback is not None and cast(dict[str, object], rollback)["phase"] in {
            "claiming",
            "claimed",
            "discarding",
            "discarded",
        }
    if purpose == "source":
        source = carriers["source"]
        return source is not None and cast(dict[str, object], source)["phase"] in {
            "claiming",
            "claimed",
            "restoring",
            "discarding",
            "discarded",
        }
    if "_" in purpose and purpose != "operation_parent":
        key, suffix = purpose.rsplit("_", 1)
        record = claims.CarrierRecord.from_json(_encode(carriers[key]))
        if record.phase == "discarded":
            return True
        if suffix == "artifact":
            if record.phase in {"allocated", "claiming", "discarding"}:
                fingerprints = _document(cast(str, row["fingerprints_json"]))
                return record.phase != "allocated" or (
                    key == "source" or fingerprints.get(key) is None
                )
            return (
                key == "publication"
                and record.phase == "restoring"
                and row["publication_receipt_json"] is not None
            )
    return False


def capture_set(
    snapshot: OwnerSnapshot,
    authority_id: str,
    epoch_id: str,
    guard: FileMutationGuard,
    *,
    previous: IdentityContext | None = None,
    dependencies: tuple[OwnerSnapshot, ...] = (),
    _budget: _CaptureBudget | None = None,
) -> ProofSet:
    """Capture complete phase evidence before the caller's SQL writer begins."""
    _verify_guard(guard)
    specs = claim_roles_for(snapshot)
    objects: list[ObjectProof] = []
    budget = _budget if _budget is not None else _CaptureBudget()
    absent: list[dict[str, object]] = []
    absent_purposes: set[str] = set()
    row = _row(snapshot)
    phase = (
        cast(str, row["state"]) if snapshot.owner.domain == "rescan" else "namespace"
    )
    intent: dict[str, object] | None = None
    if snapshot.owner.domain == "rescan":
        journal = cast(str, row["carriers_json"])
        intent = {
            "kind": "journal",
            "snapshot_sha256": snapshot.sha256,
            "phase": phase,
            "column": "carriers_json",
            "evidence_json": journal,
            "evidence_sha256": _digest(journal),
        }
    for spec in specs:
        _verify_guard(guard)
        try:
            with _opened(spec.path) as (fd, parent, name):
                info = _current_name(fd, parent, name)
                _policy(guard, spec, info)
                if spec.parent_role is not None or spec.role.purpose.endswith(
                    "_carrier"
                ):
                    if info.st_dev != _media.fstat(parent).st_dev:
                        raise IdentityMismatch(
                            "claim role crosses a nested media device"
                        )
                if spec.role.kind != "directory":
                    budget.hash_bytes += info.st_size
                    if budget.hash_bytes > _MAX_HASH_BYTES:
                        raise InvalidIdentity(
                            "complete claim set exceeds the fixed hash budget"
                        )
                captured = native.capture_native(fd)
                fingerprint = (
                    None
                    if spec.role.kind == "directory"
                    else _fingerprint(fd, parent, name, info)
                )
                expected = _legacy_fingerprint(spec, snapshot)
                if (
                    previous is None
                    and snapshot.owner.domain == "namespace"
                    and spec.role.purpose == "namespace_parent"
                ):
                    namespace_proof = claims._NamespaceProof.from_json(
                        cast(str, row["ownership_json"])
                    )
                    boundary = namespace_proof.creation_boundary
                    if boundary is not None and (
                        info.st_uid,
                        stat.S_IMODE(info.st_mode),
                    ) != (boundary.parent_uid, boundary.parent_mode):
                        raise IdentityMismatch(
                            "exact namespace creation boundary differs"
                        )
                if spec.role.purpose == "operation_parent" and dependencies:
                    namespace = claims._NamespaceProof.from_json(
                        cast(str, _row(dependencies[0])["ownership_json"])
                    )
                    expected = namespace.parent_dev, namespace.parent_ino
                if expected is not None:
                    actual = (
                        (info.st_dev, info.st_ino)
                        if isinstance(expected, tuple)
                        else fingerprint
                    )
                    if previous is None:
                        equal = actual == expected
                    else:
                        equal = (
                            (actual[1] == expected[1])
                            if isinstance(expected, tuple) and isinstance(actual, tuple)
                            else (
                                fingerprint is not None
                                and not isinstance(expected, tuple)
                                and (
                                    fingerprint.inode,
                                    fingerprint.size,
                                    fingerprint.mtime_ns,
                                    fingerprint.sha256,
                                )
                                == (
                                    expected.inode,
                                    expected.size,
                                    expected.mtime_ns,
                                    expected.sha256,
                                )
                            )
                        )
                    if not equal:
                        raise IdentityMismatch("exact legacy claim evidence differs")
                elif spec.role.kind != "directory":
                    raise IdentityMismatch(
                        "present claim role lacks an acknowledged fingerprint"
                    )
                constraints = _constraints(spec, snapshot, info, fingerprint)
                if previous is None and spec.role.kind == "marker":
                    if snapshot.owner.domain == "namespace":
                        payload = claims._NamespaceProof.from_json(
                            cast(str, row["ownership_json"])
                        ).marker_payload()
                    else:
                        key = spec.role.purpose.removesuffix("_marker")
                        record = claims.CarrierRecord.from_json(
                            _encode(_document(cast(str, row["carriers_json"]))[key])
                        )
                        payload = claims._carrier_payload(record)
                    if not isinstance(expected, claims.FullFileFingerprint):
                        raise IdentityMismatch("exact marker fingerprint is missing")
                    try:
                        claims._verify_marker(parent, expected, payload)
                    except claims.PrivateClaimError as exc:
                        raise IdentityMismatch(
                            "exact legacy marker binding differs"
                        ) from exc
                if native.capture_native(fd) != captured:
                    raise IdentityMismatch("claim native changed during capture")
                obj = ObjectProof(
                    spec.role, spec.path, spec.parent_role, captured, constraints
                )
                budget.wire_bytes += store._successor_size(
                    {
                        "role": asdict(obj.role),
                        "canonical_path": obj.canonical_path,
                        "parent_role": asdict(obj.parent_role)
                        if obj.parent_role
                        else None,
                        "native": obj.native.to_value(),
                        "constraints_json": obj.constraints_json,
                    }
                )
                if budget.wire_bytes > _MAX_WIRE_BYTES:
                    raise InvalidIdentity(
                        "complete claim set exceeds the fixed wire budget"
                    )
                objects.append(obj)
        except FileNotFoundError:
            if intent is None or not _absent_allowed(spec, snapshot):
                raise IdentityMismatch(
                    "complete claim phase object is missing"
                ) from None
            absent_purposes.add(spec.role.purpose)
            absent.append(
                {
                    "role": asdict(spec.role),
                    "canonical_path": spec.path,
                    "parent_role": asdict(spec.parent_role)
                    if spec.parent_role
                    else None,
                    "intent_sha256": _digest(_encode(intent)),
                }
            )
    if previous is not None:
        # A removed hard link is derived from the complete captured absence set,
        # never from a changed link count or an unacknowledged same-inode name.
        by_role = {obj.role: obj for obj in objects}
        for spec in specs:
            obj = by_role.get(spec.role)
            if obj is not None:
                _transition(
                    previous,
                    spec,
                    snapshot,
                    obj.native,
                    obj.constraints_json,
                    absent_purposes,
                )
    if snapshot.owner.domain == "rescan":
        present = {o.role.purpose for o in objects}
        carriers = _document(cast(str, row["carriers_json"]))
        source = carriers["source"]
        if (
            "source" not in present
            and "source_artifact" not in present
            and not (
                row["state"] in {"db_committed", "completed"}
                and source is not None
                and cast(dict[str, object], source)["phase"]
                in {"discarding", "discarded"}
            )
        ):
            raise IdentityMismatch("complete rescan phase has lost the original source")
        public_role = (
            "source" if row["source_path"] == row["destination_path"] else "destination"
        )
        if row["publication_receipt_json"] is not None and public_role not in present:
            rollback = carriers["rollback"]
            if rollback is None or (
                "rollback_artifact" not in present
                and cast(dict[str, object], rollback)["phase"]
                not in {"discarding", "discarded"}
            ):
                raise IdentityMismatch(
                    "complete rescan phase has lost its acknowledged publication"
                )
    manifest = _encode(
        {
            "version": 1,
            "snapshot_sha256": snapshot.sha256,
            "phase": phase,
            "required_roles": [asdict(s.role) for s in specs],
            "roles": [
                {
                    "role": asdict(o.role),
                    "canonical_path": o.canonical_path,
                    "parent_role": asdict(o.parent_role) if o.parent_role else None,
                    "native": o.native.to_value(),
                    "constraints_json": o.constraints_json,
                }
                for o in objects
            ],
            "absent_roles": absent,
            "required_dependencies": [
                store._owner_value(s.owner) for s in dependencies
            ],
            "dependencies": [
                {"snapshot_json": s.rows_json, "sha256": s.sha256, "requires": []}
                for s in dependencies
            ],
            "intent": intent,
        }
    )
    budget.wire_bytes += store._successor_size(snapshot.rows_json)
    budget.wire_bytes += store._successor_size(manifest)
    if budget.wire_bytes > _MAX_WIRE_BYTES:
        raise InvalidIdentity("complete claim set exceeds the fixed wire budget")
    _verify_guard(guard)
    return ProofSet(
        secrets.token_hex(16),
        snapshot.owner,
        snapshot,
        authority_id,
        epoch_id,
        "exact_legacy",
        tuple(objects),
        manifest,
    )


def enroll_exact_legacy(
    guard: FileMutationGuard, closure: ReferenceClosure, authority_id: str
) -> tuple[int, ...]:
    """Enroll the entire exact pre-drift closure while maintenance is exclusive."""
    from maintenance_lock import _assert_exclusive_maintenance
    from shared import get_db

    _assert_exclusive_maintenance(guard._db_path)
    guard.verify()
    closure.__post_init__()
    if not closure.snapshots or any(
        s.owner.domain not in {"namespace", "rescan"} for s in closure.snapshots
    ):
        raise IdentityUnavailable("exact enrollment closure has an unintegrated domain")
    namespaces = [s for s in closure.snapshots if s.owner.domain == "namespace"]
    if not namespaces:
        raise IdentityMismatch("exact enrollment requires complete namespaces")
    by_parent = {cast(str, s.owner.key[0]): s for s in namespaces}
    parents = tuple(sorted(by_parent))
    namespace_for: dict[OwnerRef, OwnerSnapshot] = {}
    for snapshot in closure.snapshots:
        parent = (
            cast(str, snapshot.owner.key[0])
            if snapshot.owner.domain == "namespace"
            else os.path.dirname(cast(str, _row(snapshot)["source_path"]))
        )
        namespace = by_parent.get(parent)
        if namespace is None:
            raise IdentityMismatch("exact enrollment omits an owner's namespace")
        namespace_for[snapshot.owner] = namespace
    with get_db() as db:
        assert_database(db, guard)
        if _collect_references(db, parents, InventoryBudget()) != closure:
            raise IdentityMismatch(
                "exact enrollment reference closure is incomplete or stale"
            )
        for snapshot in closure.snapshots:
            store.assert_snapshot(db, snapshot)
            if store._latest(db, snapshot.owner) is not None:
                raise IdentityMismatch(
                    "exact legacy enrollment cannot replace identity history"
                )
        authority = db.execute(
            "SELECT profile_json,qualification_sha256,declaration_json FROM file_identity_authorities WHERE authority_id=?",
            (authority_id,),
        ).fetchone()
        if authority is None:
            raise IdentityMismatch("exact enrollment authority is missing")
        profile = _document(cast(str, authority[0]))
        if authority[1] != profile["qualification_sha256"]:
            raise InvalidIdentity("exact enrollment authority qualification differs")
        declaration = store._version(
            store._load(authority[2]), {"scope_path", "scope_token"}
        )
        scope = store._path(declaration["scope_path"])
        store._hex(declaration["scope_token"], 32)
        if any(
            not store._within(spec.path, scope)
            for s in closure.snapshots
            for spec in claim_roles_for(s)
        ):
            raise IdentityMismatch(
                "exact enrollment role escapes declared authority scope"
            )
        native.validate_profile(
            native.StorageProfile(
                cast(str, profile["profile_id"]),
                cast(int, profile["revision"]),
                cast(str, profile["qualification_sha256"]),
                cast(dict[str, object], profile["client_contract"]),
                cast(dict[str, object], profile["server_contract"]),
            )
        )
    epochs = {s.owner: secrets.token_hex(16) for s in namespaces}
    if sum(len(claim_roles_for(s)) for s in closure.snapshots) > 10000:
        raise InvalidIdentity("exact enrollment exceeds the fixed role budget")
    budget = _CaptureBudget()
    proofs = [
        capture_set(
            s,
            authority_id,
            epochs[namespace_for[s.owner].owner],
            guard,
            dependencies=()
            if s.owner.domain == "namespace"
            else (namespace_for[s.owner],),
            _budget=budget,
        )
        for s in closure.snapshots
    ]
    _assert_exclusive_maintenance(guard._db_path)
    guard.verify()
    events: list[int] = []
    with get_db() as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        assert_database(db, guard)
        if _capture_references(db, parents, InventoryBudget()) != closure:
            raise IdentityMismatch(
                "exact enrollment reference closure changed before CAS"
            )
        for namespace in namespaces:
            ownership = cast(str, _row(namespace)["ownership_json"])
            legacy = claims._NamespaceProof.from_json(ownership)
            parent = cast(str, namespace.owner.key[0])
            db.execute(
                "INSERT INTO file_identity_epochs(epoch_id,authority_id,origin_path,allocation_path,root_path,birth_json) VALUES(?,?,?,?,?,?)",
                (
                    epochs[namespace.owner],
                    authority_id,
                    parent,
                    parent,
                    legacy.root_path,
                    _encode(
                        {
                            "version": 1,
                            "layout": "same_parent",
                            "enrollment": "exact_legacy",
                            "legacy_namespace": {
                                "version": legacy.version,
                                "json": ownership,
                                "sha256": _digest(ownership),
                            },
                            "controlled_birth": None,
                        }
                    ),
                ),
            )
        for proof in proofs:
            prepared = store.prepare_set(db, proof, None)
            events.append(store.activate_set(db, proof, proof.snapshot, prepared))
    with get_db() as db:
        assert_database(db, guard)
        for proof, event in zip(proofs, events, strict=True):
            active = store.read_active(db, proof.owner)
            if active is None or active.event_id != event or active.proof != proof:
                raise IdentityMismatch("exact enrollment durable activation differs")
    return tuple(events)

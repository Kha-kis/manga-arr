"""Import staging: two-phase commit with hidden staging directory."""
import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile as _tempfile
from dataclasses import dataclass

from files import _maybe_convert_to_cbz
from comicinfo import _try_inject_comicinfo
from events import log_event
from shared import get_cfg

# Staging root for auto-packed image-only chapter dirs (PR #147).
# Default value; tests monkeypatch import_pipeline.PACK_STAGING_ROOT at runtime.
PACK_STAGING_ROOT = '/config/mangarr-image-pack'


def _cleanup_pack_staging_dir(download_id: str) -> None:
    """Remove the per-queue auto-pack staging dir, if present.

    Reads PACK_STAGING_ROOT from import_pipeline at runtime to support
    monkeypatching by tests.
    """
    if not download_id:
        return
    try:
        from import_pipeline import PACK_STAGING_ROOT as _psr
        staging_root = _psr
    except ImportError:
        staging_root = PACK_STAGING_ROOT
    pack_dir = os.path.join(staging_root, f'queue-{download_id}')
    if os.path.isdir(pack_dir):
        shutil.rmtree(pack_dir, ignore_errors=True)


@dataclass(slots=True)
class _StagedFile:
    """One source/staging/final path tuple owned by a staging batch."""

    stage_path: str
    final_path: str
    src_path: str


@dataclass(slots=True)
class _StageOutcome:
    """Phase 2 result for one file, including its post-transform stage path."""

    file_id: int
    ok: bool
    final_dst: str
    error: str
    stage_path: str = ""


class _ImportStaging:
    """Per-import-batch staging directory + two-phase commit.

    Usage:
        staging = _ImportStaging(dst_dir, queue_id, import_mode)
        try:
            for f in files:
                stage_path = staging.stage(src, final_path)
                # ... transforms operate on stage_path ...
                # If a transform renamed the in-staging file:
                final_path = staging.rename(stage_path, new_stage_path)
            staging.commit_all()
        except Exception:
            staging.rollback()
            raise
    """

    def __init__(
        self,
        dst_dir: str,
        queue_id: int,
        import_mode: str,
        *,
        staging_dir: str | None = None,
        journal_owned: bool = False,
        publication_id: int | None = None,
        owner_token: str | None = None,
    ) -> None:
        self.dst_dir = dst_dir
        self.import_mode = import_mode
        self.journal_owned = journal_owned
        self.publication_id = publication_id
        self.owner_token = owner_token
        self._pinned_fd: int | None = None
        if staging_dir is None:
            self.staging_dir = _tempfile.mkdtemp(
                prefix=f".mangarr-staging-{queue_id}-",
                dir=dst_dir,
            )
        else:
            self.staging_dir = staging_dir
            if not journal_owned:
                os.makedirs(self.staging_dir, mode=0o700, exist_ok=True)
        self._staged: list[_StagedFile] = []

    @property
    def records(self) -> tuple[_StagedFile, ...]:
        """Return immutable access to staged path records."""
        return tuple(self._staged)

    def stage(self, src: str, final_path: str) -> str:
        """Place `src` at a staging path using per-mode strategy.
        Returns the staging path. Raises OSError on filesystem failure.
        """
        fname = os.path.basename(final_path)
        stage_path = os.path.join(self.staging_dir, fname)
        if self.journal_owned:
            if self._pinned_fd is None:
                raise RuntimeError("journal staging requires its pinned synchronous worker")
            if self.import_mode == "hardlink":
                os.link(src, fname, dst_dir_fd=self._pinned_fd, follow_symlinks=False)
            else:
                source_fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    source_stat = os.fstat(source_fd)
                    if not stat.S_ISREG(source_stat.st_mode):
                        raise OSError("stage source is not regular")
                    target_fd = os.open(fname, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self._pinned_fd)
                    try:
                        # copy2 follows the already-open target descriptor, not
                        # the carrier's replaceable public absolute path.
                        shutil.copy2(f"/proc/self/fd/{source_fd}", f"/proc/self/fd/{target_fd}")
                        os.fsync(target_fd)
                    finally:
                        os.close(target_fd)
                finally:
                    os.close(source_fd)
            os.fsync(self._pinned_fd)
        elif self.import_mode == 'hardlink':
            os.link(src, stage_path)
        else:
            shutil.copy2(src, stage_path)
        self._staged.append(
            _StagedFile(
                stage_path=stage_path,
                final_path=final_path,
                src_path=src,
            )
        )
        return stage_path

    def rename(self, old_stage_path: str, new_stage_path: str) -> str:
        """Tell the helper that an in-staging transform renamed the staged file."""
        for rec in self._staged:
            if rec.stage_path == old_stage_path:
                rec.stage_path = new_stage_path
                new_basename = os.path.basename(new_stage_path)
                rec.final_path = os.path.join(
                    os.path.dirname(rec.final_path), new_basename,
                )
                return rec.final_path
        raise ValueError(f"rename on unknown stage path: {old_stage_path!r}")

    def prepare_for_mutation(self, stage_path: str) -> str:
        """Break a source hardlink before an in-place staged-file mutation.

        The private copy is written beside the staged file and atomically
        replaces only the staging-directory entry. Only a staged inode with
        exactly one link is already demonstrably private.
        """
        if self.import_mode != "hardlink":
            return stage_path

        record = next(
            (rec for rec in self._staged if rec.stage_path == stage_path),
            None,
        )
        if record is None:
            raise ValueError(f"mutation on unknown stage path: {stage_path!r}")

        open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        work_path = stage_path
        work_dir = self.staging_dir
        if self.journal_owned:
            if self._pinned_fd is None:
                raise RuntimeError("COW requires a pinned stage worker")
            work_dir = f"/proc/self/fd/{self._pinned_fd}"
            work_path = os.path.join(work_dir, os.path.basename(stage_path))
        stage_fd = os.open(work_path, open_flags)
        temp_fd = -1
        temp_path = ""
        try:
            staged_stat = os.fstat(stage_fd)
            if not stat.S_ISREG(staged_stat.st_mode):
                raise OSError(f"staged path is not a regular file: {stage_path!r}")
            if staged_stat.st_nlink == 1:
                return stage_path

            temp_fd, temp_path = _tempfile.mkstemp(
                prefix=".mangarr-cow-",
                dir=work_dir,
            )
            while chunk := os.read(stage_fd, 1024 * 1024):
                remaining = memoryview(chunk)
                while remaining:
                    written = os.write(temp_fd, remaining)
                    if written == 0:
                        raise OSError("short write while copying staged archive")
                    remaining = remaining[written:]
            os.fchmod(temp_fd, stat.S_IMODE(staged_stat.st_mode))
            os.utime(
                temp_fd,
                ns=(staged_stat.st_atime_ns, staged_stat.st_mtime_ns),
            )
            os.fsync(temp_fd)
            os.close(temp_fd)
            temp_fd = -1

            current_stat = os.stat(work_path, follow_symlinks=False)
            if (
                current_stat.st_dev != staged_stat.st_dev
                or current_stat.st_ino != staged_stat.st_ino
            ):
                raise RuntimeError(
                    f"staged path changed during copy-on-write: {stage_path!r}"
                )
            os.replace(temp_path, work_path)
            temp_path = ""
            return stage_path
        finally:
            os.close(stage_fd)
            if temp_fd >= 0:
                os.close(temp_fd)
            if temp_path:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass

    def commit_all(self) -> None:
        """Move every staged file to its final destination."""
        if self.journal_owned:
            raise RuntimeError("journal publication requires its durable batch decision")
        for rec in self._staged:
            os.replace(rec.stage_path, rec.final_path)
        if self.import_mode == 'move' and not self.journal_owned:
            for rec in self._staged:
                try:
                    os.unlink(rec.src_path)
                except FileNotFoundError:
                    pass
                except OSError as e:
                    log_event(
                        "error",
                        f"[Import] could not remove source {rec.src_path}: {e}",
                    )
        self._cleanup()

    def rollback(self) -> None:
        """Remove every staged file; sources are untouched."""
        if self.journal_owned:
            from import_publication import abort_private_staging
            if self.publication_id is None or not abort_private_staging(self.publication_id):
                raise RuntimeError("private staging abort retained its journal/fence")
            return
        self._cleanup()

    def _cleanup(self) -> None:
        if self.journal_owned:
            raise RuntimeError("journal stage cleanup requires durable private discard")
        try:
            shutil.rmtree(self.staging_dir)
        except FileNotFoundError:
            pass
        except OSError as e:
            log_event(
                "error",
                f"[Import] failed to clean staging dir {self.staging_dir}: {e}",
            )

    def stage_one(self, plan, fp) -> _StageOutcome:
        """One synchronous mutation unit, including a pinned trusted exec."""
        from import_publication import (
            PublicationBlocked, _read_publication, _regular_fingerprint,
            open_publication_stage,
        )
        from private_pack_source import _open_pack_file_source
        import shared
        if self.publication_id is None or self.owner_token is None:
            raise RuntimeError("missing journal staging identity")
        with open_publication_stage(self.publication_id, self.owner_token) as (guard, carrier):
            self._pinned_fd = carrier.fd
            try:
                publication = _read_publication(self.publication_id)
                if publication is None:
                    raise PublicationBlocked("stage source admission journal missing")
                snapshot = publication.plan.queue.get("_pack_source_origins")
                if (not isinstance(snapshot, dict) or set(snapshot) != {"version", "files"}
                        or type(snapshot["version"]) is not int or snapshot["version"] != 1
                        or not isinstance(snapshot["files"], dict)):
                    raise PublicationBlocked("stage source origin snapshot missing or invalid")
                origin = snapshot["files"].get(str(fp.file_id))
                if not isinstance(origin, dict):
                    raise PublicationBlocked("stage source origin admission missing")
                with _open_pack_file_source(
                    guard, publication.plan.queue, fp.file_id, fp.src_path, origin,
                ) as source:
                    if source is None:
                        stage_path = self.stage(fp.src_path, fp.dst_path)
                    else:
                        source_fd, source_alias, expected = source
                        name = os.path.basename(fp.dst_path)
                        stage_path = os.path.join(self.staging_dir, name)
                        if self.import_mode == "hardlink":
                            os.link(source_alias, name, dst_dir_fd=carrier.fd, follow_symlinks=False)
                        else:
                            target = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                             | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=carrier.fd)
                            try:
                                shutil.copy2(f"/proc/self/fd/{source_fd}", f"/proc/self/fd/{target}")
                                os.fsync(target)
                            finally:
                                os.close(target)
                        os.fsync(carrier.fd)
                        staged = _regular_fingerprint(
                            f"/proc/self/fd/{carrier.fd}/{name}", include_hash=True,
                        )
                        if (staged.size, staged.sha256) != (expected.size, expected.sha256):
                            raise PublicationBlocked("staged pack bytes differ from admitted source")
                        self._staged.append(_StagedFile(stage_path, fp.dst_path, fp.src_path))
                mutation = bool(plan.series) and os.path.splitext(stage_path)[1].lower() not in (".epub", ".pdf", ".mobi", ".azw3")
                if mutation:
                    self.prepare_for_mutation(stage_path)
                payload = {"fd": carrier.fd, "name": os.path.basename(stage_path), "db_path": shared.DB_PATH,
                           "series": dict(plan.series) if plan.series else None,
                           "tags": plan.series_tags, "chapter": fp.file_type == "chapter",
                           "number": fp.proposed_chap if fp.file_type == "chapter" else fp.proposed_vol}
                result = subprocess.run(
                    [sys.executable, "-c", _PINNED_TRANSFORM_WORKER],
                    input=json.dumps(payload), text=True, capture_output=True, check=True,
                    pass_fds=(carrier.fd, guard.subprocess_fd),
                    env={**os.environ, "PYTHONPATH": os.path.dirname(__file__) + os.pathsep + os.environ.get("PYTHONPATH", "")},
                )
                name = json.loads(result.stdout)["name"]
                if not isinstance(name, str) or os.path.basename(name) != name or name in ("", ".", "..", "owner.json"):
                    raise RuntimeError("transform result escaped private stage")
                transformed = os.path.join(self.staging_dir, name)
                final = self.rename(stage_path, transformed) if transformed != stage_path else fp.dst_path
                os.fsync(carrier.fd)
                carrier.verify()
                return _StageOutcome(fp.file_id, True, final, "", transformed)
            finally:
                self._pinned_fd = None


_PINNED_TRANSFORM_WORKER = """
import json, os, sys
import shared
from files import _maybe_convert_to_cbz
from comicinfo import _try_inject_comicinfo
p = json.loads(sys.stdin.read())
shared.DB_PATH = p['db_path']
# The decoder sees the worker PID, not its own /proc/self/fd namespace.
path = '/proc/%s/fd/%s/%s' % (os.getpid(), p['fd'], p['name'])
result = _maybe_convert_to_cbz(path)
if p['series']:
    kwargs = {'chapter_num' if p['chapter'] else 'volume_num': p['number']}
    _try_inject_comicinfo(result, p['series'], tags=p['tags'], **kwargs)
print(json.dumps({'name': os.path.basename(result)}))
"""


async def _stage_files(
    plan,
    staging: _ImportStaging,
) -> list['_StageOutcome']:
    """Phase 2: filesystem operations only (no DB)."""
    outcomes: list['_StageOutcome'] = []
    for fp in plan.files:
        if fp.plan_status != 'ready':
            outcomes.append(_StageOutcome(
                file_id=fp.file_id, ok=False, final_dst='', error='', stage_path='',
            ))
            continue
        try:
            if staging.journal_owned:
                outcomes.append(await asyncio.to_thread(staging.stage_one, plan, fp))
                continue
            stage_path = await asyncio.to_thread(staging.stage, fp.src_path, fp.dst_path)
            comicinfo_mutation_requested = (
                bool(plan.series)
                and os.path.splitext(stage_path)[1].lower()
                not in (".epub", ".pdf", ".mobi", ".azw3")
            )
            if comicinfo_mutation_requested:
                stage_path = await asyncio.to_thread(
                    staging.prepare_for_mutation,
                    stage_path,
                )
            stage_after = await asyncio.to_thread(_maybe_convert_to_cbz, stage_path)
            final_dst = fp.dst_path
            if stage_after != stage_path:
                final_dst = staging.rename(stage_path, stage_after)
            if plan.series:
                if fp.file_type == 'chapter':
                    await asyncio.to_thread(
                        _try_inject_comicinfo,
                        stage_after, plan.series,
                        chapter_num=fp.proposed_chap, tags=plan.series_tags,
                    )
                else:
                    await asyncio.to_thread(
                        _try_inject_comicinfo,
                        stage_after, plan.series,
                        volume_num=fp.proposed_vol, tags=plan.series_tags,
                    )
            outcomes.append(_StageOutcome(
                file_id=fp.file_id,
                ok=True,
                final_dst=final_dst,
                error='',
                stage_path=stage_after,
            ))
        except Exception as e:
            outcomes.append(_StageOutcome(
                file_id=fp.file_id,
                ok=False,
                final_dst='',
                error=str(e),
                stage_path='',
            ))
    return outcomes


def _make_stage_outcome(
    file_id: int,
    ok: bool,
    final_dst: str,
    error: str,
    stage_path: str = "",
) -> _StageOutcome:
    """Factory for _StageOutcome instances."""
    return _StageOutcome(
        file_id=file_id,
        ok=ok,
        final_dst=final_dst,
        error=error,
        stage_path=stage_path,
    )

"""Import staging: two-phase commit with hidden staging directory."""
import asyncio
import os
import shutil
import stat
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
    ) -> None:
        self.dst_dir = dst_dir
        self.import_mode = import_mode
        self.journal_owned = journal_owned
        if staging_dir is None:
            self.staging_dir = _tempfile.mkdtemp(
                prefix=f".mangarr-staging-{queue_id}-",
                dir=dst_dir,
            )
        else:
            self.staging_dir = staging_dir
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
        if self.import_mode == 'hardlink':
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
        stage_fd = os.open(stage_path, open_flags)
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
                dir=self.staging_dir,
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

            current_stat = os.stat(stage_path, follow_symlinks=False)
            if (
                current_stat.st_dev != staged_stat.st_dev
                or current_stat.st_ino != staged_stat.st_ino
            ):
                raise RuntimeError(
                    f"staged path changed during copy-on-write: {stage_path!r}"
                )
            os.replace(temp_path, stage_path)
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
        self._cleanup()

    def _cleanup(self) -> None:
        try:
            shutil.rmtree(self.staging_dir)
        except FileNotFoundError:
            pass
        except OSError as e:
            log_event(
                "error",
                f"[Import] failed to clean staging dir {self.staging_dir}: {e}",
            )


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

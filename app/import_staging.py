"""Import staging: two-phase commit with hidden staging directory."""
import asyncio
import os
import shutil
import tempfile as _tempfile

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

    def __init__(self, dst_dir: str, queue_id: int, import_mode: str):
        self.dst_dir = dst_dir
        self.import_mode = import_mode
        self.staging_dir = _tempfile.mkdtemp(
            prefix=f".mangarr-staging-{queue_id}-",
            dir=dst_dir,
        )
        self._staged: list[dict] = []

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
        self._staged.append({
            'stage_path': stage_path,
            'final_path': final_path,
            'src_path': src,
        })
        return stage_path

    def rename(self, old_stage_path: str, new_stage_path: str) -> str:
        """Tell the helper that an in-staging transform renamed the staged file."""
        for rec in self._staged:
            if rec['stage_path'] == old_stage_path:
                rec['stage_path'] = new_stage_path
                new_basename = os.path.basename(new_stage_path)
                rec['final_path'] = os.path.join(
                    os.path.dirname(rec['final_path']), new_basename,
                )
                return rec['final_path']
        raise ValueError(f"rename on unknown stage path: {old_stage_path!r}")

    def commit_all(self) -> None:
        """Move every staged file to its final destination."""
        for rec in self._staged:
            os.replace(rec['stage_path'], rec['final_path'])
        if self.import_mode == 'move':
            for rec in self._staged:
                try:
                    os.unlink(rec['src_path'])
                except FileNotFoundError:
                    pass
                except OSError as e:
                    print(f"[Import] could not remove source {rec['src_path']}: {e}")
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
            print(f"[Import] failed to clean staging dir {self.staging_dir}: {e}")


async def _stage_files(
    plan,
    staging: _ImportStaging,
) -> list['_StageOutcome']:
    """Phase 2: filesystem operations only (no DB)."""
    outcomes: list['_StageOutcome'] = []
    for fp in plan.files:
        if fp.plan_status != 'ready':
            outcomes.append(_StageOutcome(
                file_id=fp.file_id, ok=False, final_dst='', error='',
            ))
            continue
        try:
            stage_path = await asyncio.to_thread(staging.stage, fp.src_path, fp.dst_path)
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
                file_id=fp.file_id, ok=True, final_dst=final_dst, error='',
            ))
        except Exception as e:
            outcomes.append(_StageOutcome(
                file_id=fp.file_id, ok=False, final_dst='', error=str(e),
            ))
    return outcomes


def _make_stage_outcome(file_id: int, ok: bool, final_dst: str, error: str):
    """Factory for _StageOutcome instances."""
    return _StageOutcome(file_id=file_id, ok=ok, final_dst=final_dst, error=error)


# Data class for Phase 2 results
class _StageOutcome:
    """Phase 2 result for one file. Carries the final destination
    after any CBR→CBZ rename and the error string if staging failed.
    """
    def __init__(self, file_id: int, ok: bool, final_dst: str, error: str):
        self.file_id = file_id
        self.ok = ok
        self.final_dst = final_dst
        self.error = error

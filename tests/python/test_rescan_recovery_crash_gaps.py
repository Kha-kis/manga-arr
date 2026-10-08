"""Real process kills in unacknowledged result windows, not live NFS tests."""

import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys

import pytest

from test_rescan_transactions import rescan_env as rescan_env
from test_rescan_nfs_recovery import (
    _seed_source,
    _run_child,
    _source_artifacts,
    _operation_carrier_dirs,
    _volume_state,
)


_CHILD = r"""
import errno, os, signal, sys
import rescan
import rescan_file_recovery as recovery
import private_file_claim as claims
import shared
shared.DB_PATH = sys.argv[1]
shared.CONFIG.clear()
shared.CONFIG['folder_format'] = ''
checkpoint = sys.argv[2]
def kill():
    print('killed:' + checkpoint, flush=True)
    os.kill(os.getpid(), signal.SIGKILL)
if checkpoint == 'native_receipt':
    real = rescan._rename_noreplace
    def native(*args):
        result = real(*args)
        assert result is True
        kill()
    rescan._rename_noreplace = native
elif checkpoint in ('link_receipt', 'restore_receipt'):
    if checkpoint == 'link_receipt':
        def unsupported(*args):
            raise OSError(errno.EOPNOTSUPP, 'injected unsupported renameat2')
        rescan._rename_noreplace = unsupported
    else:
        recovery._commit = lambda *args: False
    real = claims.link_private_regular
    def link(*args):
        result = real(*args)
        kill()
    claims.link_private_regular = link
elif checkpoint == 'discard_result':
    real = claims.discard_private_regular
    def discard(*args):
        real(*args)
        kill()
    claims.discard_private_regular = discard
rescan.rescan_series_folder(7)
"""


def _kill(env, checkpoint):
    root = Path(__file__).resolve().parents[2]
    child_env = {**os.environ, "PYTHONPATH": str(root / "app")}
    child = subprocess.run(
        [sys.executable, "-c", _CHILD, env["db_path"], checkpoint],
        cwd=root,
        env=child_env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert child.returncode == -signal.SIGKILL, (child.stdout, child.stderr)
    assert "killed:" + checkpoint in child.stdout


@pytest.mark.parametrize("checkpoint", ["native_receipt", "link_receipt"])
def test_unacknowledged_publication_is_retained_and_fenced_after_real_kill(
    rescan_env, checkpoint
):
    volume_id, source, original = _seed_source(rescan_env, "cbz")
    _kill(rescan_env, checkpoint)
    public = source.read_bytes()
    assert public != original
    retained = _source_artifacts(rescan_env, source)
    assert len(retained) == 1 and retained[0].read_bytes() == original
    before = _volume_state(rescan_env, volume_id)
    restarted = _run_child(rescan_env, "normal", "cbz")
    assert restarted.returncode == 0, restarted.stderr
    assert source.read_bytes() == public
    assert retained[0].read_bytes() == original
    assert _volume_state(rescan_env, volume_id) == before
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute(
            "SELECT state,publication_receipt_json FROM rescan_file_operations"
        ).fetchone() == ("rollback", None)


def test_unacknowledged_restore_link_is_not_same_inode_authority(rescan_env):
    volume_id, source, original = _seed_source(rescan_env, "cbz")
    _kill(rescan_env, "restore_receipt")
    retained = _source_artifacts(rescan_env, source)
    assert len(retained) == 1 and retained[0].read_bytes() == original
    assert source.read_bytes() == original
    assert source.stat().st_ino == retained[0].stat().st_ino
    before = _volume_state(rescan_env, volume_id)
    restarted = _run_child(rescan_env, "normal", "cbz")
    assert restarted.returncode == 0, restarted.stderr
    assert source.read_bytes() == original
    assert retained[0].read_bytes() == original
    assert _volume_state(rescan_env, volume_id) == before
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute("SELECT state FROM rescan_file_operations").fetchone() == (
            "rollback",
        )


def test_committed_private_unlink_result_gap_finishes_without_rollback(rescan_env):
    volume_id, source, original = _seed_source(rescan_env, "cbz")
    _kill(rescan_env, "discard_result")
    public = source.read_bytes()
    assert public != original
    before = _volume_state(rescan_env, volume_id)
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute("SELECT state FROM rescan_file_operations").fetchone() == (
            "db_committed",
        )
    restarted = _run_child(rescan_env, "normal", "cbz")
    assert restarted.returncode == 0, restarted.stderr
    assert source.read_bytes() == public
    assert _volume_state(rescan_env, volume_id) == before
    assert not _operation_carrier_dirs(source)
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute("SELECT state FROM rescan_file_operations").fetchone() == (
            "completed",
        )

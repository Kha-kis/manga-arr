"""A complete linked queue remains fenced until pack promotion settles."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

from file_mutation_lock import file_mutation_guard
from test_import_pack_cleanup_durability import (
    _PackEnv,
    _terminal_queue,
    pack_env,  # noqa: F401
)


_KILL_AFTER_QUEUE_COMMIT = """
import errno
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys

import conftest  # Existing container-path/static-file test scaffolding.
import import_pipeline
import import_pack_cleanup
import main
import shared
from test_import_pack_nfs_lifecycle import _queue_images

os.umask(0o022)
raw = json.loads(sys.argv[1])
env = {key: value if key == 'db_path' else Path(value)
       for key, value in raw.items()}
main.DB_PATH = shared.DB_PATH = env['db_path']
import_pipeline.PACK_STAGING_ROOT = str(env['pack_root'])
main.load_config()

def unsupported(source, destination):
    raise OSError(errno.EINVAL, 'unsupported directory NOREPLACE', destination)

import_pack_cleanup._rename_noreplace = unsupported
real_connect = sqlite3.connect

class CrashConnection(sqlite3.Connection):
    queue_inserted = False

    def execute(self, sql, parameters=()):
        result = super().execute(sql, parameters)
        if sql.startswith('INSERT INTO import_queue('):
            self.queue_inserted = True
        return result

    def commit(self):
        super().commit()
        if self.queue_inserted:
            os.kill(os.getpid(), signal.SIGKILL)

def connect(*args, **kwargs):
    kwargs['factory'] = CrashConnection
    return real_connect(*args, **kwargs)

sqlite3.connect = connect
_queue_images(env, 'linked-provisional-crash')
raise AssertionError('queue COMMIT crash checkpoint not reached')
"""


def test_crash_after_complete_queue_link_blocks_matching_claim_before_promotion(
    pack_env: _PackEnv,
) -> None:
    import import_lease
    import main

    root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [
            str(root / "app"),
            str(root / "tests" / "python"),
            environment.get("PYTHONPATH", ""),
        ]
    )
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            _KILL_AFTER_QUEUE_COMMIT,
            json.dumps({key: str(value) for key, value in pack_env.items()}),
        ],
        cwd=root,
        env=environment,
        timeout=30,
        capture_output=True,
        text=True,
    )
    assert child.returncode == -signal.SIGKILL, child.stdout + child.stderr

    with sqlite3.connect(pack_env["db_path"]) as db:
        queues = db.execute("SELECT id,status FROM import_queue").fetchall()
        assert len(queues) == 1 and queues[0][1] == "pending"
        queue_id = queues[0][0]
        rows = db.execute("SELECT queue_id,src_path FROM import_queue_files").fetchall()
        assert len(rows) == 1 and rows[0][0] == queue_id
        reservation = db.execute(
            "SELECT purpose,queue_id,directory_ownership_json"
            " FROM import_pack_cleanup_reservations"
        ).fetchone()
        assert reservation is not None and reservation[:2] == ("cleanup", queue_id)
        assert json.loads(reservation[2])["phase"] == "queued"
    with zipfile.ZipFile(rows[0][1]) as archive:
        assert archive.read("001.jpg") == b"page-one"

    # The child died before _finish_generated_pack_queue. A fresh sidecar owner
    # can acquire now, but the exact linked queue is still SQL-fenced.
    with file_mutation_guard(pack_env["db_path"]):
        for expired in (False, True):
            with main.get_db() as db:
                if expired:
                    db.execute(
                        "UPDATE import_pack_cleanup_reservations"
                        " SET expires_at=datetime('now','-1 second')"
                    )
                    db.commit()
                assert not import_lease.claim_import_queue_row(
                    db, queue_id, "linked-consumer"
                )
                assert tuple(
                    db.execute(
                        "SELECT status,lease_owner,lease_expires_at"
                        " FROM import_queue WHERE id=?",
                        (queue_id,),
                    ).fetchone()
                ) == ("pending", None, None)

        unrelated = _terminal_queue(pack_env["db_path"], "unrelated-crash-control")
        with main.get_db() as db:
            db.execute(
                "UPDATE import_queue SET status='pending' WHERE id=?", (unrelated,)
            )
            db.commit()
            assert import_lease.claim_import_queue_row(db, unrelated, "unrelated")
        with sqlite3.connect(pack_env["db_path"]) as db:
            assert db.execute(
                "SELECT purpose,queue_id FROM import_pack_cleanup_reservations"
            ).fetchone() == ("cleanup", queue_id)
            assert db.execute(
                "SELECT COUNT(*) FROM import_publications"
            ).fetchone() == (0,)
            assert db.execute(
                "SELECT COUNT(*) FROM history WHERE event_type='imported'"
            ).fetchone() == (0,)

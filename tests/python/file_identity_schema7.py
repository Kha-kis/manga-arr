"""Genuine predecessor initialization, never a schema8 database backstamp."""

from pathlib import Path
import sqlite3

import pytest


def initialize_schema7(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import schema
    import shared

    def skip_new_migration(db: sqlite3.Connection) -> None:
        """Leave the predecessor's own version/trigger/catalog untouched."""

    with monkeypatch.context() as patch:
        patch.setattr(shared, "DB_PATH", str(path))
        patch.setattr(
            schema,
            "_migrate_file_identity_provisioning",
            skip_new_migration,
            raising=False,
        )
        schema.init_db()


@pytest.fixture(autouse=True)
def predecessor_init(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy migration test modules bootstrap before schema8 exists.

    Only the main.py alias used by their media fixtures is changed. Their real
    calls to schema.init_db/_migrate_schema_constraints still upgrade normally.
    """
    import main
    import shared

    def initialize() -> None:
        initialize_schema7(Path(shared.DB_PATH), monkeypatch)

    monkeypatch.setattr(main, "init_db", initialize)

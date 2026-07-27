"""Focused route regression tests for system-log filtering."""

from __future__ import annotations

import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401, E402


@pytest.fixture
def logs_env(tmp_path, monkeypatch):
    import main
    import security
    import shared

    db_path = str(tmp_path / "logs-filter.db")
    key_dir = str(tmp_path / "keys")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(shared, "DB_PATH", db_path)
    monkeypatch.setattr(security, "_SECRET_CIPHER", None)
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()
    return main, db_path


def test_logs_page_filters_warning_events(logs_env):
    main, db_path = logs_env
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO events(event_type, message) VALUES(?, ?)",
            ("warning", "warning-filter-canary"),
        )
        db.execute(
            "INSERT INTO events(event_type, message) VALUES(?, ?)",
            ("error", "error-filter-canary"),
        )

    response = TestClient(main.app).get("/logs?event_type=warning")

    assert response.status_code == 200
    assert "warning-filter-canary" in response.text
    assert "error-filter-canary" not in response.text

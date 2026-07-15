import sqlite3

import cli


def test_admin_reset_uses_config_directory_and_revokes_sessions(tmp_path, capsys):
    db_path = tmp_path / "manga_arr.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE auth_admin(id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE auth_sessions(token_hash TEXT)")
        connection.execute("INSERT INTO auth_admin(id) VALUES(1)")
        connection.execute("INSERT INTO auth_sessions(token_hash) VALUES('session')")
    legacy_token = tmp_path / ".mangarr-setup-token"
    legacy_token.write_text("obsolete")

    result = cli.main(["--config-dir", str(tmp_path), "admin", "reset", "--yes"])

    assert result == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM auth_admin").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 0
        )
    assert not legacy_token.exists()
    assert "browser sessions revoked" in capsys.readouterr().out

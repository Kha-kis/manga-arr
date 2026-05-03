"""Per-route partial-POST regression tests for the editor-clobber epic.

The shape: seed a row with non-default values in every clobber-prone
column, POST a partial form containing only one field, assert the named
column changed AND every other column kept its seeded value.

This file accumulates routes as the conversion PRs land:

  PR-B: edit_series, edit_indexer, edit_download_client (this file)
  PR-C: 5 profile routes + import_lists (added in PR-C)
  PR-D: edit_notification_connection + settings (added in PR-D)
"""
import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


@pytest.fixture
def env(tmp_path):
    """Fresh DB; tests seed their own rows directly."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-partial-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()
    main.ensure_api_key()
    try:
        yield {'db_path': db.name}
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _client():
    import main
    from fastapi.testclient import TestClient
    return TestClient(main.app)


def _csrf(tag="t"):
    tok = f"csrf-{tag}-" + "x" * 30
    return {
        'cookies': {'csrftoken': tok},
        'headers': {'X-CSRFToken': tok},
    }


# ═════════════════════════════════════════════════════════════════════
# edit_series — POST /series/{series_id}/edit
# ═════════════════════════════════════════════════════════════════════


def _seed_series(db_path) -> int:
    """Insert a series with non-default values in every clobber-prone
    column. Returns the series id."""
    with sqlite3.connect(db_path) as c:
        c.execute("""
            INSERT INTO series(
                title, search_pattern, monitored, status,
                preferred_groups, blocked_groups, omnibus_preference,
                quality_profile_id, language_profile_id, quality_cutoff,
                update_strategy, required_scanlator, edition_type,
                source_type, ddl_language, total_volumes, vol_count_source
            ) VALUES (
                'Seeded', 'seeded-search', 1, 'continuing',
                '["LuCaZ","Stick"]', '["BadGroup"]', 'prefer_omnibus',
                NULL, NULL, 'cbz',
                'throttled', 'LuCaZ', 'omnibus',
                'official_only', 'en', 12, 'manual'
            )
        """)
        return c.execute(
            "SELECT id FROM series WHERE title='Seeded'"
        ).fetchone()[0]


def test_edit_series_partial_post_does_not_clobber_unmentioned_fields(env):
    """Submit only `title` and verify every other clobber-prone column
    keeps its seeded value. Without the partial-POST fix, this submit
    would reset preferred_groups, blocked_groups, omnibus_preference,
    quality_cutoff, update_strategy, required_scanlator, edition_type,
    source_type, and ddl_language to defaults."""
    sid = _seed_series(env['db_path'])
    csrf = _csrf("series-partial")

    r = _client().post(
        f"/series/{sid}/edit",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'title': 'Updated Title',
            # Deliberately omit everything else
        },
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = dict(c.execute(
            "SELECT * FROM series WHERE id=?", (sid,)
        ).fetchone())

    assert row['title']            == 'Updated Title'
    # Every other clobber-prone column must still hold the seeded value
    assert row['preferred_groups']    == '["LuCaZ","Stick"]'
    assert row['blocked_groups']      == '["BadGroup"]'
    assert row['omnibus_preference']  == 'prefer_omnibus'
    assert row['quality_cutoff']      == 'cbz'
    assert row['update_strategy']     == 'throttled'
    assert row['required_scanlator']  == 'LuCaZ'
    assert row['edition_type']        == 'omnibus'
    assert row['source_type']         == 'official_only'
    assert row['ddl_language']        == 'en'
    assert row['total_volumes']       == 12
    assert row['vol_count_source']    == 'manual'


def test_edit_series_full_form_post_still_works(env):
    """Regression: the HTML editor page submits every input. The full
    form POST must continue to work unchanged."""
    sid = _seed_series(env['db_path'])
    csrf = _csrf("series-full")

    r = _client().post(
        f"/series/{sid}/edit",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'title': 'Full Form',
            'search_pattern': 'new-search',
            'preferred_groups_input': 'A,B,C',
            'blocked_groups_input': 'X',
            'omnibus_preference': 'only_individual',
            'quality_profile_id': '',
            'language_profile_id': '',
            'quality_cutoff': 'epub',
            'update_strategy': 'always',
            'required_scanlator': '',
            'source_type': 'fan_only',
            'edition_type': 'standard',  # no implied source — source_type independent
            'total_volumes': '0',
            'ddl_language': 'fr',
        },
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = dict(c.execute(
            "SELECT * FROM series WHERE id=?", (sid,)
        ).fetchone())
    assert row['title']               == 'Full Form'
    assert row['preferred_groups']    == '["A", "B", "C"]'
    assert row['blocked_groups']      == '["X"]'
    assert row['omnibus_preference']  == 'only_individual'
    assert row['quality_cutoff']      == 'epub'
    assert row['source_type']         == 'fan_only'
    assert row['edition_type']        == 'standard'
    assert row['ddl_language']        == 'fr'


def test_edit_series_edition_implied_source_still_overrides(env):
    """Regression: when edition_type is submitted with a value that
    implies a source (official_color, colored, unlocalized), the
    source_type column must be force-written even if source_type is
    submitted with a different value (preserves prior behaviour)."""
    sid = _seed_series(env['db_path'])
    csrf = _csrf("series-implied")

    r = _client().post(
        f"/series/{sid}/edit",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'edition_type': 'official_color',
            'source_type': 'fan_only',  # should be overridden by edition
        },
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303)

    with sqlite3.connect(env['db_path']) as c:
        row = c.execute(
            "SELECT edition_type, source_type FROM series WHERE id=?", (sid,)
        ).fetchone()
    assert row[0] == 'official_color'
    assert row[1] == 'official_only', "edition implies source — must override"


def test_edit_series_clearing_groups_uses_explicit_empty_string(env):
    """An explicit empty `preferred_groups_input=""` must clear the
    column (distinct from the field being absent, which leaves it
    alone)."""
    sid = _seed_series(env['db_path'])
    csrf = _csrf("series-clear")

    r = _client().post(
        f"/series/{sid}/edit",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'preferred_groups_input': '',
        },
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303)

    with sqlite3.connect(env['db_path']) as c:
        row = c.execute(
            "SELECT preferred_groups, blocked_groups FROM series WHERE id=?", (sid,)
        ).fetchone()
    assert row[0] == '[]', "explicit empty preferred_groups must clear"
    assert row[1] == '["BadGroup"]', "blocked_groups must be untouched"


# ═════════════════════════════════════════════════════════════════════
# edit_indexer — POST /indexers/{indexer_id}
# ═════════════════════════════════════════════════════════════════════


def _seed_indexer(db_path) -> int:
    """Insert an indexer with non-default values everywhere."""
    with sqlite3.connect(db_path) as c:
        c.execute("""
            INSERT INTO indexers(
                name, type, url, api_key, priority, enabled,
                categories, settings, client_id,
                min_seeders, seed_ratio,
                use_rss, use_auto_search, use_interactive_search,
                min_size_mb, max_size_mb
            ) VALUES (
                'Seeded Indexer', 'prowlarr', 'http://prowl:9696',
                'PRESERVED-KEY', 17, 0,
                '[7000,7010]', '{"foo":"bar"}', NULL,
                3, 1.5,
                0, 1, 0,
                100, 5000
            )
        """)
        idx = c.execute("SELECT id FROM indexers WHERE name='Seeded Indexer'").fetchone()[0]
        c.execute("INSERT OR IGNORE INTO indexer_tags(indexer_id, tag) VALUES(?, 'seeded-tag')", (idx,))
        return idx


def test_edit_indexer_partial_post_does_not_clobber(env):
    """Submit only `priority`. Every other field — including the
    api_key, the per-purpose flags (use_rss/auto_search/interactive),
    the categories blob, the settings JSON, the size guards, AND the
    tag set — must be untouched."""
    iid = _seed_indexer(env['db_path'])
    csrf = _csrf("indexer-partial")

    r = _client().post(
        f"/indexers/{iid}",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'priority': '42',
        },
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = dict(c.execute(
            "SELECT * FROM indexers WHERE id=?", (iid,)
        ).fetchone())
        tags = [r[0] for r in c.execute(
            "SELECT tag FROM indexer_tags WHERE indexer_id=?", (iid,)
        ).fetchall()]

    assert row['priority']     == 42
    # Every other column unchanged
    assert row['name']                   == 'Seeded Indexer'
    assert row['enabled']                == 0
    assert row['categories']             == '[7000,7010]'
    assert row['settings']               == '{"foo":"bar"}'
    assert row['min_seeders']            == 3
    assert abs(row['seed_ratio'] - 1.5)  < 1e-9
    assert row['use_rss']                == 0
    assert row['use_auto_search']        == 1
    assert row['use_interactive_search'] == 0
    assert row['min_size_mb']            == 100
    assert row['max_size_mb']            == 5000
    # api_key must NOT be re-encrypted to nothing
    assert row['api_key'] is not None and row['api_key'] != ''
    # Tag set must still hold the seeded tag
    assert tags == ['seeded-tag']


def test_edit_indexer_partial_post_does_not_wipe_api_key(env):
    """Submit a partial form WITHOUT api_key. The stored encrypted
    api_key must not be touched. This is the legacy keep_api_key=1
    behaviour, now natural under partial-POST semantics."""
    iid = _seed_indexer(env['db_path'])
    with sqlite3.connect(env['db_path']) as c:
        seeded_key = c.execute(
            "SELECT api_key FROM indexers WHERE id=?", (iid,)
        ).fetchone()[0]
    assert seeded_key  # non-null

    csrf = _csrf("indexer-keepkey")
    _client().post(
        f"/indexers/{iid}",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'name': 'Renamed Only',
        },
        **csrf, follow_redirects=False,
    )

    with sqlite3.connect(env['db_path']) as c:
        row = c.execute(
            "SELECT name, api_key FROM indexers WHERE id=?", (iid,)
        ).fetchone()
    assert row[0] == 'Renamed Only'
    assert row[1] == seeded_key, "api_key must be untouched on partial POST"


def test_edit_indexer_explicit_empty_api_key_does_not_overwrite(env):
    """If the form carries `api_key=""` (explicitly empty), we still
    refuse to overwrite the stored key — the HTML page submits an
    empty api_key when the user didn't type a new one, and overwriting
    it with an empty value would delete the working key."""
    iid = _seed_indexer(env['db_path'])
    with sqlite3.connect(env['db_path']) as c:
        seeded_key = c.execute(
            "SELECT api_key FROM indexers WHERE id=?", (iid,)
        ).fetchone()[0]

    csrf = _csrf("indexer-empty-key")
    _client().post(
        f"/indexers/{iid}",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'name': 'Renamed',
            'api_key': '',  # explicitly empty
        },
        **csrf, follow_redirects=False,
    )

    with sqlite3.connect(env['db_path']) as c:
        new_key = c.execute(
            "SELECT api_key FROM indexers WHERE id=?", (iid,)
        ).fetchone()[0]
    assert new_key == seeded_key, "empty api_key must not overwrite"


def test_edit_indexer_tags_not_rebuilt_when_field_absent(env):
    """The legacy bug: omitting `tags` from the form silently wipes
    the entire indexer_tags set via DELETE-and-rebuild. Verify the
    new behaviour: tags only rebuild if `tags` is actually in the form.
    """
    iid = _seed_indexer(env['db_path'])
    csrf = _csrf("indexer-tags-untouched")

    _client().post(
        f"/indexers/{iid}",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'priority': '99',  # unrelated
        },
        **csrf, follow_redirects=False,
    )

    with sqlite3.connect(env['db_path']) as c:
        tags = [r[0] for r in c.execute(
            "SELECT tag FROM indexer_tags WHERE indexer_id=?", (iid,)
        ).fetchall()]
    assert tags == ['seeded-tag'], (
        f"tags must be untouched when `tags` field is absent; got {tags}"
    )


def test_edit_indexer_explicit_empty_tags_clears_set(env):
    """When the form carries `tags=""` (explicit empty), the tag set
    IS wiped — that's the intentional clear path for the HTML page.
    """
    iid = _seed_indexer(env['db_path'])
    csrf = _csrf("indexer-tags-clear")

    _client().post(
        f"/indexers/{iid}",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'tags': '',  # explicit clear
        },
        **csrf, follow_redirects=False,
    )

    with sqlite3.connect(env['db_path']) as c:
        tags = [r[0] for r in c.execute(
            "SELECT tag FROM indexer_tags WHERE indexer_id=?", (iid,)
        ).fetchall()]
    assert tags == []


# ═════════════════════════════════════════════════════════════════════
# edit_download_client — POST /download-clients/{client_id}
# ═════════════════════════════════════════════════════════════════════


def _seed_download_client(db_path) -> int:
    with sqlite3.connect(db_path) as c:
        c.execute("""
            INSERT INTO download_clients(
                name, type, host, port, use_ssl, url_base,
                username, password, category, post_import_category,
                recent_priority, older_priority, initial_state,
                sequential_order, first_last_first, content_layout,
                priority, enabled, remove_completed, remove_failed,
                download_path, merge_chapters
            ) VALUES (
                'Seeded DLC', 'qbittorrent', 'qbit.local', 8081, 1, '/qb',
                'admin', 'PRESERVED-PASS', 'manga-shelf', 'imported',
                'first', 'first', 'paused',
                1, 1, 'subfolder',
                7, 0, 1, 1,
                '/downloads/manga', 1
            )
        """)
        cid = c.execute("SELECT id FROM download_clients WHERE name='Seeded DLC'").fetchone()[0]
        c.execute(
            "INSERT OR IGNORE INTO download_client_tags(client_id,tag) VALUES(?, 'dlc-seed-tag')",
            (cid,)
        )
        return cid


def test_edit_dlclient_partial_post_does_not_clobber(env):
    cid = _seed_download_client(env['db_path'])
    csrf = _csrf("dlc-partial")

    r = _client().post(
        f"/download-clients/{cid}",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'name': 'Renamed DLC',
        },
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = dict(c.execute(
            "SELECT * FROM download_clients WHERE id=?", (cid,)
        ).fetchone())
        tags = [r[0] for r in c.execute(
            "SELECT tag FROM download_client_tags WHERE client_id=?", (cid,)
        ).fetchall()]

    assert row['name']                  == 'Renamed DLC'
    # Everything else unchanged
    assert row['host']                  == 'qbit.local'
    assert row['port']                  == 8081
    assert row['use_ssl']               == 1
    assert row['url_base']              == '/qb'
    assert row['username']              == 'admin'
    assert row['category']              == 'manga-shelf'
    assert row['post_import_category']  == 'imported'
    assert row['recent_priority']       == 'first'
    assert row['older_priority']        == 'first'
    assert row['initial_state']         == 'paused'
    assert row['sequential_order']      == 1
    assert row['first_last_first']      == 1
    assert row['content_layout']        == 'subfolder'
    assert row['priority']              == 7
    assert row['enabled']               == 0
    assert row['remove_completed']      == 1
    assert row['remove_failed']         == 1
    assert row['download_path']         == '/downloads/manga'
    assert row['merge_chapters']        == 1
    assert row['password'] is not None and row['password'] != ''
    assert tags == ['dlc-seed-tag']


def test_edit_dlclient_partial_post_does_not_wipe_password(env):
    """Same semantics as indexer api_key: absent password = leave alone."""
    cid = _seed_download_client(env['db_path'])
    with sqlite3.connect(env['db_path']) as c:
        seeded_pw = c.execute(
            "SELECT password FROM download_clients WHERE id=?", (cid,)
        ).fetchone()[0]
    assert seeded_pw

    csrf = _csrf("dlc-keep-pw")
    _client().post(
        f"/download-clients/{cid}",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'priority': '15',
        },
        **csrf, follow_redirects=False,
    )

    with sqlite3.connect(env['db_path']) as c:
        new_pw = c.execute(
            "SELECT password FROM download_clients WHERE id=?", (cid,)
        ).fetchone()[0]
    assert new_pw == seeded_pw


def test_edit_dlclient_explicit_empty_password_does_not_overwrite(env):
    """Same semantics as indexer api_key: explicit empty password
    submitted by the HTML page (when the user didn't type a new one)
    must not overwrite the stored password."""
    cid = _seed_download_client(env['db_path'])
    with sqlite3.connect(env['db_path']) as c:
        seeded_pw = c.execute(
            "SELECT password FROM download_clients WHERE id=?", (cid,)
        ).fetchone()[0]

    csrf = _csrf("dlc-empty-pw")
    _client().post(
        f"/download-clients/{cid}",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'name': 'Renamed',
            'password': '',
        },
        **csrf, follow_redirects=False,
    )

    with sqlite3.connect(env['db_path']) as c:
        new_pw = c.execute(
            "SELECT password FROM download_clients WHERE id=?", (cid,)
        ).fetchone()[0]
    assert new_pw == seeded_pw


def test_edit_dlclient_tags_untouched_when_field_absent(env):
    cid = _seed_download_client(env['db_path'])
    csrf = _csrf("dlc-tags-keep")

    _client().post(
        f"/download-clients/{cid}",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'enabled': '1',
        },
        **csrf, follow_redirects=False,
    )

    with sqlite3.connect(env['db_path']) as c:
        tags = [r[0] for r in c.execute(
            "SELECT tag FROM download_client_tags WHERE client_id=?", (cid,)
        ).fetchall()]
    assert tags == ['dlc-seed-tag']


# ═════════════════════════════════════════════════════════════════════
# edit_quality_profile — POST /quality-profiles/{profile_id}
# ═════════════════════════════════════════════════════════════════════


def test_edit_quality_profile_partial_post(env):
    """Submit only `name` — qualities, cutoff, score thresholds untouched."""
    with sqlite3.connect(env['db_path']) as c:
        c.execute("DELETE FROM quality_profiles")
        c.execute(
            "INSERT INTO quality_profiles(id, name, qualities, cutoff,"
            " upgrades_allowed, minimum_custom_format_score,"
            " cutoff_format_score, min_upgrade_format_score, is_default)"
            " VALUES(50, 'Seeded QP', '[\"epub\",\"cbr\"]', 'epub', 0, 100, 5000, 25, 1)"
        )

    csrf = _csrf("qp-partial")
    r = _client().post(
        "/quality-profiles/50",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'name':       'Renamed QP',
        },
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = dict(c.execute(
            "SELECT * FROM quality_profiles WHERE id=50"
        ).fetchone())
    assert row['name']                        == 'Renamed QP'
    assert row['qualities']                   == '["epub","cbr"]'
    assert row['cutoff']                      == 'epub'
    assert row['upgrades_allowed']            == 0
    assert row['minimum_custom_format_score'] == 100
    assert row['cutoff_format_score']         == 5000
    assert row['min_upgrade_format_score']    == 25


# ═════════════════════════════════════════════════════════════════════
# edit_release_profile — POST /release-profiles/{profile_id}
# ═════════════════════════════════════════════════════════════════════


def test_edit_release_profile_partial_post(env):
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO release_profiles(id, name, enabled, required, ignored,"
            " preferred, include_preferred_when_renaming)"
            " VALUES(60, 'Seeded RP', 0, 'must-have', 'must-not',"
            "        '[{\"term\":\"x265\",\"score\":100}]', 1)"
        )
        c.execute(
            "INSERT OR IGNORE INTO release_profile_tags(profile_id, tag)"
            " VALUES(60, 'rp-seed-tag')"
        )

    csrf = _csrf("rp-partial")
    _client().post(
        "/release-profiles/60",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'enabled':    '1',
        },
        **csrf, follow_redirects=False,
    )

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = dict(c.execute(
            "SELECT * FROM release_profiles WHERE id=60"
        ).fetchone())
        tags = [r[0] for r in c.execute(
            "SELECT tag FROM release_profile_tags WHERE profile_id=60"
        ).fetchall()]

    assert row['enabled']  == 1
    # Everything else preserved
    assert row['name']                            == 'Seeded RP'
    assert row['required']                        == 'must-have'
    assert row['ignored']                         == 'must-not'
    assert row['preferred']                       == '[{"term":"x265","score":100}]'
    assert row['include_preferred_when_renaming'] == 1
    assert tags == ['rp-seed-tag']


# ═════════════════════════════════════════════════════════════════════
# edit_delay_profile — POST /delay-profiles/{profile_id}
# ═════════════════════════════════════════════════════════════════════


def test_edit_delay_profile_partial_post(env):
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO delay_profiles(id, name, enable_usenet, enable_torrent,"
            " usenet_delay, torrent_delay, bypass_if_highest_quality, order_num)"
            " VALUES(70, 'Seeded DP', 0, 0, 60, 30, 1, 5)"
        )
        c.execute(
            "INSERT OR IGNORE INTO delay_profile_tags(profile_id, tag)"
            " VALUES(70, 'dp-seed-tag')"
        )

    csrf = _csrf("dp-partial")
    _client().post(
        "/delay-profiles/70",
        data={
            'csrf_token':    csrf['headers']['X-CSRFToken'],
            'usenet_delay':  '120',
        },
        **csrf, follow_redirects=False,
    )

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = dict(c.execute(
            "SELECT * FROM delay_profiles WHERE id=70"
        ).fetchone())
        tags = [r[0] for r in c.execute(
            "SELECT tag FROM delay_profile_tags WHERE profile_id=70"
        ).fetchall()]

    assert row['usenet_delay']               == 120
    assert row['name']                       == 'Seeded DP'
    assert row['enable_usenet']              == 0
    assert row['enable_torrent']             == 0
    assert row['torrent_delay']              == 30
    assert row['bypass_if_highest_quality']  == 1
    assert tags == ['dp-seed-tag']


# ═════════════════════════════════════════════════════════════════════
# update_language_profile — POST /language-profiles/{profile_id}
# ═════════════════════════════════════════════════════════════════════


def test_edit_language_profile_partial_post(env):
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO language_profiles(id, name, languages, allow_any)"
            " VALUES(80, 'Seeded LP', '[\"ja\",\"ko\"]', 0)"
        )

    csrf = _csrf("lp-partial")
    _client().post(
        "/language-profiles/80",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'name':       'Renamed LP',
        },
        **csrf, follow_redirects=False,
    )

    with sqlite3.connect(env['db_path']) as c:
        row = c.execute(
            "SELECT name, languages, allow_any FROM language_profiles WHERE id=80"
        ).fetchone()
    assert row[0] == 'Renamed LP'
    assert row[1] == '["ja","ko"]'
    assert row[2] == 0


# ═════════════════════════════════════════════════════════════════════
# edit_custom_format — POST /custom-formats/{format_id}
# ═════════════════════════════════════════════════════════════════════


def test_edit_custom_format_partial_post(env):
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO custom_formats(id, name, specifications,"
            " include_custom_format_when_renaming)"
            " VALUES(90, 'Seeded CF',"
            "        '[{\"type\":\"release_title_contains\",\"value\":\"x265\"}]', 1)"
        )

    csrf = _csrf("cf-partial")
    _client().post(
        "/custom-formats/90",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'name':       'Renamed CF',
        },
        **csrf, follow_redirects=False,
    )

    with sqlite3.connect(env['db_path']) as c:
        row = c.execute(
            "SELECT name, specifications, include_custom_format_when_renaming"
            " FROM custom_formats WHERE id=90"
        ).fetchone()
    assert row[0] == 'Renamed CF'
    # specifications JSON re-serialized but content preserved (it wasn't submitted)
    # Most importantly, it's NOT empty:
    assert '"type"' in row[1]
    assert '"value"' in row[1]
    assert 'x265' in row[1]
    assert row[2] == 1


# ═════════════════════════════════════════════════════════════════════
# edit_import_list — POST /import-lists/{list_id}
# ═════════════════════════════════════════════════════════════════════


def test_edit_import_list_partial_post(env):
    """Import-list edit must not clobber settings JSON, monitor_mode,
    or quality/root-folder FK columns when only `name` is submitted."""
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO import_lists(id, name, type, enabled,"
            " quality_profile_id, root_folder_id, monitor_mode, settings)"
            " VALUES(110, 'Seeded IL', 'mal', 0, NULL, NULL, 'future',"
            "        '{\"endpoint\":\"x\",\"interval\":3600}')"
        )

    csrf = _csrf("il-partial")
    _client().post(
        "/import-lists/110",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'name':       'Renamed IL',
        },
        **csrf, follow_redirects=False,
    )

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = dict(c.execute(
            "SELECT * FROM import_lists WHERE id=110"
        ).fetchone())
    assert row['name']         == 'Renamed IL'
    assert row['type']         == 'mal'
    assert row['enabled']      == 0
    assert row['monitor_mode'] == 'future'
    assert '"endpoint"' in row['settings'] and 'x' in row['settings']
    assert '"interval"' in row['settings']


# ═════════════════════════════════════════════════════════════════════
# edit_notification_connection — POST /notifications/{conn_id}
# ═════════════════════════════════════════════════════════════════════


def test_edit_notification_partial_post_does_not_clobber_events(env):
    """Submit only `name` — the 6 event flags must keep their seeded
    values. Without partial-POST safety, every flag would be wiped to
    0 (or its `Form(0)` default) on each submit."""
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO notification_connections(id, name, type, enabled, settings,"
            " on_grab, on_download, on_upgrade, on_series_add,"
            " on_health_issue, on_health_restored)"
            " VALUES(120, 'Seeded NTF', 'discord', 0,"
            "        '{\"webhook_url\":\"https://example.com/hook\"}',"
            "        0, 1, 0, 1, 0, 1)"
        )

    csrf = _csrf("ntf-partial")
    r = _client().post(
        "/notifications/120",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'name':       'Renamed NTF',
        },
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = dict(c.execute(
            "SELECT * FROM notification_connections WHERE id=120"
        ).fetchone())

    assert row['name']                  == 'Renamed NTF'
    # Type, enabled, settings unchanged
    assert row['type']                  == 'discord'
    assert row['enabled']               == 0
    assert 'webhook_url' in row['settings']
    # Event flags unchanged
    assert row['on_grab']               == 0
    assert row['on_download']           == 1
    assert row['on_upgrade']            == 0
    assert row['on_series_add']         == 1
    assert row['on_health_issue']       == 0
    assert row['on_health_restored']    == 1


def test_edit_notification_partial_post_can_toggle_single_event(env):
    """Submit only `on_grab=1` — only that column changes; other event
    flags AND name/type/settings stay seeded."""
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO notification_connections(id, name, type, enabled, settings,"
            " on_grab, on_download, on_upgrade, on_series_add,"
            " on_health_issue, on_health_restored)"
            " VALUES(121, 'Seeded NTF2', 'webhook', 1, '{}',"
            "        0, 1, 1, 1, 1, 1)"
        )

    csrf = _csrf("ntf-toggle")
    _client().post(
        "/notifications/121",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'on_grab':    '1',
        },
        **csrf, follow_redirects=False,
    )

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = dict(c.execute(
            "SELECT * FROM notification_connections WHERE id=121"
        ).fetchone())
    assert row['on_grab']        == 1  # changed
    assert row['on_download']    == 1
    assert row['on_upgrade']     == 1
    assert row['on_series_add']  == 1
    assert row['name']           == 'Seeded NTF2'
    assert row['type']           == 'webhook'


# ═════════════════════════════════════════════════════════════════════
# save_general_settings — POST /settings/general
# ═════════════════════════════════════════════════════════════════════


def test_save_general_settings_partial_post(env):
    """Each setting is its own row in the `settings` table. A partial
    POST must only INSERT-OR-REPLACE rows whose form key is in the
    body — every other setting is left alone."""
    # Seed pre-existing settings to verify they're untouched
    with sqlite3.connect(env['db_path']) as c:
        for k, v in [
            ('instance_name',        'My Mangarr'),
            ('log_level',            'DEBUG'),
            ('backup_folder',        '/custom/backups'),
            ('backup_interval_days', '14'),
            ('backup_retention',     '5'),
            ('ui_date_format',       'absolute'),
            ('blocklist_ttl_days',   '180'),
        ]:
            c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))

    csrf = _csrf("settings-partial")
    r = _client().post(
        "/settings/general",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'instance_name': 'Renamed Instance',
        },
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        out = {
            r[0]: r[1] for r in c.execute(
                "SELECT key, value FROM settings WHERE key IN ("
                "'instance_name','log_level','backup_folder','backup_interval_days',"
                "'backup_retention','ui_date_format','blocklist_ttl_days')"
            ).fetchall()
        }
    assert out['instance_name']         == 'Renamed Instance'  # changed
    # Everything else preserved — would have been clobbered to defaults
    # under the old wholesale-write pattern.
    assert out['log_level']             == 'DEBUG'
    assert out['backup_folder']         == '/custom/backups'
    assert out['backup_interval_days']  == '14'
    assert out['backup_retention']      == '5'
    assert out['ui_date_format']        == 'absolute'
    assert out['blocklist_ttl_days']    == '180'


def test_save_general_settings_does_not_overwrite_api_key_when_absent(env):
    """The api_key field gets special handling: only written if the
    form carries it AND it's non-empty. A partial POST that doesn't
    mention api_key must leave the stored key alone."""
    from security import encrypt_if_cipher_available
    # Seed an encrypted api_key directly (mirrors what the route would do)
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('api_key',?)",
            (encrypt_if_cipher_available('PRESERVED-API-KEY'),)
        )
        seeded = c.execute(
            "SELECT value FROM settings WHERE key='api_key'"
        ).fetchone()[0]

    csrf = _csrf("settings-keepkey")
    _client().post(
        "/settings/general",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'log_level':  'INFO',
            # api_key omitted
        },
        **csrf, follow_redirects=False,
    )

    with sqlite3.connect(env['db_path']) as c:
        after = c.execute(
            "SELECT value FROM settings WHERE key='api_key'"
        ).fetchone()[0]
    assert after == seeded, "api_key must not be overwritten on partial POST"

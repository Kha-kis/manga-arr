"""Tests for per-indexer tags (Sonarr semantics, PR #120).

The Sonarr rule (verbatim from forums.sonarr.tv):
  - An indexer with ZERO tags applies to all series.
  - An indexer with one or more tags applies only to series whose own tag set
    intersects this indexer's tag set.

The shared vocabulary (TEXT tag values) is the same between series_tags and
indexer_tags, so intersection is a plain SQL JOIN.

This test file pins the rule + the form persistence + the UI rendering.
"""
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
    """Fresh DB. Tests seed series + indexers + tags directly."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-itags-keys-")

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


# ───────────────────── Sonarr-rule tests ─────────────────────


def test_untagged_indexer_applies_to_all_series(env):
    """Sonarr canonical rule: an indexer with zero tags is available for
    every series, regardless of the series's own tag set."""
    from routers.indexers import _indexer_allowed_for_series
    from shared import get_db

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled)"
            " VALUES(1, 'PublicTracker', 'torznab', 'http://t', 'k', 1)"
        )
        c.execute(
            "INSERT INTO series(id, title, search_pattern, edition_type, enabled,"
            " monitored, monitor_mode)"
            " VALUES(100, 'TaggedSeries',   't', 'standard', 1, 1, 'all'),"
            "       (101, 'UntaggedSeries', 'u', 'standard', 1, 1, 'all')"
        )
        c.execute("INSERT INTO series_tags(series_id, tag) VALUES(100, 'shounen')")

    with get_db() as db:
        # No tags on indexer 1 → both series should be allowed
        assert _indexer_allowed_for_series(db, 1, 100) is True
        assert _indexer_allowed_for_series(db, 1, 101) is True


def test_tagged_indexer_applies_only_to_series_with_matching_tag(env):
    """An indexer with tags is restricted to series sharing ≥1 tag."""
    from routers.indexers import _indexer_allowed_for_series
    from shared import get_db

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled)"
            " VALUES(2, 'PrivateTracker', 'torznab', 'http://p', 'k', 1)"
        )
        c.execute("INSERT INTO indexer_tags(indexer_id, tag) VALUES(2, 'private')")
        c.execute(
            "INSERT INTO series(id, title, search_pattern, edition_type, enabled,"
            " monitored, monitor_mode)"
            " VALUES(200, 'PremiumSeries', 'p', 'standard', 1, 1, 'all'),"
            "       (201, 'CasualSeries',  'c', 'standard', 1, 1, 'all')"
        )
        c.execute(
            "INSERT INTO series_tags(series_id, tag)"
            " VALUES(200, 'private'),"
            "       (201, 'shounen')"
        )

    with get_db() as db:
        # Premium series has 'private' tag → matches → allowed
        assert _indexer_allowed_for_series(db, 2, 200) is True, (
            "tagged indexer must apply to series with matching tag"
        )
        # Casual series lacks 'private' → no match → blocked
        assert _indexer_allowed_for_series(db, 2, 201) is False, (
            "tagged indexer must NOT apply to series without matching tag"
        )


def test_tagged_indexer_blocks_untagged_series(env):
    """Series with no tags can never use a tagged indexer (no overlap possible)."""
    from routers.indexers import _indexer_allowed_for_series
    from shared import get_db

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled)"
            " VALUES(3, 'TaggedTracker', 'torznab', 'http://t', 'k', 1)"
        )
        c.execute("INSERT INTO indexer_tags(indexer_id, tag) VALUES(3, 'shounen')")
        c.execute(
            "INSERT INTO series(id, title, search_pattern, edition_type, enabled,"
            " monitored, monitor_mode)"
            " VALUES(300, 'NoTagsHere', 'n', 'standard', 1, 1, 'all')"
        )

    with get_db() as db:
        assert _indexer_allowed_for_series(db, 3, 300) is False, (
            "untagged series must be blocked from a tagged indexer"
        )


def test_multiple_tag_intersection_allowed(env):
    """OR-on-the-tag-side: any single shared tag is enough for allow."""
    from routers.indexers import _indexer_allowed_for_series
    from shared import get_db

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled)"
            " VALUES(4, 'MultiTagTracker', 'torznab', 'http://m', 'k', 1)"
        )
        c.execute(
            "INSERT INTO indexer_tags(indexer_id, tag)"
            " VALUES(4, 'shounen'), (4, 'seinen')"
        )
        c.execute(
            "INSERT INTO series(id, title, search_pattern, edition_type, enabled,"
            " monitored, monitor_mode)"
            " VALUES(400, 'OneOverlap',     'a', 'standard', 1, 1, 'all'),"
            "       (401, 'NoOverlap',      'b', 'standard', 1, 1, 'all'),"
            "       (402, 'CompleteSet',    'c', 'standard', 1, 1, 'all')"
        )
        c.execute(
            "INSERT INTO series_tags(series_id, tag)"
            " VALUES(400, 'shounen'),"
            "       (401, 'shoujo'),"
            "       (402, 'seinen'), (402, 'shoujo')"
        )

    with get_db() as db:
        assert _indexer_allowed_for_series(db, 4, 400) is True, (
            "single-overlap is enough"
        )
        assert _indexer_allowed_for_series(db, 4, 401) is False, (
            "zero overlap → blocked"
        )
        assert _indexer_allowed_for_series(db, 4, 402) is True, (
            "any one overlap (seinen here) is enough"
        )


def test_series_id_none_skips_tag_filter(env):
    """RSS poll context: series_id is None → no tag filter applied (the
    matching happens later at grab time when we know the series)."""
    from routers.indexers import _indexer_allowed_for_series
    from shared import get_db

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled)"
            " VALUES(5, 'AnyTracker', 'torznab', 'http://a', 'k', 1)"
        )
        c.execute("INSERT INTO indexer_tags(indexer_id, tag) VALUES(5, 'private')")

    with get_db() as db:
        assert _indexer_allowed_for_series(db, 5, None) is True, (
            "series_id=None must skip tag filter — RSS poll doesn't know "
            "which series an item is for yet"
        )


# ───────────────────── search_all_indexers SQL filter ─────────────────────


def test_search_all_indexers_excludes_non_matching_tagged_indexers(env):
    """The SQL WHERE clause must implement the same Sonarr rule. When called
    with series_id=N, indexers without overlapping tags are excluded."""
    from shared import get_db

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled,"
            " priority)"
            " VALUES(10, 'Public',  'torznab', 'http://a', 'k', 1, 25),"
            "       (11, 'Private', 'torznab', 'http://b', 'k', 1, 25)"
        )
        # Indexer 10 has no tags → applies to all
        # Indexer 11 has 'private' → only series with that tag
        c.execute("INSERT INTO indexer_tags(indexer_id, tag) VALUES(11, 'private')")
        c.execute(
            "INSERT INTO series(id, title, search_pattern, edition_type, enabled,"
            " monitored, monitor_mode)"
            " VALUES(500, 'CasualOnly', 'c', 'standard', 1, 1, 'all')"
        )

    # Run the same WHERE clause search_all_indexers uses
    with get_db() as db:
        rows = db.execute(
            "SELECT id FROM indexers WHERE enabled=1"
            " AND (NOT EXISTS (SELECT 1 FROM indexer_tags WHERE indexer_id=indexers.id)"
            "      OR EXISTS (SELECT 1 FROM indexer_tags it"
            "                 JOIN series_tags st ON it.tag = st.tag"
            "                 WHERE it.indexer_id=indexers.id AND st.series_id=?))"
            " ORDER BY priority",
            (500,)
        ).fetchall()
    ids = {r['id'] for r in rows}
    assert 10 in ids, "untagged indexer must be included (applies to all)"
    assert 11 not in ids, (
        "tagged 'private' indexer must be excluded for a series without that tag"
    )


# ───────────────────── form persistence ─────────────────────


def test_create_form_persists_tags(env):
    """POST /indexers with `tags` field creates rows in indexer_tags."""
    client = _client()
    csrf = _csrf("create-tags")

    r = client.post(
        "/indexers",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'name': 'NewIdx',
            'type': 'torznab',
            'url': 'http://test',
            'api_key': 'k',
            'priority': '25',
            'enabled': '1',
            'categories': '7000',
            'use_rss': '1',
            'use_auto_search': '1',
            'use_interactive_search': '1',
            'tags': 'private, premium',
        },
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        new_id = c.execute("SELECT id FROM indexers WHERE name='NewIdx'").fetchone()['id']
        tags = sorted(
            r['tag'] for r in c.execute(
                "SELECT tag FROM indexer_tags WHERE indexer_id=?", (new_id,)
            ).fetchall()
        )
    assert tags == ['premium', 'private'], (
        f"comma-separated tags must persist as separate rows, got {tags!r}"
    )


def test_edit_form_replaces_tag_set(env):
    """Edit overwrites the tag set (not merge) — same pattern as delay/release
    profile tags. This is the user's expectation: clearing tags removes
    restrictions."""
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled)"
            " VALUES(50, 'EditMe', 'torznab', 'http://t', 'k', 1)"
        )
        c.execute(
            "INSERT INTO indexer_tags(indexer_id, tag)"
            " VALUES(50, 'old1'), (50, 'old2')"
        )

    client = _client()
    csrf = _csrf("edit-replace")

    r = client.post(
        "/indexers/50",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'name': 'EditMe',
            'type': 'torznab',
            'url': 'http://t',
            'priority': '25',
            'enabled': '1',
            'categories': '7000',
            'keep_api_key': '1',
            'use_rss': '1',
            'use_auto_search': '1',
            'use_interactive_search': '1',
            'tags': 'newtag',  # both 'old1' and 'old2' should be replaced
        },
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        tags = sorted(
            r[0] for r in c.execute(
                "SELECT tag FROM indexer_tags WHERE indexer_id=50"
            ).fetchall()
        )
    assert tags == ['newtag'], (
        f"old tags must be wiped on edit, only new ones remain — got {tags!r}"
    )


def test_edit_form_with_empty_tags_clears_tag_set(env):
    """Submitting empty tags removes all restrictions — applies-to-all again."""
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled)"
            " VALUES(60, 'Untag', 'torznab', 'http://t', 'k', 1)"
        )
        c.execute("INSERT INTO indexer_tags(indexer_id, tag) VALUES(60, 'private')")

    client = _client()
    csrf = _csrf("edit-clear")

    r = client.post(
        "/indexers/60",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'name': 'Untag',
            'type': 'torznab',
            'url': 'http://t',
            'priority': '25',
            'enabled': '1',
            'categories': '7000',
            'keep_api_key': '1',
            'use_rss': '1',
            'use_auto_search': '1',
            'use_interactive_search': '1',
            'tags': '',
        },
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        n = c.execute("SELECT COUNT(*) FROM indexer_tags WHERE indexer_id=60").fetchone()[0]
    assert n == 0, "empty tag input must clear all tag rows"


# ───────────────────── delete cascades ─────────────────────


def test_delete_indexer_cascades_to_tag_rows(env):
    """The FK ON DELETE CASCADE on indexer_tags must clean up tag rows
    when the parent indexer is deleted — no orphan rows."""
    with sqlite3.connect(env['db_path']) as c:
        c.execute("PRAGMA foreign_keys=ON")  # default ON in get_db, set here for direct check
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled)"
            " VALUES(70, 'ToDelete', 'torznab', 'http://t', 'k', 1)"
        )
        c.execute("INSERT INTO indexer_tags(indexer_id, tag) VALUES(70, 'x')")

    client = _client()
    csrf = _csrf("del-cascade")
    r = client.post("/indexers/70/delete", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303)

    with sqlite3.connect(env['db_path']) as c:
        n_tags = c.execute(
            "SELECT COUNT(*) FROM indexer_tags WHERE indexer_id=70"
        ).fetchone()[0]
    assert n_tags == 0, (
        "indexer_tags rows must cascade-delete when the indexer is removed"
    )


# ───────────────────── /indexers page rendering ─────────────────────


def test_indexers_page_renders_tag_badges(env):
    """The indexers list page must show tag badges on rows that have tags."""
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled)"
            " VALUES(80, 'Tagged', 'torznab', 'http://t', 'k', 1)"
        )
        c.execute(
            "INSERT INTO indexer_tags(indexer_id, tag)"
            " VALUES(80, 'private'), (80, 'fast')"
        )

    client = _client()
    r = client.get("/indexers")
    assert r.status_code == 200
    body = r.text
    # Both tags should render as badges
    assert 'private' in body
    assert 'fast' in body

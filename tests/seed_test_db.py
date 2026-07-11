"""Seed minimum fixtures into the isolated test container.

Run inside the mangarr-test container after it boots cleanly:
    docker exec mangarr-test python3 /app/../tests/seed_test_db.py
or, equivalently from the host helper:
    ./tests/seed_test_db.sh

Idempotent — running twice leaves the same state.

What's seeded (driven by what the browser test files require):
  - series id=37 "Omnibus & Packs Test Series" with baseline fields and a complete pack
    (browser_smoke.js line 326 loads /series/37 for Omnibus & Packs section)
  - series id=40 "Vinland Saga" with baseline fields browser_e2e.js asserts and reverts on
  - download_client id=1, qbittorrent, enabled — browser_e2e.js E3.6 calls
    /api/download-clients/1/test on this id
  - custom_format id=990 "Browser Digital" — browser_integration.js previews it
  - one custom tag visible on /tags so the integration page sweep finds it

Production data is never touched: this script connects to /config/manga_arr.db
inside the test container, which is mounted from ./.test-config on the host.
"""

import os
import sqlite3
from datetime import datetime

DB_PATH = "/config/manga_arr.db"
COVERS_DIR = "/config/covers"

# 1×1 transparent PNG. The series page emits <img src="/covers/{id}.jpg">
# and the browser logs a console error when that 404s, which would fail
# the smoke "no console errors" assertion in an empty test DB.
_PLACEHOLDER_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63f8cf000000030001fe79bff70000000049454e44ae42"
    "6082"
)

SERIES_37 = {
    "id": 37,
    "title": "Omnibus & Packs Test Series",
    "search_pattern": "Omnibus Test",
    "omnibus_preference": "prefer_omnibus",
    "update_strategy": "once",
    "monitored": 1,
    "enabled": 1,
    "total_volumes": 12,  # so the complete pack marks as covering all
}

SERIES_40 = {
    "id": 40,
    "title": "Vinland Saga",
    "search_pattern": "Vinland Saga",
    "omnibus_preference": "prefer_individual",
    "update_strategy": "once",
    "monitored": 1,
    "enabled": 1,
}

SERIES_40 = {
    "id": 40,
    "title": "Vinland Saga",
    "search_pattern": "Vinland Saga",
    "omnibus_preference": "prefer_individual",
    "update_strategy": "once",
    "monitored": 1,
    "enabled": 1,
}

DL_CLIENT_1 = {
    "id": 1,
    "name": "test-qbit",
    "type": "qbittorrent",
    # Points at the mock-qbit sidecar inside the test compose network.
    # See docker-compose.test.yml and tests/mock_qbit.py.
    "host": "http://mock-qbit",
    "port": 8080,
    "username": "admin",
    "password": "test-pw",
    "category": "manga",
    "priority": 1,
    "enabled": 1,
}

CUSTOM_FORMAT_1 = {
    "id": 990,
    "name": "Browser Digital",
    "specifications": '[{"type":"release_title_contains","value":"Digital","negate":false}]',
}


def main():
    # Placeholder cover so /covers/{id}.jpg doesn't 404 and pollute the
    # browser console-error sweep.
    os.makedirs(COVERS_DIR, exist_ok=True)

    for sid in (37, 40):
        cover_path = os.path.join(COVERS_DIR, f"{sid}.jpg")
        if not os.path.exists(cover_path):
            with open(cover_path, "wb") as f:
                f.write(_PLACEHOLDER_PNG)
            print(f"seeded placeholder cover at {cover_path}")

    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row

        # series 37 (for omnibus & packs test)
        existing = db.execute(
            "SELECT id FROM series WHERE id=?", (SERIES_37["id"],)
        ).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO series(id, title, search_pattern, omnibus_preference, "
                " update_strategy, monitored, enabled) VALUES(?,?,?,?,?,?,?)",
                (
                    SERIES_37["id"],
                    SERIES_37["title"],
                    SERIES_37["search_pattern"],
                    SERIES_37["omnibus_preference"],
                    SERIES_37["update_strategy"],
                    SERIES_37["monitored"],
                    SERIES_37["enabled"],
                ),
            )
            print(f"seeded series id={SERIES_37['id']} ({SERIES_37['title']})")

        # series 40
        existing = db.execute(
            "SELECT id FROM series WHERE id=?", (SERIES_40["id"],)
        ).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO series(id, title, search_pattern, omnibus_preference, "
                " update_strategy, monitored, enabled) VALUES(?,?,?,?,?,?,?)",
                (
                    SERIES_40["id"],
                    SERIES_40["title"],
                    SERIES_40["search_pattern"],
                    SERIES_40["omnibus_preference"],
                    SERIES_40["update_strategy"],
                    SERIES_40["monitored"],
                    SERIES_40["enabled"],
                ),
            )
            print(f"seeded series id={SERIES_40['id']} ({SERIES_40['title']})")
        else:
            # Reset the mutable fields back to baseline so reruns are deterministic.
            db.execute(
                "UPDATE series SET title=?, search_pattern=?, omnibus_preference=?, "
                " update_strategy=?, monitored=?, enabled=? WHERE id=?",
                (
                    SERIES_40["title"],
                    SERIES_40["search_pattern"],
                    SERIES_40["omnibus_preference"],
                    SERIES_40["update_strategy"],
                    SERIES_40["monitored"],
                    SERIES_40["enabled"],
                    SERIES_40["id"],
                ),
            )
            print(f"reset series id={SERIES_40['id']} fields to baseline")

        # download_client 1
        existing = db.execute(
            "SELECT id FROM download_clients WHERE id=?", (DL_CLIENT_1["id"],)
        ).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO download_clients(id, name, type, host, port, use_ssl, "
                " username, password, category, priority, enabled) "
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    DL_CLIENT_1["id"],
                    DL_CLIENT_1["name"],
                    DL_CLIENT_1["type"],
                    DL_CLIENT_1["host"],
                    DL_CLIENT_1["port"],
                    0,
                    DL_CLIENT_1["username"],
                    DL_CLIENT_1["password"],
                    DL_CLIENT_1["category"],
                    DL_CLIENT_1["priority"],
                    DL_CLIENT_1["enabled"],
                ),
            )
            print(f"seeded download_client id={DL_CLIENT_1['id']}")

        # at least one tag so /tags page has rendered content for the
        # integration sweep
        existing = db.execute(
            "SELECT 1 FROM series_tags WHERE series_id=? AND tag=?", (40, "seed-tag")
        ).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO series_tags(series_id, tag) VALUES(?,?)", (40, "seed-tag")
            )
            print("seeded tag 'seed-tag' on series 40")

        existing = db.execute(
            "SELECT id FROM custom_formats WHERE id=?", (CUSTOM_FORMAT_1["id"],)
        ).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO custom_formats(id, name, specifications)"
                " VALUES(?,?,?)",
                (
                    CUSTOM_FORMAT_1["id"],
                    CUSTOM_FORMAT_1["name"],
                    CUSTOM_FORMAT_1["specifications"],
                ),
            )
            print(f"seeded custom_format id={CUSTOM_FORMAT_1['id']}")

        # complete pack for series 37 so the smoke test Omnibus & Packs section has content
        db.execute(
            "INSERT INTO volumes(series_id, status, vol_range_start, vol_range_end, pack_type, "
            " grabbed_at, source_url, download_id, torrent_name, indexer, protocol, "
            " client, release_group, size_bytes)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                37,
                "downloaded",
                1,
                12,
                "complete",
                datetime.now().isoformat(),
                "https://example.com/complete",
                "dl-complete-37",
                "Omnibus & Packs Test Series - Complete",
                "nyaa",
                "torrent",
                "test-qbit",
                "TEST",
                52428800,
            ),
        )
        print("seeded complete pack for series 37")

        # Individual volumes so the pack covers them → bi-archive icon appears
        for vnum in range(1, 13):
            db.execute(
                "INSERT OR IGNORE INTO volumes(series_id, volume_num, status) VALUES(?,?,?)",
                (37, float(vnum), "downloaded"),
            )
        print("seeded individual volumes 1-12 for series 37")

        db.commit()

    print("seed complete")


if __name__ == "__main__":
    main()

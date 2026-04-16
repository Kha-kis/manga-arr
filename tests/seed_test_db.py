"""Seed minimum fixtures into the isolated test container.

Run inside the mangarr-test container after it boots cleanly:
    docker exec mangarr-test python3 /app/../tests/seed_test_db.py
or, equivalently from the host helper:
    ./tests/seed_test_db.sh

Idempotent — running twice leaves the same state.

What's seeded (driven by what the browser test files require):
  - series id=40 "Vinland Saga" with the field defaults browser_e2e.js
    asserts and reverts on (search_pattern, omnibus_preference, update_strategy)
  - download_client id=1, qbittorrent, enabled — browser_e2e.js E3.6 calls
    /api/download-clients/1/test on this id
  - one custom tag visible on /tags so the integration page sweep finds it

Production data is never touched: this script connects to /config/manga_arr.db
inside the test container, which is mounted from ./.test-config on the host.
"""
import os
import sqlite3

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

SERIES_40 = {
    "id":                  40,
    "title":               "Vinland Saga",
    "search_pattern":      "Vinland Saga",
    "omnibus_preference":  "prefer_individual",
    "update_strategy":     "once",
    "monitored":           1,
    "enabled":             1,
}

DL_CLIENT_1 = {
    "id":       1,
    "name":     "test-qbit",
    "type":     "qbittorrent",
    # Points at the mock-qbit sidecar inside the test compose network.
    # See docker-compose.test.yml and tests/mock_qbit.py.
    "host":     "http://mock-qbit",
    "port":     8080,
    "username": "admin",
    "password": "test-pw",
    "category": "manga",
    "priority": 1,
    "enabled":  1,
}


def main():
    # Placeholder cover so /covers/40.jpg doesn't 404 and pollute the
    # browser console-error sweep.
    os.makedirs(COVERS_DIR, exist_ok=True)
    cover_path = os.path.join(COVERS_DIR, f"{SERIES_40['id']}.jpg")
    if not os.path.exists(cover_path):
        with open(cover_path, "wb") as f:
            f.write(_PLACEHOLDER_PNG)
        print(f"seeded placeholder cover at {cover_path}")

    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row

        # series 40
        existing = db.execute("SELECT id FROM series WHERE id=?", (SERIES_40["id"],)).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO series(id, title, search_pattern, omnibus_preference, "
                " update_strategy, monitored, enabled) VALUES(?,?,?,?,?,?,?)",
                (SERIES_40["id"], SERIES_40["title"], SERIES_40["search_pattern"],
                 SERIES_40["omnibus_preference"], SERIES_40["update_strategy"],
                 SERIES_40["monitored"], SERIES_40["enabled"])
            )
            print(f"seeded series id={SERIES_40['id']} ({SERIES_40['title']})")
        else:
            # Reset the mutable fields back to baseline so reruns are deterministic.
            db.execute(
                "UPDATE series SET title=?, search_pattern=?, omnibus_preference=?, "
                " update_strategy=?, monitored=?, enabled=? WHERE id=?",
                (SERIES_40["title"], SERIES_40["search_pattern"],
                 SERIES_40["omnibus_preference"], SERIES_40["update_strategy"],
                 SERIES_40["monitored"], SERIES_40["enabled"], SERIES_40["id"])
            )
            print(f"reset series id={SERIES_40['id']} fields to baseline")

        # download_client 1
        existing = db.execute("SELECT id FROM download_clients WHERE id=?",
                              (DL_CLIENT_1["id"],)).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO download_clients(id, name, type, host, port, use_ssl, "
                " username, password, category, priority, enabled) "
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (DL_CLIENT_1["id"], DL_CLIENT_1["name"], DL_CLIENT_1["type"],
                 DL_CLIENT_1["host"], DL_CLIENT_1["port"], 0,
                 DL_CLIENT_1["username"], DL_CLIENT_1["password"],
                 DL_CLIENT_1["category"], DL_CLIENT_1["priority"], DL_CLIENT_1["enabled"])
            )
            print(f"seeded download_client id={DL_CLIENT_1['id']}")

        # at least one tag so /tags page has rendered content for the
        # integration sweep
        existing = db.execute(
            "SELECT 1 FROM series_tags WHERE series_id=? AND tag=?",
            (40, "seed-tag")
        ).fetchone()
        if not existing:
            db.execute("INSERT INTO series_tags(series_id, tag) VALUES(?,?)",
                       (40, "seed-tag"))
            print("seeded tag 'seed-tag' on series 40")

        db.commit()

    print("seed complete")


if __name__ == "__main__":
    main()

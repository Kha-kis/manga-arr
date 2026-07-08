from __future__ import annotations

from datetime import datetime

from shared import get_cfg, get_db
from events import log_event
from parsing import is_foreign_language, matches

try:
    from .grab_core import grab_item
except ImportError:
    from grab_core import grab_item


async def poll_rss():
    from routers.indexers import fetch_all_rss as _fetch_all_rss_db

    with get_db() as _rdb:
        items = await _fetch_all_rss_db(_rdb)
    source = "Indexers"
    if not items:
        return

    _global_delay = max(0, int(get_cfg("grab_delay_minutes", "0") or "0"))
    now_ts = datetime.utcnow()

    with get_db() as db:
        series_list = [
            dict(r)
            for r in db.execute(
                "SELECT id, title, search_pattern, pub_year, edition_type FROM series"
                " WHERE monitored=1 AND deleted_at IS NULL"
            ).fetchall()
        ]
        seen_urls = {
            r["torrent_url"]
            for r in db.execute("SELECT torrent_url FROM seen").fetchall()
        }
        blocked_urls = {
            r["torrent_url"]
            for r in db.execute("SELECT torrent_url FROM blocklist").fetchall()
        }
        alias_map: dict[int, list[str]] = {}
        for row in db.execute("SELECT series_id, alias FROM series_aliases").fetchall():
            alias_map.setdefault(row["series_id"], []).append(row["alias"])

    grabbed = 0
    for item in items:
        if not item["url"] or item["url"] in seen_urls or item["url"] in blocked_urls:
            continue
        if is_foreign_language(item["title"]):
            continue
        for s in series_list:
            all_patterns = list(
                {s["title"], s["search_pattern"]} | set(alias_map.get(s["id"], []))
            )
            pub_year = s["pub_year"]
            if not any(
                matches(p, item["title"], pub_year=pub_year) for p in all_patterns
            ):
                continue

            try:
                from routers.delay_profiles import get_delay_for_series

                with get_db() as _ddb:
                    delay_minutes = get_delay_for_series(
                        _ddb, s["id"], item.get("protocol", "torrent")
                    )
                if delay_minutes == 0:
                    delay_minutes = _global_delay
            except Exception:
                delay_minutes = _global_delay

            if delay_minutes < 0:
                break

            if delay_minutes > 0:
                with get_db() as db2:
                    existing_pr = db2.execute(
                        "SELECT first_seen FROM pending_releases WHERE series_id=? AND url=?",
                        (s["id"], item["url"]),
                    ).fetchone()
                    if not existing_pr:
                        db2.execute(
                            "INSERT OR IGNORE INTO pending_releases"
                            "(series_id, url, title, indexer, protocol, size_bytes)"
                            " VALUES(?,?,?,?,?,?)",
                            (
                                s["id"],
                                item["url"],
                                item["title"],
                                item.get("indexer", ""),
                                item.get("protocol", "torrent"),
                                item.get("size_bytes", 0),
                            ),
                        )
                    else:
                        elapsed = (
                            now_ts
                            - datetime.fromisoformat(
                                existing_pr["first_seen"].replace("Z", "")
                            )
                        ).total_seconds() / 60
                        if elapsed >= delay_minutes:
                            if await grab_item(item, s["id"]):
                                grabbed += 1
                                seen_urls.add(item["url"])
                                with get_db() as db3:
                                    db3.execute(
                                        "DELETE FROM pending_releases WHERE series_id=? AND url=?",
                                        (s["id"], item["url"]),
                                    )
            else:
                if await grab_item(item, s["id"]):
                    grabbed += 1
                    seen_urls.add(item["url"])
            break

    with get_db() as db:
        db.execute(
            "DELETE FROM pending_releases WHERE first_seen < datetime('now', '-7 days')"
        )

    log_event(
        "rss_poll", f"{source} RSS: {len(items)} items checked, {grabbed} grabbed"
    )

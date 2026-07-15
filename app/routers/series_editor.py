"""Series Editor — bulk edit multiple series at once (Sonarr parity)."""

import json
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from routers._templates import templates

from shared import get_db

router = APIRouter()

MONITOR_MODES = ["all", "future", "missing", "existing", "none"]


# ── Page ──────────────────────────────────────────────────────────────────────
@router.get("/series-editor", response_class=HTMLResponse)
async def series_editor_page(request: Request):
    # Optional pre-selection from library multi-select
    ids_param = request.query_params.get("ids", "")
    preselected = set()
    if ids_param:
        for part in ids_param.split(","):
            try:
                preselected.add(int(part.strip()))
            except ValueError:
                pass

    with get_db() as db:
        series_list = db.execute(
            "SELECT s.id, s.title, s.status, s.monitored, s.monitor_mode,"
            " s.quality_profile_id, s.root_folder_id, s.tags,"
            " (SELECT COUNT(*) FROM volumes v WHERE v.series_id=s.id AND v.status='wanted') as wanted_count,"
            " (SELECT COUNT(*) FROM volumes v WHERE v.series_id=s.id AND v.status='downloaded') as downloaded_count,"
            " qp.name as quality_profile_name,"
            " rf.path as root_folder_path"
            " FROM series s"
            " LEFT JOIN quality_profiles qp ON qp.id=s.quality_profile_id"
            " LEFT JOIN root_folders rf ON rf.id=s.root_folder_id"
            " WHERE s.deleted_at IS NULL"
            " ORDER BY s.title"
        ).fetchall()
        profiles = db.execute(
            "SELECT id, name FROM quality_profiles ORDER BY name"
        ).fetchall()
        root_folders = db.execute(
            "SELECT id, path FROM root_folders ORDER BY path"
        ).fetchall()
        all_tags = [
            r["tag"]
            for r in db.execute(
                "SELECT DISTINCT tag FROM series_tags ORDER BY tag"
            ).fetchall()
        ]
        lang_profiles = db.execute(
            "SELECT id, name FROM language_profiles ORDER BY name"
        ).fetchall()
    return templates.TemplateResponse(
        request,
        "series_editor.html",
        {
            "series": series_list,
            "profiles": profiles,
            "lang_profiles": lang_profiles,
            "root_folders": root_folders,
            "all_tags": all_tags,
            "monitor_modes": MONITOR_MODES,
            "preselected": list(preselected),
        },
    )


# ── Bulk Edit ─────────────────────────────────────────────────────────────────
@router.post("/series-editor/save")
async def series_editor_save(request: Request):
    """
    Body: {
      series_ids: [1,2,3],
      monitored: true | false | null (null=no change),
      monitor_mode: "all" | null,
      quality_profile_id: 1 | null,
      root_folder_id: 1 | null,
      tags_add: ["tag1"],
      tags_remove: ["tag2"],
    }
    """
    body = await request.json()
    series_ids = [int(i) for i in body.get("series_ids", [])]
    if not series_ids:
        return JSONResponse({"ok": False, "message": "No series selected"})

    ph = ",".join("?" * len(series_ids))

    with get_db() as db:
        # Build SET clause dynamically based on what's changing
        updates: dict[str, object] = {}
        if body.get("monitored") is not None:
            updates["monitored"] = 1 if body["monitored"] else 0
        if body.get("monitor_mode"):
            updates["monitor_mode"] = body["monitor_mode"]
        if body.get("quality_profile_id") is not None:
            qpid = body["quality_profile_id"]
            updates["quality_profile_id"] = int(qpid) if qpid else None
        if body.get("language_profile_id") is not None:
            lpid = body["language_profile_id"]
            updates["language_profile_id"] = int(lpid) if lpid else None
        if body.get("root_folder_id") is not None:
            rfid = body["root_folder_id"]
            updates["root_folder_id"] = int(rfid) if rfid else None

        # Update strategy
        valid_strategies = {"always", "once", "throttled"}
        if body.get("update_strategy") in valid_strategies:
            updates["update_strategy"] = body["update_strategy"]

        # Required scanlator — empty string clears it (explicit clear via 'CLEAR' keyword or empty)
        if "required_scanlator" in body:
            sc = (body["required_scanlator"] or "").strip()
            updates["required_scanlator"] = sc or None

        # Source type filter
        valid_source_types = {"any", "official_only", "fan_only"}
        if body.get("source_type") in valid_source_types:
            updates["source_type"] = body["source_type"]

        if updates:
            set_clause = ", ".join(f"{k}=?" for k in updates)
            values = list(updates.values()) + series_ids
            db.execute(f"UPDATE series SET {set_clause} WHERE id IN ({ph})", values)

        # Tags
        tags_add = body.get("tags_add", [])
        tags_remove = body.get("tags_remove", [])
        for sid in series_ids:
            for tag in tags_add:
                db.execute(
                    "INSERT INTO series_tags(series_id,tag,source) VALUES(?,?,'manual')"
                    " ON CONFLICT(series_id,tag) DO UPDATE SET source='manual'",
                    (sid, tag),
                )
            for tag in tags_remove:
                db.execute(
                    "DELETE FROM series_tags WHERE series_id=? AND tag=?", (sid, tag)
                )

    return JSONResponse({"ok": True, "updated": len(series_ids)})


# ── Search ────────────────────────────────────────────────────────────────────
@router.get("/api/series-editor/search")
async def series_editor_search(q: str = "", tag: str = "", status: str = ""):
    with get_db() as db:
        clauses = ["s.deleted_at IS NULL"]
        params: list = []
        if q:
            clauses.append("s.title LIKE ?")
            params.append(f"%{q}%")
        if status:
            clauses.append("s.status=?")
            params.append(status)
        where = "WHERE " + " AND ".join(clauses)
        rows = db.execute(
            f"SELECT s.id, s.title, s.status, s.monitored, s.monitor_mode,"
            f" s.quality_profile_id, qp.name as qp_name"
            f" FROM series s LEFT JOIN quality_profiles qp ON qp.id=s.quality_profile_id"
            f" {where} ORDER BY s.title LIMIT 200",
            params,
        ).fetchall()
        # Tag filter
        if tag:
            tagged = {
                r["series_id"]
                for r in db.execute(
                    "SELECT series_id FROM series_tags WHERE tag=?", (tag,)
                ).fetchall()
            }
            rows = [r for r in rows if r["id"] in tagged]
    return JSONResponse([dict(r) for r in rows])

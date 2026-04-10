"""Quality Definitions — min/max file size constraints per quality type."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from routers._templates import templates

from shared import get_db

router = APIRouter()

QUALITY_ORDER = ["cbz", "cbr", "epub", "pdf", "zip", "unknown"]


def _all_definitions(db):
    return db.execute(
        "SELECT * FROM quality_definitions ORDER BY order_num, quality"
    ).fetchall()


# ── List / Edit page ──────────────────────────────────────────────────────────
@router.get("/quality-definitions", response_class=HTMLResponse)
async def quality_definitions_page(request: Request, saved: str = ""):
    with get_db() as db:
        definitions = _all_definitions(db)
    return templates.TemplateResponse(request, "quality_definitions.html", {
        "definitions": definitions,
        "saved": saved == "1",
    })


# ── Save all rows ─────────────────────────────────────────────────────────────
@router.post("/quality-definitions")
async def save_quality_definitions(request: Request):
    form = await request.form()
    with get_db() as db:
        defs = _all_definitions(db)
        for d in defs:
            q = d["quality"]
            title   = (form.get(f"q_{q}_title") or "").strip() or d["title"]
            try:
                min_size = float(form.get(f"q_{q}_min") or 0)
            except (TypeError, ValueError):
                min_size = d["min_size"]
            try:
                max_size = float(form.get(f"q_{q}_max") or 0)
            except (TypeError, ValueError):
                max_size = d["max_size"]
            db.execute(
                "UPDATE quality_definitions SET title=?, min_size=?, max_size=? WHERE quality=?",
                (title, min_size, max_size, q)
            )
    return RedirectResponse("/quality-definitions?saved=1", status_code=303)

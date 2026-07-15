"""Single-administrator setup, login, session, and password routes."""

from __future__ import annotations

import logging
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from auth import (
    AUTH_COOKIE_NAME,
    LOGIN_THROTTLE,
    SESSION_ABSOLUTE_SECONDS,
    count_sessions,
    create_admin,
    create_session,
    delete_other_sessions,
    delete_session,
    get_admin,
    hash_password,
    is_admin_configured,
    update_admin_password,
    validate_password,
    validate_session,
    validate_username,
    verify_admin_credentials,
)
from middleware import _should_secure_cookie
from routers._templates import templates
from shared import get_db


router = APIRouter()


def _safe_next(value: str | None) -> str:
    candidate = str(value or "/").strip()
    parsed = urlsplit(candidate)
    if (
        not candidate.startswith("/")
        or candidate.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or "\\" in candidate
        or "\r" in candidate
        or "\n" in candidate
    ):
        return "/"
    return candidate


def _next_location(path: str, destination: str) -> str:
    return f"{path}?next={quote(destination, safe='')}"


def _client_id(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_session_cookie(response, request: Request, token: str) -> None:
    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        max_age=SESSION_ABSOLUTE_SECONDS,
        path="/",
        secure=_should_secure_cookie(request.scope),
        httponly=True,
        samesite="lax",
    )


def _render_auth(
    request: Request,
    template_name: str,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    **context,
):
    response_headers = {"Cache-Control": "no-store", **(headers or {})}
    return templates.TemplateResponse(
        request,
        template_name,
        context,
        status_code=status_code,
        headers=response_headers,
    )


@router.get("/healthz", include_in_schema=False)
async def healthz():
    try:
        with get_db() as db:
            db.execute("SELECT 1").fetchone()
        return JSONResponse({"status": "ok"})
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, next: str = "/"):
    if is_admin_configured():
        return RedirectResponse(
            _next_location("/login", _safe_next(next)), status_code=303
        )
    return _render_auth(request, "auth_setup.html", next=_safe_next(next))


@router.post("/setup", response_class=HTMLResponse)
async def setup_admin(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    password_confirm: str = Form(""),
    next: str = Form("/"),
):
    destination = _safe_next(next)
    if is_admin_configured():
        return RedirectResponse(_next_location("/login", destination), status_code=303)
    normalized_username = validate_username(username)
    error = None
    if normalized_username is None:
        error = "Username must be 3-32 letters, numbers, dots, dashes, or underscores."
    elif password != password_confirm:
        error = "Passwords do not match."
    else:
        error = validate_password(password)
    if error:
        return _render_auth(
            request,
            "auth_setup.html",
            status_code=400,
            error=error,
            username=username,
            next=destination,
        )

    password_hash = await run_in_threadpool(hash_password, password)
    try:
        admin = create_admin(normalized_username, password_hash)
    except RuntimeError:
        return RedirectResponse(_next_location("/login", destination), status_code=303)
    token = create_session(admin["id"])
    response = RedirectResponse(destination, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    _set_session_cookie(response, request, token)
    logging.getLogger(__name__).info("local administrator setup completed")
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    destination = _safe_next(next)
    if not is_admin_configured():
        return RedirectResponse(_next_location("/setup", destination), status_code=303)
    if validate_session(request.cookies.get(AUTH_COOKIE_NAME, "")):
        return RedirectResponse(destination, status_code=303)
    return _render_auth(request, "auth_login.html", next=destination)


@router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
):
    destination = _safe_next(next)
    if not is_admin_configured():
        return RedirectResponse(_next_location("/setup", destination), status_code=303)
    client_id = _client_id(request)
    retry_after = LOGIN_THROTTLE.retry_after(client_id)
    if retry_after:
        return _render_auth(
            request,
            "auth_login.html",
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            error="Too many login attempts. Try again later.",
            username=username,
            next=destination,
        )

    admin = await run_in_threadpool(verify_admin_credentials, username, password)
    if not admin:
        LOGIN_THROTTLE.record_failure(client_id)
        logging.getLogger(__name__).warning(
            "browser login failed for peer %s", client_id
        )
        return _render_auth(
            request,
            "auth_login.html",
            status_code=401,
            error="Invalid username or password.",
            username=username,
            next=destination,
        )

    LOGIN_THROTTLE.record_success(client_id)
    token = create_session(admin["id"])
    response = RedirectResponse(destination, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    _set_session_cookie(response, request, token)
    logging.getLogger(__name__).info("browser login succeeded for peer %s", client_id)
    return response


@router.post("/logout")
async def logout(request: Request):
    delete_session(request.cookies.get(AUTH_COOKIE_NAME, ""))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return response


@router.get("/settings/security", response_class=HTMLResponse)
async def security_settings(request: Request, changed: str = "", revoked: str = ""):
    admin = get_admin()
    return templates.TemplateResponse(
        request,
        "settings_security.html",
        {
            "admin_username": admin["username"] if admin else "",
            "session_count": count_sessions(),
            "changed": changed,
            "revoked": revoked,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/settings/security/password", response_class=HTMLResponse)
async def change_password(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    new_password_confirm: str = Form(""),
):
    admin = get_admin()
    current = await run_in_threadpool(
        verify_admin_credentials,
        admin["username"] if admin else "",
        current_password,
    )
    error = None
    if not current:
        error = "Current password is incorrect."
    elif new_password != new_password_confirm:
        error = "New passwords do not match."
    else:
        error = validate_password(new_password)
    if error:
        return templates.TemplateResponse(
            request,
            "settings_security.html",
            {
                "admin_username": admin["username"] if admin else "",
                "session_count": count_sessions(),
                "error": error,
            },
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    replacement = await run_in_threadpool(hash_password, new_password)
    update_admin_password(replacement)
    token = create_session(current["id"])
    response = RedirectResponse("/settings/security?changed=1", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    _set_session_cookie(response, request, token)
    logging.getLogger(__name__).info("administrator password changed; sessions revoked")
    return response


@router.post("/settings/security/sessions/revoke")
async def revoke_other_sessions(request: Request):
    delete_other_sessions(request.cookies.get(AUTH_COOKIE_NAME, ""))
    return RedirectResponse("/settings/security?revoked=1", status_code=303)

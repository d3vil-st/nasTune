import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.services import plex as plex_svc
from app.services import plex_sessions as sessions

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory="app/templates")


def _set_session_cookie(response, token: str, request: Request) -> None:
    response.set_cookie(
        sessions.COOKIE_NAME, token,
        max_age=sessions.SESSION_TTL,
        httponly=True, samesite="lax", path="/",
        secure=request.url.scheme == "https",
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if not sessions.auth_enabled():
        return RedirectResponse("/", status_code=302)
    token = request.cookies.get(sessions.COOKIE_NAME, "")
    if await sessions.get_session(token):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {})


@router.post("/auth/plex/pin")
async def create_login_pin():
    try:
        pin = await plex_svc.create_pin()
    except httpx.HTTPError as exc:
        return JSONResponse({"detail": f"plex.tv unreachable: {exc}"}, status_code=502)
    pin["auth_url"] = await plex_svc.build_auth_url(pin["code"])
    return pin


class PinCheckBody(BaseModel):
    pin_id: int
    code: str


@router.post("/auth/plex/pin/check")
async def check_login_pin(body: PinCheckBody, request: Request):
    try:
        token = await plex_svc.check_pin(body.pin_id, body.code)
    except httpx.HTTPError as exc:
        return JSONResponse({"detail": f"plex.tv unreachable: {exc}"}, status_code=502)
    if not token:
        return {"pending": True}

    user = await plex_svc.check_membership(token)
    if user is None:
        return JSONResponse(
            {"detail": "This Plex account does not have access to the linked server."},
            status_code=403,
        )

    row = await sessions.get_user_row(user["id"])
    if row is None:
        # First sign-in: register as pending until the admin approves.
        await sessions.upsert_user(user)
        return JSONResponse(
            {"detail": "Your account is awaiting approval by the administrator."},
            status_code=403,
        )
    if not row["enabled"]:
        await sessions.upsert_user(user)  # refresh name/avatar
        return JSONResponse(
            {"detail": "Your account is awaiting approval by the administrator."},
            status_code=403,
        )

    await sessions.upsert_user(user)
    session_token = await sessions.create_session(user)
    response = JSONResponse({"ok": True})
    _set_session_cookie(response, session_token, request)
    return response


@router.post("/auth/logout")
async def logout(request: Request):
    token = request.cookies.get(sessions.COOKIE_NAME, "")
    if token:
        await sessions.delete_session(token)
    response = JSONResponse({"ok": True})
    response.delete_cookie(sessions.COOKIE_NAME, path="/")
    return response


@router.get("/auth/me")
async def me(request: Request):
    token = request.cookies.get(sessions.COOKIE_NAME, "")
    user = await sessions.get_session_user(token) if token else None
    return {"auth_enabled": sessions.auth_enabled(), "user": user}

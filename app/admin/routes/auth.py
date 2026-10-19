"""Admin login / logout."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import templates
from app.db.base import get_session
from app.services import admin_auth

router = APIRouter()


@router.get("/login")
async def login_form(request: Request):
    if request.session.get("admin"):
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    if not await admin_auth.verify_login(session, username, password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Неверный логин или пароль"},
            status_code=401,
        )
    request.session["admin"] = username
    return RedirectResponse("/admin", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.pop("admin", None)
    return RedirectResponse("/admin/login", status_code=303)

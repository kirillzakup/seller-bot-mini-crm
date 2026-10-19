"""Admin CRUD for comment funnels (keyword → auto-DM a text or link)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import require_login, templates
from app.db.base import get_session
from app.db.models import CommentFunnel
from app.services.media import active_media

router = APIRouter(prefix="/funnels")


def _img_id(value: str) -> int | None:
    value = (value or "").strip()
    return int(value) if value.isdigit() else None


@router.get("")
async def list_funnels(
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    funnels = (
        (await session.execute(select(CommentFunnel).order_by(CommentFunnel.id))).scalars().all()
    )
    return templates.TemplateResponse(
        request, "funnels_list.html", {"active": "funnels", "funnels": funnels}
    )


@router.get("/new")
async def new_funnel(
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    return templates.TemplateResponse(
        request,
        "funnel_form.html",
        {"active": "funnels", "funnel": None, "media": await active_media(session)},
    )


@router.post("/new")
async def create_funnel(
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    keyword: str = Form(...),
    reply_type: str = Form("text"),
    reply_value: str = Form(""),
    public_reply: str = Form(""),
    image_id: str = Form(""),
    enabled: str = Form(None),
):
    session.add(
        CommentFunnel(
            keyword=keyword.strip(),
            reply_type="link" if reply_type == "link" else "text",
            reply_value=reply_value.strip(),
            public_reply=public_reply.strip(),
            image_id=_img_id(image_id),
            enabled=bool(enabled),
        )
    )
    return RedirectResponse("/admin/funnels", status_code=303)


@router.get("/{funnel_id}/edit")
async def edit_funnel(
    funnel_id: int,
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    funnel = await session.get(CommentFunnel, funnel_id)
    if funnel is None:
        return RedirectResponse("/admin/funnels", status_code=303)
    return templates.TemplateResponse(
        request,
        "funnel_form.html",
        {"active": "funnels", "funnel": funnel, "media": await active_media(session)},
    )


@router.post("/{funnel_id}/edit")
async def update_funnel(
    funnel_id: int,
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    keyword: str = Form(...),
    reply_type: str = Form("text"),
    reply_value: str = Form(""),
    public_reply: str = Form(""),
    image_id: str = Form(""),
    enabled: str = Form(None),
):
    funnel = await session.get(CommentFunnel, funnel_id)
    if funnel is None:
        return RedirectResponse("/admin/funnels", status_code=303)
    funnel.keyword = keyword.strip()
    funnel.reply_type = "link" if reply_type == "link" else "text"
    funnel.reply_value = reply_value.strip()
    funnel.public_reply = public_reply.strip()
    funnel.image_id = _img_id(image_id)
    funnel.enabled = bool(enabled)
    return RedirectResponse("/admin/funnels", status_code=303)


@router.post("/{funnel_id}/delete")
async def delete_funnel(
    funnel_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    funnel = await session.get(CommentFunnel, funnel_id)
    if funnel is not None:
        await session.delete(funnel)
    return RedirectResponse("/admin/funnels", status_code=303)

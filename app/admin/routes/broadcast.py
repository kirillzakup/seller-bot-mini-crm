"""Admin «📣 Рассылка»: send an informational broadcast about new courses/news to every
client who has «🎁 Рассылки» enabled (the per-client toggle in «Диалоги»)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import require_login, templates
from app.db.base import get_session
from app.db.models import MediaAsset
from app.services import broadcast as broadcast_svc
from app.services.media import active_media, public_url

router = APIRouter(prefix="/broadcast")


async def _ctx(session: AsyncSession, result: dict | None = None) -> dict:
    media = [m for m in await active_media(session) if (m.kind or "image") == "image"]
    return {
        "active": "broadcast",
        "count": await broadcast_svc.count_recipients(session),
        "media": media,
        "result": result,
    }


@router.get("")
async def page(
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    return templates.TemplateResponse(request, "broadcast.html", await _ctx(session))


@router.post("/send")
async def send(
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    text: str = Form(""),
    image_id: int = Form(0),
):
    image_url = None
    if image_id:
        asset = await session.get(MediaAsset, image_id)
        if asset is not None:
            image_url = public_url(asset.filename)
    result = await broadcast_svc.send_broadcast(session, text=text, image_url=image_url)
    return templates.TemplateResponse(request, "broadcast.html", await _ctx(session, result=result))

"""Admin «🖼 Медиатека»: upload media assets (images/circles/videos/voice/links) the bot can send.

Files are stored under the data volume and served publicly at /media/{filename};
the AI picks one by description via the ``send_asset`` tool or the legacy ``send_image``.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import require_login, templates
from app.db.base import get_session
from app.db.models import MediaAsset
from app.services import media as media_svc

router = APIRouter(prefix="/media")


@router.get("")
async def page(
    request: Request,
    err: int | None = None,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    items = await media_svc.list_media(session)
    return templates.TemplateResponse(
        request, "media.html", {"active": "media", "items": items, "err": err}
    )


@router.post("/upload")
async def upload(
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    title: str = Form(""),
    tags: str = Form(""),
    kind: str = Form("image"),
    trigger: str = Form(""),
    url: str = Form(""),
    file: UploadFile | None = File(None),
):
    # For kind=link, no file is needed; otherwise file is required.
    if kind == "link":
        if not url.strip():
            return RedirectResponse("/admin/media?err=1", status_code=303)
        asset = MediaAsset(
            title=(title or "").strip() or "Ссылка",
            filename="",
            mime="",
            tags=(tags or "").strip(),
            kind="link",
            url=(url or "").strip(),
            trigger=(trigger or "").strip(),
        )
        session.add(asset)
        await session.flush()
        return RedirectResponse("/admin/media", status_code=303)

    # For file-backed kinds, file is required.
    if not file:
        return RedirectResponse("/admin/media?err=1", status_code=303)
    data = await file.read()

    # Extend allowed file types for video/audio/etc.
    allowed_ext = dict(media_svc.ALLOWED_EXT)
    if kind in ("circle", "video"):
        allowed_ext.update({
            "video/mp4": ".mp4",
            "video/quicktime": ".mov",
            "video/webm": ".webm",
            "video/3gpp": ".3gp",
        })
    if kind == "voice":
        allowed_ext.update({
            "audio/ogg": ".ogg",
            "audio/opus": ".opus",
            "audio/mpeg": ".mp3",
            "audio/mp4": ".m4a",
            "audio/aac": ".aac",
            "audio/wav": ".wav",
            "audio/webm": ".weba",
        })

    # Images stay at the small library cap; video/circle/voice may be larger.
    max_bytes = media_svc.MAX_BYTES if kind == "image" else media_svc.operator_max_bytes()
    ext = allowed_ext.get((file.content_type or "").lower())
    if ext is None or not data or len(data) > max_bytes:
        return RedirectResponse("/admin/media?err=1", status_code=303)

    media_svc.ensure_media_dir()
    filename = f"{uuid.uuid4().hex}{ext}"
    (media_svc.MEDIA_DIR / filename).write_bytes(data)

    asset = MediaAsset(
        title=(title or "").strip() or filename,
        filename=filename,
        mime=file.content_type or "",
        tags=(tags or "").strip(),
        kind=kind,
        url="",
        trigger=(trigger or "").strip(),
    )
    session.add(asset)
    await session.flush()
    return RedirectResponse("/admin/media", status_code=303)


@router.post("/{media_id}/delete")
async def delete(
    media_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    await media_svc.delete_media(session, media_id)
    return RedirectResponse("/admin/media", status_code=303)

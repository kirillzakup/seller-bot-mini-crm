"""Admin «📄 Документы»: upload files the bot can send to clients in chat — price lists,
catalogs, presentations, certificates, achievements.

Files are stored under the data volume and served publicly at /media/{filename}; the AI
picks one by description via the ``send_document`` tool.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import require_login, templates
from app.db.base import get_session
from app.services import documents as doc_svc

router = APIRouter(prefix="/documents")


@router.get("")
async def page(
    request: Request,
    err: int | None = None,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    items = await doc_svc.list_documents(session)
    return templates.TemplateResponse(
        request,
        "documents.html",
        {"active": "documents", "items": items, "categories": doc_svc.CATEGORIES, "err": err},
    )


@router.post("/upload")
async def upload(
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    title: str = Form(""),
    tags: str = Form(""),
    category: str = Form("другое"),
    file: UploadFile = File(...),
):
    data = await file.read()
    doc = await doc_svc.save_document(
        session,
        title=title or (file.filename or ""),
        data=data,
        content_type=file.content_type or "",
        orig_name=file.filename or "",
        category=category,
        tags=tags,
    )
    return RedirectResponse(
        "/admin/documents" if doc else "/admin/documents?err=1", status_code=303
    )


@router.post("/{doc_id}/delete")
async def delete(
    doc_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    await doc_svc.delete_document(session, doc_id)
    return RedirectResponse("/admin/documents", status_code=303)

"""Admin CRUD for the knowledge base (FAQ / objections / info)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import require_login, templates
from app.db.base import get_session
from app.db.models import KbItem
from app.db.models.enums import KbKind

router = APIRouter(prefix="/kb")


@router.get("")
async def list_kb(
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    items = (
        (await session.execute(select(KbItem).order_by(KbItem.kind, KbItem.sort_order, KbItem.id)))
        .scalars()
        .all()
    )
    return templates.TemplateResponse(request, "kb_list.html", {"active": "kb", "items": items})


@router.get("/new")
async def new_kb(request: Request, admin: str = Depends(require_login)):
    return templates.TemplateResponse(
        request, "kb_form.html", {"active": "kb", "item": None, "kinds": list(KbKind)}
    )


@router.post("/new")
async def create_kb(
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    kind: str = Form(...),
    title: str = Form(...),
    body: str = Form(...),
    tags: str = Form(""),
    sort_order: int = Form(0),
    is_active: str = Form(None),
):
    session.add(
        KbItem(
            kind=KbKind(kind),
            title=title,
            body=body,
            tags=tags,
            sort_order=sort_order,
            is_active=bool(is_active),
        )
    )
    return RedirectResponse("/admin/kb", status_code=303)


@router.get("/{item_id}/edit")
async def edit_kb(
    item_id: int,
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    item = await session.get(KbItem, item_id)
    if item is None:
        return RedirectResponse("/admin/kb", status_code=303)
    return templates.TemplateResponse(
        request, "kb_form.html", {"active": "kb", "item": item, "kinds": list(KbKind)}
    )


@router.post("/{item_id}/edit")
async def update_kb(
    item_id: int,
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    kind: str = Form(...),
    title: str = Form(...),
    body: str = Form(...),
    tags: str = Form(""),
    sort_order: int = Form(0),
    is_active: str = Form(None),
):
    item = await session.get(KbItem, item_id)
    if item is None:
        return RedirectResponse("/admin/kb", status_code=303)
    item.kind = KbKind(kind)
    item.title = title
    item.body = body
    item.tags = tags
    item.sort_order = sort_order
    item.is_active = bool(is_active)
    return RedirectResponse("/admin/kb", status_code=303)


@router.post("/{item_id}/delete")
async def delete_kb(
    item_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    item = await session.get(KbItem, item_id)
    if item is not None:
        await session.delete(item)
    return RedirectResponse("/admin/kb", status_code=303)

"""Admin CRUD for creatives (ad creative + landing) with attribution stats."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import require_login, templates
from app.config import get_settings
from app.db.base import get_session
from app.db.models import Creative, Order, User
from app.db.models.enums import OrderStatus

router = APIRouter(prefix="/creatives")


@router.get("")
async def list_creatives(
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    creatives = (
        (await session.execute(select(Creative).order_by(Creative.sort_order, Creative.id)))
        .scalars()
        .all()
    )

    leads_by = dict(
        (
            await session.execute(
                select(User.source, func.count(User.id))
                .where(User.source.is_not(None))
                .group_by(User.source)
            )
        ).all()
    )
    paid_states = [OrderStatus.PAID, OrderStatus.FULFILLED]
    paid_rows = (
        await session.execute(
            select(
                User.source,
                func.count(Order.id),
                func.coalesce(func.sum(Order.amount_kzt), 0),
            )
            .select_from(Order)
            .join(User, Order.user_id == User.id)
            .where(Order.status.in_(paid_states), User.source.is_not(None))
            .group_by(User.source)
        )
    ).all()
    paid_by = {src: (cnt, rev) for src, cnt, rev in paid_rows}

    base = get_settings().base_url.rstrip("/")
    rows = []
    for c in creatives:
        cnt, rev = paid_by.get(c.code, (0, 0))
        rows.append(
            {
                "c": c,
                "leads": leads_by.get(c.code, 0),
                "paid": cnt,
                "revenue_fmt": f"{rev:,}".replace(",", " "),
                "landing_url": f"{base}/l/{c.code}",
            }
        )
    return templates.TemplateResponse(
        request, "creatives_list.html", {"active": "creatives", "rows": rows}
    )


@router.get("/new")
async def new_creative(request: Request, admin: str = Depends(require_login)):
    return templates.TemplateResponse(
        request, "creative_form.html", {"active": "creatives", "creative": None}
    )


def _form_fields(
    code: str,
    title: str,
    headline: str,
    subtext: str,
    media_url: str,
    cta_text: str,
    is_active: str,
    sort_order: int,
) -> dict:
    return {
        "code": code.strip(),
        "title": title,
        "headline": headline,
        "subtext": subtext,
        "media_url": media_url.strip() or None,
        "cta_text": cta_text or "Написать в Telegram",
        "is_active": bool(is_active),
        "sort_order": sort_order,
    }


@router.post("/new")
async def create_creative(
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    code: str = Form(...),
    title: str = Form(...),
    headline: str = Form(""),
    subtext: str = Form(""),
    media_url: str = Form(""),
    cta_text: str = Form("Написать в Telegram"),
    is_active: str = Form(None),
    sort_order: int = Form(0),
):
    session.add(
        Creative(
            **_form_fields(
                code, title, headline, subtext, media_url, cta_text, is_active, sort_order
            )
        )
    )
    return RedirectResponse("/admin/creatives", status_code=303)


@router.get("/{creative_id}/edit")
async def edit_creative(
    creative_id: int,
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    creative = await session.get(Creative, creative_id)
    if creative is None:
        return RedirectResponse("/admin/creatives", status_code=303)
    return templates.TemplateResponse(
        request, "creative_form.html", {"active": "creatives", "creative": creative}
    )


@router.post("/{creative_id}/edit")
async def update_creative(
    creative_id: int,
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    code: str = Form(...),
    title: str = Form(...),
    headline: str = Form(""),
    subtext: str = Form(""),
    media_url: str = Form(""),
    cta_text: str = Form("Написать в Telegram"),
    is_active: str = Form(None),
    sort_order: int = Form(0),
):
    creative = await session.get(Creative, creative_id)
    if creative is None:
        return RedirectResponse("/admin/creatives", status_code=303)
    for key, value in _form_fields(
        code, title, headline, subtext, media_url, cta_text, is_active, sort_order
    ).items():
        setattr(creative, key, value)
    return RedirectResponse("/admin/creatives", status_code=303)


@router.post("/{creative_id}/delete")
async def delete_creative(
    creative_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    creative = await session.get(Creative, creative_id)
    if creative is not None:
        await session.delete(creative)
    return RedirectResponse("/admin/creatives", status_code=303)

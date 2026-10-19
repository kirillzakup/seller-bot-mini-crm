"""Admin dashboard — at-a-glance funnel + revenue + pending work."""

from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import require_login, templates
from app.admin.routes.guide import build_setup_steps, setup_summary
from app.db.base import get_session
from app.db.models import Event, Order, Product, User
from app.db.models.enums import OrderStatus


async def _count(session: AsyncSession, stmt) -> int:
    return (await session.execute(stmt)).scalar_one()


async def dashboard(
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    paid_states = [OrderStatus.PAID, OrderStatus.FULFILLED]

    users = await _count(session, select(func.count(User.id)))
    by_channel = (
        await session.execute(select(User.channel, func.count(User.id)).group_by(User.channel))
    ).all()
    leads_by_channel = {ch.value: cnt for ch, cnt in by_channel}
    products = await _count(session, select(func.count(Product.id)))
    orders = await _count(session, select(func.count(Order.id)))
    paid = await _count(session, select(func.count(Order.id)).where(Order.status.in_(paid_states)))
    fulfilled = await _count(
        session, select(func.count(Order.id)).where(Order.status == OrderStatus.FULFILLED)
    )
    pending_manual = await _count(
        session,
        select(func.count(Order.id)).where(Order.status == OrderStatus.PENDING_CONFIRMATION),
    )
    revenue = await _count(
        session,
        select(func.coalesce(func.sum(Order.amount_kzt), 0)).where(Order.status.in_(paid_states)),
    )
    nudged = await _count(session, select(func.count(Event.id)).where(Event.type == "order_nudged"))
    revoked = await _count(
        session, select(func.count(Event.id)).where(Event.type == "access_revoked")
    )

    stats = {
        "users": users,
        "leads_by_channel": leads_by_channel,
        "products": products,
        "orders": orders,
        "paid": paid,
        "fulfilled": fulfilled,
        "pending_manual": pending_manual,
        "revenue_fmt": f"{revenue:,}".replace(",", " "),
        "nudged": nudged,
        "revoked": revoked,
    }
    setup = setup_summary(await build_setup_steps(session))
    return templates.TemplateResponse(
        request, "dashboard.html", {"active": "dashboard", "stats": stats, "setup": setup}
    )

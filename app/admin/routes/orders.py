"""Admin orders: list, detail, and confirm/reject manual payments."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import require_login, templates
from app.bot.runner import get_bot
from app.channels.registry import get_messenger
from app.db.base import get_session
from app.db.models import AccessGrant, Order, Payment
from app.db.models.enums import OrderStatus
from app.services import orders as orders_svc

router = APIRouter(prefix="/orders")
logger = logging.getLogger(__name__)


@router.get("")
async def list_orders(
    request: Request,
    status: str | None = None,
    deleted: int | None = None,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Order).order_by(Order.id.desc())
    if status:
        try:
            stmt = stmt.where(Order.status == OrderStatus(status))
        except ValueError:
            status = None
    orders = (await session.execute(stmt)).scalars().all()
    return templates.TemplateResponse(
        request,
        "orders_list.html",
        {
            "active": "orders",
            "orders": orders,
            "statuses": list(OrderStatus),
            "current_status": status,
            "bot_ready": get_bot() is not None,
            "deleted": deleted,
        },
    )


@router.get("/{order_id}")
async def order_detail(
    order_id: int,
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    order = await session.get(Order, order_id)
    if order is None:
        return RedirectResponse("/admin/orders", status_code=303)
    payment = (
        (
            await session.execute(
                select(Payment).where(Payment.order_id == order_id).order_by(Payment.id.desc())
            )
        )
        .scalars()
        .first()
    )
    return templates.TemplateResponse(
        request,
        "order_detail.html",
        {
            "active": "orders",
            "order": order,
            "payment": payment,
            "bot_ready": get_bot() is not None,
            "OrderStatus": OrderStatus,
        },
    )


@router.post("/{order_id}/confirm")
async def confirm(
    order_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    order = await session.get(Order, order_id)
    if order is not None and order.user is not None and get_bot() is not None:
        try:
            messenger = await get_messenger(session, order.user.channel)
            await orders_svc.confirm_payment(
                session, order, by=f"admin:{admin}", messenger=messenger, tg_bot=get_bot()
            )
        except Exception:  # noqa: BLE001
            logger.exception("Admin confirm failed for order %s", order_id)
    return RedirectResponse(f"/admin/orders/{order_id}", status_code=303)


@router.post("/{order_id}/reject")
async def reject(
    order_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    order = await session.get(Order, order_id)
    if order is not None and order.user is not None and get_bot() is not None:
        try:
            messenger = await get_messenger(session, order.user.channel)
            await orders_svc.reject_payment(
                session, order, by=f"admin:{admin}", messenger=messenger
            )
        except Exception:  # noqa: BLE001
            logger.exception("Admin reject failed for order %s", order_id)
    return RedirectResponse(f"/admin/orders/{order_id}", status_code=303)


@router.post("/{order_id}/delete")
async def delete_order(
    order_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    """Permanently remove an order and its payments/access grants.

    Note: this only deletes the DB records — it does not revoke any channel
    access already issued. Use for cleaning up test/cancelled orders.
    """
    order = await session.get(Order, order_id)
    if order is not None:
        # Remove dependents first to satisfy foreign keys, then the order.
        await session.execute(delete(Payment).where(Payment.order_id == order_id))
        await session.execute(delete(AccessGrant).where(AccessGrant.order_id == order_id))
        await session.delete(order)
    return RedirectResponse("/admin/orders?deleted=1", status_code=303)

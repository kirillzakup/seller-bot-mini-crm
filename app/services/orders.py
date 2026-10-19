"""Order lifecycle: create → (pay) → confirm → fulfill.

The single security rule of the whole system lives here: ``fulfill`` refuses to
deliver access unless the order is PAID, and fulfillment is idempotent (one
AccessGrant per order, enforced by a UNIQUE column).

Delivery is channel-agnostic: the deliverable is sent through the customer's
``Messenger`` (Telegram / WhatsApp / Instagram). The Telegram bot is passed
separately only so the channel-invite provider can mint an invite link.
"""

from __future__ import annotations

import logging
import secrets
import string
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AccessGrant, Order, Payment, Product, User
from app.db.models.enums import OrderStatus, PaymentProviderType, PaymentStatus
from app.fulfillment.registry import build_fulfillment
from app.services.events import log_event

if TYPE_CHECKING:
    from aiogram import Bot

    from app.channels.base import Messenger

logger = logging.getLogger(__name__)

_ACTIVE_MANUAL = (OrderStatus.AWAITING_PAYMENT, OrderStatus.PENDING_CONFIRMATION)


def _gen_code() -> str:
    return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))


async def _unique_code(session: AsyncSession) -> str:
    for _ in range(10):
        code = _gen_code()
        exists = (await session.execute(select(Order.id).where(Order.code == code))).first()
        if not exists:
            return code
    return _gen_code()


async def _latest_payment(session: AsyncSession, order_id: int) -> Payment | None:
    return (
        (
            await session.execute(
                select(Payment).where(Payment.order_id == order_id).order_by(Payment.id.desc())
            )
        )
        .scalars()
        .first()
    )


async def _send_to_user(
    session: AsyncSession, messenger: Messenger, order: Order, text: str
) -> None:
    user = await session.get(User, order.user_id)
    if user is None:
        return
    try:
        await messenger.send_text(user.external_id, text)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to message user %s on %s", user.external_id, user.channel)


async def create_order(
    session: AsyncSession,
    user: User,
    product: Product,
    method=None,
    quantity: int = 1,
) -> Order:
    """Create an order in AWAITING_PAYMENT for the given product/method."""
    order = Order(
        code=await _unique_code(session),
        user_id=user.id,
        product_id=product.id,
        quantity=max(1, int(quantity or 1)),
        amount_kzt=product.price_kzt * max(1, int(quantity or 1)),
        currency=product.currency,
        status=OrderStatus.AWAITING_PAYMENT,
        payment_method_id=method.id if method else None,
        provider_type=method.type if method else None,
    )
    session.add(order)
    await session.flush()
    await log_event(session, "order_created", user_id=user.id, data={"code": order.code})
    return order


async def get_active_manual_order(session: AsyncSession, user_id: int) -> Order | None:
    """The user's most recent order still awaiting payment/confirmation.

    Used to attach an uploaded receipt. The payment method is now chosen on the
    hosted payment page (not in chat), so we match any active order regardless of
    provider — gateway-paid orders are already PAID/FULFILLED and won't match.
    """
    return (
        (
            await session.execute(
                select(Order)
                .where(
                    Order.user_id == user_id,
                    Order.status.in_(_ACTIVE_MANUAL),
                )
                .order_by(Order.id.desc())
            )
        )
        .scalars()
        .first()
    )


async def attach_receipt(session: AsyncSession, order: Order, file_id: str) -> Payment:
    """Record a manual-payment receipt and move the order to PENDING_CONFIRMATION."""
    pay = await _latest_payment(session, order.id)
    if pay is None:
        pay = Payment(
            order_id=order.id,
            provider_type=order.provider_type or PaymentProviderType.MANUAL,
            amount_kzt=order.amount_kzt,
            currency=order.currency,
            status=PaymentStatus.PENDING,
        )
        session.add(pay)
    pay.receipt_file_id = file_id
    order.status = OrderStatus.PENDING_CONFIRMATION
    await session.flush()
    return pay


async def fulfill(
    session: AsyncSession, order: Order, *, messenger: Messenger, tg_bot: Bot | None = None
) -> AccessGrant:
    """Deliver access. GATED: order must be PAID/FULFILLED. Idempotent."""
    if order.status not in (OrderStatus.PAID, OrderStatus.FULFILLED):
        raise PermissionError("Нельзя выдать доступ: заказ не оплачен")

    existing = (
        await session.execute(select(AccessGrant).where(AccessGrant.order_id == order.id))
    ).scalar_one_or_none()
    if existing:
        return existing

    product = await session.get(Product, order.product_id)
    user = await session.get(User, order.user_id)
    if product is None or user is None:
        raise ValueError("Order is missing product or user")

    provider = build_fulfillment(product)
    result = await provider.grant(tg_bot=tg_bot, product=product, order=order, user=user)

    grant = AccessGrant(
        user_id=user.id,
        product_id=product.id,
        order_id=order.id,
        type=result.type,
        channel_id=result.channel_id,
        invite_link=result.invite_link,
        payload=result.payload,
        expires_at=result.expires_at,
    )
    try:
        async with session.begin_nested():  # savepoint: recover cleanly on a race
            session.add(grant)
            await session.flush()
    except IntegrityError:
        # A concurrent confirm already created the grant (UNIQUE(order_id)) → stay
        # idempotent: never double-deliver, never 500. Return the existing grant.
        grant = (
            await session.execute(select(AccessGrant).where(AccessGrant.order_id == order.id))
        ).scalar_one_or_none()
        if grant is None:
            raise
        order.status = OrderStatus.FULFILLED
        return grant
    order.status = OrderStatus.FULFILLED
    await session.flush()
    await log_event(session, "fulfilled", user_id=user.id, data={"code": order.code})

    try:
        await messenger.send_text(user.external_id, result.message)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to deliver access to %s on %s", user.external_id, user.channel)
    return grant


async def confirm_payment(
    session: AsyncSession,
    order: Order,
    *,
    by: str,
    messenger: Messenger,
    tg_bot: Bot | None = None,
    idempotency_key: str | None = None,
) -> AccessGrant:
    """Mark the order paid (operator/webhook) and deliver access.

    ``idempotency_key`` — идентификатор транзакции шлюза («провайдер:id»).
    Колонка объявлена уникальной, поэтому повторный колбэк по той же транзакции
    не сможет создать вторую подтверждённую оплату: раньше ключ не заполнялся
    никогда, и эта защита существовала только на бумаге.
    """
    if order.status == OrderStatus.FULFILLED:
        return await fulfill(session, order, messenger=messenger, tg_bot=tg_bot)

    now = datetime.now(UTC)
    order.status = OrderStatus.PAID
    order.paid_at = now

    pay = await _latest_payment(session, order.id)
    if pay is not None:
        pay.status = PaymentStatus.CONFIRMED
        pay.confirmed_by = by
        pay.confirmed_at = now
        pay.signature_verified = True  # operator confirmation for manual
        if idempotency_key and not pay.idempotency_key:
            pay.idempotency_key = idempotency_key
    await session.flush()

    return await fulfill(session, order, messenger=messenger, tg_bot=tg_bot)


async def payment_already_processed(session: AsyncSession, idempotency_key: str) -> bool:
    """Был ли уже обработан колбэк по этой транзакции шлюза."""
    if not idempotency_key:
        return False
    row = (
        await session.execute(
            select(Payment).where(
                Payment.idempotency_key == idempotency_key,
                Payment.status == PaymentStatus.CONFIRMED,
            )
        )
    ).scalars().first()
    return row is not None


async def reject_payment(
    session: AsyncSession, order: Order, *, by: str, messenger: Messenger, reason: str = ""
) -> None:
    """Reject a manual payment and notify the client."""
    order.status = OrderStatus.CANCELED
    pay = await _latest_payment(session, order.id)
    if pay is not None:
        pay.status = PaymentStatus.FAILED
        pay.confirmed_by = by
        pay.confirmed_at = datetime.now(UTC)
    await session.flush()

    text = f"❌ Оплата по заказу <code>{order.code}</code> не подтверждена."
    if reason:
        text += f"\nПричина: {reason}"
    text += "\nЕсли это ошибка — пришлите чек повторно или напишите в поддержку."
    await _send_to_user(session, messenger, order, text)

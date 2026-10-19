"""Checkout: receive a manual receipt, operator confirm/reject.

Payment links are sent by the catalog handler / AI tool (a link to the hosted
payment page); method selection happens on that page, not in chat.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.channels.registry import get_messenger
from app.db.base import AsyncSessionLocal
from app.db.models import Order
from app.db.models.enums import OrderStatus
from app.services import orders as orders_svc
from app.services.notify import get_notify_chat_ids, notify_manual_receipt
from app.services.users import get_or_create_telegram_user

router = Router()
logger = logging.getLogger(__name__)


@router.message(F.photo | F.document)
async def receipt_upload(message: Message) -> None:
    """Treat an incoming photo/document as a payment receipt for the active order."""
    if not message.from_user:
        return

    file_id = message.photo[-1].file_id if message.photo else message.document.file_id

    async with AsyncSessionLocal() as session:
        user = await get_or_create_telegram_user(session, message.from_user)
        order = await orders_svc.get_active_manual_order(session, user.id)
        if order is None:
            await session.commit()
            await message.answer(
                "Если это чек об оплате — сначала выберите товар и способ оплаты в 🛍 Каталоге."
            )
            return
        payment = await orders_svc.attach_receipt(session, order, file_id)
        await notify_manual_receipt(session, order, payment)
        await session.commit()

    await message.answer(
        "Спасибо! Чек получен и отправлен на проверку ⏳\n"
        "Как только оплату подтвердят — пришлём доступ сюда."
    )


# ── Operator actions (inside the operators/admin chat) ────────────────────


async def _is_operator_chat(session, callback: CallbackQuery) -> bool:
    if callback.message is None:
        return False
    chat_ids = await get_notify_chat_ids(session)
    return callback.message.chat.id in chat_ids


@router.callback_query(F.data.startswith("pay:confirm:"))
async def op_confirm(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[2])
    async with AsyncSessionLocal() as session:
        if not await _is_operator_chat(session, callback):
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        order = await session.get(Order, order_id)
        if order is None or order.user is None:
            await callback.answer("Заказ не найден", show_alert=True)
            return
        if order.status == OrderStatus.FULFILLED:
            await callback.answer("Уже подтверждён")
            return
        try:
            messenger = await get_messenger(session, order.user.channel)
            await orders_svc.confirm_payment(
                session,
                order,
                by=f"tg:{callback.from_user.id}",
                messenger=messenger,
                tg_bot=callback.message.bot,
            )
            await session.commit()
            code = order.code
        except Exception:  # noqa: BLE001
            await session.rollback()
            logger.exception("Operator confirm failed for order %s", order_id)
            await callback.answer("Ошибка при подтверждении", show_alert=True)
            return

    await callback.answer("Подтверждено ✅")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"✅ Заказ <code>{code}</code> подтверждён, доступ выдан.")


@router.callback_query(F.data.startswith("pay:reject:"))
async def op_reject(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[2])
    async with AsyncSessionLocal() as session:
        if not await _is_operator_chat(session, callback):
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        order = await session.get(Order, order_id)
        if order is None or order.user is None:
            await callback.answer("Заказ не найден", show_alert=True)
            return
        messenger = await get_messenger(session, order.user.channel)
        await orders_svc.reject_payment(
            session, order, by=f"tg:{callback.from_user.id}", messenger=messenger
        )
        await session.commit()
        code = order.code

    await callback.answer("Отклонено")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"❌ Заказ <code>{code}</code> отклонён.")

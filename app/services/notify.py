"""Operator/admin notifications (new manual payment to confirm, escalations).

Notifications go to EVERY configured chat — the operators' group
(``operator_chat_id``) and the admin's personal chat (``admin_chat_id``) — so you
can have both at once. The list is deduplicated (same id in both fields → one send).
"""

from __future__ import annotations

import html
import logging

from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Order, Payment, Product, User
from app.db.models.enums import Channel
from app.services.settings_service import get_setting

logger = logging.getLogger(__name__)


def _contact_link(user: User | None) -> tuple[str, str] | None:
    """(label, url) to reach the client on their OWN channel, or None."""
    if user is None:
        return None
    if user.channel == Channel.INSTAGRAM and user.username:
        return "Instagram", f"https://instagram.com/{user.username}"
    if user.channel == Channel.TELEGRAM and user.username:
        return "Telegram", f"https://t.me/{user.username}"
    if user.channel == Channel.WHATSAPP:
        digits = "".join(c for c in (user.external_id or "") if c.isdigit())
        if digits:
            return "WhatsApp", f"https://wa.me/{digits}"
    return None


def client_block(user: User | None) -> str:
    """HTML lines identifying the client with a reachable account link + a
    'reply in panel' link — so a human can actually contact them (an Instagram
    IGSID alone isn't clickable). Escapes user-supplied text for HTML parse mode."""
    if user is None:
        return "Клиент: —"
    handle = f" (@{html.escape(user.username)})" if user.username else ""
    lines = [f"Клиент: {html.escape(user.full_name)}{handle}"]
    link = _contact_link(user)
    if link:
        label, url = link
        lines.append(f"{label}: {url}")
    if getattr(user, "ad_ref", None):  # which Meta ad brought this lead
        lines.append(f"🎯 Из рекламы: {html.escape(user.ad_ref)}")
    base = get_settings().base_url.rstrip("/")
    lines.append(f"💬 Ответить в панели: {base}/admin/dialogs?u={user.id}")
    return "\n".join(lines)


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


async def get_notify_targets(session: AsyncSession) -> list[tuple[int, int | None]]:
    """(chat_id, message_thread_id) for each operator/admin notification target.

    The operators' chat can be a forum supergroup with Topics — notifications go
    into the configured topic (``operator_chat_thread_id``) instead of the General
    thread. The admin's personal chat (``admin_chat_id``) never uses a thread.
    Deduplicated by chat id, order preserved (operator first, so its thread wins).
    """
    thread_id = _int_or_none(await get_setting(session, "operator_chat_thread_id"))
    targets: list[tuple[int, int | None]] = []
    seen: set[int] = set()
    for key, th in (("operator_chat_id", thread_id), ("admin_chat_id", None)):
        n = _int_or_none(await get_setting(session, key))
        if n is not None and n not in seen:
            seen.add(n)
            targets.append((n, th))
    return targets


async def get_notify_chat_ids(session: AsyncSession) -> list[int]:
    """Distinct Telegram chat ids that receive operator notifications (thread-agnostic —
    used for membership checks like 'did this callback come from an operator chat')."""
    return [cid for cid, _ in await get_notify_targets(session)]


async def send_operator_message(session: AsyncSession, text: str) -> bool:
    """Send a plain notification to every configured operator/admin chat.

    Returns True if it reached at least one chat, False if none are configured
    or the send failed everywhere.
    """
    targets = await get_notify_targets(session)
    if not targets:
        logger.warning("operator/admin chat not configured — escalation not delivered")
        return False
    from app.bot.runner import get_bot

    bot = get_bot()
    if bot is None:
        logger.warning("Bot not configured — operator message not delivered")
        return False
    delivered = False
    for cid, thread_id in targets:
        try:
            await bot.send_message(cid, text, message_thread_id=thread_id)
            delivered = True
        except Exception:  # noqa: BLE001
            logger.exception("send_operator_message failed for chat %s", cid)
    return delivered


def confirm_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить", callback_data=f"pay:confirm:{order_id}"
                ),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"pay:reject:{order_id}"),
            ]
        ]
    )


async def _receipt_photo(session: AsyncSession, user: User | None, ref: str):
    """Return a Telegram-sendable receipt image so the operator actually SEES it: a
    native file_id (Telegram), or the downloaded bytes for an Instagram attachment URL
    / WhatsApp media id (fetched via that channel's messenger, token-gated)."""
    if not ref or user is None:
        return None
    if user.channel == Channel.TELEGRAM:
        return ref  # native Telegram file_id — send as-is
    from app.channels.registry import get_messenger

    messenger = await get_messenger(session, user.channel)
    ref_dict = {"url": ref} if str(ref).startswith("http") else {"file_id": ref}
    data = await messenger.download_media(ref_dict)
    return BufferedInputFile(data, "receipt.jpg") if data else None


async def notify_manual_receipt(
    session: AsyncSession, order: Order, payment: Payment | None, *,
    receipt_amount: int | None = None,
) -> None:
    """Send the uploaded receipt to every operators'/admin chat with action buttons.

    Operators always work in Telegram. The receipt photo itself is forwarded: a
    Telegram file_id as-is, or the bytes downloaded from the WhatsApp/Instagram media
    ref. If the download fails, the caption + buttons are still sent as a note.
    """
    targets = await get_notify_targets(session)
    if not targets:
        logger.warning("admin_chat_id/operator_chat_id not set — cannot notify operators")
        return

    product = await session.get(Product, order.product_id)
    user = await session.get(User, order.user_id)
    amount = f"{order.amount_kzt:,} {order.currency}".replace(",", " ")
    channel = user.channel.value if user else "?"
    # Compare the amount the bot READ off the receipt with the order amount, so a wrong /
    # fake / partial receipt is flagged instantly instead of eyeballed.
    if receipt_amount is not None:
        rc = f"{receipt_amount:,} {order.currency}".replace(",", " ")
        if receipt_amount == order.amount_kzt:
            amount_line = f"Сумма заказа: {amount}\nСумма на чеке: {rc} ✅ совпадает\n"
        else:
            amount_line = (
                f"Сумма заказа: {amount}\n"
                f"⚠️ <b>Сумма на чеке: {rc} — НЕ совпадает с заказом! Проверьте вручную.</b>\n"
            )
    else:
        amount_line = f"Сумма заказа: {amount} — <b>сверьте с суммой на чеке</b> 👇\n"
    caption = (
        "🧾 <b>Чек на проверку</b>\n"
        f"Заказ: <code>{order.code}</code>\n"
        f"Канал: {channel}\n"
        f"Товар: {html.escape(product.title) if product else order.product_id}\n"
        f"{amount_line}"
        f"{client_block(user)}\n"
        "Доступ уйдёт клиенту только после «Подтвердить»."
    )
    kb = confirm_kb(order.id)

    from app.bot.runner import get_bot

    bot = get_bot()
    if bot is None:
        logger.warning("Bot not configured — operators not notified about order %s", order.code)
        return

    ref = payment.receipt_file_id if payment else None
    photo = await _receipt_photo(session, user, str(ref or "")) if ref else None
    for cid, thread_id in targets:
        try:
            if photo is not None:
                await bot.send_photo(
                    cid, photo, caption=caption, reply_markup=kb, message_thread_id=thread_id
                )
            else:
                note = caption + ("\n\n⚠️ Фото чека — в переписке с клиентом." if ref else "")
                await bot.send_message(cid, note, reply_markup=kb, message_thread_id=thread_id)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to notify chat %s about order %s", cid, order.code)

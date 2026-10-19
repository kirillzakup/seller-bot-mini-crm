"""Deliver access to a private channel/group via a one-time invite link.

Requirements: the bot must be an admin of the target channel/group with the
"invite users via link" permission. Subscription expiry/kick is handled by the
scheduler (Phase 4) using ``AccessGrant.expires_at`` + ``revoke``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from app.db.models import Order, Product, User
from app.db.models.enums import FulfillmentType
from app.fulfillment.base import FulfillmentProvider, FulfillmentResult

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)


class ChannelInviteFulfillment(FulfillmentProvider):
    type = FulfillmentType.CHANNEL_INVITE

    async def grant(
        self, *, tg_bot: Bot | None, product: Product, order: Order, user: User
    ) -> FulfillmentResult:
        if tg_bot is None:
            raise ValueError("Для выдачи инвайта нужен Telegram-бот (tg_bot)")
        cfg = product.fulfillment_config or {}
        chat_id = cfg.get("chat_id")
        if not chat_id:
            raise ValueError("В карточке товара не задан chat_id канала/группы")

        member_limit = int(cfg.get("member_limit", 1))
        expires_at = None
        if product.subscription_days:
            expires_at = datetime.now(UTC) + timedelta(days=int(product.subscription_days))

        link = await tg_bot.create_chat_invite_link(
            chat_id=chat_id,
            member_limit=member_limit,
            name=f"order {order.code}"[:32],
        )

        intro = (cfg.get("text") or "Готово! Доступ открыт. Перейдите по ссылке 👇").strip()
        msg = f"{intro}\n\n{link.invite_link}"
        if expires_at:
            msg += f"\n\n⏳ Доступ действует до {expires_at:%d.%m.%Y}."

        return FulfillmentResult(
            type=self.type,
            message=msg,
            invite_link=link.invite_link,
            channel_id=str(chat_id),
            expires_at=expires_at,
        )

    async def revoke(self, *, tg_bot: Bot | None, channel_id: str | None, user: User) -> None:
        # We can only kick a known Telegram member (buyers who joined via Telegram).
        if not channel_id or tg_bot is None or user.telegram_id is None:
            return
        try:
            # ban + unban == kick while still allowing a future re-purchase
            await tg_bot.ban_chat_member(chat_id=channel_id, user_id=user.telegram_id)
            await tg_bot.unban_chat_member(chat_id=channel_id, user_id=user.telegram_id)
        except Exception as e:  # noqa: BLE001 — best-effort revoke, just log
            logger.warning("Failed to revoke channel access for %s: %s", user.telegram_id, e)

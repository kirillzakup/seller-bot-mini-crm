"""Resolve the right Messenger for a channel.

Telegram uses the in-process aiogram Bot. WhatsApp/Instagram use the Meta
adapter (added in the Meta step); until configured, requesting them raises.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import Messenger
from app.channels.telegram import TelegramMessenger
from app.db.models.enums import Channel


async def get_messenger(session: AsyncSession, channel: Channel) -> Messenger:
    if channel == Channel.TELEGRAM:
        from app.bot.runner import get_bot

        return TelegramMessenger(get_bot())

    if channel in (Channel.WHATSAPP, Channel.INSTAGRAM):
        try:
            from app.channels.meta import build_meta_messenger
        except ImportError as exc:  # pragma: no cover - until Meta step lands
            raise NotImplementedError("Meta adapter not installed yet") from exc
        return await build_meta_messenger(session, channel)

    raise NotImplementedError(f"No messenger for channel {channel}")

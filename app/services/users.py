"""Contact/lead persistence — channel-agnostic."""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.db.models.enums import Channel


async def get_or_create_contact(
    session: AsyncSession,
    *,
    channel: Channel,
    external_id: str,
    first_name: str | None = None,
    last_name: str | None = None,
    username: str | None = None,
    phone: str | None = None,
    telegram_id: int | None = None,
    reset_reminders: bool = True,
) -> User:
    """Return the stored contact for (channel, external_id), creating/refreshing it."""
    external_id = str(external_id)
    user = (
        await session.execute(
            select(User).where(User.channel == channel, User.external_id == external_id)
        )
    ).scalar_one_or_none()

    now = datetime.now(UTC)
    if user is None:
        user = User(
            channel=channel,
            external_id=external_id,
            telegram_id=telegram_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
            phone=phone,
            last_seen=now,
        )
        session.add(user)
        await session.flush()
    else:
        # Имя, которое клиент назвал сам, не затираем ником из мессенджера: так
        # «Бахтан» превращался в «Б», и бот обращался к человеку одной буквой.
        if first_name is not None and not user.name_confirmed:
            user.first_name = first_name
        if last_name is not None:
            user.last_name = last_name
        if username is not None:
            user.username = username
        if phone is not None:
            user.phone = phone
        user.last_seen = now
        if reset_reminders:
            # Только настоящий ответ клиента перезапускает цепочку напоминаний.
            # Эхо наших же сообщений сбрасывало счётчик — и цепочка шла по кругу.
            user.reminders_sent = 0
        user.is_blocked = False  # messaged us → reachable again (clears any auto-block)
    return user


async def get_or_create_telegram_user(session: AsyncSession, tg_user: TgUser) -> User:
    """Convenience wrapper for the Telegram channel."""
    return await get_or_create_contact(
        session,
        channel=Channel.TELEGRAM,
        external_id=str(tg_user.id),
        telegram_id=tg_user.id,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
        username=tg_user.username,
    )

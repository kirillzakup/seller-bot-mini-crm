"""/start — register the lead, greet, and offer a one-tap CTA into the dialogue.

The welcome text from the admin is the *trigger* for the AI's first message
(ai_manager.generate_start_message). The greeting carries an inline button
"Хочу узнать подробнее" so the user has a clear next step after Telegram's
mandatory Start tap — no bottom (reply) keyboard.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import start_kb
from app.channels.registry import get_messenger
from app.db.base import AsyncSessionLocal
from app.db.models.enums import Channel
from app.sales import ai_manager
from app.services.events import log_event
from app.services.users import get_or_create_telegram_user

router = Router()
logger = logging.getLogger(__name__)


async def _typing(message: Message) -> None:
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
    except Exception:  # noqa: BLE001
        pass


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    if not message.from_user:
        return

    await _typing(message)
    async with AsyncSessionLocal() as session:
        user = await get_or_create_telegram_user(session, message.from_user)
        # Deep-link creative tag: t.me/<bot>?start=<code> → first-touch attribution
        if command.args and not user.source:
            user.source = command.args.strip()[:64]
            await log_event(session, "lead_source", user_id=user.id, data={"source": user.source})
        greeting = await ai_manager.generate_start_message(session, user=user)
        await session.commit()

    await message.answer(greeting, reply_markup=start_kb())


@router.callback_query(F.data == "start:more")
async def start_more(callback: CallbackQuery) -> None:
    """User tapped «Хочу узнать подробнее» → start the AI sales dialogue."""
    if not callback.from_user:
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)  # consume the button
        except Exception:  # noqa: BLE001
            pass
        await _typing(callback.message)

    async with AsyncSessionLocal() as session:
        user = await get_or_create_telegram_user(session, callback.from_user)
        messenger = await get_messenger(session, Channel.TELEGRAM)
        await ai_manager.handle_text(
            session,
            user=user,
            text="Расскажите подробнее: что это, что входит и сколько стоит?",
            messenger=messenger,
            tg_bot=callback.bot,
        )
        await session.commit()
    await callback.answer()

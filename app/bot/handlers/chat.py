"""Free-text catch-all → AI sales manager (registered last)."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import Message

from app.channels.registry import get_messenger
from app.db.base import AsyncSessionLocal
from app.db.models.enums import Channel
from app.sales import ai_manager
from app.sales.conversation import save_message
from app.services import inbox as inbox_svc
from app.services.users import get_or_create_telegram_user
from app.voice import elevenlabs as voice

router = Router()
logger = logging.getLogger(__name__)


# Any private text that isn't a command reaches the AI manager. Scoped to private so
# the bot never auto-replies inside the operator inbox group (topics are relayed).
@router.message(F.chat.type == "private", F.text & ~F.text.startswith("/"))
async def ai_chat(message: Message) -> None:
    if not message.from_user or not message.text:
        return

    async with AsyncSessionLocal() as session:
        user = await get_or_create_telegram_user(session, message.from_user)

        if user.bot_paused:  # an operator took this chat over — record only, no AI reply
            await save_message(
                session, user.id, "user", message.text, channel_mid=str(message.message_id)
            )
            await inbox_svc.mirror_incoming(session, user, message.text)
            await session.commit()
            return

        # Show "typing" while the model thinks (best-effort)
        try:
            await message.bot.send_chat_action(message.chat.id, "typing")
        except Exception:  # noqa: BLE001
            pass

        messenger = await get_messenger(session, Channel.TELEGRAM)
        vcfg = await voice.voice_config(session)
        reply_voice = voice.should_speak(vcfg, was_voice=False, user_text=message.text)
        await ai_manager.handle_text(
            session,
            user=user,
            text=message.text,
            messenger=messenger,
            tg_bot=message.bot,
            reply_voice=reply_voice,
            incoming_message_id=str(message.message_id),
        )
        await session.commit()

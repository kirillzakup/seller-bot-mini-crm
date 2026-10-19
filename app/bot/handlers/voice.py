"""Voice/audio messages → transcribe (ElevenLabs) → AI sales manager.

Registered before the text catch-all. Disjoint filter (F.voice | F.audio), so it
never competes with the text handler. If voice isn't configured, we nudge the
client to type instead of silently ignoring them.
"""

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
from app.services.media import save_inbound_audio
from app.services.users import get_or_create_telegram_user
from app.voice import elevenlabs as voice

router = Router()
logger = logging.getLogger(__name__)

_VOICE_DISABLED = "Голосовые пока не подключены — напишите, пожалуйста, текстом 🙏"
_VOICE_UNCLEAR = "Не разобрал аудио 🤔 Повторите или напишите текстом."


@router.message(F.chat.type == "private", F.voice | F.audio)
async def voice_in(message: Message) -> None:
    if not message.from_user:
        return
    media = message.voice or message.audio
    if media is None:
        return

    async with AsyncSessionLocal() as session:
        user = await get_or_create_telegram_user(session, message.from_user)
        messenger = await get_messenger(session, Channel.TELEGRAM)

        # Download ONCE — the bytes are reused for the playable copy AND speech-to-text
        # below, so «Диалоги» gets the voice note regardless of what happens next.
        audio = b""
        try:
            file = await message.bot.get_file(media.file_id)
            buf = await message.bot.download_file(file.file_path)
            if buf is not None:
                audio = buf.read()
        except Exception:  # noqa: BLE001
            logger.exception("Telegram voice download failed")
        audio_media = save_inbound_audio(audio) if audio else None

        async def _save_voice_only() -> None:
            """Record the voice note itself (playable) even when we can't turn it into
            text — or the bot is paused — so it's still visible in «Диалоги»."""
            vmsg = await save_message(
                session, user.id, "user", "🎤 Голосовое", channel_mid=str(message.message_id)
            )
            if audio_media:
                vmsg.media_url = audio_media["url"]
                vmsg.media_kind = "voice"
            await inbox_svc.mirror_incoming(
                session,
                user,
                "🎤 Голосовое",
                media_url=audio_media["url"] if audio_media else None,
                media_kind="voice" if audio_media else None,
            )

        if user.bot_paused:  # an operator took this chat over — record only, no AI reply
            await _save_voice_only()
            await session.commit()
            return

        try:
            await message.bot.send_chat_action(message.chat.id, "record_voice")
        except Exception:  # noqa: BLE001
            pass

        vcfg = await voice.voice_config(session)
        if not voice.is_enabled(vcfg):
            await _save_voice_only()
            await messenger.send_text(user.external_id, _VOICE_DISABLED)
            await session.commit()
            return

        text = await voice.transcribe(audio, filename="voice.ogg", cfg=vcfg) if audio else None
        if not text:
            await _save_voice_only()
            await messenger.send_text(user.external_id, _VOICE_UNCLEAR)
            await session.commit()
            return

        # Зеркалим расшифровку вместе с самой записью — как в канальном диспетчере.
        # Без этого голосовые из Telegram не попадали в тему оператора вообще.
        await inbox_svc.mirror_incoming(
            session,
            user,
            text,
            media_url=audio_media["url"] if audio_media else None,
            media_kind="voice" if audio_media else None,
        )
        reply_voice = voice.should_speak(vcfg, was_voice=True, user_text=text)
        await ai_manager.handle_text(
            session,
            user=user,
            text=text,
            messenger=messenger,
            tg_bot=message.bot,
            reply_voice=reply_voice,
            incoming_message_id=str(message.message_id),
            media_url=audio_media["url"] if audio_media else None,
            media_kind="voice" if audio_media else None,
        )
        await session.commit()

"""Telegram operator inbox handlers.

`/setinbox` connects a forum supergroup as the inbox. `topic_reply` relays whatever
an operator posts inside a client's topic (text OR media) back to the client's real
channel, pausing the AI — the mirror image of ``services/inbox.mirror_*``.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select

from app.channels.registry import get_messenger
from app.db.base import AsyncSessionLocal
from app.db.models import User
from app.db.models.enums import Channel
from app.sales.conversation import save_message
from app.services import inbox as inbox_svc
from app.services import media as media_svc
from app.services import receipts as receipts_svc

router = Router()
logger = logging.getLogger(__name__)

_LABEL = {"photo": "📷 Фото", "video": "🎥 Видео", "voice": "🎤 Голосовое", "document": "📎 Файл"}


def _relay_warning(user: User) -> str:
    """Posted back into the operator's topic when a relay did NOT reach the client."""
    if user.channel == Channel.WHATSAPP:
        return (
            "⚠️ Не доставлено клиенту. Скорее всего прошло больше 24 часов с его последнего "
            "сообщения — тогда WhatsApp разрешает писать только шаблонами. Попросите клиента "
            "написать первым (например, «отправьте “+”»)."
        )
    return "⚠️ Не доставлено клиенту — проверьте подключение канала."


@router.message(Command("setinbox"))
async def setinbox(message: Message) -> None:
    chat = message.chat
    if chat.type != "supergroup" or not getattr(chat, "is_forum", False):
        await message.answer(
            "Отправьте /setinbox в супергруппе с включёнными «Темами» (Topics), "
            "где бот добавлен админом с правом управления темами."
        )
        return
    async with AsyncSessionLocal() as s:
        await inbox_svc.set_inbox(s, chat.id)
        await s.commit()
    await message.answer(
        "✅ Готово — эта группа теперь инбокс операторов. У каждого клиента появится "
        "своя тема; отвечайте прямо в теме — уйдёт клиенту.\n\n"
        "Совет: чтобы отвечать простым текстом, отключите Privacy Mode у бота "
        "(@BotFather → /setprivacy → Disable). Иначе отвечайте через «Ответить» на "
        "сообщение клиента."
    )


def _media_ref(message: Message):
    """Return (kind, mime, filename, file_id) for a media message, else all-None (text)."""
    if message.photo:
        return "photo", "image/jpeg", "photo.jpg", message.photo[-1].file_id
    if message.voice:
        return "voice", "audio/ogg", "voice.ogg", message.voice.file_id
    if message.audio:
        a = message.audio
        return "voice", (a.mime_type or "audio/mpeg"), (a.file_name or "audio.mp3"), a.file_id
    if message.video:
        return "video", "video/mp4", "video.mp4", message.video.file_id
    if message.document:
        d = message.document
        return ("document", d.mime_type or "application/octet-stream",
                d.file_name or "file", d.file_id)
    return None, None, None, None


@router.message(F.chat.type == "supergroup", F.message_thread_id)
async def topic_reply(message: Message) -> None:
    """Operator posted inside a client's topic → relay to the client, pause the AI."""
    if (message.text or "").startswith("/"):
        return  # commands are handled by their own routers
    async with AsyncSessionLocal() as s:
        chat_id = await inbox_svc.inbox_chat_id(s)
        if not chat_id or message.chat.id != chat_id:
            return  # message in some other forum group — not our inbox
        user = (
            await s.execute(select(User).where(User.inbox_topic_id == message.message_thread_id))
        ).scalars().first()
        if user is None:
            return  # unmapped topic (e.g. the group's General)

        messenger = await get_messenger(s, user.channel)
        caption = message.caption or ""
        kind, mime, fname, file_id = _media_ref(message)

        if file_id:
            saved = None
            try:
                tg_file = await message.bot.get_file(file_id)
                buf = await message.bot.download_file(tg_file.file_path)
                data = buf.read() if hasattr(buf, "read") else bytes(buf)
                saved = media_svc.save_operator_file(data, mime, fname)
            except Exception:  # noqa: BLE001
                logger.warning("inbox: media download failed")
            if saved is None:
                await message.reply("⚠️ Не удалось переслать вложение клиенту.")
                return
            mid = None
            try:
                mid = await messenger.send_media(
                    user.external_id, url=saved["url"], mime=saved["mime"],
                    kind=saved["kind"], filename=fname, caption=caption,
                )
            except Exception:  # noqa: BLE001
                logger.warning("inbox: media relay failed")
            op = await save_message(
                s, user.id, "operator", caption or _LABEL.get(saved["kind"], "📎 Файл")
            )
            op.media_url, op.media_kind = saved["url"], saved["kind"]
            op.status = "sent" if mid else "failed"
            if mid:
                op.channel_mid = str(mid)
        else:
            text = (message.text or "").strip()
            if not text:
                return
            mid = None
            try:
                mid = await messenger.send_text(user.external_id, text)
            except Exception:  # noqa: BLE001
                logger.warning("inbox: text relay failed")
            op = await save_message(s, user.id, "operator", text)
            op.status = "sent" if mid else "failed"
            if mid:
                op.channel_mid = str(mid)

        user.bot_paused = True  # operator took over → AI stays silent for this chat
        await receipts_svc.mark_read_for_user(s, user.id)
        if op.status == "failed":
            await message.reply(_relay_warning(user))
        await s.commit()

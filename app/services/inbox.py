"""Telegram operator inbox — mirror every client conversation into a forum topic.

Each client gets one topic in the configured supergroup (created lazily on their
first message). Inbound client messages are mirrored in; the bot's and operator's
replies are mirrored too, so the topic is a full transcript. Operators reply IN the
topic and a bot handler relays it to the client's real channel (see handlers/inbox).
All sends are best-effort — a mirror failure must never break the actual conversation.
"""

from __future__ import annotations

import html
import logging
import os
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import User
from app.db.models.enums import Channel
from app.services.settings_service import get_setting, set_setting

logger = logging.getLogger(__name__)

INBOX_KEY = "operator_inbox_chat_id"

_CHANNEL_LABEL = {
    Channel.TELEGRAM: "💬 Telegram",
    Channel.WHATSAPP: "🟢 WhatsApp",
    Channel.INSTAGRAM: "📸 Instagram",
}


async def inbox_chat_id(session: AsyncSession) -> int | None:
    raw = await get_setting(session, INBOX_KEY)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


async def set_inbox(session: AsyncSession, chat_id: int) -> None:
    await set_setting(session, INBOX_KEY, int(chat_id))


async def clear_inbox(session: AsyncSession) -> None:
    await set_setting(session, INBOX_KEY, None)


def _client_card(user: User) -> str:
    base = get_settings().base_url.rstrip("/")
    lines = [
        _CHANNEL_LABEL.get(user.channel, user.channel.value),
        f"👤 <b>{html.escape(user.full_name)}</b>",
    ]
    if user.display_phone:
        lines.append(f"📞 {html.escape(user.display_phone)}")
    if user.username:
        lines.append(f"🔗 @{html.escape(user.username)}")
    lines.append("")
    lines.append("Ответьте прямо в этой теме — сообщение уйдёт клиенту, а бот встанет на паузу.")
    lines.append(f"Открыть в панели: {base}/admin/dialogs?u={user.id}")
    return "\n".join(lines)


async def _ensure_topic(session: AsyncSession, user: User, bot, chat_id: int) -> int | None:
    """Return the client's forum topic id, creating it (+ a client card) on first use."""
    if user.inbox_topic_id:
        return user.inbox_topic_id
    name = (user.full_name or "Клиент")[:120]
    try:
        topic = await bot.create_forum_topic(chat_id, name=name)
    except Exception as e:  # noqa: BLE001 — bot not admin / topics off / rate limit
        logger.warning("inbox: create_forum_topic failed: %s", e)
        return None
    user.inbox_topic_id = topic.message_thread_id
    await session.flush()
    try:
        await bot.send_message(chat_id, _client_card(user), message_thread_id=user.inbox_topic_id)
    except Exception:  # noqa: BLE001
        logger.debug("inbox: client card post failed")
    return user.inbox_topic_id


async def _send_media(bot, chat_id: int, tid: int, src, kind: str, caption: str | None) -> None:
    """Post one media file into the topic, picking the method by kind."""
    if kind == "voice":
        await bot.send_voice(chat_id, src, caption=caption, message_thread_id=tid)
    elif kind == "photo":
        await bot.send_photo(chat_id, src, caption=caption, message_thread_id=tid)
    elif kind == "video":
        await bot.send_video(chat_id, src, caption=caption, message_thread_id=tid)
    else:
        await bot.send_document(chat_id, src, caption=caption, message_thread_id=tid)


async def mirror_incoming(
    session: AsyncSession,
    user: User,
    text: str,
    *,
    photo=None,
    media_url: str | None = None,
    media_kind: str | None = None,
) -> None:
    """Post a client's inbound message into their topic.

    ``media_url``/``media_kind`` forward the real file — a client's voice note arrives
    playable, with the transcript (or the «🎤 Голосовое» label) as its caption. Without
    it the operator saw only the bare text and had no way to hear what was said.
    """
    from app.bot.runner import get_bot  # lazy: avoids an import cycle via bot handlers

    bot = get_bot()
    if bot is None:
        return
    chat_id = await inbox_chat_id(session)
    if not chat_id:
        return
    tid = await _ensure_topic(session, user, bot, chat_id)
    if not tid:
        return
    # Client messages render as a quote (accent bar) so they stand out from
    # the bot's/operator's replies — the closest Telegram allows to a colour.
    quoted = f"<blockquote>{html.escape(text)}</blockquote>" if text else ""
    try:
        if photo is not None:
            await bot.send_photo(
                chat_id, photo, caption=html.escape(text) or None, message_thread_id=tid
            )
        elif media_url and media_kind:
            # Telegram caps captions at ~1024 chars; a long transcript follows as its own
            # message so nothing is truncated away.
            cap = quoted if 0 < len(text) <= 900 else None
            await _send_media(bot, chat_id, tid, _tg_input(media_url), media_kind, cap)
            if quoted and cap is None:
                await bot.send_message(chat_id, quoted, message_thread_id=tid)
        elif text:
            await bot.send_message(chat_id, quoted, message_thread_id=tid)
    except Exception as e:  # noqa: BLE001
        logger.warning("inbox: mirror_incoming failed: %s", e)


def _tg_input(media_url: str):
    """Local media file as an FSInputFile (so Telegram gets a real playable voice/photo,
    not a text label), falling back to the URL string if the file isn't on disk."""
    from aiogram.types import FSInputFile

    from app.config import MEDIA_DIR

    name = os.path.basename(urlparse(media_url or "").path)
    path = MEDIA_DIR / name
    if name and path.is_file():
        return FSInputFile(str(path))
    return media_url


async def mirror_outgoing(
    session: AsyncSession,
    user: User,
    text: str,
    *,
    who: str = "bot",
    media_url: str | None = None,
    media_kind: str | None = None,
) -> None:
    """Post the bot's or operator's reply into the client's topic (only if it exists —
    we never create a topic just to echo an outbound message). When ``media_url``/``media_kind``
    are given, the actual media is forwarded (a playable voice note / photo / file), not just a
    text label — otherwise operator voices showed up in Telegram as the bare text "🎤 Голосовое"."""
    if not user.inbox_topic_id or not (media_url or (text or "").strip()):
        return
    from app.bot.runner import get_bot  # lazy: avoids an import cycle via bot handlers

    bot = get_bot()
    if bot is None:
        return
    chat_id = await inbox_chat_id(session)
    if not chat_id:
        return
    prefix = "🤖 " if who == "bot" else "👤 "
    tid = user.inbox_topic_id
    try:
        if media_url and media_kind:
            caption_text = (text or "").strip()
            # Caption only when the text is short enough for Telegram's ~1024-char limit;
            # a longer reply (e.g. the bot's spoken answer) is sent as a follow-up message.
            cap = prefix + html.escape(caption_text) if 0 < len(caption_text) <= 900 else None
            await _send_media(bot, chat_id, tid, _tg_input(media_url), media_kind, cap)
            if caption_text and cap is None:
                await bot.send_message(
                    chat_id, prefix + html.escape(caption_text), message_thread_id=tid
                )
        else:
            body = prefix + html.escape(text)
            await bot.send_message(chat_id, body, message_thread_id=tid)
    except Exception as e:  # noqa: BLE001
        logger.warning("inbox: mirror_outgoing failed: %s", e)

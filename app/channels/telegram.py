"""Telegram outbound messenger (wraps the aiogram Bot)."""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

from aiogram import Bot
from aiogram.types import BufferedInputFile, FSInputFile

from app.channels.base import Messenger
from app.config import MEDIA_DIR
from app.db.models.enums import Channel

logger = logging.getLogger(__name__)


class TelegramMessenger(Messenger):
    channel = Channel.TELEGRAM

    def __init__(self, bot: Bot | None) -> None:
        self.bot = bot

    async def send_text(self, external_id: str, text: str) -> str | None:
        if self.bot is None:
            logger.warning("Telegram bot not configured — message dropped")
            return None
        msg = await self.bot.send_message(int(external_id), text)
        # Telegram bots can't observe delivery/read, so this id only ever means "sent".
        return str(msg.message_id) if msg else None

    async def send_typing(self, external_id: str, *, message_id: str | None = None) -> None:
        """Show the '…печатает' status. Auto-clears when the next message is sent
        (or after ~5s), so we refresh it right before replying."""
        if self.bot is None:
            return
        try:
            await self.bot.send_chat_action(int(external_id), "typing")
        except Exception:  # noqa: BLE001 — a cosmetic indicator must never break the reply
            logger.debug("Telegram typing action failed")

    async def send_image(self, external_id: str, url: str, caption: str = "") -> None:
        if self.bot is None:
            logger.warning("Telegram bot not configured — image dropped")
            return
        # Upload our own /media file directly (multipart) instead of handing Telegram the
        # URL to fetch. send_photo-by-URL depends on Telegram's servers reaching our host
        # and fails intermittently even for small, valid, reachable images (and is capped
        # at 5 MB). Uploading the local file is reliable.
        media = self._local_or_url(url)
        try:
            await self.bot.send_photo(int(external_id), media, caption=caption or None)
        except Exception as e:  # noqa: BLE001 — fall back to a plain link on any failure
            logger.warning("Telegram photo send failed (%s); sending link instead", e)
            await self.send_text(external_id, f"{caption}\n{url}".strip() if caption else url)

    async def send_video_note(self, external_id: str, url: str, caption: str = "") -> None:
        """Send a round video note (circle) via Telegram's send_video_note.
        Video notes do not support captions — send caption as a separate text message if given."""
        if self.bot is None:
            logger.warning("Telegram bot not configured — video note dropped")
            return
        try:
            media = self._local_or_url(url)
            await self.bot.send_video_note(int(external_id), media)
            if caption:
                await self.send_text(external_id, caption)
        except Exception:  # noqa: BLE001 — fall back to a plain link on any failure
            logger.warning("Telegram video_note send failed; sending link instead")
            text = f"{caption}\n{url}".strip() if caption else url
            await self.send_text(external_id, text)

    @staticmethod
    def _local_or_url(url: str, filename: str = ""):
        """An FSInputFile when the media is one of our own /media files on disk (lets it go
        inline via multipart, avoiding Telegram's flaky URL-fetch); otherwise the URL."""
        try:
            name = os.path.basename(urlparse(url).path)
            path = MEDIA_DIR / name
            if name and path.is_file():
                return FSInputFile(str(path), filename=filename or name)
        except Exception:  # noqa: BLE001 — any resolution issue → just use the URL
            pass
        return url

    async def send_media(
        self,
        external_id: str,
        *,
        url: str,
        mime: str,
        kind: str,
        filename: str = "",
        caption: str = "",
    ) -> str | None:
        """Send a photo/video/voice/document by URL (Telegram fetches it)."""
        if self.bot is None:
            return None
        cid, cap = int(external_id), (caption or None)
        try:
            if kind == "photo":
                m = await self.bot.send_photo(cid, url, caption=cap)
            elif kind == "video":
                m = await self.bot.send_video(cid, url, caption=cap)
            elif kind == "voice":
                # A native voice bubble needs ogg/opus; anything else goes as audio.
                if (mime or "").lower() in ("audio/ogg", "audio/opus"):
                    m = await self.bot.send_voice(cid, url, caption=cap)
                else:
                    m = await self.bot.send_audio(cid, url, caption=cap)
            else:
                m = await self.bot.send_document(cid, url, caption=cap)
            return str(m.message_id) if m else None
        except Exception:  # noqa: BLE001 — fall back to a plain link on any failure
            logger.warning("Telegram media send failed; sending link instead")
            return await self.send_text(
                external_id, f"{caption}\n{url}".strip() if caption else url
            )

    async def send_voice(
        self, external_id: str, audio: bytes, *, mime: str, filename: str
    ) -> bool:
        if self.bot is None:
            return False
        file = BufferedInputFile(audio, filename)
        try:
            if mime == "audio/ogg":
                # Native voice note (waveform bubble) — requires ogg/opus.
                await self.bot.send_voice(int(external_id), file)
            else:
                # mp3 etc. — plays inline as an audio message.
                await self.bot.send_audio(int(external_id), file)
            return True
        except Exception:  # noqa: BLE001 — a failed voice send must not break the reply
            logger.exception("Telegram voice send failed")
            return False

    async def send_reaction(self, external_id: str, message_mid: str, emoji: str) -> bool:
        """React to a Telegram message. Telegram accepts only its standard reaction
        emoji set; an unsupported emoji (or empty = clear) may be rejected — best-effort."""
        if self.bot is None or not message_mid:
            return False
        from aiogram.types import ReactionTypeEmoji

        try:
            react = [ReactionTypeEmoji(emoji=emoji)] if emoji else []
            await self.bot.set_message_reaction(int(external_id), int(message_mid), reaction=react)
            return True
        except Exception:  # noqa: BLE001 — unsupported emoji / too old / no rights
            logger.debug("Telegram reaction failed for %s/%s", external_id, message_mid)
            return False

    async def delete_message(self, external_id: str, mid: str) -> bool:
        """Remove a message from the client's Telegram chat. A bot may delete its own
        messages (and, in groups, others') within Telegram's ~48h window. Best-effort:
        returns False if the message is too old, already gone, or the bot lacks rights."""
        if self.bot is None:
            return False
        try:
            return bool(await self.bot.delete_message(int(external_id), int(mid)))
        except Exception:  # noqa: BLE001 — too old / already deleted / no permission
            logger.debug("Telegram delete_message failed for %s/%s", external_id, mid)
            return False

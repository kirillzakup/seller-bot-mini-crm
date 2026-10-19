"""Channel-agnostic messaging contracts.

``IncomingMessage`` normalizes an inbound message from any platform; ``Messenger``
is the outbound interface each channel implements. The sales engine speaks only
these — it never imports aiogram or the Graph API directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.db.models.enums import Channel


@dataclass
class IncomingMessage:
    channel: Channel
    external_id: str  # platform user id (telegram id, WA phone, IG-scoped id)
    text: str | None = None
    name: str | None = None
    username: str | None = None
    # Platform id of THIS inbound message (WA wamid / IG mid / Telegram message_id),
    # stored on the saved turn so an operator can react to it via the channel API.
    external_message_id: str | None = None
    # Normalized attachments, e.g. {"kind": "image", "file_id"/"url": ...}
    attachments: list[dict] = field(default_factory=list)
    # A message SENT BY the business (owner replying in the IG app, or the bot itself).
    # external_id is the CLIENT (webhook recipient) so it lands in the client's history.
    is_echo: bool = False
    # Emoji when the event is a reaction (e.g. an emoji reaction to a story/message).
    reaction: str | None = None
    # True when the client is replying to / mentioning one of our stories.
    story_reply: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def has_image(self) -> bool:
        return any(a.get("kind") == "image" for a in self.attachments)

    @property
    def has_audio(self) -> bool:
        return any(a.get("kind") == "audio" for a in self.attachments)

    @property
    def has_document(self) -> bool:
        # A file attachment (PDF/doc) — Kaspi receipts are often shared this way.
        return any(a.get("kind") == "document" for a in self.attachments)

    @property
    def audio_ref(self) -> dict | None:
        """The first audio attachment ({"kind": "audio", "file_id"/"url": ...})."""
        return next((a for a in self.attachments if a.get("kind") == "audio"), None)


@dataclass
class IncomingComment:
    """A comment left on one of the business's Instagram posts/reels."""

    channel: Channel
    comment_id: str
    text: str
    from_id: str  # commenter's Instagram-scoped id
    from_username: str | None = None
    media_id: str | None = None
    parent_id: str | None = None  # set when this comment is itself a reply to another
    raw: dict = field(default_factory=dict)

    @property
    def is_reply(self) -> bool:
        return bool(self.parent_id)


class Messenger(ABC):
    """Outbound messaging for one channel."""

    channel: Channel

    @abstractmethod
    async def send_text(self, external_id: str, text: str) -> str | None:
        """Send a plain text message to a contact. Returns the platform message id
        (for delivery/read tracking) when the channel provides one, else None."""

    async def send_template(
        self,
        external_id: str,
        *,
        name: str,
        language: str = "ru",
        body_params: list[str] | None = None,
    ) -> str | None:
        """Send a pre-approved message template to re-open a conversation outside the
        platform's messaging window (WhatsApp's 24-hour rule). Returns the platform
        message id on success. Default: unsupported (only WhatsApp needs this)."""
        return None

    async def send_voice(
        self, external_id: str, audio: bytes, *, mime: str, filename: str
    ) -> bool:
        """Send a spoken reply. Returns True if audio was actually delivered.

        Default: not supported on this channel (caller falls back to text).
        """
        return False

    async def send_audio_by_url(self, external_id: str, url: str, *, mime: str) -> bool:
        """Send a voice/audio clip that is ALREADY hosted at a public URL as a playable
        audio message. Used for channels that can't upload voice-note bytes but accept an
        audio attachment by URL (Instagram). Returns True only if actually delivered.
        Default: unsupported (caller falls back to text)."""
        return False

    async def send_image(self, external_id: str, url: str, caption: str = "") -> None:
        """Send an image by public URL. Default: send the link as text so the
        content still reaches the client on channels without native image send."""
        await self.send_text(external_id, f"{caption}\n{url}".strip() if caption else url)

    async def send_video_note(self, external_id: str, url: str, caption: str = "") -> None:
        """Send a round video note (circle) by public URL. Default: send the link
        as text (like send_media) on channels without native video note support."""
        await self.send_text(external_id, f"{caption}\n{url}".strip() if caption else url)

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
        """Send a media file (kind = photo|video|voice|document) by public URL. Returns
        the platform message id when available. Default: send the link as text."""
        return await self.send_text(external_id, f"{caption}\n{url}".strip() if caption else url)

    async def delete_message(self, external_id: str, mid: str) -> bool:
        """Unsend a previously delivered message from the contact's chat, when the
        platform allows it. ``mid`` is the stored ``channel_mid``. Returns True if the
        message was actually removed on the platform. Default: unsupported — WhatsApp
        and Instagram have no delete API, so the message only leaves our own records."""
        return False

    async def send_reaction(self, external_id: str, message_mid: str, emoji: str) -> bool:
        """React to a message with an emoji (WhatsApp/Telegram support this; the reaction
        reaches the client). ``message_mid`` is the target's stored ``channel_mid``.
        Returns True if the platform accepted it. Default: unsupported (panel-only)."""
        return False

    async def send_typing(self, external_id: str, *, message_id: str | None = None) -> None:
        """Show a 'typing…' indicator to the contact (best-effort, must not raise).

        ``message_id`` is the inbound message being answered — required by
        WhatsApp's read-receipt-based indicator, ignored by other channels.
        Default: no-op (channel has no typing indicator).
        """
        return None

    async def fetch_profile(self, external_id: str) -> dict | None:
        """Resolve a contact's public display name + @username from the platform.

        Instagram IGSIDs aren't reachable on their own — this turns one into a real
        handle so a human can find the person. Returns ``{"name", "username"}`` or
        None. Default: unsupported."""
        return None

    async def fetch_media_caption(self, media_id: str) -> str | None:
        """Fetch the caption/description of the post a comment was left under, so the
        bot answers in context. Returns the caption text or None. Default: unsupported."""
        return None

    async def fetch_comments(self, media_id: str, *, limit: int = 40) -> list[dict]:
        """Fetch recent comments on a post so the bot reads the whole thread before
        replying to one comment (tone, what others asked, what was already answered).
        Returns ``[{"from": username, "text": ...}]`` oldest-first. Default: unsupported."""
        return []

    async def fetch_conversations(
        self, *, max_threads: int = 30, per_thread: int = 40
    ) -> list[dict]:
        """Pull recent DM threads with their messages for style learning. Returns
        ``[{messages: [{from, text, ts}]}]``. Default: unsupported (empty)."""
        return []

    async def download_audio(self, ref: dict) -> bytes | None:
        """Download an inbound audio attachment's bytes. Default: unsupported."""
        return None

    async def download_media(self, ref: dict) -> bytes | None:
        """Download inbound media bytes (audio, image, …) from a ref dict with a
        ``url`` (pre-signed) or ``file_id`` (token-gated media id). Default: none."""
        return None

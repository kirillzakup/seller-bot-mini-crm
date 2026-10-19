"""Runtime settings, analytics events, and the knowledge base."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.models.enums import ConversationRole, KbKind, enum_column


class Setting(Base):
    """Key/value runtime configuration editable from the admin panel.

    Values are JSON, so a key can hold a string, number, or object — e.g.
    ``persona_prompt`` (str), ``llm`` ({base_url, api_key, model, temperature}),
    ``operator_chat_id`` (int), ``welcome_text`` (str).
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Event(Base):
    """Funnel / analytics event."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class Message(Base):
    """One persisted dialog turn (user or assistant), keyed by user id.

    Gives the bot memory across restarts/days: the AI manager reloads the last
    N turns before replying. Tool-call plumbing is rebuilt fresh and NOT stored.
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[ConversationRole] = mapped_column(enum_column(ConversationRole))
    content: Mapped[str] = mapped_column(Text)
    # Outbound only: the platform message id (WhatsApp/Instagram) so status webhooks
    # can map back to this row, plus its delivery status for read-receipt ticks.
    channel_mid: Mapped[str | None] = mapped_column(String(255), index=True)
    status: Mapped[str | None] = mapped_column(String(16))  # sent | delivered | read
    # Attachment sent with this turn: public URL + kind (photo|video|voice|document).
    media_url: Mapped[str | None] = mapped_column(String(512))
    media_kind: Mapped[str | None] = mapped_column(String(16))
    # An emoji reaction placed on THIS message by the operator (mirrors WhatsApp/IG/
    # Telegram reactions, which are also sent to the client). Empty/None = no reaction.
    reaction: Mapped[str | None] = mapped_column(String(16))
    # Operator pinned this message to the top of the «Диалоги» thread (panel-only).
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class KbItem(Base, TimestampMixin):
    """A knowledge-base entry: FAQ answer, objection rebuttal, or product info.
    Used to ground the AI manager and answer support questions."""

    __tablename__ = "kb_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[KbKind] = mapped_column(enum_column(KbKind), default=KbKind.FAQ, index=True)
    title: Mapped[str] = mapped_column(String(255))  # question or objection trigger
    body: Mapped[str] = mapped_column(Text)  # answer / rebuttal
    tags: Mapped[str] = mapped_column(String(255), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Followup(Base):
    """A scheduled re-engagement message for a lead.

    Created either by the AI (``schedule_follow_up`` tool, kind=``think_over`` when
    the client says "I'll think about it") or by the silence job (kind=``silence``).
    ``send_due_followups`` delivers rows whose ``due_at`` has passed and marks them
    ``sent`` so each fires once.
    """

    __tablename__ = "followups"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="think_over")  # think_over | silence
    message: Mapped[str] = mapped_column(Text)  # text to send when due
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    sent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class KbSuggestion(Base):
    """A candidate FAQ entry mined from a human operator's reply in «Диалоги».

    When the operator answers a client, we pair the client's preceding question
    with the operator's answer and stash it here as ``pending``. The owner reviews
    it on the «Обучение» page; on approval it becomes a real ``KbItem`` that the
    bot reuses via ``search_knowledge`` — so the bot learns from human answers
    without any model retraining.
    """

    __tablename__ = "kb_suggestions"

    id: Mapped[int] = mapped_column(primary_key=True)
    question: Mapped[str] = mapped_column(Text)  # client's preceding message
    answer: Mapped[str] = mapped_column(Text)  # operator's reply
    source: Mapped[str] = mapped_column(String(32), default="operator")
    # pending | approved | rejected
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ScriptProposal(Base):
    """An LLM-generated suggestion to improve the sales script, mined from
    successful (paid) conversations. Never applied automatically — the owner
    reviews the proposed text + rationale on «Обучение» and applies with one
    click (which overwrites the ``sales_script`` setting)."""

    __tablename__ = "script_proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposed_text: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text, default="")
    based_on: Mapped[int] = mapped_column(Integer, default=0)  # dialogs analyzed
    # pending | applied | rejected
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MediaAsset(Base):
    """An image/meme the owner uploads for the bot to send in chats.

    The file lives under the data volume; ``title`` + ``tags`` are what the AI
    matches against when it calls the ``send_image`` tool ("мем про дедлайн").
    Delivered to clients as a public link ``{base_url}/media/{filename}``.
    """

    __tablename__ = "media_assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))  # label + AI match key
    filename: Mapped[str] = mapped_column(String(255))  # stored file (uuid.ext), empty for links
    mime: Mapped[str] = mapped_column(String(64), default="")
    tags: Mapped[str] = mapped_column(String(255), default="")  # extra match keywords
    kind: Mapped[str] = mapped_column(String(16), default="image")  # kind of asset
    url: Mapped[str] = mapped_column(String(1024), default="")  # URL for kind=link
    trigger: Mapped[str] = mapped_column(Text, default="")  # when-to-send hint for AI match
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CommentFunnel(Base, TimestampMixin):
    """A keyword lead-magnet: when a client writes ``keyword`` under a post, the bot
    DMs them ``reply_value`` (a text or a link) and optionally replies publicly. A
    deterministic funnel — no LLM — so «напиши СЛОВО в комментах» campaigns just work."""

    __tablename__ = "comment_funnels"

    id: Mapped[int] = mapped_column(primary_key=True)
    keyword: Mapped[str] = mapped_column(String(80), index=True)  # trigger word
    reply_type: Mapped[str] = mapped_column(String(8), default="text")  # text | link
    reply_value: Mapped[str] = mapped_column(Text, default="")  # the DM text or URL
    public_reply: Mapped[str] = mapped_column(String(300), default="")  # optional public reply
    # Optional picture DM'd along with the reply (id of a MediaAsset from 🖼 Картинки).
    image_id: Mapped[int | None] = mapped_column(ForeignKey("media_assets.id"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    triggered_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Creative(Base, TimestampMixin):
    """An ad creative + its landing page. ``code`` is the tag carried into the bot
    via ``t.me/<bot>?start=<code>`` and the public landing URL ``/l/<code>``;
    leads acquired through it are attributed by ``User.source == code``."""

    __tablename__ = "creatives"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # ?start= tag
    title: Mapped[str] = mapped_column(String(255))  # admin-only name
    headline: Mapped[str] = mapped_column(String(255), default="")  # landing H1
    subtext: Mapped[str] = mapped_column(Text, default="")  # landing body
    media_url: Mapped[str | None] = mapped_column(Text)  # image/video of the creative
    cta_text: Mapped[str] = mapped_column(String(80), default="Написать в Telegram")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Document(Base):
    """A file the owner uploads for the bot to send to clients in chat — price list,
    catalog, presentation, certificate, or achievement. The AI picks one via the
    ``send_document`` tool, matched on ``title`` + ``tags``. Stored under the data volume
    and served publicly at ``/media/{filename}`` (same as images), so every channel can
    fetch it by link. ``kind`` is how the channel sends it: ``document`` or ``photo``."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))  # label + AI match key
    filename: Mapped[str] = mapped_column(String(255))  # stored file (uuid.ext)
    mime: Mapped[str] = mapped_column(String(64), default="")
    kind: Mapped[str] = mapped_column(String(16), default="document")  # document | photo
    # прайс | каталог | презентация | сертификат | достижение | другое
    category: Mapped[str] = mapped_column(String(24), default="другое")
    tags: Mapped[str] = mapped_column(String(255), default="")  # extra match keywords
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

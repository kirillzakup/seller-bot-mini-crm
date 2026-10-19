"""User / lead model — a person the bot talks to on some channel.

Identity is the pair ``(channel, external_id)`` so the same engine serves
Telegram, WhatsApp and Instagram. ``telegram_id`` is kept as a convenience for
the Telegram channel (== int(external_id) there).
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.models.enums import Channel, enum_column


class User(Base, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("channel", "external_id", name="uq_user_channel_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[Channel] = mapped_column(
        enum_column(Channel), default=Channel.TELEGRAM, index=True
    )
    external_id: Mapped[str] = mapped_column(String(64), index=True)  # platform user id
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True)  # convenience

    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    # True ONLY once the client told us their name themselves (save_contact / request_callback).
    # Platform profile names — IG/WA @handles and nicknames — do NOT set this, so the bot asks
    # how to address the person instead of using a possibly-offensive handle as their name.
    name_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    # Captured during the sales dialog
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(255))

    # Funnel attributes
    segment: Mapped[str | None] = mapped_column(String(64))
    stage: Mapped[str | None] = mapped_column(String(64))
    # Creative/source attribution (first-touch): the ?start= tag the lead came with
    source: Mapped[str | None] = mapped_column(String(64), index=True)
    # Which Meta ad brought this lead (click-to-WhatsApp/Direct referral, first touch).
    ad_ref: Mapped[str | None] = mapped_column(String(255))

    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Loyalty
    birthday: Mapped[date | None] = mapped_column(Date)
    birthday_greet: Mapped[bool] = mapped_column(Boolean, default=False)
    # Silence-reminder sequence progress; reset to 0 whenever the lead replies.
    reminders_sent: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Per-client opt-out toggles (managed in «Диалоги»): silence reminders (дожим) and
    # promos (birthday greetings + new-course broadcasts). Default ON for everyone.
    reminders_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    promos_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # When we last auto-sent a WhatsApp re-engagement template to re-open a closed 24h
    # window. Set once so the дожим only spends on a template on the FIRST closed step.
    reengaged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Operator takeover: when True the AI stays silent for this chat (human replies).
    bot_paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Telegram operator-inbox: the forum topic id mirroring this client's conversation.
    inbox_topic_id: Mapped[int | None] = mapped_column(Integer, index=True)
    # Operator marked this chat unread in «Диалоги» (panel-only flag, WhatsApp-style).
    unread: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Портрет клиента (JSON, см. app/sales/client_profile.py): кто он, откуда, на каком
    # языке, что строит, чего хочет и что мешает решиться. История переписки в промпте
    # обрезается, а портрет живёт весь диалог — бот не забывает детали и не переспрашивает.
    profile: Mapped[str | None] = mapped_column(Text, default="")
    profile_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def full_name(self) -> str:
        parts = [self.first_name, self.last_name]
        return " ".join(p for p in parts if p) or (self.username or self.external_id)

    @property
    def display_phone(self) -> str | None:
        """Best phone to show an operator: the captured phone, or — for WhatsApp —
        the wa_id itself (which IS the client's number), formatted with a leading +."""
        if self.phone:
            return self.phone
        if self.channel == Channel.WHATSAPP and self.external_id and self.external_id.isdigit():
            return "+" + self.external_id
        return None

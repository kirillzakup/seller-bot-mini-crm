"""Persistent conversation history (DB-backed), keyed by internal user id.

User/assistant text turns are stored in the ``messages`` table so the bot keeps
context across restarts and days — it reloads the last N turns before replying.
Tool-call plumbing (assistant tool_calls + tool results) is rebuilt fresh each
turn and is intentionally NOT persisted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message
from app.db.models.enums import Channel, ConversationRole

WA_WINDOW = timedelta(hours=24)  # WhatsApp's customer-care (free-form) window

_MAX_TURNS = 40  # how many recent text turns to feed back as context (incl. operator turns)

# Map our stored roles to roles the OpenAI chat API accepts. Crucially, an
# ``operator`` turn (a human operator's reply via the «Диалоги» takeover) must
# NOT be sent as role="operator" — OpenAI rejects the whole request (400
# invalid_value). The operator answers on behalf of the business, so it reads as
# an assistant turn to the model. Unknown roles fall back to assistant too.
_OPENAI_ROLE = {
    ConversationRole.USER.value: "user",
    ConversationRole.ASSISTANT.value: "assistant",
    ConversationRole.OPERATOR.value: "assistant",
    ConversationRole.SYSTEM.value: "system",
}


async def wa_window_open(
    session: AsyncSession, user_id: int, now: datetime | None = None
) -> bool:
    """True while WhatsApp's 24-hour customer-care window is open — i.e. the client sent
    us a message within the last 24h. Outside it, free-form WhatsApp sends are rejected
    (error 131047) and only an approved template gets through."""
    last = (
        await session.execute(
            select(Message.created_at)
            .where(Message.user_id == user_id, Message.role == ConversationRole.USER)
            .order_by(Message.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - last) < WA_WINDOW


async def window_open(session: AsyncSession, user, now: datetime | None = None) -> bool:
    """Можно ли написать клиенту свободным текстом прямо сейчас.

    У Instagram такое же 24-часовое окно, как у WhatsApp, только без шаблонов:
    вне окна Meta отвечает 403 (code 10, subcode 2534022). Проверка раньше стояла
    лишь для WhatsApp — дожимы клиентам из Instagram падали десятками.
    Telegram окна не имеет.
    """
    if user.channel not in (Channel.WHATSAPP, Channel.INSTAGRAM):
        return True
    return await wa_window_open(session, user.id, now)


async def load_history(session: AsyncSession, user_id: int, limit: int = _MAX_TURNS) -> list[dict]:
    """Return the last ``limit`` turns as OpenAI-style messages (oldest first)."""
    rows = (
        (
            await session.execute(
                select(Message)
                .where(Message.user_id == user_id)
                .order_by(Message.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {"role": _OPENAI_ROLE.get(m.role.value, "assistant"), "content": m.content}
        for m in reversed(rows)
    ]


async def record_sent_media(
    session: AsyncSession,
    user,
    *,
    title: str,
    word: str,
    url: str,
    kind: str,
    caption: str = "",
) -> Message:
    """Log an attachment the bot just sent to the client.

    The bot's pictures and files used to go straight to the messenger and nowhere else —
    the team only found out about them from client screenshots. This puts every send where
    they already look: a bubble in «Диалоги» (with an inline preview) AND a mirror into the
    client's Telegram topic. It also lands in the history the model reloads, so the bot can
    SEE it already sent that asset instead of sending it again on the next turn.
    """
    from app.services import inbox as inbox_svc  # lazy: avoids an import cycle

    label = f"[{word}: {title}]"
    content = f"{label} {caption.strip()}".strip() if caption.strip() else label
    msg = await save_message(
        session, user.id, "assistant", content, media_url=url, media_kind=kind
    )
    await inbox_svc.mirror_outgoing(
        session, user, content, who="bot", media_url=url, media_kind=kind
    )
    return msg


async def record_sent_text(session: AsyncSession, user, text: str) -> Message:
    """Same idea as :func:`record_sent_media` for a plain-text send made outside the normal
    reply path (e.g. a link from the media library), which otherwise left no trace at all."""
    from app.services import inbox as inbox_svc  # lazy: avoids an import cycle

    msg = await save_message(session, user.id, "assistant", text)
    await inbox_svc.mirror_outgoing(session, user, text, who="bot")
    return msg


async def media_sent_recently(
    session: AsyncSession, user_id: int, title: str, *, within=timedelta(hours=24)
) -> bool:
    """True if the same asset was already sent to this client within ``within``.

    Guards the repeat-send: the model has no reliable memory of its own attachments, so on a
    follow-up turn it happily fires the same picture off again.
    """
    since = datetime.now(UTC) - within
    return bool(
        (
            await session.execute(
                select(Message.id)
                .where(
                    Message.user_id == user_id,
                    Message.media_url.is_not(None),
                    Message.content.contains(f": {title}]"),
                    Message.created_at >= since,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
    )


async def save_message(
    session: AsyncSession,
    user_id: int,
    role: str,
    content: str,
    *,
    media_url: str | None = None,
    media_kind: str | None = None,
    channel_mid: str | None = None,
) -> Message:
    """Persist one dialog turn. ``role`` is 'user' | 'assistant' | 'operator'. Returns
    the saved row so callers can attach a channel message-id / delivery status.

    ``media_url`` + ``media_kind`` (photo|video|voice|document) attach a playable
    file to the turn — e.g. a client's inbound voice note, shown in «Диалоги».
    ``channel_mid`` records the platform id of an inbound message so an operator can
    later react to it via the channel API."""
    msg = Message(
        user_id=user_id,
        role=ConversationRole(role),
        content=content,
        media_url=media_url,
        media_kind=media_kind,
        channel_mid=channel_mid,
    )
    session.add(msg)
    await session.flush()
    return msg

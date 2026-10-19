"""Delivery / read receipts for outbound messages.

WhatsApp sends per-message status webhooks (sent → delivered → read); Instagram
sends a read event. We also treat "the client replied" as a read on EVERY channel
(including Telegram, which has no receipts at all) — if they answered, they saw it.
Statuses only ever move forward (sent < delivered < read), never backward.
"""

from __future__ import annotations

import logging

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message, User
from app.db.models.enums import Channel, ConversationRole

logger = logging.getLogger(__name__)

_RANK = {"sent": 1, "delivered": 2, "read": 3}
_OUTBOUND = (ConversationRole.ASSISTANT, ConversationRole.OPERATOR)


async def apply_status(session: AsyncSession, *, mid: str, status: str) -> None:
    """Bump one outbound message's status by its platform id (never downgrades).

    ``failed`` is a special case: WhatsApp reports it as a delivery-status webhook,
    not a rank — it always overwrites (a message can go from "sent" straight to
    undelivered), and it must never be silently promoted back to "read" afterwards
    (see ``mark_read_for_user``)."""
    if not mid or status not in (*_RANK, "failed"):
        return
    msg = (
        (await session.execute(select(Message).where(Message.channel_mid == str(mid))))
        .scalars()
        .first()
    )
    if msg is None:
        return
    if status == "failed":
        msg.status = "failed"
    elif _RANK[status] > _RANK.get(msg.status or "", 0):
        msg.status = status


async def mark_read_for_user(session: AsyncSession, user_id: int) -> None:
    """Mark all of a contact's outbound messages as read (cumulative read receipt).

    Excludes ``failed`` — an undelivered message must never be repainted as "read"
    just because the client replied afterwards; that would hide the delivery failure."""
    await session.execute(
        update(Message)
        .where(
            Message.user_id == user_id,
            Message.role.in_(_OUTBOUND),
            or_(Message.status.is_(None), Message.status.in_(("sent", "delivered"))),
        )
        .values(status="read")
        .execution_options(synchronize_session=False)
    )


async def mark_read(session: AsyncSession, *, channel: Channel, external_id: str) -> None:
    """Look up a contact by (channel, external_id) and mark their outbound as read."""
    user = (
        (
            await session.execute(
                select(User).where(
                    User.channel == channel, User.external_id == str(external_id)
                )
            )
        )
        .scalars()
        .first()
    )
    if user is not None:
        await mark_read_for_user(session, user.id)

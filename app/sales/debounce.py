"""Debounce bursts of fragmented client messages into a single reply.

Clients often type one word per message ("здравствуйте" / "нейросети" / "цена" /
"расписание"). Instead of firing a separate answer at each, every fragment is saved
as it arrives and a short quiet-timer is (re)started; when the client goes silent for
``seconds`` the bot composes ONE reply that addresses the whole burst.

The timer state is in-process (a dict of asyncio tasks). Fragments themselves live in
the DB, so a restart at worst loses the pending timer, never the messages.
"""

from __future__ import annotations

import asyncio
import logging

from app.channels.registry import get_messenger
from app.db.base import AsyncSessionLocal
from app.db.models import User
from app.db.models.enums import Channel
from app.sales import ai_manager

logger = logging.getLogger(__name__)

# user_id → the pending "reply once quiet" task. A newer fragment cancels the old one.
_timers: dict[int, asyncio.Task] = {}


def schedule(
    user_id: int,
    *,
    delay: float,
    channel: Channel,
    incoming_message_id: str | None = None,
) -> None:
    """(Re)start the quiet-timer for a user; any previously pending reply is cancelled."""
    old = _timers.get(user_id)
    if old is not None and not old.done():
        old.cancel()
    _timers[user_id] = asyncio.create_task(
        _reply_after_quiet(user_id, delay, channel, incoming_message_id)
    )


def cancel(user_id: int) -> None:
    """Drop any pending debounced reply (e.g. the operator took over)."""
    task = _timers.pop(user_id, None)
    if task is not None and not task.done():
        task.cancel()


async def _reply_after_quiet(
    user_id: int, delay: float, channel: Channel, incoming_message_id: str | None
) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return  # a newer fragment arrived (or operator took over) → superseded
    _timers.pop(user_id, None)
    try:
        async with AsyncSessionLocal() as session:
            user = await session.get(User, user_id)
            if user is None or user.bot_paused:
                return
            messenger = await get_messenger(session, channel)
            await ai_manager.respond_from_history(
                session,
                user=user,
                messenger=messenger,
                incoming_message_id=incoming_message_id,
            )
            await session.commit()
    except Exception:  # noqa: BLE001 — a failed debounced reply must not crash the loop
        logger.exception("debounced reply failed for user %s", user_id)

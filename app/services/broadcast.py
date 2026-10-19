"""Send an informational broadcast (e.g. a new course) to opted-in clients.

Recipients are every client with ``promos_enabled`` who isn't blocked — the same
per-client toggle («🎁 Рассылки») shown in «Диалоги». Best-effort per client, so one
failed send never aborts the batch; each delivery is recorded in the client's thread.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.registry import get_messenger
from app.db.models import User
from app.sales.conversation import save_message

logger = logging.getLogger(__name__)


async def count_recipients(session: AsyncSession) -> int:
    """How many clients would receive a broadcast right now."""
    rows = (
        await session.execute(
            select(User.id).where(User.promos_enabled.is_(True), User.is_blocked.is_(False))
        )
    ).scalars().all()
    return len(rows)


async def send_broadcast(session: AsyncSession, *, text: str, image_url: str | None = None) -> dict:
    """Deliver a broadcast to every opted-in client. Returns ``{total, sent, failed}``."""
    text = (text or "").strip()
    if not text and not image_url:
        return {"total": 0, "sent": 0, "failed": 0}
    recipients = (
        await session.execute(
            select(User).where(User.promos_enabled.is_(True), User.is_blocked.is_(False))
        )
    ).scalars().all()
    sent = failed = 0
    for u in recipients:
        try:
            messenger = await get_messenger(session, u.channel)
            if image_url:
                await messenger.send_image(u.external_id, image_url, text)
            else:
                await messenger.send_text(u.external_id, text)
        except Exception:  # noqa: BLE001 — one failure must not abort the batch
            logger.warning("broadcast failed for user %s", u.id)
            failed += 1
            continue
        msg = await save_message(session, u.id, "assistant", text or "[рассылка]")
        if image_url:
            msg.media_url, msg.media_kind = image_url, "photo"
        sent += 1
    await session.commit()
    logger.info("broadcast: sent %s, failed %s (of %s)", sent, failed, len(recipients))
    return {"total": len(recipients), "sent": sent, "failed": failed}

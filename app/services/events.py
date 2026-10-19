"""Funnel/analytics event logging (used for nudge idempotency + future analytics)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event


async def log_event(
    session: AsyncSession,
    type_: str,
    *,
    user_id: int | None = None,
    data: dict | None = None,
) -> None:
    session.add(Event(type=type_, user_id=user_id, data=data or {}))
    await session.flush()

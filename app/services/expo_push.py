"""Expo push notifications for the native «Диалоги» app.

Operators register their device's Expo push token (POST /appapi/push/register);
on a new inbound client message we POST to Expo's push service, which fans out to
APNs (iOS) and FCM (Android). No tokens registered → silent no-op.
"""

from __future__ import annotations

import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.settings_service import get_setting, set_setting

logger = logging.getLogger(__name__)

_ENDPOINT = "https://exp.host/--/api/v2/push/send"
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


async def notify_new_message(
    session: AsyncSession, *, name: str, preview: str, user_id: int
) -> None:
    """Push 'new message from <name>' to every registered operator device."""
    tokens = list(await get_setting(session, "app_push_tokens") or [])
    if not tokens:
        return
    messages = [
        {
            "to": t,
            "title": f"Сообщение от {name}",
            "body": (preview or "")[:150],
            "sound": "default",
            "priority": "high",
            "data": {"user_id": user_id, "screen": "chat"},
        }
        for t in tokens
    ]
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(_ENDPOINT, json=messages)
        # Prune tokens Expo reports as dead (DeviceNotRegistered) so the list stays clean.
        if resp.status_code == 200:
            await _prune_dead(session, tokens, resp.json())
    except Exception:  # noqa: BLE001 — a push hiccup must never block message handling
        logger.warning("Expo push failed")


async def _prune_dead(session: AsyncSession, tokens: list[str], payload: dict) -> None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return
    dead = {
        tokens[i]
        for i, item in enumerate(data)
        if i < len(tokens)
        and isinstance(item, dict)
        and (item.get("details") or {}).get("error") == "DeviceNotRegistered"
    }
    if dead:
        await set_setting(session, "app_push_tokens", [t for t in tokens if t not in dead])
        await session.commit()

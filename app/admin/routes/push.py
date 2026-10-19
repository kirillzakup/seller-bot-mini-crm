"""Admin Web Push endpoints: hand the browser the VAPID public key and store the
operator's push subscription so we can notify their device when the app is closed.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import require_login
from app.db.base import get_session
from app.services.push import add_subscription, get_vapid

router = APIRouter(prefix="/push")


@router.get("/key")
async def public_key(
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    vapid = await get_vapid(session)
    return {"public_key": vapid["public_key"]}


@router.post("/subscribe")
async def subscribe(
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    sub = await request.json()
    if isinstance(sub, dict) and sub.get("endpoint"):
        await add_subscription(session, sub)
        return {"ok": True}
    return {"ok": False}

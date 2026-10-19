"""Admin «Лояльность»: client base + birthdays + check-in outreach.

Every lead the bot has ever talked to is a ``User`` row — this is the client
base. Here the owner sets each client's birthday and a "congratulate" flag, and
can send a check-in message ("how are you / new courses?") on demand. Birthday
greetings are delivered automatically by the ``greet_birthdays`` job.
"""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import require_login, templates
from app.channels.registry import get_messenger
from app.db.base import get_session
from app.db.models import User
from app.services.settings_service import get_setting, set_setting

router = APIRouter(prefix="/clients")
logger = logging.getLogger(__name__)


def _parse_date(value: str) -> date | None:
    value = (value or "").strip()
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


@router.get("")
async def list_clients(
    request: Request,
    saved: int | None = None,
    sent: int | None = None,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    users = (
        (
            await session.execute(
                select(User)
                .order_by(User.last_seen.is_(None), User.last_seen.desc())
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    loyalty = await get_setting(session, "loyalty") or {}
    return templates.TemplateResponse(
        request,
        "clients.html",
        {"active": "clients", "users": users, "loyalty": loyalty, "saved": saved, "sent": sent},
    )


@router.post("/settings")
async def save_loyalty(
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    birthday_enabled: str = Form(None),
    birthday_text: str = Form(""),
    checkin_text: str = Form(""),
):
    await set_setting(
        session,
        "loyalty",
        {
            "birthday_enabled": bool(birthday_enabled),
            "birthday_text": birthday_text.strip(),
            "checkin_text": checkin_text.strip(),
        },
    )
    return RedirectResponse("/admin/clients?saved=1", status_code=303)


@router.post("/{user_id}")
async def save_client(
    user_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    birthday: str = Form(""),
    birthday_greet: str = Form(None),
):
    user = await session.get(User, user_id)
    if user is not None:
        user.birthday = _parse_date(birthday)
        user.birthday_greet = bool(birthday_greet)
    return RedirectResponse("/admin/clients?saved=1", status_code=303)


@router.post("/{user_id}/checkin")
async def checkin_client(
    user_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    """Send the loyalty check-in message to one client right now."""
    user = await session.get(User, user_id)
    ok = 0
    if user is not None:
        cfg = await get_setting(session, "loyalty") or {}
        text = (cfg.get("checkin_text") or "").strip()
        if text:
            try:
                messenger = await get_messenger(session, user.channel)
                await messenger.send_text(user.external_id, text)
                ok = 1
            except Exception:  # noqa: BLE001
                logger.warning("Check-in send failed for user %s", user_id)
    return RedirectResponse(f"/admin/clients?sent={ok}", status_code=303)

"""Admin: connect channels (Telegram status + Meta WhatsApp/Instagram config).

WhatsApp can be connected two ways:
  - Manual: paste phone_number_id + access token.
  - Embedded Signup (Coexistence): the owner links their EXISTING WhatsApp
    Business app number via a Facebook popup; we exchange the returned code for a
    permanent token. The number then works in BOTH the app (owner sees messages)
    and our bot (Cloud API). See /whatsapp/connect + /whatsapp/exchange below.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import require_login, templates
from app.bot.runner import get_bot
from app.config import get_settings
from app.db.base import get_session
from app.services.settings_service import get_setting, set_setting

router = APIRouter(prefix="/channels")
logger = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com"


@router.get("")
async def channels_page(
    request: Request,
    saved: int | None = None,
    connected: int | None = None,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    meta = await get_setting(session, "meta") or {}
    cfg = get_settings()
    return templates.TemplateResponse(
        request,
        "channels.html",
        {
            "active": "channels",
            "meta": meta,
            "wa": meta.get("whatsapp") or {},
            "ig": meta.get("instagram") or {},
            "comments": await get_setting(session, "comments") or {},
            "base_url": cfg.base_url,
            # Real running-bot status (token now lives in the DB, not .env).
            "bot_ready": get_bot() is not None,
            "bot_username": await get_setting(session, "bot_username") or "",
            "saved": saved,
            "connected": connected,
        },
    )


@router.post("")
async def save_channels(
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    app_id: str = Form(""),
    app_secret: str = Form(""),
    verify_token: str = Form(""),
    graph_version: str = Form("v21.0"),
    embedded_config_id: str = Form(""),
    wa_enabled: str = Form(None),
    wa_phone_number_id: str = Form(""),
    wa_access_token: str = Form(""),
    wa_reengage_name: str = Form(""),
    wa_reengage_text: str = Form(""),
    wa_reengage_lang: str = Form("ru"),
    ig_enabled: str = Form(None),
    ig_id: str = Form(""),
    ig_access_token: str = Form(""),
    ig_app_secret: str = Form(""),
    ig_human_agent: str = Form(None),
    c_enabled: str = Form(None),
    c_reply: str = Form(None),
    c_dm: str = Form(None),
    c_hide: str = Form(None),
):
    prev = await get_setting(session, "meta") or {}
    wa_prev = prev.get("whatsapp") or {}
    ig_prev = prev.get("instagram") or {}
    meta = {
        "app_id": app_id.strip(),
        "app_secret": app_secret,
        "verify_token": verify_token,
        "graph_version": graph_version or "v21.0",
        "embedded_config_id": embedded_config_id.strip(),
        "whatsapp": {
            "enabled": bool(wa_enabled),
            "phone_number_id": wa_phone_number_id,
            "access_token": wa_access_token,
            "waba_id": wa_prev.get("waba_id", ""),  # set by Embedded Signup
            # Approved template used to re-open a closed 24h window (дожим + «Разбудить»).
            "reengage_template": {
                "name": wa_reengage_name.strip(),
                "language": (wa_reengage_lang or "ru").strip() or "ru",
                "text": wa_reengage_text.strip(),
            },
        },
        "instagram": {
            "enabled": bool(ig_enabled),
            "ig_id": ig_id.strip(),
            "access_token": ig_access_token,
            "app_secret": ig_app_secret,
            # Tag outbound as human-agent → 7-day window instead of 24h (needs the
            # Human Agent feature approved for the app).
            "human_agent": bool(ig_human_agent),
            # Preserved (not form fields): expiry stamped by the refresh job;
            # username stamped by the comments subscription (used as a loop guard).
            "token_expires_at": ig_prev.get("token_expires_at", ""),
            "username": ig_prev.get("username", ""),
        },
    }
    await set_setting(session, "meta", meta)
    await set_setting(
        session,
        "comments",
        {
            "enabled": bool(c_enabled),
            "reply_enabled": bool(c_reply),
            "dm_enabled": bool(c_dm),
            "hide_spam": bool(c_hide),
        },
    )
    return RedirectResponse("/admin/channels?saved=1", status_code=303)


@router.get("/whatsapp/connect")
async def whatsapp_connect(
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    """Embedded Signup page — launches the Facebook popup to link the number."""
    meta = await get_setting(session, "meta") or {}
    return templates.TemplateResponse(
        request,
        "wa_connect.html",
        {
            "active": "channels",
            "app_id": meta.get("app_id", ""),
            "config_id": meta.get("embedded_config_id", ""),
            "graph_version": meta.get("graph_version", "v21.0"),
        },
    )


@router.post("/whatsapp/exchange")
async def whatsapp_exchange(
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    """Exchange the Embedded Signup ``code`` for a permanent token and store it."""
    body = await request.json()
    code = (body.get("code") or "").strip()
    phone_id = (body.get("phone_number_id") or "").strip()
    waba_id = (body.get("waba_id") or "").strip()

    meta = await get_setting(session, "meta") or {}
    app_id = (meta.get("app_id") or "").strip()
    app_secret = (meta.get("app_secret") or "").strip()
    ver = meta.get("graph_version") or "v21.0"

    if not code or not app_id or not app_secret:
        msg = "Заполните App ID и App Secret в настройках каналов и сохраните."
        return JSONResponse({"ok": False, "error": msg}, status_code=400)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            resp = await client.get(
                f"{GRAPH}/{ver}/oauth/access_token",
                params={"client_id": app_id, "client_secret": app_secret, "code": code},
            )
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.error("WhatsApp Embedded Signup exchange failed: %s", e)
        return JSONResponse(
            {"ok": False, "error": "Не удалось обменять код на токен."}, status_code=502
        )

    token = data.get("access_token")
    if not token:
        logger.error("WhatsApp exchange returned no token: %s", data)
        return JSONResponse({"ok": False, "error": data.get("error", data)}, status_code=400)

    wa = meta.get("whatsapp") or {}
    wa["enabled"] = True
    wa["access_token"] = token
    if phone_id:
        wa["phone_number_id"] = phone_id
    if waba_id:
        wa["waba_id"] = waba_id
    meta["whatsapp"] = wa
    await set_setting(session, "meta", meta)
    await session.commit()
    logger.info("WhatsApp connected via Embedded Signup (phone_number_id=%s)", phone_id)
    return JSONResponse({"ok": True, "phone_number_id": wa.get("phone_number_id", "")})

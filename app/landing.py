"""Public creative landing pages: GET /l/{code}.

Shows the ad creative + a CTA that opens the Telegram sales bot with the creative
tag (t.me/<bot>?start=<code>), so the lead is attributed to this creative.
"""

from __future__ import annotations

import html
import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.db.base import AsyncSessionLocal
from app.db.models import Creative
from app.services.events import log_event
from app.services.settings_service import get_setting

router = APIRouter(tags=["landing"])
logger = logging.getLogger(__name__)


@router.get("/l/{code}")
async def landing(code: str) -> HTMLResponse:
    async with AsyncSessionLocal() as session:
        creative = (
            (await session.execute(select(Creative).where(Creative.code == code))).scalars().first()
        )
        if creative is None or not creative.is_active:
            return HTMLResponse(_page("Страница не найдена", "<p>Проверьте ссылку.</p>"), 404)
        bot_username = await get_setting(session, "bot_username")
        await log_event(session, "creative_view", data={"code": code})
        await session.commit()

        headline = creative.headline or creative.title
        subtext = creative.subtext or ""
        media_url = creative.media_url
        cta_text = creative.cta_text or "Написать в Telegram"

    deep_link = f"https://t.me/{bot_username}?start={code}" if bot_username else None

    media = f"<img src='{html.escape(media_url)}' alt='' class='media'>" if media_url else ""
    if deep_link:
        cta = f"<a class='cta' href='{html.escape(deep_link)}'>{html.escape(cta_text)}</a>"
    else:
        cta = "<p class='warn'>Бот ещё не настроен — задайте BOT_TOKEN.</p>"

    body = (
        f"{media}"
        f"<h1>{html.escape(headline)}</h1>"
        + (f"<p class='sub'>{html.escape(subtext)}</p>" if subtext else "")
        + cta
    )
    return HTMLResponse(_page(headline, body))


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>"
        "*{box-sizing:border-box}body{margin:0;font-family:system-ui,sans-serif;"
        "background:linear-gradient(160deg,#0f172a,#1e293b);color:#e2e8f0;min-height:100vh;"
        "display:flex;align-items:center;justify-content:center;padding:24px}"
        ".card{max-width:520px;width:100%;text-align:center}"
        ".media{width:100%;border-radius:18px;margin-bottom:22px;"
        "box-shadow:0 10px 40px rgba(0,0,0,.5)}"
        "h1{font-size:30px;line-height:1.15;margin:0 0 12px}"
        ".sub{font-size:17px;color:#cbd5e1;margin:0 0 26px}"
        ".cta{display:inline-block;background:#229ED9;color:#fff;text-decoration:none;"
        "font-weight:700;font-size:18px;padding:16px 34px;border-radius:14px;"
        "box-shadow:0 8px 24px rgba(34,158,217,.45)}"
        ".cta:hover{background:#1c87ba}.warn{color:#fbbf24}"
        "</style></head>"
        f"<body><div class=card>{body}</div></body></html>"
    )

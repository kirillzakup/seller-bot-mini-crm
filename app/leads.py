"""Public web-lead intake: POST /leads.

The company website's "Получите прайс" popup posts a name + phone here. We ping
the operators' Telegram chat (the same chat where the bot's leads land) so a human
calls the client back, and log a funnel event for stats. No client account exists
for a web lead — the operator reaches out by phone / WhatsApp using the contact.
"""

from __future__ import annotations

import html
import logging

from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse

from app.db.base import AsyncSessionLocal
from app.services import leads_service
from app.services.events import log_event
from app.services.notify import send_operator_message

router = APIRouter(tags=["leads"])
logger = logging.getLogger(__name__)

_MIN_PHONE_DIGITS = 6  # reject obviously-empty/garbage phone fields
_MAX_NAME = 120
_MAX_PHONE = 40
_MAX_SOURCE = 200


def _clip(value: str | None, limit: int) -> str:
    return (value or "").strip()[:limit]


@router.post("/leads")
async def submit_lead(
    name: str = Form(""),
    phone: str = Form(""),
    source: str = Form(""),
    # Метки рекламы: gclid Google подставляет сам при клике по объявлению,
    # utm приходят из ссылки. По ним потом видно, какая кампания дала продажу.
    gclid: str = Form(""),
    utm: str = Form(""),
    # Что именно человек заказал: состав и сумма. Без этого менеджер видит
    # только телефон и не понимает, с чем звонить.
    note: str = Form(""),
    # Honeypot: a hidden field real users never see. Bots fill every input, so a
    # non-empty value means spam — we accept-and-drop silently (don't tip them off).
    contact_email: str = Form(""),
) -> JSONResponse:
    if contact_email.strip():
        logger.info("web lead dropped by honeypot")
        return JSONResponse({"ok": True})

    name = _clip(name, _MAX_NAME)
    phone = _clip(phone, _MAX_PHONE)
    source = _clip(source, _MAX_SOURCE)

    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) < _MIN_PHONE_DIGITS:
        return JSONResponse({"ok": False, "error": "phone_required"}, status_code=422)

    lines = [
        "🌐 <b>Заявка с сайта</b>",
        f"👤 {html.escape(name)}" if name else "👤 Имя не указано",
        f"📞 {html.escape(phone)}",
    ]
    if source:
        lines.append(f"📍 {html.escape(source)}")
    lines.append(f"💬 WhatsApp: https://wa.me/{digits}")
    text = "\n".join(lines)

    async with AsyncSessionLocal() as session:
        # Заявка с сайта — такая же заявка, как из переписки: карточка с кнопкой
        # «Забрал в работу», а не просто текст, который некому взять и по которому
        # потом не видно, чем дело кончилось.
        delivered = False
        try:
            lead = await leads_service.record_web_lead(
                session,
                name=name,
                phone=phone,
                source=source,
                gclid=_clip(gclid, 255),
                utm=_clip(utm, 500),
                note=_clip(note, 2000),
            )
            delivered = bool(lead and lead.card_message_id)
        except Exception:  # noqa: BLE001 — контакт важнее карточки, ниже запасной путь
            logger.exception("web lead: карточка не создана, шлём обычным сообщением")
        if not delivered:
            delivered = await send_operator_message(session, text)
        await log_event(
            session,
            "web_lead",
            data={
                "name": name,
                "phone": phone,
                "source": source,
                "gclid": _clip(gclid, 255),
                "utm": _clip(utm, 500),
                "delivered": delivered,
            },
        )
        await session.commit()

    if not delivered:
        logger.warning("web lead saved but operators not notified (chat not configured?)")
    return JSONResponse({"ok": True})

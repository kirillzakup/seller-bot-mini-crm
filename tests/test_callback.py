"""request_callback / save_contact resolve a vague 'этот' to the client's real number."""

from __future__ import annotations

import asyncio

import app.sales.tools as tools


def _run(coro_factory):
    return asyncio.run(coro_factory())


def test_callback_resolves_this_number_for_whatsapp(client, monkeypatch):
    from app.db.base import AsyncSessionLocal
    from app.db.models import User
    from app.db.models.enums import Channel

    sent: list[str] = []

    async def fake_send(session, text):
        sent.append(text)
        return True

    monkeypatch.setattr(tools, "send_operator_message", fake_send)

    async def run():
        async with AsyncSessionLocal() as s:
            u = User(channel=Channel.WHATSAPP, external_id="77011234567", first_name="Кирилл")
            s.add(u)
            await s.flush()
            await tools._request_callback(s, u, "Кирилл", "этот", "Перезвонить по заказу")
            await s.commit()
            return u.phone

    saved = _run(run)
    assert sent, "operator not notified"
    msg = sent[0]
    before_note = msg.split("Вопрос:")[0]
    assert "Телефон: +77011234567" in msg  # resolved to the WhatsApp number
    assert "wa.me/77011234567" in msg  # clickable contact for the operator
    assert "этот" not in before_note  # no literal 'этот' in the phone/contact block
    assert saved == "+77011234567"  # stored a real number, not 'этот'


def test_callback_keeps_dictated_number(client, monkeypatch):
    from app.db.base import AsyncSessionLocal
    from app.db.models import User
    from app.db.models.enums import Channel

    sent: list[str] = []

    async def fake_send(session, text):
        sent.append(text)
        return True

    monkeypatch.setattr(tools, "send_operator_message", fake_send)

    async def run():
        async with AsyncSessionLocal() as s:
            u = User(channel=Channel.TELEGRAM, external_id="tg777", telegram_id=777, first_name="A")
            s.add(u)
            await s.flush()
            await tools._request_callback(s, u, "A", "+7 701 999 88 77", None)
            await s.commit()
            return u.phone

    saved = _run(run)
    assert "Телефон: +7 701 999 88 77" in sent[0]  # a real dictated number is preserved
    assert saved == "+7 701 999 88 77"

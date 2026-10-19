"""Web-lead intake endpoint (POST /leads): forwarding, honeypot, validation."""

from __future__ import annotations

import app.leads as leads


def test_lead_from_site_becomes_a_card(client, monkeypatch):
    """Заявка с сайта попадает в ту же воронку, что и заявки из переписки."""
    import asyncio

    from app.services import leads_service

    cards: list = []

    async def fake_post(session, lead, user):
        cards.append((lead.id, user.external_id))

    monkeypatch.setattr(leads_service, "post_card", fake_post)

    r = client.post(
        "/leads",
        data={"name": "Иван", "phone": "+7 701 234 5678", "source": "Каталог — Модель А"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert len(cards) == 1, "карточка ушла продавцам"
    _lead_id, external = cards[0]
    assert external == "77012345678", "контакт заведён по цифрам номера"

    async def check():
        from sqlalchemy import select

        from app.db.base import AsyncSessionLocal
        from app.db.models import Lead

        async with AsyncSessionLocal() as session:
            lead = (
                await session.execute(select(Lead).order_by(Lead.id.desc()).limit(1))
            ).scalar_one()
            assert "Каталог — Модель А" in lead.source
            assert lead.user.full_name.startswith("Иван")
            # По номеру собирается ссылка на WhatsApp — продавцу есть куда нажать.
            assert leads_service.contact_link(lead.user) == (
                "WhatsApp",
                "https://wa.me/77012345678",
            )

    asyncio.run(check())


def test_lead_honeypot_dropped(client, monkeypatch):
    called = False

    async def fake_send(session, text):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(leads, "send_operator_message", fake_send)

    r = client.post(
        "/leads",
        data={"name": "Bot", "phone": "+7 700 000 0000", "contact_email": "spam@x.com"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert called is False  # honeypot → silently dropped, operators not pinged


def test_lead_requires_phone(client):
    r = client.post("/leads", data={"name": "Без телефона", "phone": "123"})
    assert r.status_code == 422
    assert r.json()["error"] == "phone_required"


def test_site_order_reaches_managers_with_its_contents(client, monkeypatch):
    """Заказ с сайта приходит карточкой и говорит, что именно заказали."""
    import asyncio

    from app.services import leads_service

    async def fake_post(session, lead, user):
        lead.card_message_id = 77
        return True

    monkeypatch.setattr(leads_service, "post_card", fake_post)

    r = client.post(
        "/leads",
        data={
            "name": "Ержан",
            "phone": "+7 702 111 22 33",
            "source": "Оформление заказа",
            "note": "Заказ №142: Модель А, 2900 шт, 626 400 ₸",
            "gclid": "Cj0KCQjwORDER",
        },
    )
    assert r.status_code == 200

    async def check():
        from sqlalchemy import select

        from app.db.base import AsyncSessionLocal
        from app.db.models import Lead

        async with AsyncSessionLocal() as session:
            lead = (
                await session.execute(select(Lead).order_by(Lead.id.desc()).limit(1))
            ).scalar_one()
            assert "Модель А" in lead.request
            assert "626 400" in lead.request
            assert lead.gclid == "Cj0KCQjwORDER", "реклама получит эту продажу"
            assert "Оформление заказа" in lead.source

    asyncio.run(check())

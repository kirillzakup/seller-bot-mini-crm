"""Повторные сообщения о заявке ведут к её карточке и к переписке с клиентом."""

from __future__ import annotations

from app.db.models import Lead
from app.db.models.enums import LeadStatus
from app.services import leads_reminders
from app.services import leads_service as leads


def test_card_link_points_to_the_card_in_the_sales_group():
    lead = Lead(
        user_id=7, status=LeadStatus.IN_WORK, card_chat_id=-1001111111111, card_message_id=1242
    )
    assert leads.card_link(lead) == "https://t.me/c/1111111111/1242"
    no_card = Lead(user_id=7, status=LeadStatus.NEW)
    assert leads.card_link(no_card) == "", "нет карточки — нет ссылки"
    assert leads.dialog_link(lead).endswith("/admin/dialogs?u=7")


def test_digest_rows_link_to_the_card_or_to_the_dialog(client):
    """В утренних списках номер — ссылка на карточку; нет карточки — на переписку."""
    import asyncio

    from app.db.base import AsyncSessionLocal

    lead = Lead(
        user_id=7,
        status=LeadStatus.IN_WORK,
        taken_by_name="Ксения",
        card_chat_id=-1001111111111,
        card_message_id=1242,
    )
    lead.id = 46
    no_card = Lead(user_id=8, status=LeadStatus.NEW)
    no_card.id = 47

    async def run():
        async with AsyncSessionLocal() as session:
            work = await leads_reminders._lead_rows(session, [lead], owner=True)
            new = await leads_reminders._lead_rows(session, [no_card], owner=False)
            return work[0], new[0]

    work, new = asyncio.run(run())
    assert work.startswith('<a href="https://t.me/c/1111111111/1242">#46</a>')
    assert "Ксения" in work
    assert new.startswith("#47 ") and "/admin/dialogs?u=8" in new


def test_telegram_dialog_topic_link_on_card():
    """Тема клиента в группе «Диалоги» — чтобы ответить ему прямо из Telegram."""
    from app.db.models import User

    user = User(first_name="Ратмир", inbox_topic_id=812)
    lead = Lead(user_id=7, status=LeadStatus.IN_WORK, taken_by_name="Ксения")
    lead.id = 48
    inbox = -1002222222222
    assert leads.tg_dialog_link(inbox, user) == "https://t.me/c/2222222222/812"
    assert leads.tg_dialog_link(None, user) == "", "чат «Диалоги» не подключён"
    assert leads.tg_dialog_link(inbox, User(first_name="Без темы")) == ""

    card = leads.card_text(lead, user, inbox_chat=inbox)
    assert "🗂 Диалог в Telegram: https://t.me/c/2222222222/812" in card
    assert "Диалог в Telegram" not in leads.card_text(lead, user), "без чата — без строки"

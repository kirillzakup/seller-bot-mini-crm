"""Self-learning feedback loops: KB from operator answers + script analysis.

The test DB is shared across the session (see conftest), so these tests avoid
asserting exact global counts and set the ``llm`` setting explicitly where it
matters.
"""

from __future__ import annotations

import asyncio
import random

from app.db.base import AsyncSessionLocal
from app.db.models import Product, User
from app.db.models.enums import Channel, FulfillmentType, OrderStatus
from app.knowledge.rag import search_kb
from app.sales.conversation import save_message
from app.services import learning
from app.services import orders as orders_svc
from app.services.settings_service import set_setting


async def _user(session) -> User:
    n = random.randint(1, 10**9)
    u = User(channel=Channel.TELEGRAM, external_id=str(n), telegram_id=n, first_name="Тест")
    session.add(u)
    await session.flush()
    return u


def test_capture_operator_answer_creates_suggestion():
    async def main():
        async with AsyncSessionLocal() as s:
            u = await _user(s)
            await save_message(s, u.id, "user", "А есть рассрочка на курс?")
            sug = await learning.capture_operator_answer(
                s, u.id, "Да, можно разбить на 3 платежа без процентов."
            )
            assert sug is not None
            assert sug.question == "А есть рассрочка на курс?"
            assert sug.status == "pending"

    asyncio.run(main())


def test_capture_skips_short_answer():
    async def main():
        async with AsyncSessionLocal() as s:
            u = await _user(s)
            await save_message(s, u.id, "user", "Вопрос?")
            assert await learning.capture_operator_answer(s, u.id, "ок") is None

    asyncio.run(main())


def test_capture_dedups_identical_pending():
    async def main():
        async with AsyncSessionLocal() as s:
            u = await _user(s)
            await save_message(s, u.id, "user", "Сколько длится обучение?")
            answer = "Курс идёт 3 месяца по 2 занятия в неделю."
            first = await learning.capture_operator_answer(s, u.id, answer)
            second = await learning.capture_operator_answer(s, u.id, answer)
            assert first is not None and second is None

    asyncio.run(main())


def test_approve_kb_suggestion_becomes_searchable():
    async def main():
        async with AsyncSessionLocal() as s:
            u = await _user(s)
            await save_message(s, u.id, "user", "Можно ли оплатить через Kaspi?")
            sug = await learning.capture_operator_answer(
                s, u.id, "Да, принимаем Kaspi — пришлём номер для перевода."
            )
            item = await learning.approve_kb_suggestion(s, sug.id)
            assert item is not None
            await s.flush()
            found = await search_kb(s, "оплата kaspi перевод")
            assert any("Kaspi" in it.body for it in found)

    asyncio.run(main())


def test_analyze_successful_dialogs_creates_proposal(monkeypatch):
    async def fake_cc(*, messages, tools, config):
        return {
            "role": "assistant",
            "content": (
                '{"improved_script": "Новый скрипт: 1. Контакт 2. Ценность 3. Оплата",'
                ' "rationale": "Добавил явный шаг закрытия на оплату."}'
            ),
        }

    monkeypatch.setattr(learning, "chat_completion", fake_cc)

    async def main():
        async with AsyncSessionLocal() as s:
            await set_setting(
                s, "llm", {"base_url": "http://fake/v1", "api_key": "k", "model": "m"}
            )
            u = await _user(s)
            product = Product(
                sku=f"learn{random.randint(1, 10**9)}",
                title="Курс",
                price_kzt=9000,
                fulfillment_type=FulfillmentType.TEXT_LINK,
                fulfillment_config={"text": "ok"},
            )
            s.add(product)
            await s.flush()
            order = await orders_svc.create_order(s, u, product)
            order.status = OrderStatus.PAID
            await save_message(s, u.id, "user", "Хочу купить курс")
            await save_message(s, u.id, "assistant", "Отлично! Вот ссылка на оплату")
            await s.flush()

            proposal, reason = await learning.analyze_successful_dialogs(s)
            assert reason is None
            assert proposal is not None
            assert "Новый скрипт" in proposal.proposed_text
            assert proposal.based_on >= 1

    asyncio.run(main())


def test_apply_script_proposal_updates_setting(monkeypatch):
    async def fake_cc(*, messages, tools, config):
        return {"role": "assistant", "content": '{"improved_script": "СКРИПТ-2", "rationale": "r"}'}

    monkeypatch.setattr(learning, "chat_completion", fake_cc)

    async def main():
        async with AsyncSessionLocal() as s:
            await set_setting(
                s, "llm", {"base_url": "http://fake/v1", "api_key": "k", "model": "m"}
            )
            u = await _user(s)
            product = Product(
                sku=f"learn{random.randint(1, 10**9)}",
                title="Курс",
                price_kzt=9000,
                fulfillment_type=FulfillmentType.TEXT_LINK,
                fulfillment_config={"text": "ok"},
            )
            s.add(product)
            await s.flush()
            order = await orders_svc.create_order(s, u, product)
            order.status = OrderStatus.PAID
            await save_message(s, u.id, "user", "Куплю")
            await s.flush()

            proposal, _ = await learning.analyze_successful_dialogs(s)
            assert proposal is not None
            ok = await learning.apply_script_proposal(s, proposal.id)
            assert ok is True
            from app.services.settings_service import get_setting

            assert await get_setting(s, "sales_script") == proposal.proposed_text

    asyncio.run(main())


def test_analyze_without_llm_configured_returns_reason():
    async def main():
        async with AsyncSessionLocal() as s:
            await set_setting(s, "llm", {"base_url": "", "api_key": ""})
            proposal, reason = await learning.analyze_successful_dialogs(s)
            assert proposal is None
            assert reason and "настроен" in reason.lower()

    asyncio.run(main())

"""Портрет клиента и подстройка под него: бот помнит детали и говорит языком клиента."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.sales import client_profile
from app.sales.adaptation import directives, live_signals, profile_block, region_of


@pytest.mark.parametrize(
    ("city", "region"),
    [
        ("Павлодар", "север"),
        ("г. Шымкент", "юг"),
        ("Өскемен", "восток"),
        ("Алматы", "мегаполис"),
        ("Актобе", "запад"),
        ("Караганда", "центр"),
        ("Москва", ""),
        ("", ""),
    ],
)
def test_region_is_recognised_from_the_city(city, region):
    assert region_of(city) == region


def test_merge_keeps_known_facts_and_rejects_made_up_values():
    old = {"city": "Павлодар", "objections": ["дорого"], "open_questions": ["есть рыжий?"]}
    new = {
        "city": "",  # о городе в последних сообщениях молчат — не забываем его
        "stance": "передумал",  # нет такого значения — правило под него не найдётся
        "emotion": "тревога",
        "objections": ["далеко везти", "Дорого"],
        "open_questions": [],
    }
    merged = client_profile.merge(old, new)
    assert merged["city"] == "Павлодар"
    assert "stance" not in merged
    assert merged["emotion"] == "тревога"
    assert merged["objections"] == ["дорого", "далеко везти"], "возражения копятся без дублей"
    assert merged["open_questions"] == [], "на старые вопросы бот мог уже ответить"


def test_portrait_block_answers_open_questions_and_calms_feelings_first():
    profile = {
        "city": "Шымкент",
        "deadline": "до конца месяца",
        "language": "kk",
        "emotion": "тревога",
        "motive": "одобрение семьи",
        "choice_stress": "высокий",
        "main_need": "надёжная система для дома",
        "open_questions": ["есть ли графит в наличии"],
    }
    signals = live_signals(["Турин жиберинизши"], 0)
    block = profile_block(profile, signals)
    assert "Сроки: до конца месяца" in block and "не переспрашивай" in block
    assert "есть ли графит в наличии" in block and "ask_admin" in block
    assert "казахском" in block
    how = directives(profile, signals)
    assert how[0].startswith("Клиент боится ошибиться"), "сначала снять чувство"
    assert any("не больше двух вариантов" in line for line in how)
    assert any("одобрение семьи" in line for line in how)
    assert any(line.startswith("Юг") for line in how)


def test_lists_in_text_fields_and_contradicting_rules_are_tidied():
    merged = client_profile.merge({}, {"preferences": ["графит", "гладкий"]})
    assert merged["preferences"] == "графит, гладкий", "не «['графит', ...]»"
    how = directives({"emotion": "интерес", "stance": "думает"}, live_signals([], 0))
    assert not any("не тяни" in line for line in how), "пауза клиента важнее"
    assert any("взял паузу" in line for line in how)


def test_short_writer_gets_short_answers_and_empty_portrait_adds_nothing():
    signals = live_signals(["цена?", "павлодар", "100 м2"], voice_count=0)
    assert any("коротко" in line for line in directives({}, signals))
    assert profile_block({}, live_signals([], 0)) == ""


def test_no_nudges_after_client_said_he_will_call_himself():
    from app.db.models import User

    user = User(profile=json.dumps({"stance": "сам свяжется"}, ensure_ascii=False))
    assert client_profile.nudges_allowed(user) is False
    assert client_profile.nudges_allowed(User(profile="")) is True
    assert client_profile.nudges_allowed(User(profile="{битый json")) is True


def test_refresh_builds_portrait_and_fills_empty_lead_city(client, monkeypatch):
    from app.db.base import AsyncSessionLocal
    from app.db.models import Lead, User
    from app.db.models.enums import Channel, LeadStatus
    from app.sales.conversation import save_message
    from app.services.settings_service import set_setting

    async def fake_llm(*, messages, tools, config, response_format=None):
        assert response_format == {"type": "json_object"}
        assert "К: дом 12 на 12 в Алматы" in messages[1]["content"]
        return {
            "content": json.dumps(
                {"city": "Алматы", "size": "12×12", "emotion": "спешка", "stance": "выбирает"},
                ensure_ascii=False,
            )
        }

    monkeypatch.setattr(client_profile, "chat_completion", fake_llm)

    async def run():
        async with AsyncSessionLocal() as session:
            await set_setting(session, "llm", {"base_url": "http://x", "api_key": "k"})
            user = User(channel=Channel.WHATSAPP, external_id="77010000999", first_name="Б")
            session.add(user)
            await session.flush()
            lead = Lead(user_id=user.id, status=LeadStatus.NEW, city="")
            session.add(lead)
            await save_message(session, user.id, "user", "дом 12 на 12 в Алматы")
            profile = await client_profile.refresh(session, user)
            assert profile["city"] == "Алматы" and profile["emotion"] == "спешка"
            assert client_profile.load(user)["size"] == "12×12"
            assert lead.city == "Алматы", "город из портрета попадает в пустую заявку"
            await session.rollback()

    asyncio.run(run())


def test_system_prompt_carries_method_and_portrait(client):
    from app.db.base import AsyncSessionLocal
    from app.db.models import User
    from app.db.models.enums import Channel
    from app.sales.ai_manager import build_system_prompt

    async def run():
        async with AsyncSessionLocal() as session:
            user = User(
                channel=Channel.WHATSAPP,
                external_id="77010000998",
                profile=json.dumps(
                    {"city": "Павлодар", "size": "4 камеры"}, ensure_ascii=False
                ),
            )
            session.add(user)
            await session.flush()
            prompt = await build_system_prompt(session, user)
            await session.rollback()
        return prompt

    prompt = asyncio.run(run())
    assert "ПОНЯТЬ → СУЗИТЬ → ПОСОВЕТОВАТЬ → ЗАКРЫТЬ" in prompt
    assert "ПОРТРЕТ КЛИЕНТА" in prompt and "Объём: 4 камеры" in prompt


def test_confirmed_name_is_not_overwritten_by_messenger_nickname(client):
    from app.db.base import AsyncSessionLocal
    from app.db.models.enums import Channel
    from app.services.users import get_or_create_contact

    async def run():
        async with AsyncSessionLocal() as session:
            user = await get_or_create_contact(
                session, channel=Channel.WHATSAPP, external_id="77010000997", first_name="Б"
            )
            user.first_name, user.name_confirmed = "Бахтан", True
            again = await get_or_create_contact(
                session, channel=Channel.WHATSAPP, external_id="77010000997", first_name="Б"
            )
            name = again.first_name
            await session.rollback()
        return name

    assert asyncio.run(run()) == "Бахтан"

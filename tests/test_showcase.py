"""Просьба показать фото: бот обязан ответить материалами, а не как получится."""

from __future__ import annotations

import asyncio

import pytest

from app.sales.conversation import save_message
from app.services import showcase


@pytest.mark.parametrize(
    "text",
    [
        "Фото можно",
        "здравствуйте можете фото отправить выше озвученных товаров?",
        "фото можно что бы подобрать",
        "фотоларын жиберчи",
        "покажите ваши объекты",
        "есть каталог?",
        "примеры работ есть?",
        "отправьте фото",
        "Турин жиберинизши",
        "түрін жіберіңізші",
        "көрсетіңізші",
    ],
)
def test_asking_for_photos_is_recognised(text):
    assert showcase.is_photo_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "Я могу отправить фото.",
        "скину фото своего дома",
        "отправлю фото завтра",
        "могу прислать фото",
        "сколько стоит доставка",
        "спасибо",
        "",
    ],
)
def test_offering_own_photo_is_not_a_request(text):
    assert not showcase.is_photo_request(text)


_LINKS = {
    "objects_url": "https://example.kz/works",
    "catalog_url": "https://example.kz/catalog",
    "document_query": "портфолио",
}


def test_showcase_message_has_both_links():
    text = showcase.showcase_text(_LINKS)
    assert "example.kz/works" in text, "ссылка на работы"
    assert "example.kz/catalog" in text, "каталог с фотографиями"


def test_no_links_configured_means_no_link_message():
    assert showcase.showcase_text(showcase.DEFAULTS) == ""


def _use_links(monkeypatch):
    async def cfg(session):
        return dict(_LINKS)

    monkeypatch.setattr(showcase, "showcase_config", cfg)


def test_showcase_sends_portfolio_and_links(client, monkeypatch):
    """Уходит и документ, и ссылки — одним пакетом."""
    from app.db.base import AsyncSessionLocal
    from app.db.models import User
    from app.db.models.enums import Channel

    _use_links(monkeypatch)
    sent = {"media": [], "text": []}

    class FakeMessenger:
        async def send_media(self, ext, *, url, mime, kind, filename="", caption=""):
            sent["media"].append(filename)

        async def send_text(self, ext, text):
            sent["text"].append(text)
            return "mid-1"

    async def run():
        async with AsyncSessionLocal() as session:
            user = User(channel=Channel.WHATSAPP, external_id="77015550001", first_name="Асхат")
            session.add(user)
            await session.flush()
            # Как в dispatch: входящее «пришлите фото» сохранено раньше ответа.
            await save_message(session, user.id, "user", "отправьте фото")
            ok = await showcase.send_showcase(session, user, FakeMessenger())
            assert ok is True
            await session.rollback()

    asyncio.run(run())
    assert len(sent["text"]) == 1
    assert "example.kz/works" in sent["text"][0]


def test_links_go_out_even_without_the_portfolio_file(client, monkeypatch):
    """Портфолио не нашлось — клиент всё равно получает ссылки, а не молчание."""
    from app.db.base import AsyncSessionLocal
    from app.db.models import User
    from app.db.models.enums import Channel

    async def no_docs(session, query, limit=1):
        return []

    monkeypatch.setattr(showcase, "search_documents", no_docs)
    _use_links(monkeypatch)
    sent = []

    class FakeMessenger:
        async def send_text(self, ext, text):
            sent.append(text)
            return "mid-2"

    async def run():
        async with AsyncSessionLocal() as session:
            user = User(channel=Channel.WHATSAPP, external_id="77015550002", first_name="Бота")
            session.add(user)
            await session.flush()
            # Как в dispatch: входящее «пришлите фото» сохранено раньше ответа.
            await save_message(session, user.id, "user", "отправьте фото")
            assert await showcase.send_showcase(session, user, FakeMessenger()) is True
            await session.rollback()

    asyncio.run(run())
    assert len(sent) == 1 and "example.kz/works" in sent[0]


def test_showcase_is_not_sent_outside_the_24h_window(client):
    """Клиент молчит больше суток: WhatsApp такое не доставит — не делаем вид, что отправили."""
    from datetime import UTC, datetime, timedelta

    from app.db.base import AsyncSessionLocal
    from app.db.models import User
    from app.db.models.enums import Channel

    sent = []

    class FakeMessenger:
        async def send_media(self, *a, **kw):
            sent.append("media")

        async def send_text(self, ext, text):
            sent.append(text)
            return "mid-3"

    async def run():
        async with AsyncSessionLocal() as session:
            user = User(channel=Channel.INSTAGRAM, external_id="ig-silent", first_name="Тихий")
            session.add(user)
            await session.flush()
            msg = await save_message(session, user.id, "user", "фото можно?")
            msg.created_at = datetime.now(UTC) - timedelta(hours=30)
            await session.flush()
            assert await showcase.send_showcase(session, user, FakeMessenger()) is False
            await session.rollback()

    asyncio.run(run())
    assert sent == []

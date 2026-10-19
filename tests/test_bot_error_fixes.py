"""Регрессии по ошибкам из логов прода (октябрь 2026)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from app.db.models import MediaAsset, User
from app.db.models.enums import Channel
from app.sales.conversation import save_message, window_open
from app.services.media import is_picture


def test_instagram_has_the_same_24h_window_as_whatsapp(client):
    """Дожимы в Instagram вне окна падали с 403 — окно теперь проверяется и там."""
    from app.db.base import AsyncSessionLocal

    async def run():
        async with AsyncSessionLocal() as session:
            ig = User(channel=Channel.INSTAGRAM, external_id="ig-1", first_name="И")
            tg = User(channel=Channel.TELEGRAM, external_id="tg-1", first_name="Т")
            session.add_all([ig, tg])
            await session.flush()
            msg = await save_message(session, ig.id, "user", "привет")
            msg.created_at = datetime.now(UTC) - timedelta(hours=25)
            await session.flush()
            assert await window_open(session, ig) is False
            assert await window_open(session, tg) is True, "у Telegram окна нет"
            msg.created_at = datetime.now(UTC) - timedelta(hours=1)
            await session.flush()
            assert await window_open(session, ig) is True
            await session.rollback()

    asyncio.run(run())


def test_link_assets_are_never_sent_as_photos():
    """Ссылка «2ГИС» с тегом «адрес» уходила фото с URL «/media/» и не доставлялась."""
    link = MediaAsset(title="2ГИС", filename="", kind="link", url="https://2gis.kz/x")
    empty = MediaAsset(title="битая", filename="  ", kind="image")
    photo = MediaAsset(title="ждун", filename="abc.jpg", kind="image")
    legacy = MediaAsset(title="старая", filename="old.jpg", kind=None)
    assert not is_picture(link)
    assert not is_picture(empty)
    assert is_picture(photo) and is_picture(legacy)


def test_write_is_retried_after_a_duplicate_contact_race(client):
    """Второй одновременный вебхук нового клиента падал на UNIQUE и терял сообщение."""
    from app.db.base import run_write_with_retry

    calls = []

    async def work(session):
        calls.append(1)
        if len(calls) == 1:
            raise IntegrityError(
                "INSERT", {}, Exception("UNIQUE constraint failed: users.channel")
            )

    assert asyncio.run(run_write_with_retry(work, label="test")) is True
    assert len(calls) == 2

"""Operator/admin notification fan-out — both chats receive, deduplicated."""

from __future__ import annotations

import asyncio

from app.db.base import AsyncSessionLocal
from app.db.models import User
from app.db.models.enums import Channel
from app.services import notify
from app.services.settings_service import set_setting


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[int] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(chat_id)

    async def send_photo(self, chat_id, *a, **kw):
        self.sent.append(chat_id)


def test_send_operator_message_reaches_both_chats(monkeypatch):
    import app.bot.runner as runner

    fake = FakeBot()
    monkeypatch.setattr(runner, "get_bot", lambda: fake)

    async def main() -> bool:
        async with AsyncSessionLocal() as s:
            await set_setting(s, "operator_chat_id", -1001112223)
            await set_setting(s, "admin_chat_id", 5556667)
            return await notify.send_operator_message(s, "тест")

    ok = asyncio.run(main())
    assert ok is True
    assert set(fake.sent) == {-1001112223, 5556667}  # delivered to both


def test_notify_chat_ids_dedup():
    async def main() -> list[int]:
        async with AsyncSessionLocal() as s:
            await set_setting(s, "operator_chat_id", 777)
            await set_setting(s, "admin_chat_id", 777)  # same id in both fields
            return await notify.get_notify_chat_ids(s)

    assert asyncio.run(main()) == [777]  # one send, not two


def test_no_chats_returns_empty():
    async def main() -> list[int]:
        async with AsyncSessionLocal() as s:
            await set_setting(s, "operator_chat_id", None)
            await set_setting(s, "admin_chat_id", None)
            return await notify.get_notify_chat_ids(s)

    assert asyncio.run(main()) == []


def test_operator_message_routes_into_forum_topic(monkeypatch):
    """A forum-supergroup operators' chat delivers into its configured topic."""
    import app.bot.runner as runner

    sent: list[tuple[int, int | None]] = []

    class ThreadBot:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, kw.get("message_thread_id")))

    monkeypatch.setattr(runner, "get_bot", lambda: ThreadBot())

    async def main() -> None:
        async with AsyncSessionLocal() as s:
            await set_setting(s, "operator_chat_id", -1001111111111)
            await set_setting(s, "operator_chat_thread_id", 571)
            await set_setting(s, "admin_chat_id", None)
            await s.commit()
        try:
            async with AsyncSessionLocal() as s:
                await notify.send_operator_message(s, "заявка")
        finally:
            async with AsyncSessionLocal() as s:  # restore for other tests
                await set_setting(s, "operator_chat_id", None)
                await set_setting(s, "operator_chat_thread_id", None)
                await s.commit()

    asyncio.run(main())
    assert sent == [(-1001111111111, 571)]  # routed into topic 571, not General


def test_client_block_gives_reachable_links():
    """Every notification identifies the client with a real, clickable link."""
    ig = User(
        id=7, channel=Channel.INSTAGRAM, external_id="1784100",
        username="ivan_ig", first_name="Иван",
    )
    block = notify.client_block(ig)
    assert "@ivan_ig" in block
    assert "https://instagram.com/ivan_ig" in block  # reach them on Instagram
    assert "/admin/dialogs?u=7" in block  # or reply in the panel

    wa = User(id=8, channel=Channel.WHATSAPP, external_id="77771234567", first_name="Аскар")
    assert "https://wa.me/77771234567" in notify.client_block(wa)

    anon = User(id=9, channel=Channel.INSTAGRAM, external_id="123")  # no @username yet
    b = notify.client_block(anon)
    assert "@" not in b.splitlines()[0]  # no fake handle…
    assert "/admin/dialogs?u=9" in b  # …but the panel link still lets you reply


def test_enrich_contact_resolves_instagram_handle():
    """A fresh Instagram contact (IGSID only) gets its @username + name filled in."""
    from app.channels import dispatch

    class _FakeMsgr:
        async def fetch_profile(self, external_id):
            return {"name": "Иван Петров", "username": "ivan_petrov"}

    async def main():
        async with AsyncSessionLocal() as s:
            u = User(channel=Channel.INSTAGRAM, external_id="1000000000000001")
            s.add(u)
            await s.flush()
            await dispatch._enrich_contact(s, _FakeMsgr(), u)
            assert u.username == "ivan_petrov"
            assert u.first_name == "Иван Петров"

    asyncio.run(main())

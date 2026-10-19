"""Telegram operator-inbox mirroring tests (fake bot, no live Telegram)."""

from __future__ import annotations

import asyncio

from app.db.base import AsyncSessionLocal
from app.db.models import User
from app.db.models.enums import Channel
from app.services import inbox


class _FakeTopic:
    def __init__(self, tid: int) -> None:
        self.message_thread_id = tid


class _FakeBot:
    def __init__(self) -> None:
        self.posts: list = []
        self.topics_created = 0

    async def create_forum_topic(self, chat_id, name):
        self.topics_created += 1
        return _FakeTopic(555)

    async def send_message(self, chat_id, text, message_thread_id=None):
        self.posts.append(("msg", message_thread_id, text))

    async def send_photo(self, chat_id, photo, caption=None, message_thread_id=None):
        self.posts.append(("photo", message_thread_id, caption))


def _msgs(bot: _FakeBot) -> list[str]:
    return [p[2] for p in bot.posts if p[0] == "msg"]


def test_inbox_mirror_creates_topic_and_transcribes(monkeypatch):
    """First inbound creates the client's topic + card; replies reuse it; outbound too."""
    import app.bot.runner as runner

    bot = _FakeBot()
    monkeypatch.setattr(runner, "get_bot", lambda: bot)

    async def main():
        async with AsyncSessionLocal() as s:
            await inbox.set_inbox(s, -1001234567890)
            u = User(channel=Channel.INSTAGRAM, external_id="INBOX1", username="client1")
            s.add(u)
            await s.flush()

            await inbox.mirror_incoming(s, u, "здравствуйте")
            assert u.inbox_topic_id == 555 and bot.topics_created == 1
            texts = _msgs(bot)
            # client message posted as a quote (accent bar), card with @username posted
            assert any("<blockquote>здравствуйте</blockquote>" == t for t in texts)
            assert any("@client1" in t for t in texts)

            await inbox.mirror_outgoing(s, u, "чем помочь?", who="bot")
            assert any(t.startswith("🤖 ") and "чем помочь?" in t for t in _msgs(bot))

            await inbox.mirror_incoming(s, u, "второе сообщение")
            assert bot.topics_created == 1  # topic reused, not recreated

            await s.delete(u)
            await inbox.clear_inbox(s)
            await s.commit()

    asyncio.run(main())


def test_inbox_noop_when_not_configured(monkeypatch):
    """No inbox group set → mirroring is a silent no-op (never creates a topic)."""
    import app.bot.runner as runner

    bot = _FakeBot()
    monkeypatch.setattr(runner, "get_bot", lambda: bot)

    async def main():
        async with AsyncSessionLocal() as s:
            await inbox.clear_inbox(s)
            u = User(channel=Channel.TELEGRAM, external_id="INBOX2")
            s.add(u)
            await s.flush()
            await inbox.mirror_incoming(s, u, "hi")
            assert u.inbox_topic_id is None and bot.topics_created == 0
            await s.delete(u)
            await s.commit()

    asyncio.run(main())

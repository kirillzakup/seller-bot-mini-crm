"""Media library: upload validation, search, and the ``send_image`` AI tool."""

from __future__ import annotations

import asyncio
import random

from app.db.base import AsyncSessionLocal
from app.db.models import User
from app.db.models.enums import Channel
from app.sales.tools import run_tool
from app.services import media as media_svc

PNG = bytes.fromhex("89504e470d0a1a0a")  # PNG magic bytes — enough for a fake image file


class ImgFakeMessenger:
    def __init__(self) -> None:
        self.channel = Channel.TELEGRAM
        self.images: list[tuple[str, str, str]] = []
        self.texts: list[tuple[str, str]] = []

    async def send_text(self, external_id: str, text: str) -> None:
        self.texts.append((external_id, text))

    async def send_image(self, external_id: str, url: str, caption: str = "") -> None:
        self.images.append((external_id, url, caption))


async def _user(session) -> User:
    n = random.randint(1, 10**9)
    u = User(channel=Channel.TELEGRAM, external_id=str(n), telegram_id=n, first_name="X")
    session.add(u)
    await session.flush()
    return u


def test_save_upload_rejects_non_image(monkeypatch, tmp_path):
    monkeypatch.setattr(media_svc, "MEDIA_DIR", tmp_path)

    async def main():
        async with AsyncSessionLocal() as s:
            asset = await media_svc.save_upload(
                s, title="doc", data=b"hello", content_type="text/plain"
            )
            assert asset is None

    asyncio.run(main())


def test_save_upload_and_search(monkeypatch, tmp_path):
    monkeypatch.setattr(media_svc, "MEDIA_DIR", tmp_path)

    async def main():
        async with AsyncSessionLocal() as s:
            asset = await media_svc.save_upload(
                s,
                title="Мем про дедлайн",
                data=PNG,
                content_type="image/png",
                tags="дедлайн программирование",
            )
            assert asset is not None and asset.filename.endswith(".png")
            assert (tmp_path / asset.filename).exists()
            found = await media_svc.search_media(s, "покажи мем про дедлайн", limit=1)
            assert found and found[0].id == asset.id

    asyncio.run(main())


def test_send_image_tool_delivers(monkeypatch, tmp_path):
    monkeypatch.setattr(media_svc, "MEDIA_DIR", tmp_path)
    fake = ImgFakeMessenger()

    async def main():
        async with AsyncSessionLocal() as s:
            user = await _user(s)
            await media_svc.save_upload(
                s,
                title="Сертификат курса",
                data=PNG,
                content_type="image/png",
                tags="сертификат диплом",
            )
            res = await run_tool(
                "send_image",
                {"query": "пришли сертификат", "caption": "Ваш будущий сертификат"},
                session=s,
                user=user,
                messenger=fake,
                tg_bot=None,
            )
            assert "отправлена" in res.lower()
            assert len(fake.images) == 1
            assert fake.images[0][2] == "Ваш будущий сертификат"

    asyncio.run(main())


def test_send_image_tool_no_match_does_not_send(monkeypatch, tmp_path):
    monkeypatch.setattr(media_svc, "MEDIA_DIR", tmp_path)
    fake = ImgFakeMessenger()

    async def main():
        async with AsyncSessionLocal() as s:
            user = await _user(s)
            res = await run_tool(
                "send_image",
                {"query": "абракадабра-несуществующее-слово-98765"},
                session=s,
                user=user,
                messenger=fake,
                tg_bot=None,
            )
            assert "не выдумывай" in res.lower()
            assert not fake.images

    asyncio.run(main())


# ── Outbound attachments must be visible to the team, not only to the client ──
# The bot used to send pictures straight to the messenger and log nothing, so «Диалоги»
# looked empty and the operators only learned about a send from client screenshots —
# including the bot firing the same picture off several times.


async def _messages(session, user_id: int):
    from sqlalchemy import select

    from app.db.models import Message

    rows = await session.execute(
        select(Message).where(Message.user_id == user_id).order_by(Message.id)
    )
    return list(rows.scalars().all())


def _capture_mirror(monkeypatch) -> list[dict]:
    """Record what would have been pushed into the client's Telegram topic."""
    from app.services import inbox as inbox_svc

    seen: list[dict] = []

    async def fake_mirror(session, user, text, *, who="bot", media_url=None, media_kind=None):
        seen.append({"text": text, "who": who, "url": media_url, "kind": media_kind})

    monkeypatch.setattr(inbox_svc, "mirror_outgoing", fake_mirror)
    return seen


def test_sent_image_lands_in_dialog_and_telegram_mirror(monkeypatch, tmp_path):
    monkeypatch.setattr(media_svc, "MEDIA_DIR", tmp_path)
    fake = ImgFakeMessenger()
    mirrored = _capture_mirror(monkeypatch)

    async def main():
        async with AsyncSessionLocal() as s:
            user = await _user(s)
            await media_svc.save_upload(
                s, title="Кладка фото", data=PNG, content_type="image/png", tags="кладка"
            )
            await run_tool(
                "send_image",
                {"query": "кладка", "caption": "Наш объект"},
                session=s, user=user, messenger=fake, tg_bot=None,
            )
            msgs = await _messages(s, user.id)
            assert len(msgs) == 1
            assert msgs[0].media_kind == "photo" and msgs[0].media_url
            assert "Кладка фото" in msgs[0].content and "Наш объект" in msgs[0].content
            assert len(mirrored) == 1 and mirrored[0]["kind"] == "photo"
            assert mirrored[0]["who"] == "bot" and mirrored[0]["url"]

    asyncio.run(main())


def test_same_image_is_not_sent_twice(monkeypatch, tmp_path):
    """The repeat the team kept seeing: the model re-fires a picture it already sent."""
    monkeypatch.setattr(media_svc, "MEDIA_DIR", tmp_path)
    fake = ImgFakeMessenger()
    _capture_mirror(monkeypatch)

    async def main():
        async with AsyncSessionLocal() as s:
            user = await _user(s)
            await media_svc.save_upload(
                s, title="Прайс товаров", data=PNG, content_type="image/png", tags="прайс"
            )
            args = {"query": "прайс"}
            first = await run_tool(
                "send_image", args, session=s, user=user, messenger=fake, tg_bot=None
            )
            second = await run_tool(
                "send_image", args, session=s, user=user, messenger=fake, tg_bot=None
            )
            assert "отправлена" in first.lower()
            assert "уже отправлена" in second.lower()
            assert len(fake.images) == 1  # client got it exactly once
            assert len(await _messages(s, user.id)) == 1

    asyncio.run(main())


# ── Media library beyond pictures: circles, videos, voice and links ──


class AssetFakeMessenger:
    """Fake messenger recording every send kind the library can produce."""

    def __init__(self) -> None:
        self.channel = Channel.TELEGRAM
        self.images: list[tuple[str, str, str]] = []
        self.video_notes: list[tuple[str, str, str]] = []
        self.media: list[dict] = []
        self.texts: list[tuple[str, str]] = []

    async def send_text(self, external_id: str, text: str) -> None:
        self.texts.append((external_id, text))

    async def send_image(self, external_id: str, url: str, caption: str = "") -> None:
        self.images.append((external_id, url, caption))

    async def send_video_note(self, external_id: str, url: str, caption: str = "") -> None:
        self.video_notes.append((external_id, url, caption))

    async def send_media(self, external_id: str, *, url: str, mime: str, kind: str,
                         filename: str = "", caption: str = "") -> str | None:
        self.media.append({"url": url, "mime": mime, "kind": kind, "caption": caption})
        return "fake-mid"


def _asset(session, kind: str, title: str, **kw):
    from app.db.models import MediaAsset

    a = MediaAsset(title=title, filename=kw.pop("filename", "f.mp4"), mime=kw.pop("mime", ""),
                   kind=kind, tags="", trigger=kw.pop("trigger", title), url=kw.pop("url", ""),
                   is_active=True)
    session.add(a)
    return a


def test_send_asset_circle_goes_out_as_video_note(monkeypatch, tmp_path):
    monkeypatch.setattr(media_svc, "MEDIA_DIR", tmp_path)
    fake = AssetFakeMessenger()
    _capture_mirror(monkeypatch)

    async def main():
        async with AsyncSessionLocal() as s:
            user = await _user(s)
            _asset(s, "circle", "Кружок производства")
            await s.flush()
            res = await run_tool("send_asset", {"query": "Кружок производства"},
                                 session=s, user=user, messenger=fake, tg_bot=None)
            assert "отправлен" in res.lower()
            assert len(fake.video_notes) == 1 and not fake.images
            msgs = await _messages(s, user.id)
            assert msgs[0].media_kind == "video"          # playable in «Диалоги»
            assert msgs[0].content.startswith("[кружок:")  # but read as a circle

    asyncio.run(main())


def test_send_asset_link_is_sent_as_text(monkeypatch, tmp_path):
    monkeypatch.setattr(media_svc, "MEDIA_DIR", tmp_path)
    fake = AssetFakeMessenger()
    _capture_mirror(monkeypatch)

    async def main():
        async with AsyncSessionLocal() as s:
            user = await _user(s)
            _asset(s, "link", "Прайс", filename="", url="https://example.kz/price")
            await s.flush()
            await run_tool("send_asset", {"query": "Прайс"},
                           session=s, user=user, messenger=fake, tg_bot=None)
            assert len(fake.texts) == 1 and "https://example.kz/price" in fake.texts[0][1]
            msgs = await _messages(s, user.id)
            assert msgs[0].media_url is None  # a link has no attachment to preview

    asyncio.run(main())


def test_send_image_never_pushes_a_video_through_send_photo(monkeypatch, tmp_path):
    """The prompt lists pictures only, but a title match could still land on a circle."""
    monkeypatch.setattr(media_svc, "MEDIA_DIR", tmp_path)
    fake = AssetFakeMessenger()
    _capture_mirror(monkeypatch)

    async def main():
        async with AsyncSessionLocal() as s:
            user = await _user(s)
            _asset(s, "circle", "Обзор склада")
            await s.flush()
            await run_tool("send_image", {"query": "Обзор склада"},
                           session=s, user=user, messenger=fake, tg_bot=None)
            assert len(fake.video_notes) == 1 and not fake.images  # delegated, not mis-sent

    asyncio.run(main())

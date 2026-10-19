"""Regression tests for the lost-payment-receipt incident (ported from the school bot).

Root cause: a SQLite "database is locked" dropped the WhatsApp webhook carrying a client's
receipt (Meta had already received our 200, so no external retry), compounded by client
images never being persisted to the web «Диалоги». These lock in the fixes:
  1. run_write_with_retry recovers a webhook from a transient lock.
  2. Client images/PDFs are saved to «Диалоги» with a media preview.
  3. WhatsApp document (PDF) receipts are parsed and attached, never dropped.
"""

from __future__ import annotations

import asyncio
import random

from sqlalchemy import select

import app.channels.dispatch as disp
from app.channels.base import IncomingMessage
from app.channels.meta import parse_webhook
from app.db.base import AsyncSessionLocal, run_write_with_retry
from app.db.models import Message, Order, Product, User
from app.db.models.enums import Channel, FulfillmentType, OrderStatus
from app.sales.vision import ReceiptAnalysis
from app.services import orders as orders_svc


class _Msgr:
    channel = Channel.WHATSAPP

    def __init__(self, media: bytes = b"receiptbytes"):
        self.sent: list[str] = []
        self._media = media

    async def download_media(self, ref):
        return self._media

    async def download_audio(self, ref):
        return None

    async def send_text(self, ext, text):
        self.sent.append(text)

    async def send_typing(self, ext, *, message_id=None):
        pass

    async def fetch_profile(self, ext):
        return None


async def _noop(*a, **k):
    return None


def _patch_common(monkeypatch, fake):
    async def _get_msgr(session, channel):
        return fake

    monkeypatch.setattr(disp, "get_messenger", _get_msgr)
    monkeypatch.setattr(disp, "notify_manual_receipt", _noop)
    monkeypatch.setattr(disp.inbox_svc, "mirror_incoming", _noop)
    monkeypatch.setattr(
        disp, "save_operator_file", lambda data, mime, name: {"url": f"/media/x{name}"}
    )


async def _seed_pending_order(session, ext: str):
    u = User(channel=Channel.WHATSAPP, external_id=ext)
    p = Product(
        sku=f"s{ext}", title="Кирпич", price_kzt=1000,
        fulfillment_type=FulfillmentType.TEXT_LINK, fulfillment_config={"text": "ok"},
    )
    session.add_all([u, p])
    await session.flush()
    order = await orders_svc.create_order(session, u, p)
    await session.commit()
    return u.id, order.id


def test_client_receipt_image_is_saved_to_dialogs(monkeypatch):
    """A WhatsApp image during a pending order lands in «Диалоги» WITH a media preview
    (not only the Telegram mirror) AND is attached as a receipt for confirmation."""
    fake = _Msgr()
    _patch_common(monkeypatch, fake)

    async def _is_receipt(session, image_bytes, mime="image/jpeg"):
        return ReceiptAnalysis(is_receipt=True, amount=1000)

    monkeypatch.setattr(disp.vision, "analyze_receipt", _is_receipt)

    async def main():
        async with AsyncSessionLocal() as s:
            ext = str(random.randint(1, 10**9))
            uid, oid = await _seed_pending_order(s, ext)
            msg = IncomingMessage(
                channel=Channel.WHATSAPP, external_id=ext,
                attachments=[{"kind": "image", "file_id": "WA123"}],
            )
            await disp.dispatch_incoming(s, msg)
            await s.commit()
            rows = (
                await s.execute(select(Message).where(Message.user_id == uid))
            ).scalars().all()
            return rows, (await s.get(Order, oid)).status, fake.sent

    rows, status, sent = asyncio.run(main())
    assert [m for m in rows if m.media_kind == "photo" and m.media_url], \
        "receipt image must be saved to «Диалоги» with a media preview"
    assert status == OrderStatus.PENDING_CONFIRMATION
    assert any("на проверку" in t for t in sent)


def test_whatsapp_pdf_receipt_is_not_dropped(monkeypatch):
    """A Kaspi PDF receipt (WhatsApp document) is parsed, saved, and attached. The image
    vision model is skipped (can't OCR a PDF) but the receipt reaches the operator."""
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {
            "messages": [{
                "from": "77001", "type": "document",
                "document": {"id": "DOC1", "mime_type": "application/pdf",
                             "filename": "kaspi.pdf"},
            }],
            "contacts": [{"wa_id": "77001", "profile": {"name": "X"}}],
        }}]}],
    }
    parsed = parse_webhook(payload)
    assert parsed and parsed[0].has_document

    fake = _Msgr(media=b"%PDF-1.4 fake")
    _patch_common(monkeypatch, fake)
    vision_calls = {"n": 0}

    async def _spy(session, image_bytes, mime="image/jpeg"):
        vision_calls["n"] += 1
        return ReceiptAnalysis(is_receipt=None, amount=None)

    monkeypatch.setattr(disp.vision, "analyze_receipt", _spy)

    async def main():
        async with AsyncSessionLocal() as s:
            ext = str(random.randint(1, 10**9))
            uid, oid = await _seed_pending_order(s, ext)
            msg = IncomingMessage(
                channel=Channel.WHATSAPP, external_id=ext,
                attachments=[{"kind": "document", "file_id": "DOC1",
                              "mime": "application/pdf", "filename": "kaspi.pdf"}],
            )
            await disp.dispatch_incoming(s, msg)
            await s.commit()
            rows = (
                await s.execute(select(Message).where(Message.user_id == uid))
            ).scalars().all()
            return rows, (await s.get(Order, oid)).status

    rows, status = asyncio.run(main())
    assert any(m.media_kind == "document" and m.media_url for m in rows)
    assert status == OrderStatus.PENDING_CONFIRMATION
    assert vision_calls["n"] == 0


def test_paused_bot_still_saves_a_client_receipt(monkeypatch):
    """Operator had taken over (bot_paused). An image sent then must still be persisted to
    «Диалоги» — previously it was recorded as a bare "[изображение]" label."""
    fake = _Msgr()
    _patch_common(monkeypatch, fake)

    async def main():
        async with AsyncSessionLocal() as s:
            ext = str(random.randint(1, 10**9))
            u = User(channel=Channel.WHATSAPP, external_id=ext, bot_paused=True)
            s.add(u)
            await s.commit()
            uid = u.id
            msg = IncomingMessage(
                channel=Channel.WHATSAPP, external_id=ext,
                attachments=[{"kind": "image", "file_id": "WA999"}],
            )
            await disp.dispatch_incoming(s, msg)
            await s.commit()
            return (
                await s.execute(select(Message).where(Message.user_id == uid))
            ).scalars().all()

    rows = asyncio.run(main())
    assert [m for m in rows if m.media_kind == "photo" and m.media_url], \
        "a receipt sent while the bot is paused must still be visible in «Диалоги»"


def test_run_write_with_retry_recovers_from_a_transient_lock():
    """A "database is locked" mid-burst is retried, not dropped — this is what now saves a
    receipt webhook after Meta already received our 200."""
    from sqlalchemy.exc import OperationalError

    calls = {"n": 0}

    async def flaky(session):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OperationalError("stmt", {}, Exception("database is locked"))

    async def always(session):
        raise OperationalError("stmt", {}, Exception("database is locked"))

    async def main():
        ok = await run_write_with_retry(flaky, label="test", attempts=6)
        bad = await run_write_with_retry(always, label="test", attempts=3)
        return ok, bad, calls["n"]

    ok, bad, n = asyncio.run(main())
    assert ok is True and n == 3
    assert bad is False
